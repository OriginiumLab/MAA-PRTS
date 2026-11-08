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
        # 有简单档（easy_ 前缀）的章节（9-14 章）：标准行标「标准」，
        # 跟 磨难/简单 的行徽标对齐；0-8 / 15+ 章仍叫「普通」
        std_zones = {st.get('zone') for st in raw if st['id'].startswith('easy_')}
        std_zones.discard(None)
        std_zones.discard('')
        rows = []
        for st in raw:
            sid = st['id']
            # 主线难度体系（按 stageId 前缀/后缀区分，游戏数据里就这么存的）：
            #   0-8 章  main_N-M（普通）+ main_N-M#f#（突袭）
            #   9 章    easy_N-M（简单）+ main_N-M（标准，带 #f# 突袭）
            #   10-14 章 easy_N-M（简单）/ main_N-M（标准）/ tough_N-M（磨难）三套独立 id
            #   15-17 章 main_N-M（普通）+ main_N-M#s（险地）
            # #f# 突袭跟主关共用代号，作业站的作业 stage_name 也只写主关 id、
            # 靠 difficulty 位区分，所以 #f# 不单独成行，能力用 raid 标记挂主关；
            # #s 险地（15+ 章）在作业站是独立的 stage_name，单独成行。
            if sid.endswith('#f#'):
                continue
            # 剧情关不进搜索，也不参与活动展开（以前每个活动都多出几行
            # 「共 0 份 / 打不了」，用户点了也永远没作业）
            if _is_story(st):
                continue
            # 剧情关不进搜索，也不参与活动展开（以前每个活动都多出几行
            # 「共 0 份 / 打不了」，用户点了也永远没作业）
            if _is_story(st):
                continue
            zid = st.get('zone') or ''
            if sid.endswith('#s'):
                diff, diff_cn = 'risky', '险地'
            elif sid.startswith('easy_'):
                diff, diff_cn = 'easy', '简单'
            elif sid.startswith('tough_'):
                diff, diff_cn = 'tough', '磨难'
            else:
                diff, diff_cn = 'normal', ('标准' if zid in std_zones else '')
            zone = zones.get(zid) or {}
            # 活动正式名：stage id 前缀就是 act 编号（act55side_10 → act55side）
            gid = sid.split('_')[0]
            act = acts.get(gid) or {}
            rows.append({
                'stageId': sid,
                'code': st.get('code') or '',
                'name': st.get('name') or '',
                'zoneId': zid,
                'zoneName': zone.get('name') or '',
                'zoneType': zone.get('type') or '',
                'activity': act.get('name') or '',
                'raid': (sid + '#f#') in all_ids,
                'difficulty': diff,
                'difficultyCN': diff_cn,
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
    # 有人会直接粘 stage id（act13side_ex01#f#），突袭后缀回落到主关；
    # #s（险地）是作业站认可的独立 stage_name，保留直接命中险地行
    kw = re.sub(r'#f#$', '', kw, flags=re.I)
    if not kw:
        return {'mode': 'unknown', 'title': '', 'keyword': kw, 'stages': []}

    stages = _stages()
    low = kw.lower()

    # 1. 关卡代号精确（TL-10、TR-1、1-7）。10-14 章的简单/标准/磨难、
    #    15-17 章的普通/险地共用代号，全部作为独立难度行返回，
    #    由 copilot.search 排序（磨难优先，简单垫底）
    hit = [s for s in stages if s['code'].lower() == low]
    if hit:
        main = next((s for s in hit if s['difficulty'] == 'normal'), hit[0])
        return {'mode': 'stage', 'title': main['code'] + ' ' + main['name'],
                'keyword': kw, 'stages': hit}

    # 2. 关卡内部 id 精确（act55side_10）
    hit = [s for s in stages if s['stageId'].lower() == low]
    if hit:
        return {'mode': 'stage', 'title': hit[0]['code'] + ' ' + hit[0]['name'],
                'keyword': kw, 'stages': hit[:1]}

    # 3. 关卡代号前缀（TR- → 全部 TR 关，排除塔模式重名关）。
    #    系列/活动视图不摆「简单」难度行：作业站简单作业寥寥（12-7 全章才 3 份），
    #    挂进来只会让每关多一行「共 0 份」；要看简单请精确搜单关代号
    hit = [s for s in stages
           if s['code'].lower().startswith(low) and re.search(r'\d', s['code'])
           and s['difficulty'] != 'easy'
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
    hit = [s for s in stages
           if s['stageId'].lower().startswith(low) and '_' in s['stageId']
           and s['difficulty'] != 'easy']
    if hit:
        return {'mode': 'activity',
                'title': '%s（%d 关）' % (_act_name(hit[0]['stageId'], hit), len(hit)),
                'keyword': kw, 'stages': hit}

    # 5. 活动正式名 / 分区名 / 关卡名 模糊（同上，系列视图不带「简单」行）
    n = _norm(kw)
    if n:
        best = None
        for aid, a in activity_table().items():
            if n in _norm(a.get('name')):
                hs = [s for s in stages
                      if s['stageId'].startswith(aid + '_') and s['difficulty'] != 'easy']
                if hs and (best is None or len(hs) > len(best[1])):
                    best = (a['name'], hs)
        if best:
            return {'mode': 'activity',
                    'title': '%s（%d 关）' % (best[0], len(best[1])),
                    'keyword': kw, 'stages': best[1]}

    hit = [s for s in stages
           if n and n in _norm(s['zoneName']) and s['difficulty'] != 'easy']
    if not hit:
        hit = [s for s in stages
               if n and n in _norm(s['name']) and s['difficulty'] != 'easy']
    if hit:
        if len(hit) == 1:
            return {'mode': 'stage', 'title': hit[0]['code'] + ' ' + hit[0]['name'],
                    'keyword': kw, 'stages': hit[:1]}
        return {'mode': 'activity', 'title': '%s（%d 关）' % (kw, len(hit)),
                'keyword': kw, 'stages': hit}

    return {'mode': 'unknown', 'title': '', 'keyword': kw, 'stages': []}


def _nat_key(sid):
    """自然排序：活动 id 的数字没零填充（act55side_2 / act55side_10），字典序会乱"""
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', str(sid or ''))]


def _series_key(sid):
    """关卡系列（章节/活动）分组键：难度变体先归并到主形态再取系列名"""
    s = re.sub(r'^(?:easy|tough)_', 'main_', str(sid or '').split('#')[0])
    if '_' in s:
        head = s.split('_')[0]
        if head == 'main':
            return s.rsplit('-', 1)[0]        # main_12（一章一个系列）
        return head                           # act55side / tr 等
    return s


# TR 教学关（tr_01 / act55side_tr01 / act2vmulti-tr01 三种形态）：关卡表里全
# 挂在系列末尾，但游戏里实际插在流程各处、位置我们不知道，所以「下一关」
# 序列默认不带 TR。以后摸清位置了在 TR_AFTER 人工登记（TR 的 stageId → 它
# 排在哪个 stageId 的难度行之后），登记过的 TR 会自动插回序列参与推进。
_TR_ID = re.compile(r'(?:^|[._-])tr[_-]?\d+$', re.I)
TR_AFTER = {}      # 例：{'act55side_tr01': 'act55side_10'}


def _is_tr(sid):
    return bool(_TR_ID.search(str(sid or '')))


def next_stage(stage):
    """同系列（章节/活动）、同难度的下一关；系列末尾或未知序列返回 None

    按行的难度视角推进，跟用户推图的习惯一致：磨难行下一关还是磨难
    （tough_12-06 → tough_12-07）、险地行下一关还是险地（main_15-02#s →
    main_15-03#s）、活动关按活动内顺序（TL-2 → TL-3）。

    TR 教学关默认不参与推进（表里顺序不可信，见 TR_AFTER 说明）；登记过
    位置的 TR 插回锚点后面。当前关本身是未登记的 TR 时返回 None——
    它在流程里的位置未知，推不出「下一关」是哪。
    """
    sid = stage.get('stageId') or ''
    diff = stage.get('difficulty') or 'normal'
    seq = sorted(
        (s for s in _stages()
         if _series_key(s['stageId']) == _series_key(sid)
         and (s.get('difficulty') or 'normal') == diff
         and (not _is_tr(s['stageId']) or s['stageId'] in TR_AFTER)),
        key=lambda s: _nat_key(s['stageId']))
    if diff == 'normal' and TR_AFTER:
        # 已登记位置的 TR 插到锚点难度行后面（同一锚点挂多个 TR 时按 id 序）
        rows = {s['stageId']: s for s in _stages()}
        ordered = []
        for s in seq:
            ordered.append(s)
            for tr, anchor in sorted(TR_AFTER.items()):
                if anchor == s['stageId'] and tr in rows:
                    ordered.append(rows[tr])
        seq = ordered
    ids = [s['stageId'] for s in seq]
    if sid not in ids:
        return None
    i = ids.index(sid)
    if i + 1 >= len(seq):
        return None
    nxt = seq[i + 1]
    return {'code': nxt['code'], 'stageId': nxt['stageId'],
            'difficulty': nxt.get('difficulty') or 'normal',
            'difficultyCN': nxt.get('difficultyCN') or ''}


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


# ---------------------------------------------------------------- 干员联想
# GB2312 一级字库按拼音分组的编码区间：查汉字首字母用（标准库没拼音库，
# 这张短表覆盖 3755 个常用字）。二级字库的生僻字查不到，按通配处理
# （首字母序列里留空位，输入对应位任意字母都算命中），中文子串搜索兜底。
# 多音字按区间归属取（如「重」归 z），干员搜索场景够用。
_GB_PINYIN = (
    (0xB0A1, 0xB0C4, 'a'), (0xB0C5, 0xB2C0, 'b'), (0xB2C1, 0xB4ED, 'c'),
    (0xB4EE, 0xB6E9, 'd'), (0xB6EA, 0xB7A1, 'e'), (0xB7A2, 0xB8C0, 'f'),
    (0xB8C1, 0xB9FD, 'g'), (0xB9FE, 0xBBF6, 'h'), (0xBBF7, 0xBFA5, 'j'),
    (0xBFA6, 0xC0AB, 'k'), (0xC0AC, 0xC2E7, 'l'), (0xC2E8, 0xC4C2, 'm'),
    (0xC4C3, 0xC5B5, 'n'), (0xC5B6, 0xC5BD, 'o'), (0xC5BE, 0xC6D9, 'p'),
    (0xC6DA, 0xC8BA, 'q'), (0xC8BB, 0xC8F5, 'r'), (0xC8F6, 0xCBF9, 's'),
    (0xCBFA, 0xCDD9, 't'), (0xCDDA, 0xCEF3, 'w'), (0xCEF4, 0xD1B8, 'x'),
    (0xD1B9, 0xD4D0, 'y'), (0xD4D1, 0xD7F9, 'z'),
)

# 二级字库的拼音补丁：GB2312 一级区间表按拼音排、覆盖 3755 常用字，
# 二级字库按部首排、区间法完全无效。干员名里查不到首字母的就这 49 个
# （扫描全部干员名得出），逐个补上——匹配要精确，不留通配位。
_EXTRA_INITIALS = {
    '吽': 'h', '啾': 'j', '喙': 'h', '娅': 'y', '孑': 'j', '嵯': 'c',
    '幡': 'f', '弑': 's', '撷': 'x', '曦': 'x', '枳': 'z', '栎': 'l',
    '桠': 'y', '梓': 'z', '溟': 'm', '濯': 'z', '燧': 's', '琥': 'h',
    '瑕': 'x', '祐': 'y', '箸': 'z', '绮': 'q', '缇': 't', '缪': 'm',
    '翎': 'l', '聆': 'l', '芙': 'f', '荻': 'd', '莓': 'm', '莺': 'y',
    '菈': 'l', '葚': 's', '薇': 'w', '蚺': 'r', '谟': 'm', '踝': 'h',
    '钼': 'm', '铎': 'd', '锏': 'j', '隼': 's', '雉': 'z', '霆': 't',
    '鲨': 's', '鸮': 'x', '麒': 'q', '麟': 'l', '黛': 'd',
}

_char_initials_cache = [None]


def _initial(ch: str) -> str:
    """单字符拼音首字母；ASCII 字母数字原样小写，查不到的返回 ''（占位）"""
    if ch.isascii():
        return ch.lower() if ch.isalnum() else ''
    hit = _EXTRA_INITIALS.get(ch)
    if hit:
        return hit
    try:
        b = ch.encode('gb2312')
    except UnicodeEncodeError:
        return ''
    code = b[0] << 8 | b[1]
    for lo, hi, letter in _GB_PINYIN:
        if lo <= code <= hi:
            return letter
    return ''


_ANY = '\x00'   # 生僻字 / 连字符等查不到首字母的占位：匹配时当作通配


def _char_initials() -> dict:
    """{干员名: 首字母序列}（银灰 → 'yh'），全进程只算一次

    查不到首字母的字符用 _ANY 占位而不是丢掉——丢了会错位，「夜莺」就
    变成 'y' 而匹配不上 'ye' 了。"""
    with _lock:
        if _char_initials_cache[0] is None:
            out = {}
            for c in char_table().values():
                name = str(c.get('name') or '')
                if name:
                    out[name] = ''.join(_initial(ch) or _ANY for ch in name)
            _char_initials_cache[0] = out
        return _char_initials_cache[0]


def _ini_match(ini: str, q: str) -> bool:
    """字母输入按首字母序列前缀**精确**匹配

    通配位（生僻字查不到首字母，如「伊芙利特」的「芙」）不匹配任何
    字母——yhlt 不能靠通配碰上 y?lt（用户明确要精确）；这类干员用
    中文名字搜索。"""
    if len(q) > len(ini):
        return False
    for i, ch in enumerate(q):
        s = ini[i]
        if s == _ANY or s != ch:
            return False
    return True


def suggest_chars(q: str, limit: int = 12) -> list:
    """干员联想：拼音首字母（yh → 银灰）或名字片段，星级降序

    首字母是精确前缀匹配（不含通配）；黑名单弹窗的快速搜索用。"""
    q = str(q or '').strip().lower()
    if not q:
        return []
    out = []
    for c in char_table().values():
        name = str(c.get('name') or '')
        if not name:
            continue
        if q not in name.lower() and not _ini_match(_char_initials().get(name, ''), q):
            continue
        out.append({'name': name,
                    'rarity': int(c.get('rarity') or 0),
                    'profession': str(c.get('profession') or '')})
    out.sort(key=lambda x: (-x['rarity'], x['name']))
    return out[:limit]


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
