# -*- coding: utf-8 -*-
"""内置游戏资源包：关卡索引、活动名、干员表、技能名、模组名

数据来自 resources/arknights.json（由 make_pack.py 从 ArknightsGameResource 压出来），
程序运行时不依赖任何外部游戏目录，也就不需要用户额外准备资源表。
"""
from __future__ import annotations

import io
import json
import re
import threading
import time

from . import settings

_lock = threading.RLock()
_cache = {}
_pack_cache = [None]

# 剧情关：有剧情没战斗，作业站永远不会有作业。
# 新资源包带官方标记 stage['story']（来自 stage_table 的 isStoryOnly，共 208 条）；
# 用户本地还挂着旧包时没有这个字段，就用代号/id 兜底，实测这两条规则
# 正好覆盖全部 208 条且不误伤任何战斗关：
#   xx-ST-n / OF-ST1 ── 内部 id 形如 act13side_st01 / a003_st01（_stNN 结尾）
#   10-1 / EG-3      ── 主线纯剧情节点，id 形如 st_08-01 / spst_08-01（st_ 开头）
_STORY_CODE = re.compile(r'-ST-?\d+$', re.I)
_STORY_ID = re.compile(r'_st\d+$', re.I)
_STORY_PREFIX = re.compile(r'^(?:sp)?st_', re.I)


def _is_story(st: dict) -> bool:
    if 'story' in st:
        return bool(st.get('story'))
    return bool(_STORY_CODE.search(st.get('code') or '')
                or _STORY_ID.search(st.get('id') or '')
                or _STORY_PREFIX.match(st.get('id') or ''))


def pack() -> dict:
    """资源包（整个进程只读一次）"""
    with _lock:
        if _pack_cache[0] is None:
            try:
                with io.open(settings.pack_path(), encoding='utf-8') as f:
                    _pack_cache[0] = json.load(f)
            except Exception:
                _pack_cache[0] = {'chars': {}, 'skills': {}, 'equips': {},
                                  'stages': [], 'zones': {}, 'subProfessions': {}, 'counts': {}}
        return _pack_cache[0]


def pack_info() -> dict:
    p = pack()
    return {
        'version': p.get('version') or '',
        'fingerprint': p.get('fingerprint') or '',
        'generatedAt': p.get('generatedAt'),
        'counts': p.get('counts') or {},
        'path': settings.pack_path(),
    }


def _stages():
    with _lock:
        if 'stages' in _cache:
            return _cache['stages']
        p = pack()
        zones = p.get('zones') or {}
        acts = p.get('activities') or {}
        raw = p.get('stages') or []
        all_ids = {st['id'] for st in raw}
        rows = []
        for st in raw:
            # #f# 是突袭版、#s 是简单版，都跟主关共用关卡代号。作业站只认主关 id，
            # 把它们也列出来只会得到「同一个代号两行、第二行永远没作业」，
            # 所以这里直接跳过，突袭能力用 raid 标记挂在主关上。
            if '#' in st['id']:
                continue
            # 剧情关不进搜索，也不参与活动展开（以前每个活动都多出几行
            # 「共 0 份 / 打不了」，用户点了也永远没作业）
            if _is_story(st):
                continue
            zid = st.get('zone') or ''
            zone = zones.get(zid) or {}
            # 活动正式名：stage id 前缀就是 act 编号（act55side_10 → act55side）
            gid = st['id'].split('_')[0]
            act = acts.get(gid) or {}
            rows.append({
                'stageId': st['id'],
                'code': st.get('code') or '',
                'name': st.get('name') or '',
                'zoneId': zid,
                'zoneName': zone.get('name') or '',
                'zoneType': zone.get('type') or '',
                'activity': act.get('name') or '',
                'raid': (st['id'] + '#f#') in all_ids,
                'difficulty': '',
                'danger': '',
            })
        rows.sort(key=lambda r: (r['stageId'], r['code']))
        _cache['stages'] = rows
        return rows


def _norm(s):
    return re.sub(r'\s+', '', str(s or '')).lower()


