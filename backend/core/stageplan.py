# -*- coding: utf-8 -*-
"""打关预检：这号到底能打哪几关，剩下的为什么打不了

2026-11-18 用户实测：助手直接把 BB-S-1~4 排进队列，账号练度不够，MAA 跑
8 分钟 0/1 结束，用户只看到「失败了」——不知道其实第一关就打不过，后面
三关连解锁都谈不上（S 关卡是顺序解锁的，前一关没通，后一关在地图上是灰的）。
他要的是：**开打前说清楚哪几关打不了、能打的自动打掉**。

这个模块不碰设备，只做三件事：

1. **排序**：同活动按游戏内流程排（基础关 → EX → S → MO，同分区按序号）。
   顺序直接取 ``resources.flow_key``，那是按游戏解锁顺序排好的，不另造表。
2. **补前置**：要打 ``BB-S-3`` 就先把 ``BB-S-1``、``BB-S-2`` 拉进计划——
   不然点了也是灰的。
3. **判练度**：拿作业站的作业要求（``requirements``）比对账号干员
   （``copilot.req_met``），分四档：

   ``ready``   直接打
   ``borrow``  差一人、可借助战顶上，也打
   ``train``   干员有但练度不够 → 打不了
   ``blocked`` 缺干员且没得借 / 整个关没作业 → 打不了

   打不了的会**连锁**：同活动排在它后面的关一律标「前置未通」。前一关都
   过不去，后面的排进队列只是白等（用户实测就是这么烧掉 8 分钟的）。
"""

import os
import re
import threading
import time

from . import resources

# 代号后缀 → 分区类型。EX 与数字之间可能没有横杠（火蓝之心 OF-EX1）。
# HX 是 EX 分区里的另一批关（GT-EX / GT-HX 共用 ``act13d2_zone2``），归一成 ex。
# F 是火蓝之心第二区那批（OF-F1~4），自己一个分区、不并进 EX 的链。
_KIND_RE = re.compile(r'-(EX|HX|MO|ST|TR|F|S)-?(\d+)$')

# 作业站一次查询的缓存：同一 (账号, 关卡) 十分钟内不重复问。
# 预检和真正下载作业各查一次的话，4 关就是 8 次网络请求，纯浪费。
_CACHE = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 600

_INDEX = {'at': 0, 'map': {}}
_INDEX_LOCK = threading.Lock()


# ------------------------------------------------------------------ 基础查询

def kind_of(code):
    """```BB-S-3`` → ``s``；``MN-EX-1`` / ``OF-EX1`` / ``GT-HX-1`` → ``ex``；普通关 → ``base``"""
    m = _KIND_RE.search((code or '').upper().strip())
    if not m:
        return 'base'
    k = m.group(1).lower()
    return 'ex' if k == 'hx' else k


def stage_index():
    """``{代号: 资源行}``

    只留普通难度：``easy`` / ``tough`` 是主线 10~14 章的另一种难度视角，
    跟活动的 EX / S 不是一回事，混进来会把顺序算乱。
    """
    with _INDEX_LOCK:
        if _INDEX['map'] and time.time() - _INDEX['at'] < _CACHE_TTL:
            return _INDEX['map']
        idx = {}
        for r in resources._stages(True):
            code = (r.get('code') or '').strip().upper()
            if not code or (r.get('difficulty') or 'normal') != 'normal':
                continue
            idx.setdefault(code, r)
        _INDEX['map'] = idx
        _INDEX['at'] = time.time()
        return idx


def row_of(code):
    return stage_index().get((code or '').strip().upper())


def is_activity(row):
    """活动关。主线不展开前置——一章十几关，用户没这预期

    判据用 ``zoneType`` 而不是 stageId 前缀：老活动（火蓝之心）的 stageId
    是 ``a003_01`` 这种，不带 act 前缀，按前缀判会把 OF 的 EX 当成主线不展开。
    """
    row = row or {}
    if (row.get('zoneType') or '').upper() == 'ACTIVITY':
        return True
    return str(row.get('stageId') or '').startswith('act')


