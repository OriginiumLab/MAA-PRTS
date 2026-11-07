# -*- coding: utf-8 -*-
"""作业站（prts.plus）搜索 + 干员匹配

匹配算法复刻作业站前端 models/operatorMatcher.ts：
    练度：把「精英阶段 + 等级」折算成进度分再比较（不是逐项死比）
    技能：requirements.skill_level >= 8 看专精（10 = 专三），否则看技能等级 1~7
    模组：module > 0 时只要求该模组已解锁（等级 >= 1）
    人数：> 13 人或缺 2 人以上 → blocked
"""
from __future__ import annotations

import json
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
# 干员数低于这个值按萌新号对待：推荐排序里优先单核作业
NEWBIE_OWNED = 250

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


def entry_variants(flags: int, variant=None) -> list:
    """作业集条目在 MAA 里要写成哪几条（返回 IsRaid 列表）

    variant='normal'/'raid' 是从「普通 / 突袭」那一行加的，只写对应那一条；
    不带 variant（卡片、详情弹窗）沿用 MAA 的做法，按作业自身的难度标志展开。
    """
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

    for op in opers:
        name = norm(op.get('name'))
        owned = owned_map.get(name)
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
    return mode, missing, training, total, detail, six


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
    """拉某个关卡的作业。

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


def _item(stage, item, mode, missing, training, total, detail, six=0):
    meta = item['meta']
    content = item['content']
    doc = content.get('doc') or {}
    flags = difficulty_flags(content)
    return {
        'id': meta.get('id'),
        'copilotId': meta.get('id'),
        'rating': meta.get('rating_level') or 0,
        'views': meta.get('views') or 0,
        'title': doc.get('title') or '(无标题)',
        'details': (doc.get('details') or '')[:400],
        'uploader': meta.get('uploader') or doc.get('uploader') or '',
        'total': total,
        'mode': mode,
        'difficulty': flags,
        'difficultyText': difficulty_text(flags),
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


def rank_key(row, newbie=False):
    """推荐排序。

    练度可行（mode）永远第一；之后按用户要求：
    - 萌新号（干员 < 250）优先单核作业（阵容里只有一个六星），
      萌新凑不齐多核阵容，单核最容易抄；
    - 再按星级 + 浏览量：用的人多往往比星级更说明稳定。
    """
    key = [MODES.index(row['mode'])]
    if newbie:
        key.append(0 if row.get('singleCore') else 1)
    key += [-row['rating'], -row['views'], row['total']]
    return tuple(key)


def _collect_rows(stage, owned_map, pages=2, min_rating=0, newbie=False):
    """拉一个关卡的作业并打分（不截断，供普通/突袭两行共用）

    用户规则：缺 ≥2 个干员、或练度不满足 ≥2 人的作业直接 pass（不推荐）；
    整关全被 pass 时退回不过滤，免得一关作业都不剩。
    """
    items = fetch_ops(stage, pages=pages, min_rating=min_rating)
    rows, dropped = [], []
    for it in items:
        mode, missing, training, total, detail, six = match_operation(it['content'], owned_map)
        r = _item(stage, it, mode, missing, training, total, detail, six)
        (dropped if (len(missing) >= 2 or len(training) >= 2) else rows).append(r)
    if not rows and dropped:
        rows = dropped
    rows.sort(key=lambda r: rank_key(r, newbie))
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


def match_stage(stage, owned_map, pages=2, min_rating=0, limit=12, variant=None):
    """variant: None 不区分 / 'normal' 只要普通可用 / 'raid' 只要突袭可用

    宽度 3（普通+突袭通用）的作业两边都算。
    """
    rows = _collect_rows(stage, owned_map, pages=pages, min_rating=min_rating)
    if variant == 'normal':
        rows = [r for r in rows if r['difficulty'] & DIFF_NORMAL]
    elif variant == 'raid':
        rows = [r for r in rows if r['difficulty'] & DIFF_RAID]
    return _pack_stage(stage, rows, limit=limit)


def stage_variants(stage, rows, limit=12):
    """把一个关卡的作业拆成展示行

    有突袭版的关卡拆两行：普通一行、突袭一行，通用作业（difficulty=3）两边都出现；
    没有突袭版的关卡只有一行（variant 固定 normal，作业集据此只写普通那一条）。
    """
    if not stage.get('raid'):
        return [_pack_stage(dict(stage, variant='normal'), rows, limit=limit)]
    return [
        _pack_stage(dict(stage, variant='normal'),
                    [r for r in rows if r['difficulty'] & DIFF_NORMAL], limit=limit),
        _pack_stage(dict(stage, variant='raid'),
                    [r for r in rows if r['difficulty'] & DIFF_RAID], limit=limit),
    ]


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
    # 萌新号：干员数量少，优先推单核作业（只有一个六星的阵容）
    newbie = len(owned) < NEWBIE_OWNED
    stages = res['stages']
    workers = min(8, max(1, len(stages)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        collected = list(ex.map(
            lambda s: (s, _collect_rows(s, owned, pages=pages, min_rating=min_rating,
                                        newbie=newbie)),
            stages))

    # 有突袭版的关卡拆成两行：普通一行、突袭一行，通用作业（difficulty=3）
    # 在两边都出现；没有突袭版的关卡只有一行。
    results = []
    for s, rows in collected:
        results.extend(stage_variants(s, rows, limit=limit))

    results.sort(key=lambda r: (r['stage'].get('stageId') or '',
                                r['stage'].get('variant') == 'raid'))
    return {
        'mode': res['mode'],
        'title': res['title'],
        'keyword': res['keyword'],
        'stages': results,
    }


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
    """修正单个干员条目，返回 False 表示条目本身不可用"""
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
