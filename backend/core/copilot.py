# -*- coding: utf-8 -*-
"""作业站（prts.plus）搜索 + 干员匹配

匹配算法复刻作业站前端 models/operatorMatcher.ts：
    练度：把「精英阶段 + 等级」折算成进度分再比较（不是逐项死比）
    技能：requirements.skill_level >= 8 看专精（10 = 专三），否则看技能等级 1~7
    模组：module > 0 时只要求该模组已解锁（等级 >= 1）
    人数：> 13 人或缺 2 人以上 → blocked
"""
from __future__ import annotations

import io
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import resources, settings

API = 'https://prts.maa.plus/copilot'
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) MAA-PRTS/1.0'}

MAX_LEVELS = {0: (30, 0, 0), 1: (30, 0, 0), 2: (30, 0, 0), 3: (40, 55, 0),
              4: (45, 60, 70), 5: (50, 70, 80), 6: (50, 80, 90)}

MODES = ('ready', 'borrow', 'train', 'blocked')
MODE_CN = {'ready': '可直接抄', 'borrow': '差一人可借', 'train': '需补练度', 'blocked': '打不了'}
# 推荐排序算法（跟账号存的 preferAlgo 对应；顺序同下拉展示）：
#   fewer  少人优先：ready/borrow 同级，人数（0~4 同档）→ 干员权重 → 评分 → 浏览量
#   single 单核优先：阵容只有一个六星的排前面（萌新最容易抄），再评分/浏览/人数
#   rating 好评量优先：评分 → 浏览量 → 阵容人数（用的人多说明稳定）——默认
ALGOS = ('fewer', 'single', 'rating')
DEFAULT_ALGO = 'rating'

# 少人优先的干员权重：六星逐个记在 resources/oper-weights.json（用户手工调），
# 六星以外的星级固定——数值大=阵容质量高（排序时取负进键）：
#   五星 -3、四星 -1、三星 +1、一星/二星 +3（低星作业容易凑、要求低）
_STAR_WEIGHT = {5: -3, 4: -1, 3: 1, 2: 3, 1: 3}

# 作业站 v1 老作业的 opers 没有 requirements，练度要求完全未知——当时的默认
# 假设就是「练度拉满」。压级号照这种判定会直接被打不了，所以没写练度的条目
# 按星级补一个保守默认要求（用户定的规则）：
#   六星精二 60 级 + 技能专三（skill_level 9）
#   五星/四星精一 60 级 + 技能 7 级
#   三星精一 55 级 + 技能 7 级
#   一星/二星精零 30 级，无技能要求
# 配置不够的号会被判成「需补练度」，再由 _collect_rows 的 pass 规则滤掉。
# skill_level 沿用作业站语义：>= 8 看对应技能的专精（9 = 专三），1~7 看技能等级。
DEFAULT_REQ = {
    6: {'elite': 2, 'level': 60, 'skill_level': 9},
    5: {'elite': 1, 'level': 60, 'skill_level': 7},
    4: {'elite': 1, 'level': 60, 'skill_level': 7},
    3: {'elite': 1, 'level': 55, 'skill_level': 7},
    2: {'elite': 0, 'level': 30},
    1: {'elite': 0, 'level': 30},
}

# 作业里的 difficulty 字段是位标志（MAA CopilotModel.DifficultyFlags）：
#   0/缺失 = 未标注（MAA 按普通处理）、1 = 普通、2 = 突袭、3 = 普通与突袭兼容
DIFF_NORMAL, DIFF_RAID = 1, 2
DIFF_CN = {DIFF_NORMAL: '普通', DIFF_RAID: '突袭', DIFF_NORMAL | DIFF_RAID: '通用'}

# 好评率门槛（用户规则）用连续函数代替硬分档：评价量 n（like+dislike）越多，
# 要求的好评率越高。对数曲线锚在 (5, 65%) 和 (100, 85%) 两点，中间平滑过渡
# （n=20 处 ≈74.3%，与原分段规则的 75% 档基本重合）；n>100 封顶 85%，评价量
# 再大也不加码；n<5 没有统计意义，不按好评率筛。
# 作业站列表接口每条直接带 like/dislike 原始计数；返回的 rating_ratio 是
# 四舍五入到 1 位小数的点赞率（0.995 也显示 1.0），判不了高门槛档，自己算。
RATING_LO, RATING_HI = 0.65, 0.85
RATING_N_MIN, RATING_N_MAX = 5, 100