def _flow(row):
    return resources.flow_key(row.get('stageId') or '')


def chain(code):
    """同活动、同类型、排在这一关之前（含自己）的关卡，按游戏内顺序

    活动关才展开：``BB-S-3`` → ``['BB-S-1','BB-S-2','BB-S-3']``。
    类型不同不混（EX 的链不会把基础关拉进来），因为 EX / S 各自是独立分区，
    进分区之后按序号解锁。
    """
    code = (code or '').strip().upper()
    row = row_of(code)
    if not row or not is_activity(row):
        return [code]
    kind = kind_of(code)
    bucket, target = _flow(row)[0], _flow(row)[1]
    out = []
    for c, r in stage_index().items():
        if kind_of(c) != kind:
            continue
        if not is_activity(r):
            continue
        k = _flow(r)
        if k[0] != bucket or k[1] > target:
            continue
        out.append((k[1], c))
    out.sort()
    codes = [c for _, c in out]
    return codes or [code]


def expand(stages, with_prereq=True, fill_base=False):
    """把要打的关展开成有序计划，标出哪些是「补前置」而不是用户点名的

    ``fill_base=False``（默认）时，**基础关不自动补前置**：用户说「刷 BB-7
    的材料」时，把 BB-1~BB-6 一起排进去就是白烧理智。EX / S / MO 的分区
    只有 1~8 关，补上代价小，而且这几关才是「没通就锁着」的高频场景
    （用户实测：S1、S2 没打，直接挂 S3 必失败）。基础关要补就把
    ``fill_base=True`` 传进来。
    """
    want, seen = [], set()
    for s in stages:
        c = (s or '').strip().upper()
        if not c or c in seen:
            continue
        seen.add(c)
        want.append(c)
    requested = set(want)
    plan, seen = [], set()
    for c in want:
        k = kind_of(c)
        fill = with_prereq and (fill_base or k in ('ex', 's', 'mo', 'st'))
        for x in (chain(c) if fill else [c]):
            if x in seen:
                continue
            seen.add(x)
            row = row_of(x) or {}
            plan.append({'code': x, 'prereq': x not in requested,
                         'kind': kind_of(x), 'story': bool(row.get('story'))})
    # 按游戏内流程排：跨活动的按活动排（不按用户传的顺序，用户传 EX 再传基础
    # 关时，基础关必须在前）
    def key(it):
        row = row_of(it['code'])
        if not row:
            return ((9, 9, it['code']), 9999, it['code'])
        k = _flow(row)
        return (k[0], k[1], it['code'])
    plan.sort(key=key)
    return plan


# ------------------------------------------------------------------ 练度判定

def _req_text(req):
    """作业要求 →「精二60 / 专三 / 模组2」这种短文本"""
    parts = []
    if req.get('elite') is not None or req.get('level') is not None:
        parts.append('精%s %s 级' % (req.get('elite'), req.get('level') or '?'))
    sl = req.get('skill_level', req.get('skillLevel'))
    if sl:
        sl = int(sl)
        if sl >= 8:
            parts.append('专%s' % (sl - 7))
        elif sl > 1:
            parts.append('技能%s' % sl)
    mod = req.get('module')
    if mod and int(mod) > 0:
        parts.append('模组%s' % int(mod))
    return ' / '.join(parts) or '无特殊要求'


def _own_text(d):
    parts = ['精%s %s 级' % (d.get('elite'), d.get('level') or '?')]
    mv = max([int(x or 0) for x in (d.get('mastery') or [])] or [0])
    if mv:
        parts.append('专%s' % mv)
    return ' / '.join(parts)


