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


# 有「简单档」（easy_ 前缀）又要在系列视图里摆出来的章节：9-14 章都有简单档，
# 但 0-9 章的导入菜单只分普通/突袭，简单行常年 0 份，只有 10-14 章要按
# 简单/标准/磨难三档分别导入（见 docs/frontend.md「全部导入」菜单）。
_EASY_CHAPTERS = (10, 11, 12, 13, 14)


def _chapter_no(code):
    m = re.match(r'^(\d+)-', str(code or ''))
    return int(m.group(1)) if m else -1


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

    # 3. 纯章节号（0 / 2 / 12 / 15-）→ 这一章的全部关卡：普通关、剧情关、
    #    S 支线关、TR 教学关，按游戏里的推图顺序返回（2-1 → S2-1 → 2-2 →
    #    S2-2 → S2-3 → S2-4 → 2-3 …）。以前只能搜到「2-1 … 2-10」，S2-x
    #    和 TR-x 都搜不出来，只能在作业集里一关一关加。
    m = re.fullmatch(r'(\d{1,2})-?', low)
    if m:
        ch = int(m.group(1))
        flow = _flow()
        hit = [s for s in stages
               if (flow.get(_base_sid(s['stageId'])) or (None,))[0] == (0, ch)
               and (s['difficulty'] != 'easy' or ch in _EASY_CHAPTERS)
               and not s['stageId'].startswith('lt_')
               and not s['zoneId'].startswith('tower')]
        if hit:
            hit.sort(key=lambda r: flow_key(r['stageId'])[:2])
            return {'mode': 'activity', 'title': '%d 章（%d 关）' % (ch, len(hit)),
                    'keyword': kw, 'stages': hit}

    # 3. 关卡代号前缀（TR- → 全部 TR 关，排除塔模式重名关）。
    #    系列/活动视图一般不摆「简单」难度行：作业站简单作业寥寥（12-7 全章才 3 份），
    #    挂进来只会让每关多一行「共 0 份」；10-14 章例外——导入菜单要按
    #    简单/标准/磨难三档分开导，简单行得看得见。要看单章简单请精确搜关卡代号
    hit = [s for s in stages
           if s['code'].lower().startswith(low) and re.search(r'\d', s['code'])
           and (s['difficulty'] != 'easy' or _chapter_no(s['code']) in _EASY_CHAPTERS)
           and not s['stageId'].startswith('lt_')
           and not s['zoneId'].startswith('tower')]
    if hit:
        hit.sort(key=lambda r: flow_key(r['stageId'])[:2])
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
# 挂在系列末尾，但游戏里实际插在流程各处。位置来自官方 stage_table 的
# unlockCondition——TR 的解锁前置关就是它前面那一关，所以这张表是「TR → 它排
# 在哪一关后面」，不是猜的，游戏大版本更新后用 resources/make_pack.py 同源的
# 数据重生成一次即可（见 docs/backend.md「TR 教学关位置」）。
# 表里没有的 TR（多人联机 IG-TR、塔 lt_tr_01、少数早期活动）按「位置未知」处理，
# 一律排到所在系列最前面，游戏里教学关本来也都挂在活动开头。
# 三种形态：tr_01（主线）/ act55side_tr01（活动）/ act2vmulti-tr01；
# 另有综合作战训练 tr_s_1（TR-S-1），也是教学关、表里同样挂在流程中段。
_TR_ID = re.compile(r'(?:^|[._-])tr(?:[_-]s)?[_-]?\d+$', re.I)
TR_AFTER = {
    # ── 活动（act 系列）──
    'act11d0_tr01': 'act11d0_02',
    'act12d0_tr01': 'act12d0_02',
    'act12side_tr01': 'act12side_st01',
    'act12side_tr02': 'act12side_05',
    'act13d5_tr01': 'act13d5_01',
    'act13side_tr01': 'act13side_st01',
    'act13side_tr02': 'act13side_st02',
    'act14side_tr01': 'main_01-10',      # 解锁条件是主线 1-10，落到活动开头
    'act14side_tr02': 'act14side_04',
    'act15d0_tr01': 'act15d0_01',
    'act16d5_tr01': 'act16d5_01',
    'act16side_tr01': 'act16side_03',
    'act17d0_tr01': 'act17d0_st01',
    'act18d0_tr01': 'act18d0_01',
    'act18d3_tr01': 'act18d3_01',
    'act18side_tr01': 'act18side_st01',
    'act18side_tr02': 'act18side_st02',
    'act19side_tr01': 'act19side_02',
    'act1dp_tr01': 'main_01-10',
    'act1football_tr01': 'main_01-10',
    'act1halfidle_tr01': 'main_01-10',
    'act20side_tr01': 'act20side_02',
    'act21side_tr01': 'act21side_01',
    'act22side_tr01': 'act22side_st01',
    'act22side_tr02': 'act22side_04',
    'act23side_tr01': 'main_01-10',
    'act24side_tr01': 'act24side_01',
    'act25side_tr01': 'act25side_02',
    'act25side_tr02': 'act25side_06',
    'act26side_tr01': 'act26side_st01',
    'act26side_tr02': 'act26side_03',
    'act27side_tr01': 'act27side_st01',
    'act27side_tr02': 'act27side_05',
    'act28side_tr01': 'act28side_01',
    'act29side_tr01': 'act29side_01',
    'act29side_tr02': 'act29side_07',
    'act30side_tr01': 'act30side_st01',
    'act31side_tr01': 'act31side_01',
    'act31side_tr02': 'act31side_04',
    'act32side_tr01': 'act32side_st01',
    'act33side_tr01': 'act33side_01',
    'act34side_tr01': 'act34side_01',
    'act35side_tr01': 'act35side_01',
    'act36side_tr01': 'act36side_01',
    'act37side_tr01': 'act37side_01',
    'act38side_tr01': 'act38side_st01',
    'act39side_tr01': 'act39side_st01',
    'act40side_tr01': 'act40side_st01',
    'act41side_tr01': 'act41side_01',
    'act42side_tr01': 'act42side_01',
    'act43side_tr01': 'act43side_st01',
    'act44side_tr01': 'act44side_st01',
    'act44side_tr02': 'act44side_04',
    'act45side_tr01': 'act45side_st01',
    'act46side_tr01': 'act46side_st01',
    'act47side_tr01': 'act47side_01',
    'act48side_tr01': 'act48side_st01',
    'act49side_tr01': 'act49side_01',
    'act50side_tr01': 'act50side_02',
    'act51side_tr01': 'act51side_01',
    'act52side_tr01': 'act52side_01',
    'act53side_tr01': 'act53side_01',
    'act54side_tr01': 'act54side_st01',
    'act55side_tr01': 'act55side_st01',
    # ── 主线（0-14 章 + 15-17 章主线 ss）──
    'tr_01': 'main_00-01',
    'tr_02': 'main_00-02',
    'tr_03': 'main_00-03',
    'tr_04': 'main_00-04',
    'tr_05': 'main_00-06',
    'tr_06': 'main_00-07',
    'tr_07': 'main_00-09',
    'tr_08': 'main_01-05',
    'tr_09': 'main_01-06',
    'tr_10': 'main_01-08',
    'tr_12': 'main_02-03',
    'tr_13': 'sub_02-07',                # 解锁条件是 S2-7
    'tr_14': 'main_02-06',
    'tr_15': 'main_03-03',
    'tr_16': 'main_06-07',
    'tr_17': 'main_07-04',
    'tr_18': 'st_08-01',                 # 解锁条件是 8 章剧情关
    'tr_19': 'easy_09-05',
    'tr_20': 'easy_10-02',
    'tr_21': 'easy_11-05',
    'tr_22': 'st_12-01',                 # 解锁条件是 12 章剧情关
    'tr_23': 'easy_12-06',
    'tr_24': 'easy_13-05',
    'tr_25': 'easy_14-01',
    'tr_26': 'main_15-03',
    'tr_27': 'main_16-01',
    'tr_28': 'main_17-01',
    # 综合作战训练（TR-S-1/2）：解锁前置关是 TR-7 / 2-9，挂着 TR 也能推
    'tr_s_1': 'tr_07',
    'tr_s_2': 'main_02-09',
}

