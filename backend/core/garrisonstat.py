# -*- coding: utf-8 -*-
"""卫戍协议战绩：森空岛三个接口拼成一份前端能直接画的数据

三个接口各有分工，缺一个就画不全：

1. ``/web/v1/game/garrison/guide/resource``  **不需要登录**
   赛季信息 + 218 个干员的头像/技能/模组图标地址 + 赛季背景图。
   攻略工具用的公开接口，缓存 6 小时。

2. ``/api/ms/game/arknights/garrison/resource``  **需要 cred + sign**
   当期字典：``chessInfo``（棋子的阶数 / 精英化 / 是否精锐）、``bandInfo``
   （策略的名字与图标）、``bondInfo``（盟约）、``equipInfo``、``titleInfo``、
   ``bossInfo``。战绩里只存 id，全靠它翻译。

3. ``/api/ms/game/arknights/garrison/info?uid=&activityId=``  **需要 cred + sign**
   生涯统计 ``career`` + 对局列表 ``history.records`` / ``favourRecords``。

为什么要 1 和 2 两个资源接口：2 的信息最全但必须登录，1 免登录且带干员
头像、技能模组与赛季背景——两个都缓存到 ``config/cache/garrison/``。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request

from . import accounts, gasset, net, settings, skland

GUIDE_URL = 'https://zonai.skland.com/web/v1/game/garrison/guide/resource'
ACTIVITY = 'act3autochess'          # 兜底赛季；正常从接口回来的值走

GUIDE_TTL = 6 * 3600
RES_TTL = 12 * 3600

# 难度代号 → 游戏里的叫法（小黑盒同款）
MODE_CN = {
    'FAST': '极速模拟', 'NORMAL': '标准模拟', 'HARD': '终极模拟',
    'FUNNY': '趣味模拟', 'ABYSS': '深潜模拟', 'TRAINING': '训练模拟',
}
TYPE_CN = {'SINGLE': '单人', 'MULTI': '多人', 'LOCAL': '教学'}

# 森空岛只给 enemyId，不给首领中文名，这里按 handbook id 兜一层；
# 表外的就显示 enemyId 本身（宁可难看，也别瞎编一个名字）
BOSS_NAMES = {
    'enemy_1521_dslily': '盐风主教昆图斯',
    'enemy_1559_vtlionk': '阿利斯泰尔，帝国余晖',
    'enemy_2054_smdeer': '“萨米的意志”',
    'enemy_2124_dyjsfg': '矩兽',
    'enemy_9013_acstmk': '假想敌：胄',
    'enemy_9013_acstmk_2': '假想敌：胄',
    'enemy_9017_achunt': '假想敌：铳',
    'enemy_9017_achunt_2': '假想敌：铳',
    'enemy_9021_acduml': '假想敌：管',
    'enemy_9021_acduml_2': '假想敌：管',
    'enemy_9029_acfnc': '假想敌：械',
    'enemy_9035_acspider': '假想敌：茧',
    'enemy_9037_aceel': '假想敌：鳍',
}

_TAG = re.compile(r'<@[^>]*>|</>|</?(?:color|size|font|sprite)(?:=[^>]*)?>', re.I)
_ANY = re.compile(r'<([^<>]*)>')
_HTML = re.compile(r'/?(?:a|b|br|div|em|i|img|p|s|span|strong|u)(?:\s.*|/)?$', re.I)

_locks = {'guide': threading.Lock(), 'res': threading.Lock()}
_guide_mem = {'at': 0.0, 'data': None}
_res_mem = {'at': 0.0, 'list': None}
_sessions = {}                      # token -> (Skland, 到期时间)
_sess_guard = threading.Lock()
SESS_TTL = 600


def cache_dir() -> str:
    return os.path.join(settings.CONFIG_DIR, 'cache', 'garrison')


# ---------------------------------------------------------------- 文本
def text(v) -> str:
    """官方富文本转纯文本：去颜色/角标标签，保留 <战斗中> 这类语义标签"""
    if not isinstance(v, str):
        return ''
    v = v.replace('\\n', '\n')
    v = _TAG.sub('', v)
    # 剩下的 <战斗中> 这种是游戏里的语义强调，保留但换成书名号，别当 HTML 吃掉
    v = _ANY.sub(lambda m: '' if _HTML.fullmatch(m.group(1)) else '〈%s〉' % m.group(1), v)
    return v.replace('<', '〈').replace('>', '〉').strip()


def _num(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _pick(d, *keys):
    """按 id 在字典里找条目；对不上就试去掉/补上前缀的几种写法

    森空岛不同接口对同一个策略的写法不总一致（``band_bonus`` / ``bonus``），
    直接 get 会漏图标。逐个候选试一遍，再退化到忽略大小写。
    """
    if not isinstance(d, dict) or not keys:
        return None
    for k in keys:
        k = str(k or '')
        if not k:
            continue
        for cand in (k, k.replace('band_', ''), 'band_' + k,
                     k.replace('Ship', ''), k + 'Ship'):
            if cand in d:
                return d[cand]
    low = str(keys[0] or '').lower()
    for k, v in d.items():
        if k.lower() == low:
            return v
    return None


def _url(u) -> str:
    """图标地址归一并过白名单；不允许的返回空串（前端出占位块）"""
    return gasset.normalize(u) if u else ''


CDN = 'https://web.hycdn.cn/arknights/game/assets/'


def _skill_view(sid: str, meta):
    """技能：公开资源里查不到时按 id 拼 CDN 图兜底

    这里以前是 ``skill.get(...) if skill_id else None``——只判断了 id 在不在，
    没判断查没查到，某个技能不在公开资源里就整个战绩页崩成「拉取失败」（NoneType）。
    """
    if not sid:
        return None
    m = meta or {}
    icon = _url(m.get('iconUrl'))
    if not icon:
        icon = _url(CDN + 'char_skill/%s.png' % urllib.parse.quote(sid, safe=''))
    return {'id': sid, 'name': text(m.get('name') or sid), 'icon': icon}


def _module_view(mid: str, meta):
    """模组：``uniequip_001_*`` 是**证章**（精英化自带、恒 1 级，公开资源里
    本来就不列，CDN 上也没有图，同 ``training.py`` 的口径）；其余查不到的按
    id 拼 CDN 图兜底——宁可少张图，也不能让整页垮掉。
    """
    if not mid or mid.startswith('uniequip_001_'):
        return None
    m = meta or {}
    icon = _url((m.get('icon') or {}).get('type'))
    if not icon:
        icon = _url(CDN + 'uniequip/%s.png' % mid)
    return {'id': mid, 'name': text(m.get('name') or mid), 'icon': icon}


# ---------------------------------------------------------------- 网络
def _get_json(url: str, timeout=30) -> dict:
    from .version import app_version
    req = urllib.request.Request(url, headers={
        'User-Agent': 'MAA-PRTS/%s' % app_version(),
        'Accept': 'application/json',
    })
    with net.open(req, timeout=timeout) as r:
        raw = r.read()
    d = json.loads(raw.decode('utf-8'))
    if not isinstance(d, dict) or d.get('code') not in (0, None):
        raise RuntimeError(d.get('message') or '接口返回异常')
    return d.get('data') or {}


def _read_cache(name: str):
    try:
        with open(os.path.join(cache_dir(), name), 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _write_cache(name: str, data) -> None:
    d = cache_dir()
    try:
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, name)
        tmp = dst + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, separators=(',', ':'))
        os.replace(tmp, dst)
    except OSError:
        pass


def guide(force=False) -> dict:
    """公开资源（免登录）：赛季 + 干员头像/技能/模组 + 背景图"""
    now = time.time()
    if not force and _guide_mem['data'] and now - _guide_mem['at'] < GUIDE_TTL:
        return _guide_mem['data']

    with _locks['guide']:
        if not force and _guide_mem['data'] and time.time() - _guide_mem['at'] < GUIDE_TTL:
            return _guide_mem['data']
        cached = _read_cache('guide.json')
        if (not force and cached and cached.get('data') and
                now - float(cached.get('_at') or 0) < GUIDE_TTL):
            data = cached['data']
            _guide_mem.update(at=float(cached.get('_at') or now), data=data)
            return data
        try:
            data = _get_json(GUIDE_URL)
        except Exception:
            if cached and cached.get('data'):      # 拉不到就用旧的，功能照常
                data = cached['data']
                _guide_mem.update(at=now, data=data)
                return data
            raise
        _write_cache('guide.json', {'_at': now, 'data': data})
        _guide_mem.update(at=now, data=data)
        return data


def resources(sk, force=False) -> list:
    """登录版资源字典列表（第一个是当前赛季），12 小时缓存

    一次请求就把所有赛季的字典都拿回来了，所以只请求一次、整包缓存。
    """
    now = time.time()
    if not force and _res_mem['list'] is not None and now - _res_mem['at'] < RES_TTL:
        return _res_mem['list']

    with _locks['res']:
        if not force and _res_mem['list'] is not None and time.time() - _res_mem['at'] < RES_TTL:
            return _res_mem['list']
        cached = _read_cache('resources.json')
        if (not force and cached and cached.get('list') and
                now - float(cached.get('_at') or 0) < RES_TTL):
            data = cached['list']
            _res_mem.update(at=float(cached.get('_at') or now), list=data)
            return data
        try:
            data = (sk.garrison_resources(None) or {}).get('resources') or []
        except Exception:
            if cached and cached.get('list'):
                data = cached['list']
                _res_mem.update(at=now, list=data)
                return data
            raise
        if data:
            _write_cache('resources.json', {'_at': now, 'list': data})
        _res_mem.update(at=now, list=data)
        return data


def _session(token: str):
    """同一个 token 10 分钟内只登录一次（登录要两次请求，很贵）"""
    now = time.time()
    with _sess_guard:
        hit = _sessions.get(token)
        if hit and hit[1] > now:
            return hit[0]
    sk = skland.Skland(token)
    sk.login()
    with _sess_guard:
        _sessions[token] = (sk, now + SESS_TTL)
    return sk


# ---------------------------------------------------------------- 组装
def _bond_view(res: dict, guide_bonds: dict, bond_id: str, layer=0, activate=1) -> dict:
    meta = _pick(res.get('bondInfo') or {}, bond_id) or {}
    g = _pick(guide_bonds, bond_id) or {}
    kind = str(meta.get('type') or g.get('bondType') or '')
    return {
        'id': bond_id,
        'name': text(g.get('name') or meta.get('name') or bond_id),
        'icon': _url(meta.get('icon') or g.get('iconUrl')),
        'desc': text(meta.get('desc') or g.get('effect') or ''),
        'layer': _num(layer),
        'activate': 1 if activate else 0,
        'activeCount': _num(meta.get('activeCount') or g.get('activeCount')),
        'kind': 'season' if kind in ('1', 'SEASON') else 'regular',
    }


def _band_view(res: dict, guide_bands: dict, band_id: str, badge=None) -> dict:
    """分队（游戏里的叫法是「策略」）

    分队名是带队的干员 / NPC（登录版 ``bandInfo[].name``，公开版
    ``initiatorName``）；「二重奏」这类是分队**技能**名（``effectName``）。
    两个字段别搞反——搞反了界面上就把技能当队名了。
    """
    meta = _pick(res.get('bandInfo') or {}, band_id) or {}
    g = _pick(guide_bands, band_id) or {}
    icon = _url(meta.get('icon') or g.get('iconUrl'))
    name = text(meta.get('name') or g.get('initiatorName') or '')
    skill = text(g.get('name') or meta.get('effectName') or '')
    return {
        'id': band_id,
        'name': name or skill or band_id,
        'skill': skill,
        'icon': icon,
        'desc': text(meta.get('desc') or g.get('effect') or ''),
        'hp': _num(meta.get('totalHp') or g.get('hp')),
        'initiator': name,
        'initiatorIcon': _url(g.get('initiatorIconUrl')) or icon,
        'master': bool(badge and badge.get('masterVictor')),
        'victor': bool(badge),
    }


def _chess_view(res: dict, char_index: dict, chess: dict) -> dict:
    chess = chess or {}          # 列表里夹 null 是见过的，别一路 .get 崩下去
    cid = chess.get('charId') or ''
    mat = _pick(res.get('chessInfo') or {}, chess.get('chessId')) or {}
    info = chess.get('chessInfo') or {}
    g = char_index.get(cid) or {}

    tier = _num(info.get('chessLevel') or mat.get('chessLevel'))
    phase = _num(info.get('evolvePhase') or mat.get('evolvePhase'))
    cid = cid or info.get('charId') or mat.get('charId') or ''
    golden = bool(mat.get('goldenChessId') and
                  str(mat.get('id') or '') == str(mat.get('goldenChessId')))

    equips = []
    for e in chess.get('equips') or []:
        eid = (e or {}).get('equipId') or ''
        if not eid:
            continue
        em = _pick(res.get('equipInfo') or {}, eid) or {}
        equips.append({
            'id': eid, 'name': text(em.get('name') or eid),
            'icon': _url(em.get('icon')),
            'upgraded': bool(em.get('isUpgraded')),
            'level': _num(em.get('itemLevel')),
        })

    # 技能 / 模组的名字与图标只在公开接口的 chars 里
    skill_id = chess.get('skillId') or ''
    mod_id = chess.get('moduleId') or ''
    skill = next((s for s in (g.get('skills') or []) if s.get('skillId') == skill_id), None)
    module = next((m for m in (g.get('modules') or []) if m.get('moduleId') == mod_id), None)

    return {
        'charId': cid,
        'chessId': chess.get('chessId') or '',
        'name': text(info.get('charName') or mat.get('charName') or
                     g.get('name') or cid) or '未知干员',
        'tier': tier, 'phase': phase, 'golden': golden,
        'level': _num(info.get('charLevel') or mat.get('charLevel')),
        'merge': _num(chess.get('mergeLevel')),
        'profession': info.get('charProfession') or mat.get('charProfession') or
                      g.get('charProfession') or '',
        'professionIcon': _url(g.get('professionIconUrl')),
        'subIcon': _url(g.get('subProfessionIconUrl')),
        'halfBody': _url(g.get('halfBodyUrl')),
        'skill': _skill_view(skill_id, skill),
        'module': _module_view(mod_id, module),
        'equips': equips,
    }


def _record_view(res: dict, rec: dict, *, index: int, badges: dict,
                 char_index: dict, guide_bands: dict, guide_bonds: dict) -> dict:
    band_id = rec.get('bandId') or ''
    bonds = []
    for b in rec.get('bondInfo') or []:
        bid = (b or {}).get('bondId') or ''
        if bid:
            bonds.append(_bond_view(res, guide_bonds, bid,
                                    layer=(b or {}).get('layer'),
                                    activate=(b or {}).get('activate')))
    bonds.sort(key=lambda x: (-x['layer'], x['name']))

    opers = [_chess_view(res, char_index, c) for c in (rec.get('chessSquad') or [])]
    opers.sort(key=lambda o: (-o['tier'], -o['phase'], -o['level']))

    bosses = []
    for b in rec.get('acBossInfo') or []:
        bid = (b or {}).get('bossId') or ''
        bm = _pick(res.get('bossInfo') or {}, bid) or {}
        enemy = bm.get('handbookEnemyId') or bm.get('enemyId') or ''
        bosses.append({
            'id': bid,
            'name': text(bm.get('name') or BOSS_NAMES.get(enemy) or enemy or bid) or '未知首领',
            'enemyId': enemy,
            'hidden': bool((b or {}).get('isHidden')),
            'killed': bool((b or {}).get('isKilled')),
        })

    title_id = rec.get('title') or ''
    tm = _pick(res.get('titleInfo') or {}, title_id) or {}
    title = None
    if title_id:
        title = {'id': title_id, 'name': text(tm.get('name') or title_id),
                 'pic': tm.get('picId') or ''}

    start = _num(rec.get('gameStartTs'))
    end = _num(rec.get('gameEndTs'))
    dur = end - start if 0 < start <= end else 0
    diff = rec.get('modeDifficulty') or ''

    return {
        'id': rec.get('id') or '',
        'index': index,
        'success': _num(rec.get('completeState')) == 1,
        'favored': False,
        'scene': rec.get('sceneId') or '',
        'modeType': rec.get('modeType') or '',
        'modeTypeCn': TYPE_CN.get(rec.get('modeType') or '', rec.get('modeType') or ''),
        'difficulty': diff,
        'modeCn': MODE_CN.get(diff) or text(rec.get('mode') or '') or '未提供难度',
        'modeName': text(rec.get('mode') or ''),
        'rounds': _num(rec.get('passRound')),
        'startTs': start, 'endTs': end, 'dur': dur,
        'trophyBefore': _num(rec.get('trophyBefore')),
        'trophyAfter': _num(rec.get('trophyAfter')),
        'coinCost': _num(rec.get('coinCost')),
        'killCnt': _num(rec.get('killCnt')),
        'syncCnt': _num(rec.get('syncCnt')),
        'band': _band_view(res, guide_bands, band_id, badges.get(band_id)),
        'title': title,
        'bonds': bonds,
        'opers': opers,
        'bosses': bosses,
        'events': [text(e) for e in (rec.get('events') or []) if text(e)],
    }


def _duration_cn(sec: int) -> str:
    if sec <= 0:
        return '—'
    m = int(sec) // 60
    h, m = divmod(m, 60)
    return ('%d 小时 %d 分' % (h, m)) if h else ('%d 分' % m if m else '不足 1 分')


def stat(file: str, force=False) -> dict:
    """拉齐三个接口并组装成前端直接能画的结构"""
    token = accounts.token_for(file)
    if not token:
        raise RuntimeError('这个账号没有森空岛 token（MAA 导入的账号看不了战绩）')
    acc = accounts.get_account(file) or {}
    acct = acc.get('account') or {}       # uid / 服务器信息都在这一层
    uid = str(acct.get('uid') or acc.get('uid') or '')
    if not uid:
        raise RuntimeError('账号里没有 uid，重新同步一次账号再试')

    gd = guide(force=force)                     # 公开资源，失败不致命
    sk = _session(token)

    res_list = resources(sk, force=force)       # 登录版字典（一次请求）
    cur = res_list[0] if res_list else {}
    activity_id = ((cur.get('versionInfo') or {}).get('activityId') or
                   (gd.get('versionInfo') or {}).get('seasonId') or ACTIVITY)
    res = next((r for r in res_list
                if (r.get('versionInfo') or {}).get('activityId') == activity_id), cur)

    raw = sk.garrison_info(uid, activity_id)
    hist = raw.get('history') or {}
    career = raw.get('career') or {}
    player = raw.get('gameUserInfo') or {}

    guide_bands = {s.get('strategyId'): s for s in (gd.get('strategies') or [])
                   if s.get('strategyId')}
    guide_bonds = {b.get('bondId'): b for b in (gd.get('bonds') or []) if b.get('bondId')}
    char_index = {c.get('charId'): c for c in (gd.get('chars') or []) if c.get('charId')}
    badges = {b.get('id'): b for b in (career.get('bandBadges') or []) if b.get('id')}

    fav = {r.get('id') for r in (hist.get('favourRecords') or []) if isinstance(r, dict)}
    seen = set()
    uniq = []
    for r in ((hist.get('records') or []) + (hist.get('favourRecords') or [])):
        if not isinstance(r, dict):
            continue
        rid = (r.get('id') or '').strip()
        if not rid or rid in seen:
            continue
        seen.add(rid)
        uniq.append(r)
    current = [r for r in uniq if (r.get('activityId') or activity_id) == activity_id]
    if not current:
        current = uniq                          # activityId 没填就别把记录全滤掉
    current.sort(key=lambda r: -_num(r.get('gameEndTs')))

    records = []
    for i, r in enumerate(current, 1):
        v = _record_view(res, r, index=i, badges=badges, char_index=char_index,
                         guide_bands=guide_bands, guide_bonds=guide_bonds)
        v['favored'] = v['id'] in fav
        records.append(v)

    # ---- 生涯统计 ----
    # 口径对齐小黑盒：总场次与完成率都按「接口这次返回的对局」算（一屏能对上号），
    # 生涯总数单独放 totalAll，别混在一个数里让人对不上账。
    s_total, s_win = career.get('totalSingleBattleCnt'), career.get('totalSingleBattleWinCnt')
    m_total, m_win = career.get('totalMultiBattleCnt'), career.get('totalMultiBattleWinCnt')
    total_all = (_num(s_total) + _num(m_total)) if (s_total is not None and m_total is not None) else None
    total_cnt = len(records)
    total_win = sum(1 for v in records if v['success'])

    boss_kills = sum(1 for v in records for b in v['bosses'] if b['killed'])
    hidden_kills = sum(1 for v in records for b in v['bosses'] if b['killed'] and b['hidden'])
    dur_sum = sum(v['dur'] for v in records if v['dur'] > 0)

    band_cnt, bond_cnt = {}, {}
    for v in records:
        bid = v['band']['id']
        if bid:
            c, w = band_cnt.get(bid, (0, 0))
            band_cnt[bid] = (c + 1, w + (1 if v['success'] else 0))
        for b in v['bonds']:
            if not b['activate'] or b['layer'] <= 0:
                continue
            c, top = bond_cnt.get(b['id'], (0, 0))
            bond_cnt[b['id']] = (c + 1, max(top, b['layer']))

    top_bands = []
    for bid, (c, w) in sorted(band_cnt.items(), key=lambda x: -x[1][0])[:5]:
        item = _band_view(res, guide_bands, bid, badges.get(bid))
        item.update(count=c, wins=w, winRate=(round(100.0 * w / c, 1) if c else None))
        top_bands.append(item)
    top_bonds = []
    for bid, (c, top) in sorted(bond_cnt.items(), key=lambda x: -x[1][0])[:6]:
        item = _bond_view(res, guide_bonds, bid, layer=top)
        item.update(count=c, maxLayer=top)
        top_bonds.append(item)

    # 近 10 场叠层：每场最高的那个激活盟约，给折线图用
    layers = []
    for v in records[:10][::-1]:
        act = [b for b in v['bonds'] if b['activate'] and b['layer'] > 0]
        top = max(act, key=lambda b: b['layer']) if act else None
        layers.append({
            'recordId': v['id'], 'success': v['success'], 'at': v['endTs'],
            'layer': top['layer'] if top else 0,
            'bondId': top['id'] if top else '',
            'name': top['name'] if top else '',
            'icon': top['icon'] if top else '',
        })

    titles = []
    for t in (career.get('normalTitles') or []) + (career.get('specialTitles') or []):
        tid = (t or {}).get('id') or ''
        if not tid:
            continue
        tm = _pick(res.get('titleInfo') or {}, tid) or {}
        titles.append({
            'id': tid, 'name': text(t.get('name') or tm.get('name') or tid),
            'pic': t.get('picId') or tm.get('picId') or '',
            'count': _num(t.get('count')), 'type': t.get('type') or '',
        })
    type_cnt = career.get('titleTypeCnt') or {}
    dims = [{'key': k, 'label': label, 'count': _num(type_cnt.get(k))}
            for k, label in (('COIN', '资金管理'), ('KILL', '补救能力'),
                             ('DAMAGE', '领袖挑战'), ('BOND', '盟约理解'),
                             ('CHAR', '人力调度'), ('EQUIP', '装备活用'))]

    ver = res.get('versionInfo') or {}
    gver = gd.get('versionInfo') or {}
    bg = (gd.get('image') or [{}])[0] or {}
    return {
        'player': {
            'name': text(player.get('name')) or '未知博士',
            'uid': uid,
            'level': _num(player.get('level')),
            'avatar': _url((player.get('avatar') or {}).get('url')),
            'official': bool(player.get('isOfficial') or acct.get('isOfficial')),
            'server': '官服' if str(acct.get('channelMasterId') or '1') == '1' else 'B 服',
        },
        'season': {
            'activityId': activity_id,
            'versionId': ver.get('versionId') or gver.get('versionId') or '',
            'name': text(ver.get('seasonName') or '规程'),
            'phase': text(ver.get('phaseName') or gver.get('phaseName') or ''),
            'background': _url(bg.get('backgroundPc')),
            'logo': _url(bg.get('logoPc')),
        },
        'career': {
            'total': total_cnt,
            'totalAll': total_all,
            'single': {'total': _num(s_total), 'wins': _num(s_win)},
            'multi': {'total': _num(m_total), 'wins': _num(m_win)},
            'completeRate': (round(100.0 * total_win / total_cnt, 1) if total_cnt else None),
            'trophy': _num(career.get('totalTrophyCnt')),
            'beLike': _num(career.get('totalBeLikeCnt')),
            'bossKills': boss_kills,
            'hiddenBossKills': hidden_kills,
            'duration': dur_sum,
            'durationCn': _duration_cn(dur_sum),
            'bestMode': text(career.get('mode') or ''),
            'topBands': top_bands,
            'topBonds': top_bonds,
            'titles': titles,
            'titleDims': dims,
            'layers': layers,
        },
        'records': records,
        'counts': {'current': len(records), 'all': len(uniq)},
        'updatedAt': int(time.time()),
    }


def invalidate() -> None:
    """强制刷新：内存与磁盘缓存一起作废，下次重新拉"""
    _guide_mem.update(at=0.0, data=None)
    _res_mem.update(at=0.0, list=None)
    with _sess_guard:
        _sessions.clear()
    for name in ('guide.json', 'resources.json'):
        try:
            os.remove(os.path.join(cache_dir(), name))
        except OSError:
            pass