def _op_verdict(op, total):
    """单份作业对当前账号可行吗 → (state, reason, notes)"""
    mode = op.get('mode') or 'blocked'
    missing = op.get('missing') or []
    training = op.get('training') or []
    if mode == 'ready':
        return 'ready', '', []
    if mode == 'borrow':
        who = '、'.join(missing[:2]) if missing else '一名助战'
        return 'borrow', '', ['缺 %s，开打时借助战顶上' % who]
    if mode == 'train':
        notes = []
        for d in (op.get('opers') or []):
            if d.get('group') or d.get('ok') is not False or not d.get('owned'):
                continue
            notes.append('%s 需 %s，你有 %s'
                         % (d.get('name'), _req_text(d.get('req') or {}),
                            _own_text(d)))
            if len(notes) >= 4:
                break
        return 'train', '练度不足', notes or ['有干员练度不够']
    if mode == 'blocked':
        who = '、'.join([x for x in missing if x][:4]) or '多个干员'
        return 'blocked', '缺干员且借不到', ['缺 %s（作业要 %d 人）' % (who, total)]
    return 'blocked', '作业不可用', []


def _lookup(code, snap, acct='', use_cache=True):
    """查这关的作业并判可行性；网络失败/没作业都当 blocked，别炸整次预检"""
    from . import copilot
    # 缓存键用账号文件名而不是 id(snap)：review 每次都新建 dict，用 id 永远
    # 打不中缓存，同一关会被连查两次（预检一次、下载作业再一次）。
    key = (code.upper(), acct or '')
    if use_cache:
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
            if hit and time.time() - hit[0] < _CACHE_TTL:
                return hit[1]
    out = {'state': 'blocked', 'reason': '没搜到作业',
           'notes': ['这关没有可抄的作业；能纯刷就用 mode=fight'],
           'op': None, 'rating': 0, 'title': ''}
    try:
        res = copilot.search(code, snap or {}, pages=1, limit=6)
    except Exception as e:                      # noqa: BLE001 没网也要能给结论
        # **查不到 ≠ 打不了**：作业站抽风时把整批关都判 blocked，用户就白等了。
        # 标 unknown 照排，让 MAA 自己去试，日志留一句原因。
        out.update(state='unknown',
                   reason='查作业失败（%s），先按能打处理' % type(e).__name__,
                   notes=[])
        res = None
    if res:
        if res.get('mode') == 'story':
            out.update(state='story', reason='剧情关（用 skill_skip_story）')
        else:
            rows = [p for p in (res.get('stages') or [])
                    if ((p.get('stage') or {}).get('code') or '').upper()
                    == code.upper()]
            if not rows:
                rows = res.get('stages') or []
            best, best_op = None, None
            for p in rows:
                op = p.get('best') or (p.get('ops') or [None])[0]
                if not op:
                    continue
                if best is None or _band(op) < _band(best_op):
                    best, best_op = p, op
            if best_op:
                payload = {'copilotId': best_op.get('copilotId'),
                           'stageCode': code.upper(),
                           'variant': (best.get('stage') or {}).get('variant')}
                if snap:
                    state, reason, notes = _op_verdict(best_op,
                                                       best_op.get('total') or 0)
                else:
                    # 没账号快照就没法判练度。**不能当成「缺干员」**——那会把
                    # 所有关都判成打不了，用户明明有号却连开打都被拦住。
                    state, reason, notes = 'unknown', '没账号快照，练度未校验', []
                out.update(state=state, reason=reason, notes=notes,
                           rating=best_op.get('rating') or 0,
                           title=best_op.get('title') or '', op=payload)
    if use_cache:
        with _CACHE_LOCK:
            _CACHE[key] = (time.time(), out)
    return out


def _band(op):
    """同一关有多份作业时挑最省事的：能直接抄 > 借一人 > 需练度 > 打不了"""
    order = {'ready': 0, 'borrow': 1, 'train': 2, 'blocked': 3}
    return (order.get((op or {}).get('mode'), 9), -((op or {}).get('rating') or 0))


# ------------------------------------------------------------------ 对外接口