# 主线 S 关（sub_02-01 / S2-1）：关卡表里它们的 code 是 S<章>-<号>、stageId 带
# 章号，看着像「一章的尾巴」，但游戏里是插在主线中间陆续解锁的——S2-1 打完
# 2-1 开、S2-2 打完 2-2 开、S3-1 打完 TR-15 开。位置同样来自官方 stage_table
# 的 unlockCondition，不是猜的：键是 S 关自己，值是它排在谁后面。
# 活动里的 S 关（act13side_s01）不在表里，按活动内「基础 → EX → S」排。
SUB_AFTER = {
    'easy_sub_09-1-1': 'easy_09-11',      # S9-1（9 章简单档）
    'sub_02-01': 'main_02-01',            # S2-1
    'sub_02-02': 'main_02-02',            # S2-2
    'sub_02-03': 'sub_02-02',             # S2-3
    'sub_02-04': 'sub_02-03',             # S2-4
    'sub_02-05': 'main_02-04',            # S2-5
    'sub_02-06': 'sub_02-05',             # S2-6
    'sub_02-07': 'sub_02-06',             # S2-7
    'sub_02-08': 'main_02-07',            # S2-8
    'sub_02-09': 'sub_02-08',             # S2-9
    'sub_02-10': 'main_02-09',            # S2-10
    'sub_02-11': 'sub_02-10',             # S2-11
    'sub_02-12': 'sub_02-11',             # S2-12
    'sub_03-1-1': 'tr_15',                # S3-1
    'sub_03-1-2': 'sub_03-1-1',           # S3-2
    'sub_03-2-1': 'main_03-07',           # S3-3
    'sub_03-2-2': 'sub_03-2-1',           # S3-4
    'sub_03-2-3': 'sub_03-2-2',           # S3-5
    'sub_03-3-1': 'sub_03-2-3',           # S3-6
    'sub_03-3-2': 'sub_03-3-1',           # S3-7
    'sub_04-1-1': 'main_04-03',           # S4-1
    'sub_04-1-2': 'sub_04-1-1',           # S4-2
    'sub_04-1-3': 'sub_04-1-2',           # S4-3
    'sub_04-2-1': 'main_04-06',           # S4-4
    'sub_04-2-2': 'sub_04-2-1',           # S4-5
    'sub_04-2-3': 'sub_04-2-2',           # S4-6
    'sub_04-3-1': 'main_04-09',           # S4-7
    'sub_04-3-2': 'sub_04-3-1',           # S4-8
    'sub_04-3-3': 'sub_04-3-2',           # S4-9
    'sub_04-4-1': 'sub_04-3-3',           # S4-10
    'sub_05-1-1': 'main_05-02',           # S5-1
    'sub_05-1-2': 'sub_05-1-1',           # S5-2
    'sub_05-2-1': 'main_05-06',           # S5-3
    'sub_05-2-2': 'sub_05-2-1',           # S5-4
    'sub_05-3-1': 'main_05-09',           # S5-5
    'sub_05-3-2': 'sub_05-3-1',           # S5-6
    'sub_05-4-1': 'sub_05-3-2',           # S5-7
    'sub_05-4-2': 'sub_05-4-1',           # S5-8
    'sub_05-4-3': 'sub_05-4-2',           # S5-9
    'sub_06-1-1': 'main_06-09',           # S6-1
    'sub_06-1-2': 'sub_06-1-1',           # S6-2
    'sub_06-2-1': 'main_06-13',           # S6-3
    'sub_06-2-2': 'sub_06-2-1',           # S6-4
    'sub_07-1-1': 'main_07-14',           # S7-1
    'sub_07-1-2': 'sub_07-1-1',           # S7-2
    'sub_09-1-1': 'main_09-11',           # S9-1
    'sub_09-1-2': 'easy_sub_09-1-1',      # S9-2
    'sub_09-1-3': 'sub_09-1-2',           # S9-3
    'sub_09-1-4': 'sub_09-1-3',           # S9-4
}