def difficulty_flags(content) -> int:
    """把作业的 difficulty 规范成位标志：至少含普通或突袭之一"""
    try:
        raw = int((content or {}).get('difficulty') or 0)
    except (TypeError, ValueError):
        raw = 0
    if raw <= 0:                      # 老作业没有这个字段，按普通算（跟 MAA 一致）
        return DIFF_NORMAL
    flags = 0
    if raw & DIFF_NORMAL:
        flags |= DIFF_NORMAL
    if raw & DIFF_RAID:
        flags |= DIFF_RAID
    return flags or DIFF_NORMAL


def difficulty_text(flags: int) -> str:
    return DIFF_CN.get(flags, '普通')


def entry_variants(flags: int, variant=None, stage_name=None) -> list:
    """作业集条目在 MAA 里要写成哪几条（返回 IsRaid 列表）

    variant='normal'/'raid' 是从「普通 / 突袭」那一行加的，只写对应那一条；
    不带 variant（卡片、详情弹窗）沿用 MAA 的做法，按作业自身的难度标志展开。

    stage_name（作业自己的关卡 id）是最终裁判，覆盖上面的位标志：
      #s 结尾（15+ 章险地）→ 一条 IsRaid=true。MaaCore 在 15+ 章点
        ChangeToRaidDifficulty/RaidConfirm 切的就是险地（StageNavigationTask
        的 PostStageNormalHard 模式），位标志反而经常没标（作者只写了 #s id）；
      easy_/tough_ 开头（简单/磨难）→ 一条 IsRaid=false。这两档没有 raid 概念，
        位标志是几都只写一条，免得标了通用位的磨难作业被展开成两条。
    """
    if stage_name:
        if str(stage_name).endswith('#s'):
            return [True]
        if str(stage_name).startswith(('easy_', 'tough_')):
            return [False]
    if variant == 'normal' and flags & DIFF_NORMAL:
        return [False]
    if variant == 'raid' and flags & DIFF_RAID:
        return [True]
    out = [False] if flags & DIFF_NORMAL else []
    if flags & DIFF_RAID:
        out.append(True)
    return out

_cache = {}
_cache_lock = threading.Lock()
CACHE_TTL = 600


def _http_json(url, timeout=40, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode('utf-8'))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            if i == retries - 1:
                raise
            time.sleep(1.2 * (i + 1))
    raise last


def _query(**params):
    p = {'page': 1, 'limit': 50, 'orderBy': 'hot', 'desc': 'true'}
    p.update({k: v for k, v in params.items() if v is not None})
    url = API + '/query?' + urllib.parse.urlencode(p)
    d = _http_json(url)
    return (d or {}).get('data') or {}


def norm(name):
    return re.sub(r'\s+', '', str(name or ''))


# ---------------------------------------------------------------- 匹配
def progress_score(rarity, elite, level):
    lv = MAX_LEVELS.get(int(rarity or 6), MAX_LEVELS[6])
    score = max(1, int(level or 0))
    for i in range(int(elite or 0)):
        score += lv[i] if i < len(lv) else 0
    return score


def infer_elite(rarity, level, elite):
    if elite is not None:
        return int(elite)
    lv = MAX_LEVELS.get(int(rarity or 6), MAX_LEVELS[6])
    e = 0
    while e < len(lv) - 1 and int(level or 0) > lv[e]:
        e += 1
    return e


def effective_req(op, rarity, strict_skill=True):
    """作业条目的实际练度要求。

    v1 老作业没写 requirements（或只写了技能/模组没写练度）时，按星级补默认
    （练度 + 技能等级，见 DEFAULT_REQ）：返回的 dict 带 `implied: True` 标记，
    前端据此显示「默认」而不是当成作者标注。

    strict_skill=False 用于「账号根本没有技能/专精/模组数据」的情况（MAA 本地
    识别导入的账号）：这些维度不判定，也就不该出现在要求里，否则用户会看到
    一条永远满足不了的「专三」，却完全不知道是数据缺失导致的。
    """
    r = op.get('requirements') or {}
    if r.get('elite') is None and r.get('level') is None:
        r = dict(r, **DEFAULT_REQ.get(int(rarity or 6), DEFAULT_REQ[6]), implied=True)
    if not strict_skill:
        r = {k: v for k, v in r.items()
             if k not in ('skill_level', 'skillLevel', 'module')}
    return r