def resolve(keyword: str) -> dict:
    """把用户输入解析成关卡列表

    返回 {mode:'stage'|'activity'|'unknown', title, keyword, stages:[...]}
    支持：TL-10 / act55side_10 / TR- / act55side / 中文关卡名 / 中文活动名
    """
    kw = (keyword or '').strip()
    # 有人会直接粘 stage id（act13side_ex01#f# / main_15-02#s），回落到主关
    kw = re.sub(r'#(?:f#|s)$', '', kw, flags=re.I)
    if not kw:
        return {'mode': 'unknown', 'title': '', 'keyword': kw, 'stages': []}

    stages = _stages()
    low = kw.lower()

    # 1. 关卡代号精确（TL-10、TR-1、1-7）
    hit = [s for s in stages if s['code'].lower() == low]
    if hit:
        return {'mode': 'stage', 'title': hit[0]['code'] + ' ' + hit[0]['name'],
                'keyword': kw, 'stages': hit[:1]}

    # 2. 关卡内部 id 精确（act55side_10）
    hit = [s for s in stages if s['stageId'].lower() == low]
    if hit:
        return {'mode': 'stage', 'title': hit[0]['code'] + ' ' + hit[0]['name'],
                'keyword': kw, 'stages': hit[:1]}

    # 3. 关卡代号前缀（TR- → 全部 TR 关，排除塔模式重名关）
    hit = [s for s in stages
           if s['code'].lower().startswith(low) and re.search(r'\d', s['code'])
           and not s['stageId'].startswith('lt_')
           and not s['zoneId'].startswith('tower')]
    if hit:
        hit.sort(key=lambda r: (len(r['code']), r['code']))
        title = '%s 系列（%d 关）' % (kw.upper(), len(hit))
        # 整个系列只属于一个活动时，把活动名带上（SR → 月行水上 · SR 系列）
        gids = {s['stageId'].split('_')[0] for s in hit}
        if len(gids) == 1:
            act = activity_table().get(gids.pop()) or {}
            if act.get('name'):
                title = '%s · %s 系列（%d 关）' % (act['name'], kw.upper(), len(hit))
        return {'mode': 'activity', 'title': title,
                'keyword': kw, 'stages': hit}

    # 4. 活动内部 id 前缀（act55side → 该活动全部关卡）
    hit = [s for s in stages if s['stageId'].lower().startswith(low) and '_' in s['stageId']]
    if hit:
        return {'mode': 'activity',
                'title': '%s（%d 关）' % (_act_name(hit[0]['stageId'], hit), len(hit)),
                'keyword': kw, 'stages': hit}

    # 5. 活动正式名 / 分区名 / 关卡名 模糊
    n = _norm(kw)
    if n:
        best = None
        for aid, a in activity_table().items():
            if n in _norm(a.get('name')):
                hs = [s for s in stages if s['stageId'].startswith(aid + '_')]
                if hs and (best is None or len(hs) > len(best[1])):
                    best = (a['name'], hs)
        if best:
            return {'mode': 'activity',
                    'title': '%s（%d 关）' % (best[0], len(best[1])),
                    'keyword': kw, 'stages': best[1]}

    hit = [s for s in stages if n and n in _norm(s['zoneName'])]
    if not hit:
        hit = [s for s in stages if n and n in _norm(s['name'])]
    if hit:
        if len(hit) == 1:
            return {'mode': 'stage', 'title': hit[0]['code'] + ' ' + hit[0]['name'],
                    'keyword': kw, 'stages': hit[:1]}
        return {'mode': 'activity', 'title': '%s（%d 关）' % (kw, len(hit)),
                'keyword': kw, 'stages': hit}

    return {'mode': 'unknown', 'title': '', 'keyword': kw, 'stages': []}


def _act_name(stage_id: str, rows=None) -> str:
    """取 stage 所属活动的正式名（没有就用 zone 名兜底）"""
    gid = str(stage_id).split('_')[0]
    a = activity_table().get(gid) or {}
    if a.get('name'):
        return a['name']
    if rows:
        for r in rows:
            if r.get('zoneName'):
                return r['zoneName']
    return gid


def activity_list(limit: int = 16) -> list:
    """近期活动（按开始时间倒序），给前端做快捷入口

    名称用游戏内的活动正式名（activity_table），不是 zone 分区名：
    残照终尽 → 时间啊，向前！、通学路 → 月行水上。
    每条带 prefix（关卡代号字母，如 TL），前端显示在名字后面。
    """
    groups = {}
    for s in _stages():
        sid = s['stageId']
        if not sid.startswith('act'):
            continue
        m = re.match(r'act(\d+)', sid)
        if not m:
            continue
        key = sid.split('_')[0]
        g = groups.get(key)
        if g is None:
            g = groups[key] = {'id': key, 'num': int(m.group(1)),
                               'name': '', 'count': 0, 'codes': []}
        g['count'] += 1
        if s['code']:
            g['codes'].append(s['code'])
        if s['zoneName'] and not g['name']:
            g['name'] = s['zoneName']

    acts = activity_table()

    def act_span(r):
        a = acts.get(r['id']) or {}
        return a.get('start') or 0, a.get('end') or 0

    def sort_key(r):
        a = acts.get(r['id']) or {}
        return (a.get('start') or 0, r['num'])

    # 正在进行的活动永远排最前，其次是还没开始的，最后才是已结束的（各自按开始时间倒序）
    now = int(time.time())

    def state_rank(r):
        s, e = act_span(r)
        if s and s <= now and (not e or now <= e):
            return 2
        if s and s > now:
            return 1
        return 0

    rows = [r for r in groups.values() if r['count'] >= 3]
    rows.sort(key=lambda r: (state_rank(r), sort_key(r)), reverse=True)

    out = []
    for r in rows[:limit]:
        a = acts.get(r['id']) or {}
        out.append({
            'id': r['id'],
            'num': r['num'],
            'name': a.get('name') or r['name'] or r['id'],
            'prefix': _code_prefix(r['codes']),
            'count': r['count'],
            'start': a.get('start') or 0,
            'end': a.get('end') or 0,
        })
    return out


def _code_prefix(codes) -> str:
    """从关卡代号里取活动的字母前缀：TL-1..TL-EX-8 → TL"""
    hits = {}
    for c in codes or []:
        m = re.match(r'^([A-Za-z]{1,4})[-\d]', c)
        if m:
            p = m.group(1).upper()
            hits[p] = hits.get(p, 0) + 1
    if not hits:
        return ''
    return max(hits.items(), key=lambda kv: kv[1])[0]


def activity_table() -> dict:
    """活动正式名：{act55side: {name, start, end}}"""
    return pack().get('activities') or {}


def char_table() -> dict:
    return pack().get('chars') or {}


def skill_table() -> dict:
    return pack().get('skills') or {}


def equip_table() -> dict:
    return pack().get('equips') or {}


def subprof_seed() -> dict:
    """资源包里附带的分支中文名种子（分支名没有公开静态表，只能边用边学）"""
    return pack().get('subProfessions') or {}


def clear_cache():
    with _lock:
        _cache.clear()
        _pack_cache[0] = None