def _is_tr(sid):
    return bool(_TR_ID.search(str(sid or '')))


# ---------------------------------------------------------------- 流程顺序
# 结果列表与「下一关」共用一套顺序：同一章 / 同一活动内按关卡在游戏流程里的
# 先后（基础关 → EX → S，剧情关紧贴它对应的主关），TR 教学关插到 TR_AFTER
# 记的前置关后面。跨章 / 跨活动按章号、活动开始时间排，所以搜 TR- 时主线
# TR-1 → TR-28 是连续的一串，活动 TR 按活动新→旧排。
_MAIN_HEAD = {'main': 0, 'easy': 0, 'tough': 0, 'st': 0, 'sub': 2, 'hard': 3}
_ACT_SEG = {'': 0, 'st': 0, 'm': 0, 'ex': 1, 's': 2, 'sp': 3, 'tm': 3, 'h': 3}
_MAIN_SID = re.compile(r'^(main|easy|tough|st|sub|hard)[_-](\d+)[-_](\d+)(.*)$')
_NUMSEG = re.compile(r'([a-z]*)(\d+)')


def _base_sid(sid):
    """难度变体归并到主关 id，去掉 #f# / #s 后缀

    easy_12-06 / tough_12-06 → main_12-06（同名的三档共享一个流程位置）；
    easy_sub_09-1-1（S9-1 的简单档）→ sub_09-1-1。
    """
    s = str(sid or '').split('#')[0]
    if re.match(r'^(?:easy|tough)_sub_\d', s):
        return s.split('_', 1)[1]
    m = re.match(r'^(?:easy|tough)_(\d+[-_])', s)
    if m:
        return 'main_' + s.split('_', 1)[1]
    return s