def req_met(req, owned):
    """req 是作业里的干员条目，requirements 是它的练度要求。
    注意：技能序号 `skill` 在条目顶层，不在 requirements 里。

    账号没有技能数据时（owned['skillKnown'] 为 False）只比精英化 / 等级：
    MAA 干员识别拿不到技能等级、专精、模组，按「未知」处理会把这些账号的
    作业全判成需补练度；这里选择妥协，技能与模组一律当已拥有（见
    operbox.build_chars 的数据源上限说明）。
    """
    skill_known = bool(owned.get('skillKnown', True))
    r = effective_req(req, owned.get('rarity'), strict_skill=skill_known)
    elite, level = r.get('elite'), r.get('level')
    if elite is not None or level is not None:
        rl = int(level or 1)
        re_ = infer_elite(owned['rarity'], rl, elite)
        if progress_score(owned['rarity'], owned['elite'], owned['level']) < \
                progress_score(owned['rarity'], re_, rl):
            return False
    sl = r.get('skill_level', r.get('skillLevel'))
    if sl is not None:
        sl = int(sl)
        if sl >= 8:
            idx = int(req.get('skill') or r.get('skill') or 1) - 1
            mv = owned['masteryLevels'][idx] if 0 <= idx < len(owned['masteryLevels']) else None
            if mv is None or mv < sl - 7:
                return False
        elif int(owned.get('mainSkillLevel') or 0) < sl:
            return False
    module = r.get('module')
    if module is not None and int(module) > 0:
        if owned['moduleLevels'].get(int(module), 0) < 1:
            return False
    return True


def _group_ok(group, owned_map):
    cands = [c for c in (group.get('opers') or []) if norm(c.get('name')) in owned_map]
    if any(req_met(c, owned_map[norm(c.get('name'))]) for c in cands):
        return 'ready'
    return 'train' if cands else 'missing'


def _group_label(g):
    return g.get('name') or ' / '.join(o.get('name', '') for o in (g.get('opers') or [])) or '组合'


def match_operation(content, owned_map):
    opers = content.get('opers') or []
    groups = content.get('groups') or []
    missing, training, detail = [], [], []
    rmap = _rarity_by_name()
    six = 0
    wsum = 0.0

    for op in opers:
        name = norm(op.get('name'))
        owned = owned_map.get(name)
        wsum += op_weight(name)
        ok = bool(owned and req_met(op, owned))
        if not owned:
            missing.append(op.get('name'))
        elif not ok:
            training.append(op.get('name'))
        if rmap.get(name) == 6:
            six += 1
        # v1 作业没写练度：detail 里也补上默认要求（带 implied 标记），让前端
        # 能解释「为什么判需补练度」，而不是一脸懵地显示「无特殊要求」
        req = effective_req(op, owned['rarity'] if owned else rmap.get(name),
                            strict_skill=bool(owned.get('skillKnown', True)) if owned else True)
        detail.append({
            'name': op.get('name'),
            'owned': bool(owned),
            'ok': ok,
            'elite': owned['elite'] if owned else None,
            'level': owned['level'] if owned else None,
            'mastery': owned['masteryLevels'] if owned else [],
            'req': req,
            'skill': op.get('skill'),
            'skillKnown': bool(owned.get('skillKnown', True)) if owned else None,
        })

    for g in groups:
        st = _group_ok(g, owned_map)
        if st == 'missing':
            missing.append(_group_label(g))
        elif st == 'train':
            training.append(_group_label(g))
        detail.append({'name': _group_label(g), 'group': True, 'owned': st != 'missing',
                       'ok': st == 'ready'})

    total = len(opers) + len(groups)
    if total > 13 or len(missing) >= 2:
        mode = 'blocked'
    elif len(missing) == 1:
        mode = 'train' if training else 'borrow'
    elif not training:
        mode = 'borrow' if total == 13 else 'ready'
    else:
        mode = 'borrow' if (len(training) == 1 and total < 13) else 'train'
    # 阵容权重均值（少人优先排序用）：只算 opers 里的具名干员，除以具名干员数
    weight = wsum / len(opers) if opers else 0.0
    return mode, missing, training, total, detail, six, weight


# 六星判定用：名字 → 1~6 星（character_table 的 rarity 是 0 起算，转成 1 起）
_rarity_cache = None
_rarity_lock = threading.Lock()


def _rarity_by_name():
    global _rarity_cache
    with _rarity_lock:
        if _rarity_cache is None:
            m = {}
            for c in (resources.char_table() or {}).values():
                if isinstance(c, dict) and c.get('name'):
                    m[norm(c['name'])] = int(c.get('rarity') or 0) + 1
            _rarity_cache = m
    return _rarity_cache


# 六星权重表（resources/oper-weights.json，进程内缓存，文件换了自动重载）
_weight_lock = threading.Lock()
_weight_cache = {'data': None, 'mtime': 0}