def review(stages, account_file='', mode='copy', with_prereq=True, fill_base=False,
           log=None, use_cache=True, max_check=12):
    """开打前预检。

    :param stages: 用户要打的关卡（代号）
    :param account_file: 账号快照文件名；空则只判作业有没有，不判练度
    :param mode: ``copy`` 才有作业可查；``fight`` 只能给顺序、判不了练度
    :param with_prereq: 是否把前置关补进计划（默认补）
    :param fill_base: 基础关也补前置。**默认 False**——用户说「刷 BB-7」
        时把 BB-1~BB-6 一起排进去就是白烧理智（见 :func:`expand`）
    :param max_check: 最多查几关的作业/练度。一关一次作业站请求，二十关就是
        半分钟；超出的按 `unknown` 处理（照排，让 MAA 自己试）
    :return: dict，见下

    返回结构::

        {'plan': [{'code','prereq','state','reason','notes','op'}...],
         'playable': [代号...],           # 进队列的（ready / borrow / unknown）
         'blocked':  [{'code','reason','notes'}...],
         'ops': {代号: {'copilotId','stageCode','variant'}},
         'summary': '一句话结论', 'notes': [给用户的提示...]}
    """
    log = log or (lambda *_: None)
    snap = {}
    if account_file:
        try:
            from . import accounts
            snap = accounts.get_account(account_file) or {}
        except Exception:                       # noqa: BLE001 快照读不到就不判练度
            snap = {}
    plan = expand(stages, with_prereq=with_prereq, fill_base=fill_base)
    requested = [(s or '').strip().upper() for s in stages if s]
    blocked_bucket = {}
    playable, blocked, ops, notes = [], [], {}, []

    checked = 0
    for item in plan:
        code = item['code']
        row = row_of(code) or {}
        if not item['story'] and mode != 'fight':
            checked += 1
        if item['story']:
            item.update(state='story', reason='剧情关（用 skill_skip_story）', notes=[])
        elif mode == 'fight':
            item.update(state='unknown', reason='纯刷关模式，练度查不了', notes=[])
        elif checked > max_check:
            item.update(state='unknown',
                        reason='预检查询超过 %d 关，剩下的按顺序试' % max_check,
                        notes=[])
        else:
            v = _lookup(code, snap, acct=account_file, use_cache=use_cache)
            item.update(state=v['state'], reason=v['reason'], notes=v['notes'])
            if v.get('op') and v['state'] in ('ready', 'borrow'):
                ops[code] = v['op']
            item['rating'] = v.get('rating')
            item['title'] = v.get('title')

        # 查不到的代号各自独立成一个桶：共用 ('?',) 会让「第一个查不到」
        # 把后面所有查不到的关都标成「前置未通」
        bucket = _flow(row)[0] if row else ('?', code)
        # 前置连锁：同活动前面的关打不了，后面的关在游戏里是灰的，
        # 排进队列只会白等（用户实测：S1 打不过，S2~S4 根本没解锁）
        if bucket in blocked_bucket and item['state'] in ('ready', 'borrow', 'unknown'):
            first = blocked_bucket[bucket]
            item.update(state='locked', reason='%s 打不了' % first, notes=[])
        # 剧情关不挡后面的战斗关（BB-ST-1 没看过，BB-S-1 照样能打）
        if item['state'] in ('train', 'blocked'):
            blocked_bucket.setdefault(bucket, code)

        if item['state'] in ('ready', 'borrow', 'unknown'):
            playable.append(code)
        elif item['state'] == 'story':
            # 剧情关不进战斗队列，但也不算「打不了」——周目里由 skip_story 处理
            notes.append('%s 是剧情关，交给 skill_skip_story' % code)
        else:
            blocked.append({'code': code, 'reason': item['reason'],
                            'notes': item['notes']})

    # 跨分区的提醒：S / MO 在游戏里要先通关全部 EX，EX 要先通关基础关
    kinds = {(i['kind']) for i in plan if i['state'] != 'story'}
    if 's' in kinds or 'mo' in kinds:
        target_s = next((i for i in plan if i['kind'] in ('s', 'mo')), None)
        if target_s and target_s['state'] in ('ready', 'borrow', 'unknown'):
            notes.append('S / MO 分区要先通关该活动的全部 EX 关；'
                         '没通的话到那一步会报「找不到关卡」')
    # 没补进计划的前置关：基础关默认不补（刷材料时补上就是白烧理智），
    # 但要明确告诉用户「这些关没打过的话，目标关是锁着的」。
    in_plan = {i['code'] for i in plan}
    lack_note = []
    for code in requested:
        if code in in_plan and len(chain(code)) == 1:
            continue
        lack = [c for c in chain(code)[:-1] if c not in in_plan]
        if lack:
            lack_note.append('%s 前面还有 %s' % (
                code, '、'.join(lack[:8]) + ('…' if len(lack) > 8 else '')))
    if lack_note:
        notes.append('没补前置：' + '；'.join(lack_note[:3]) +
                     '。没打过的话目标关在游戏里是锁着的'
                     '（要一起补就传 autoPrereq=true）')
    if not snap and mode == 'copy':
        notes.append('没读到账号快照，只判了「有没有作业」，没判练度')

    rep = {'plan': plan, 'playable': playable, 'blocked': blocked, 'ops': ops,
           'notes': notes, 'account': account_file, 'mode': mode,
           'requested': requested}
    rep['summary'] = summary(rep)
    return rep