def _placement(sid):
    """(流程桶, 桶内位置键)：桶是「一章」或「一个活动」，只有同桶才比位置

    位置键是若干 (段, 号) 二元组，段决定大类：0 基础关 / 1 EX / 2 S / 3 特殊。
    基础关段里再分「剧情关(0) 排在同号主关(1) 前面」，所以 st_08-01 就在
    8-1 前面、act12side_st02 就在 act12side_02 前面——TR 的解锁前置关常常
    正是这种剧情节点，位置要靠它算。

    主线 S 关（sub_02-01 这种）默认排在基础关之后，但它们在 SUB_AFTER 表里
    有解锁前置关，`_flow` 会改按锚点插到真实位置；这里的位置键只是兜底。
    """
    sid = str(sid or '').split('#')[0]
    m = _MAIN_SID.match(sid)
    if m:
        head, ch, num = m.group(1), int(m.group(2)), int(m.group(3))
        seg = _MAIN_HEAD.get(head, 0)
        mark = 0 if head == 'st' else 1
        return ('m%d' % ch, ((seg, num), (mark, 0)))
    head = re.split(r'[_-]', sid)[0]
    toks = [(w, int(n)) for w, n in _NUMSEG.findall(sid[len(head):])]
    if not toks:
        return (head, ())
    w0, n0 = toks[0]
    rest = tuple((0, n) for _, n in toks[1:])
    seg = _ACT_SEG.get(w0, 3)
    mark = 0 if w0 == 'st' else 1
    return (head, ((seg, n0), (mark, 0)) + rest)


def _bucket_rank(bucket):
    """跨桶排序：主线按章号、活动按开始时间新→旧，其它按名字"""
    if bucket[:1] == 'm' and bucket[1:].isdigit():
        return (0, int(bucket[1:]))
    a = activity_table().get(bucket) or {}
    return (1, -int(a.get('start') or 0), bucket)


def _anchored(sid):
    """靠「解锁前置关」定位的关：TR 教学关 + 主线 S 关（SUB_AFTER 登记过的）"""
    if _is_tr(sid):
        return True
    s = str(sid or '').split('#')[0]
    return s in SUB_AFTER or _base_sid(s) in SUB_AFTER


def _anchor_of(sid):
    """这关排在哪一关后面（解锁前置关）；没有登记返回 ''"""
    s = str(sid or '').split('#')[0]
    return (SUB_AFTER.get(s) or SUB_AFTER.get(_base_sid(s))
            or TR_AFTER.get(s) or TR_AFTER.get(_base_sid(s)) or '')