def six_weights() -> dict:
    path = settings.weights_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    with _weight_lock:
        c = _weight_cache
        if c['data'] is None or mtime != c['mtime']:
            try:
                with io.open(path, encoding='utf-8') as f:
                    c['data'] = {norm(k): float(v or 0)
                                 for k, v in (json.load(f) or {}).items()}
            except Exception:
                c['data'] = {}
            c['mtime'] = mtime
        return c['data']


def op_weight(name) -> float:
    """单个干员的权重：六星查表，其余按星级固定，认不出的 0（中性）"""
    r = _rarity_by_name().get(name)
    if r == 6:
        return six_weights().get(name, 0)
    return _STAR_WEIGHT.get(r, 0)


# ---------------------------------------------------------------- 账号 → owned
_SLOT = {'X': 1, 'Y': 2, 'A': 3, 'D': 4}


def _module_type(equip_id):
    ic = (resources.equip_table().get(equip_id) or {}).get('typeIcon') or ''
    ic = ic.lower()
    if not ic:
        return None
    if ic == 'original':
        return 'Original'
    return {'x': 'X', 'y': 'Y', 'd': 'D', 'a': 'A'}.get(ic.rsplit('-', 1)[-1])


def build_owned(snap):
    owned = {}
    for c in snap.get('chars') or []:
        if not c.get('name'):
            continue
        mods = {}
        for m in c.get('modules') or []:
            t = _module_type(m.get('id'))
            lv = int(m.get('level') or 0)
            if t in _SLOT and lv > 0:
                mods[_SLOT[t]] = max(mods.get(_SLOT[t], 0), lv)
        owned[norm(c['name'])] = {
            'name': c['name'],
            'rarity': int(c.get('rarity') or 6),
            'elite': int(c.get('elite') or 0),
            'level': int(c.get('level') or 0),
            'mainSkillLevel': int(c.get('skillLevel') or 0),
            'masteryLevels': [int(s.get('mastery') or 0) for s in (c.get('skills') or [])],
            'moduleLevels': mods,
            # 技能数据是否存在：MAA 本地识别导入的账号整块缺失（skills 是空数组），
            # 这种干员只比精英化/等级，技能与模组按已拥有处理（req_met 里判）
            'skillKnown': bool(c.get('skills')),
        }
    return owned


# ---------------------------------------------------------------- 作业
def _pull(params, pages=2, limit=50):
    """按参数翻页拉作业，返回 [{meta, content}]"""
    out, seen = [], set()
    for page in range(1, max(1, pages) + 1):
        try:
            d = _query(page=page, limit=limit, **params)
        except Exception:
            break
        rows = d.get('data') or []
        for o in rows:
            oid = o.get('id')
            if oid in seen:
                continue
            seen.add(oid)
            try:
                content = json.loads(o.get('content') or '{}')
            except Exception:
                continue
            out.append({'meta': o, 'content': content})
        if not d.get('has_next') or not rows:
            break
    return out


def fetch_ops(stage, pages=2, min_rating=0, force=False):
    """拉某个关卡的作业（出口滤掉作者 / 作业黑名单）。

    过滤包在 _fetch_ops_raw 外面：缓存里存的是全量，黑名单增删立即生效，
    不用等 10 分钟缓存过期。
    """
    items = _fetch_ops_raw(stage, pages=pages, min_rating=min_rating, force=force)
    st = settings.load()
    ban = set()
    for row in (st.get('authorBlacklist') or []):
        aid = str((row or {}).get('id') or '').strip() if isinstance(row, dict) else ''
        if aid:
            ban.add(aid)
    if ban:
        items = [i for i in items
                 if str((i['meta'].get('uploader_id') or '')).strip() not in ban]
    ban_ops = set(st.get('opBlacklist') or [])
    if ban_ops:
        items = [i for i in items if ('maa://%s' % i['meta'].get('id')) not in ban_ops]
    return items