def summary(rep):
    """一句话结论（工具结果里给模型看的那行）"""
    plan = rep.get('plan') or []
    blocked = rep.get('blocked') or []
    if not plan:
        return '没有可打的关卡'
    if not blocked:
        return '能打 %d 关（%s）' % (len(rep['playable']),
                                    '、'.join(rep['playable'][:8]))
    head = '能打 %d 关（%s），打不了 %d 关（%s）' % (
        len(rep['playable']), '、'.join(rep['playable'][:6]) or '无',
        len(blocked),
        '；'.join('%s %s' % (b['code'], b['reason']) for b in blocked[:4]))
    return head


def details(rep, limit=8):
    """逐关明细（给确认卡 / 日志用），一行一关"""
    out = []
    for item in (rep.get('plan') or [])[:limit]:
        tag = {'ready': '可打', 'borrow': '可打（借 1 人）',
               'train': '练度不足', 'blocked': '打不了',
               'locked': '前置未通', 'story': '剧情关',
               'unknown': '按顺序试'}.get(item['state'], item['state'])
        notes = item.get('notes') or []
        # 一关可能差四五个干员，全列出来确认卡就没法看了：留两条 + 计数
        extra = '；'.join(notes[:2])
        if len(notes) > 2:
            extra += ' 等 %d 项' % len(notes)
        line = '%s %s' % (item['code'], tag)
        if extra:
            line += '（%s）' % extra
        elif item.get('reason') and item['state'] not in ('ready', 'borrow'):
            line += '：%s' % item['reason']
        out.append(line)
    if len(rep.get('plan') or []) > limit:
        out.append('…共 %d 关' % len(rep['plan']))
    return out

def explain_failure(why):
    """MAA 的失败原因 → 用户能懂的一句话

    MaaCore 的 chain 失败信息是英文/半中半英的（``CopilotError``、
    ``Unsupported stage``…），直接甩给用户等于没说。这里只按**明确的**
    关键词归类，认不出来就原样返回，别瞎猜（猜错比不说更糟）。
    """
    w = str(why or '').strip()
    low = w.lower()
    if any(k in w for k in ('未解锁', '没解锁', '未开放', '没有开放')) or \
            'unlock' in low:
        return '关卡还没解锁（前面的关没通）'
    if 'unsupported stage' in low or '未知关卡' in w or '不是有效' in w:
        return 'MAA 不认这个关卡（作业与关卡对不上）'
    if '理智' in w or 'sanity' in low:
        return '理智不够'
    if any(k in w for k in ('三星', '作战失败', '战斗失败', '行动失败')):
        return '这关打不过（练度 / 阵容不够）'
    if any(k in w for k in ('找不到', '未找到', '识别失败')) or 'not found' in low:
        return '找不到关卡（分区 / 导航不对）'
    return w


def log_report(rep, log):
    """把预检结果写进任务日志（用户在看进度那一栏能看到）"""
    if not rep:
        return
    log('—— 开打前检查 ——')
    for line in details(rep):
        log('  ' + line)
    for n in rep.get('notes') or []:
        log('  提示：' + n)
    log(rep.get('summary') or '')