def _flow():
    """{主关 id: (跨桶排序键, 桶内序号, 归属系列)}

    每个桶（一章 / 一个活动）先按 `_placement` 排出骨架（基础关 → EX → S），
    再把 TR 教学关和主线 S 关按「解锁前置关」插到锚点后面：游戏里 S2-1 打完
    2-1 才开、S2-2 打完 2-2 才开、TR-13 打完 S2-7 才开，所以列表顺序就是推图
    顺序（2-1 → S2-1 → 2-2 → S2-2 → S2-3 → S2-4 → 2-3 …）。

    锚点本身也可能是这类关（S2-3 挂在 S2-2 后面），所以按树展开：锚点链一路
    追到骨架关为止，最多 8 层防表写错成环。序号是展开后的整数下标，不用小数
    插值——S2-10 这种长链不会因为「+0.1」累积而越过下一关。

    归属系列给 `next_stage` 用，实际推进按桶（同章 / 同活动）判断。
    """
    with _lock:
        if 'flow' in _cache:
            return _cache['flow']
        raw = pack().get('stages') or []
        zone_of = {st['id']: st.get('zone') or '' for st in raw}
        skel = {}                          # 桶 -> {主关 id: 位置键}
        for st in raw:
            sid = st['id']
            if sid.endswith('#f#') or _anchored(sid):
                continue
            b, key = _placement(sid)
            slot = skel.setdefault(b, {})
            base = _base_sid(sid)
            if base not in slot or key < slot[base]:
                slot[base] = key
        seqs = {b: sorted(slot, key=lambda k: (slot[k], _nat_key(k)))
                for b, slot in skel.items()}
        where = {base: b for b, seq in seqs.items() for base in seq}

        kids = {}                          # (桶, 锚点 id) -> [排它后面的关]
        front = {}                         # 桶 -> 位置未知的关（排最前）
        for st in raw:
            sid = st['id']
            if sid.endswith('#f#') or not _anchored(sid):
                continue
            base = _base_sid(sid)
            anchor = _base_sid(_anchor_of(sid)) if _anchor_of(sid) else ''
            b = where.get(anchor) if anchor else None
            if b is None and anchor:
                # 锚点本身也要定位：顺着链往上找，直到落进某个骨架关
                seen, cur = set(), anchor
                while cur and cur not in seen:
                    seen.add(cur)
                    up = _base_sid(_anchor_of(cur)) if _anchor_of(cur) else ''
                    if up in where:
                        b = where[up]
                        break
                    cur = up
            if b is None:
                # 位置未知：排到所在章 / 活动最前面（IG-TR 这类联机教学关）
                b = _placement(sid)[0]
                m = re.match(r'main_(\d+)$', zone_of.get(sid) or '')
                if m:
                    b = 'm%d' % int(m.group(1))
                front.setdefault(b, []).append(base)
                continue
            kids.setdefault((b, anchor), []).append(base)

        flow = {}
        for b, seq in seqs.items():
            final, seen = [], set()

            def emit(base, depth=0):
                if depth > 12 or base in seen:
                    return
                seen.add(base)
                final.append(base)
                for c in sorted(kids.get((b, base), []), key=_nat_key):
                    emit(c, depth + 1)

            for base in sorted(front.get(b, []), key=_nat_key) + seq:
                emit(base)
            # 锚点跨桶 / 成环的漏网关：兜底追加到本桶末尾
            for (kb, _), cs in kids.items():
                if kb == b:
                    for c in cs:
                        if c not in seen:
                            emit(c)
            rank = _bucket_rank(b)
            for i, base in enumerate(final):
                flow[base] = (rank, float(i), _series_key(base))
        _cache['flow'] = flow
        return flow


def flow_key(sid):
    """结果行排序键：(跨桶排序键, 桶内序号)；表里没有的关卡一律排最后"""
    return _flow().get(_base_sid(sid)) or ((2, 0, ''), 1e9, '')


def next_stage(stage):
    """同章（同活动）、同难度的下一关；章末或未知序列返回 None

    按行的难度视角推进，跟用户推图的习惯一致：磨难行下一关还是磨难
    （tough_12-06 → tough_12-07）、险地行下一关还是险地（main_15-02#s →
    main_15-03#s）、活动关按活动内顺序（TL-2 → TL-3）。

    主线按 `_flow` 的推图顺序走，所以 TR 教学关和 S 支线关都在序列里：
    0-1 的下一关是 TR-1、TR-1 的下一关是 0-2、2-1 的下一关是 S2-1、
    S2-1 的下一关是 2-2。
    """
    sid = stage.get('stageId') or ''
    diff = stage.get('difficulty') or 'normal'
    flow = _flow()
    cur = flow.get(_base_sid(sid))
    if not cur:
        return None
    rank = cur[0]
    rows, seen = [], set()
    for s in _stages():
        if (s.get('difficulty') or 'normal') != diff:
            continue
        k = flow.get(_base_sid(s['stageId']))
        if not k or k[0] != rank:
            continue
        # 同一关的普通行 / 突袭行（variant）只留一条，别把突袭行当「下一关」
        if s['stageId'] in seen:
            continue
        seen.add(s['stageId'])
        rows.append((k[1], _nat_key(s['stageId']), s))
    rows.sort(key=lambda x: (x[0], x[1]))
    for i, (_, _, s) in enumerate(rows):
        if s['stageId'] != sid:
            continue
        if i + 1 >= len(rows):
            return None
        nxt = rows[i + 1][2]
        return {'code': nxt['code'], 'stageId': nxt['stageId'],
                'difficulty': nxt.get('difficulty') or 'normal',
                'difficultyCN': nxt.get('difficultyCN') or ''}
    return None


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
        # 首字母表派生自干员名：不一起清的话热更新新增的干员拼音搜索
        # 失效到进程重启为止（#50）
        _char_initials_cache[0] = None