def _fetch_ops_raw(stage, pages=2, min_rating=0, force=False):
    """拉某个关卡的作业（原始全量，含黑名单作者）。

    `levelKeyword` 只对作业站认识的关卡 id 生效；对 TR-x 这类关卡会退化成
    「返回全站热门」，所以必须用 stage_name 二次校验，再退回 document 搜索。
    """
    sid = stage['stageId']
    code = stage.get('code') or sid
    key = (sid, pages, min_rating)
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and not force and now - hit[0] < CACHE_TTL:
            return hit[1]

    only = lambda rows: [r for r in rows
                         if (r['content'].get('stage_name') or '') in (sid, code)]

    # TR / 塔关等 id 作业站不认，直接用代号搜；其余先按 id 查
    direct_by_code = sid.startswith(('tr_', 'lt_'))
    if direct_by_code:
        items = only(_pull({'document': code}, pages))
    else:
        items = only(_pull({'levelKeyword': sid}, pages))
        if not items:
            items = only(_pull({'document': code}, pages))

    items = [i for i in items if (i['meta'].get('rating_level') or 0) >= min_rating]

    with _cache_lock:
        _cache[key] = (now, items)
    return items


def required_rating(n) -> float:
    """评价量 n → 所需好评率：对数插值，见 RATING_* 常量注释。

    n<5 返回 0（不筛），n>=100 恒为 RATING_HI。
    """
    if n < RATING_N_MIN:
        return 0.0
    r = RATING_LO + (RATING_HI - RATING_LO) * \
        math.log(n / RATING_N_MIN) / math.log(RATING_N_MAX / RATING_N_MIN)
    return min(RATING_HI, r)


def rating_ok(meta) -> bool:
    """好评率门槛：like/(like+dislike) 达到 required_rating(评价量)。"""
    lk = int(meta.get('like') or 0)
    dk = int(meta.get('dislike') or 0)
    n = lk + dk
    if not n:
        return True
    return lk / n >= required_rating(n)


def difficulty_text_for(stage_name, flags):
    """写入日志/列表用的难度名：险地/磨难/简单按作业的 stageId 说，其余按位标志"""
    s = str(stage_name or '')
    if s.endswith('#s'):
        return '险地'
    if s.startswith('tough_'):
        return '磨难'
    if s.startswith('easy_'):
        return '简单'
    return difficulty_text(flags)


def _item(stage, item, mode, missing, training, total, detail, six=0, weight=0):
    meta = item['meta']
    content = item['content']
    doc = content.get('doc') or {}
    flags = difficulty_flags(content)
    # 险地行（stageId 以 #s 结尾）的作业位标志大多没标（作者只写了 #s id），
    # 展示与写入一律按险地处理
    if str(stage.get('stageId') or '').endswith('#s'):
        flags = DIFF_RAID
    return {
        'id': meta.get('id'),
        'copilotId': meta.get('id'),
        'rating': meta.get('rating_level') or 0,
        'views': meta.get('views') or 0,
        'title': doc.get('title') or '(无标题)',
        'details': (doc.get('details') or '')[:400],
        'uploader': meta.get('uploader') or doc.get('uploader') or '',
        'uploaderId': str(meta.get('uploader_id') or '').strip(),
        'total': total,
        'weight': round(weight, 4),
        'mode': mode,
        'difficulty': flags,
        'difficultyText': difficulty_text_for(stage.get('stageId'), flags),
        'difficultyCN': stage.get('difficultyCN') or '',
        'missing': missing,
        'training': training,
        'sixCount': six,
        'singleCore': six == 1,
        'opers': detail,
        'maaCode': 'maa://%s' % meta.get('id'),
        'stageId': stage['stageId'],
        'stageCode': stage['code'],
        'stageName': stage['name'],
        'speed': content.get('minimum_required') or '',
    }


def rank_key(row, algo=DEFAULT_ALGO):
    """推荐排序。练度可行（mode）永远第一；之后按账号选的算法（见 ALGOS 注释）"""
    mode = MODES.index(row['mode'])
    if algo == 'fewer':
        # ready/borrow 同级：都能上场打，少一人（助战顶上）不算劣势。
        # 人数 0~4 折成同档（再分高下没意义），5 人起按实际人数；
        # 同档内先比阵容权重均值（大者优先，取负进键），再评分、浏览
        mode = 0 if mode <= 1 else mode
        t = row['total'] if row['total'] > 4 else 4
        return (mode, t, -row.get('weight', 0), -row['rating'], -row['views'])
    if algo == 'single':
        return (mode, 0 if row.get('singleCore') else 1,
                -row['rating'], -row['views'], row['total'])
    return (mode, -row['rating'], -row['views'], row['total'])


def _collect_rows(stage, owned_map, pages=2, min_rating=0, algo=DEFAULT_ALGO):
    """拉一个关卡的作业并打分（不截断，供普通/突袭两行共用）

    拉取后先按好评率门槛滤一轮（rating_ok，评价量太少的不筛），
    全不达标时退回不过滤（跟下面的 pass 规则一致，免得一关作业都不剩）；
    用户规则：0 人作业（仅 TR 关除外）、缺 ≥2 个干员、或练度不满足 ≥2 人
    的作业直接 pass（不推荐）；整关全被 pass 时同样退回不过滤。
    """
    items = fetch_ops(stage, pages=pages, min_rating=min_rating)
    rated = [it for it in items if rating_ok(it['meta'])]
    if rated:
        items = rated
    # 0 人作业（opers/groups 全空）只在 TR 关有意义（挂机解），其余关不参与推荐，
    # 全空时仍由下面的回退兜底
    allow_zero = str(stage.get('stageId') or '').startswith('tr_')
    rows, dropped = [], []
    for it in items:
        mode, missing, training, total, detail, six, weight = \
            match_operation(it['content'], owned_map)
        r = _item(stage, it, mode, missing, training, total, detail, six, weight)
        (dropped if (total == 0 and not allow_zero
                     or len(missing) >= 2 or len(training) >= 2) else rows).append(r)
    if not rows and dropped:
        rows = dropped
    rows.sort(key=lambda r: rank_key(r, algo))
    return rows


def _pack_stage(stage, rows, limit=12):
    counts = {m: sum(1 for r in rows if r['mode'] == m) for m in MODES}
    return {
        'stage': stage,
        'total': len(rows),
        'counts': counts,
        'best': rows[0] if rows else None,
        'ops': rows[:limit],
    }


def stage_variants(stage, rows, limit=12):
    """把一个关卡的作业拆成展示行

    普通/突袭（0-9 章与活动）：有突袭版的关卡拆两行，通用作业（difficulty=3）
    两边都出现；没有突袭版的关卡只有一行（variant 固定 normal）。
    磨难/简单（10-14 章 easy_/tough_ 独立 id）：各自的关卡 id 已经决定了难度，
    不再按位标志筛，一行 variant=normal（写入 MAA 只落 IsRaid=false 一条）。
    险地（15+ 章 #s）：一行 variant=raid——写入 MAA 落 IsRaid=true，
    MaaCore 在 15+ 章正是用它切险地。
    """
    diff = stage.get('difficulty')
    if diff in ('easy', 'tough'):
        return [_pack_stage(dict(stage, variant='normal'), rows, limit=limit)]
    if diff == 'risky':
        return [_pack_stage(dict(stage, variant='raid'), rows, limit=limit)]
    if not stage.get('raid'):
        return [_pack_stage(dict(stage, variant='normal'), rows, limit=limit)]
    return [
        _pack_stage(dict(stage, variant='normal'),
                    [r for r in rows if r['difficulty'] & DIFF_NORMAL], limit=limit),
        _pack_stage(dict(stage, variant='raid'),
                    [r for r in rows if r['difficulty'] & DIFF_RAID], limit=limit),
    ]


def _base_stage_id(sid):
    """难度变体归并到同一主关 id 下排序：tough_12-06 / main_15-02#s → main_12-06 / main_15-02"""
    s = re.sub(r'^(?:easy|tough)_', 'main_', str(sid or ''))
    return s.split('#')[0]


def row_sort_key(packed):
    """结果行排序：代号间按主关 id；同代号内 磨难/险地 → 普通/标准（→ 突袭）→ 简单

    10-14 章磨难、15+ 章险地是用户真正要打的主难度，行置顶；0-9 章的突袭
    变体跟在普通/标准行后面（普通→突袭的习惯顺序）；简单永远垫底。
    """
    st = packed['stage']
    if st.get('difficulty') == 'risky':        # 险地是 15+ 章的主难度，置顶
        return (_base_stage_id(st.get('stageId')), 0)
    rank = {'tough': 0, 'easy': 2}.get(st.get('difficulty'), 1)
    if st.get('variant') == 'raid':
        rank += 0.5
    return (_base_stage_id(st.get('stageId')), rank)


def search(keyword, snap, pages=2, min_rating=0, limit=12):
    res = resources.resolve(keyword)
    if not res['stages']:
        # 回退：把关键词当关卡代号问作业站，用返回的 stage_name 反推
        pool = [p for p in _pull({'document': keyword.strip()}, pages=1)
                if p['content'].get('stage_name')]
        if not pool:
            return {'mode': 'unknown', 'title': '', 'keyword': keyword, 'stages': []}
        sid = pool[0]['content']['stage_name']
        stage = {'stageId': sid, 'code': keyword.strip().upper(), 'name': '',
                 'zoneId': '', 'zoneName': '', 'raid': sid.endswith('#f#')}
        res = {'mode': 'stage', 'title': '%s（%s）' % (keyword, sid),
               'keyword': keyword, 'stages': [stage]}

    owned = build_owned(snap)
    # 排序算法跟账号存（用户在搜索框旁的下拉里选）：不再按干员数自动判萌新号
    algo = snap.get('preferAlgo')
    if algo not in ALGOS:
        algo = DEFAULT_ALGO
    stages = res['stages']
    workers = min(8, max(1, len(stages)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        collected = list(ex.map(
            lambda s: (s, _collect_rows(s, owned, pages=pages, min_rating=min_rating,
                                        algo=algo)),
            stages))

    # 有突袭版的关卡拆成两行：普通一行、突袭一行，通用作业（difficulty=3）
    # 在两边都出现；没有突袭版的关卡只有一行。主线难度变体（简单/磨难/险地）
    # 是独立关卡 id，天然各成一行，排序见 row_sort_key。
    results = []
    for s, rows in collected:
        results.extend(stage_variants(s, rows, limit=limit))

    results.sort(key=row_sort_key)
    return {
        'mode': res['mode'],
        'title': res['title'],
        'keyword': res['keyword'],
        'stages': results,
        # 「下一关」按钮的数据：按置顶行（用户正在看的难度视角）推进
        'next': resources.next_stage(results[0]['stage']) if results else None,
    }


def suggest_authors(name, timeout=10):
    """按名字反查作业站作者：document 全文搜一页 → 聚合名字匹配的作者

    作业站没有公开的「按名搜用户」接口，但作者名几乎都会署在自己作业的
    标题 / 简介里，document 搜一页（100 条）再按 meta.uploader 过滤聚合
    就够黑名单选人用了。id 是唯一键，count 是这次搜索结果里的命中份数。
    """
    q = str(name or '').strip()
    if not q:
        return []
    try:
        items = _pull({'document': q}, pages=1, limit=100)
    except Exception:  # noqa: BLE001 - 没网就当没搜到，前端还有 id 手填兜底
        items = []
    low = q.lower()
    out = {}
    for it in items:
        meta = it['meta']
        up = str(meta.get('uploader') or '')
        aid = str(meta.get('uploader_id') or '').strip()
        if not aid or low not in up.lower():
            continue
        rec = out.setdefault(aid, {'id': aid, 'name': up, 'count': 0})
        rec['count'] += 1
    return sorted(out.values(), key=lambda r: (-r['count'], r['name']))[:12]


def operation_detail(copilot_id):
    d = _http_json(API + '/get/%s' % copilot_id)
    data = (d or {}).get('data') or {}
    raw = data.get('content') or data.get('parsedContent') or '{}'
    content = json.loads(raw) if isinstance(raw, str) else raw
    return {'meta': data, 'content': content}


def download_content(copilot_id):
    """给 MAA 用的原始作业 json"""
    d = operation_detail(copilot_id)
    c = d['content']
    if not c.get('stage_name'):
        raise ValueError('作业 #%s 内容不完整' % copilot_id)
    return c


def detail_opers(content, owned_map=None):
    """详情弹窗用的干员列表：没写练度的按星级补默认要求（带 implied 标记）。

    注意这是展示用的副本，不往写 MAA 的那份 content 里塞字段。
    owned_map 传了当前账号的练度时，按干员是否真有技能数据决定要不要展示
    技能/模组要求——MAA 导入的账号不看这些维度，展示出来只会误导。
    """
    idx = _char_index()
    out = []
    for op in content.get('opers') or []:
        o = dict(op)
        stars = (idx.get(o.get('name') or '') or (6,))[0]
        owned = (owned_map or {}).get(norm(o.get('name')))
        known = bool(owned.get('skillKnown', True)) if owned else True
        o['requirements'] = effective_req(op, stars, strict_skill=known)
        o['skillKnown'] = known if owned else None
        out.append(o)
    return out


# ---------------------------------------------------------------- 交给 MAA 前修正
# 作业站的原始 json 里经常有「技能选择与稀有度不匹配」「精英要求填 0 但技能等级
# 要求精一」这类历史遗留问题。MAA 桌面端在「添加作业」时会自己修一遍再存盘，
# 而 MaaCore 解析时会直接判错退出（日志：CopilotConfig parse failed），表现就是
# 「作业启动失败 / 读取文件失败！」。这里把 MAA 桌面端那套修正复刻一遍。
#
# 规则来源（v6.21.4）：
#   MaaWpfGui/ViewModels/UI/CopilotViewModel.cs  ParseCopilotAsync
#   MaaCore/Config/Miscellaneous/CopilotConfig.cpp  parse_oper_usage / parse_actions
_CHAR_INDEX = {'key': None, 'map': None}


def _char_index() -> dict:
    """干员名 → (星级 1~6, charId)。同名多形态取稀有度最高的那个，与 MAA 一致。"""
    pack = resources.pack()
    key = (pack.get('version'), pack.get('fingerprint'))
    if _CHAR_INDEX['map'] is not None and _CHAR_INDEX['key'] == key:
        return _CHAR_INDEX['map']

    idx = {}
    for cid, c in (pack.get('chars') or {}).items():
        name = c.get('name')
        if not name:
            continue
        stars = int(c.get('rarity') or 0) + 1     # 资源包里 rarity 是 0 起（0 = 一星）
        old = idx.get(name)
        if old is None or stars > old[0]:
            idx[name] = (stars, cid)
    _CHAR_INDEX['map'] = idx
    _CHAR_INDEX['key'] = key
    return idx


_ACTION_SKILL_TYPES = ('Skill', 'Retreat', 'BulletTime', 'SkillUsage')


def _fix_oper(op, idx, notes):
    """修正单个干员条目（星级/技能合法性、练度下限），问题就地改并记进 notes。

    恒返回 True：查不到的条目（装置、召唤物）MAA 也会跳过检查，不算不可用。
    """
    if not isinstance(op, dict) or not op.get('name'):
        return True

    name = op.get('name')
    hit = idx.get(name)
    if not hit:
        return True                     # 查不到（装置、召唤物）→ MAA 也会跳过检查
    stars, cid = hit

    skill = int(op.get('skill') or 0)
    bad = ((skill == 3 and stars < 6 and cid != 'char_002_amiya')
           or (skill == 2 and stars < 4)
           or (skill == 1 and stars < 3))
    if bad:
        notes.append('%s 是 %d 星，没有技能 %d，已按 0 处理' % (name, stars, skill))
        op['skill'] = 0
        skill = 0

    req = op.get('requirements')
    need = max(skill - 1, 0)
    if isinstance(req, dict):
        sl = req.get('skill_level')
        if sl is not None:
            sl = int(sl)
            if sl > 7:
                need = max(2, need)     # 专精要求精二
            elif sl > 4:
                need = max(1, need)     # 7 级技能要求精一
        mod = req.get('module')
        if mod is not None and int(mod) > 0:
            need = max(2, need)         # 带模组要求精二

    if need > 0:
        if not isinstance(req, dict):
            op['requirements'] = {'elite': need}
            notes.append('%s 缺练度字段，补上精英 %d' % (name, need))
        elif int(req.get('elite') or 0) < need:
            notes.append('%s 精英要求 %s → %d' % (name, req.get('elite'), need))
            req['elite'] = need
    return True


def _fix_actions(actions, notes):
    out = []
    for a in (actions or []):
        if not isinstance(a, dict):
            continue
        t = a.get('type')
        if t in _ACTION_SKILL_TYPES and a.get('name') is not None and a.get('location'):
            # 同时给了干员名和坐标时，MAA 按坐标执行，名字会被清掉
            a.pop('name', None)
            a.pop('role', None)
        if t == 'Click':
            if 'rect' in a and 'location' in a:
                a.pop('location', None)     # Core 按 rect 执行
            elif 'rect' not in a and 'location' not in a:
                notes.append('丢弃缺少 rect/location 的点击动作')
                continue
        if t == 'Swipe' and ('begin' not in a or 'end' not in a):
            notes.append('丢弃缺少 begin/end 的滑动动作')
            continue
        out.append(a)
    return out


def normalize_for_maa(content):
    """把作业站原始 json 修成 MaaCore 能解析的样子，返回 (新 json, 修正说明列表)"""
    c = json.loads(json.dumps(content, ensure_ascii=False))   # 深拷贝，不动缓存里那份
    idx = _char_index()
    notes = []

    opers = list(c.get('opers') or [])
    for g in (c.get('groups') or []):
        if isinstance(g, dict):
            opers.extend(g.get('opers') or [])
    for op in opers:
        _fix_oper(op, idx, notes)

    c['actions'] = _fix_actions(c.get('actions'), notes)

    # MaaCore 里 actions 是必读字段，opers/groups 缺失时 MAA 桌面端会空引用
    c.setdefault('opers', [])
    c.setdefault('groups', [])
    if not isinstance(c.get('doc'), dict):
        c['doc'] = {}
    if not c.get('stage_name'):
        raise ValueError('作业缺少 stage_name')
    return c, notes
