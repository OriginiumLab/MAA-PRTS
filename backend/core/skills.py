# -*- coding: utf-8 -*-
"""挂关流程的 skill 化：把「唤醒 / 导航 / 过剧情 / 自动战斗 / 签到」拆成
可单独调用、也可按周目编排串起来的最小单元。

为什么拆（2026-11-17 用户需求）：
    MN 这种周目流程 = 唤醒 → 导航 → 过 mn-st-1 → 打 mn-1~7 → 过 mn-st-2
    → 打 mn-8 → 导航 EX → 打 mn-ex-1~8。以前这些能力全耦合在
    maaplan.run_core 的一次任务队列里，MAA 一被剧情关卡卡住整条链就停；
    拆开后每步独立执行、失败当场停 + 落日志，上层（助手 / 前端）能
    精确知道卡在哪一步并提醒用户。

每个 skill 都是一个 ``run(params, log) -> dict``，统一由 :func:`run_skill`
调度到后台任务（tasks.py），返回 taskId 给前端 / 助手轮询进度。

  skill        对应能力                                    依托
  ----------  ------------------------------------------  ----------------
  wakeup      唤醒游戏到主界面（按尾号切号）               maaplan StartUp
  navigate    从主界面导航到任意活动（含 EX）              stagenav.goto_stage_list
  fight       自动战斗：编队 / 借助战 / 吃理智药 / 开打    maaplan.run_core
  skip_story  过剧情关（mn-st-1 这类 ST 关）               stagenav + 跳幕
  signin      森空岛签到（单号 / 全部）                    signin.run
  weekly      周目编排：把上面几个按关卡顺序串起来         本文件 _plan_cycle
"""
from __future__ import annotations

import re
import threading
import time

from . import accounts
from . import maaplan
from . import resources
from . import settings
from . import stagenav
from . import tasks


# ---------------------------------------------------------------- 工具

def _default_maa_dir():
    d = settings.load().get('maaDir') or ''
    if not d:
        raise ValueError('还没设置 MAA 目录（设置里先绑定一次 MAA）')
    return d


def _account_file(name):
    """账号名（用户/助手给 file 或昵称）→ 账号文件名；空串返回默认账号"""
    if name:
        rows = accounts.list_accounts()
        for r in rows:
            if r['file'] == name:
                return name
        low = str(name).lower()
        for r in rows:
            nick = (r.get('nickName') or '').lower()
            if low and low in nick:
                return r['file']
        raise ValueError('找不到账号：%s' % name)
    rows = [c for c in accounts.list_accounts()
            if (c.get('game') or 'arknights') == 'arknights']
    if not rows:
        raise ValueError('左侧还没有账号')
    return rows[0]['file']


def _account_name_tail(file):
    """账号的切号名（官服=手机号（含尾号），B 服=游戏内用户名），没有给空串"""
    try:
        snap = accounts.get_account(file)
    except Exception:
        return ''
    return (snap.get('accountName') or snap.get('nickName') or '').strip()


def _client_of(file):
    try:
        snap = accounts.get_account(file)
        return (snap.get('clientType') or snap.get('channelName') or '').strip()
    except Exception:
        return ''


# 关卡代号形态：CW-1 / MN-EX-1 / 15-3 / TR-1。**这是唯一能被导航直接消化的
# 形态**，其余写法都要先换（见 norm_stage）。
_STAGE_CODE_RE = re.compile(r'^(?:\d{1,2}|[A-Za-z][A-Za-z0-9]*)(?:-[A-Za-z]+)?-\d+')


def norm_stage(raw, log=None):
    """把各种写法统一成**真实关卡代号**，返回 ``(code, 说明)``

    模型经常把「活动 id」当关卡号传下来（2026-11-17 用户实测：导航愚人号时
    传 act1d3d3，goto_stage_list 认不出就掉进「当期活动」分支去点活动大图，
    必然报「没进到活动地图」）。所以入口统一过一遍：

      中文活动名   孤星 / 愚人号        → CW-1 / SV-1
      活动 id      act25side           → CW-1
      代号前缀     CW / MN              → CW-1 / MN-1
      关卡 id      act25side_01        → CW-1
      关卡代号     CW-1 / MN-EX-1      → 原样

    解析不到返回 ``('', 原因)``——调用方要如实报错，别拿原始串去乱点。
    """
    s = str(raw or '').strip()
    if not s:
        return '', '没给关卡或活动名'
    log = log or (lambda *_: None)

    # 已经是标准代号：直接放行（省一次全表扫描，也避免把 MN-EX-1 展开成别的）
    if _STAGE_CODE_RE.match(s) and re.match(r'^[A-Za-z]', s):
        return s.upper(), ''
    if re.match(r'^\d{1,2}-\d+$', s):
        return s, ''

    try:
        res = resources.resolve(s)
    except Exception:
        res = {}
    rows = res.get('stages') or []
    if rows:
        code = rows[0]['code']
        title = (res.get('title') or '').strip()
        if code.upper() != s.upper():
            log('「%s」→ %s%s' % (s, code, ('（%s）' % title) if title else ''))
        return code, ''

    # 活动 id / zone id 前缀兜底（act25side、act11sre_zone1 这类）
    low = s.lower()
    for st in resources._stages(True):
        if (st['stageId'].lower().startswith(low)
                or st['zoneId'].lower().startswith(low)):
            code = st['code']
            log('「%s」→ %s' % (s, code))
            return code, ''

    names = []
    try:
        for r in resources.activity_list(40)[:12]:
            if r.get('name'):
                names.append('%s(%s)' % (r['name'], r.get('prefix') or '?'))
    except Exception:
        pass
    return '', ('认不出「%s」是哪个活动/关卡' % s
                + ('；近期活动有：' + '、'.join(names) if names else ''))


# ---------------------------------------------------------------- wakeup

def skill_wakeup(params, log, tid=None):
    """skill 1：唤醒游戏到主界面

    accountName / tail 有值时走 MAA StartUp（能按切号名换账号，官服填手机号
    后 4 位如 5505）；没给就用 adb 直连重启（快，且登录页/START 页/弹窗都能
    自己处理——stagenav.restart_app）。
    """
    file = _account_file(params.get('account') or '')
    tail = (params.get('accountName') or params.get('tail') or '').strip()
    if tail:
        spec = {'tasks': ['startup'], 'accountName': tail,
                'maaDir': _default_maa_dir()}
        log('按切号名唤醒：%s' % tail)
        maaplan.run_core(spec, tid=tid, timeout=360)
        return {'ok': True, 'how': 'maa-startup', 'account': file}

    d = _default_maa_dir()
    conn = maaplan._connection(d, None)
    client = _client_of(file)
    t = stagenav.Touch(conn['adb_path'], conn['address'], log,
                       package=stagenav.game_package(client))
    alive, detail = t.ready()
    log('adb：%s' % detail)
    if not alive:
        raise RuntimeError(detail)
    if stagenav.at_home(t) and not stagenav.on_login_page(t) \
            and not stagenav.in_story_page(t) and not stagenav.in_story_dialog(t):
        log('已经在主界面，不用唤醒')
        return {'ok': True, 'how': 'already-home', 'account': file}
    if not t.restart_app(log):
        raise RuntimeError('唤醒失败：重启后没回到主界面（看日志排查）')
    return {'ok': True, 'how': 'adb-restart', 'account': file}


# ---------------------------------------------------------------- navigate

def skill_navigate(params, log, tid=None):
    """skill 2/3：从主界面导航到活动（老活动走曲谱，当期走区域列表）

    stage 支持任意写法：关卡号（MN-1 / MN-EX-1）、中文活动名（孤星 / 愚人号）、
    活动 id（act25side）、代号前缀（CW）。**一律先 norm_stage 换成真实关卡代号
    再导航**，否则活动 id 会被当成当期活动去点大图（用户 2026-11-17 实测）。
    """
    raw = (params.get('stage') or params.get('activity')
           or params.get('keyword') or '').strip()
    if not raw:
        raise ValueError('缺 stage（要导航到哪个活动/关卡，中文名也行）')
    stage, why = norm_stage(raw, log)
    if not stage:
        raise ValueError(why)
    file = _account_file(params.get('account') or '')
    d = _default_maa_dir()
    conn = maaplan._connection(d, None)
    client = _client_of(file)
    t = stagenav.Touch(conn['adb_path'], conn['address'], log,
                       package=stagenav.game_package(client))
    alive, detail = t.ready()
    log('adb：%s' % detail)
    if not alive:
        raise RuntimeError(detail)
    # 游戏没开的时候别硬导航：getop 判据全 False，恢复循环会白转一分钟再报
    # 「回不到主界面」，看着像导航坏了。直接说清楚下一步该做什么。
    if not t.game_alive():
        raise RuntimeError('游戏没在跑（模拟器连着但客户端没开）——先调 skill_wakeup '
                           '唤醒，再导航')
    ok, why = stagenav.goto_stage_list(t, stage, log)
    if not ok:
        raise RuntimeError('导航失败：%s' % why)
    return {'ok': True, 'stage': stage, 'input': raw, 'why': why}


# ---------------------------------------------------------------- fight

def _prepare_ops(stages, file, log):
    """mode=copy：把关卡代号列表变成 MaaCore 的 copilotList

    每个 stage 按代号搜作业站，取第一个作业下载落盘（prepare_copilot）。
    没有 copilotList 的 spec 里 Copilot 任务会被 core_tasks 直接 continue——
    队列就只剩强制补的 StartUp，noNav 一摘就「没有可执行的任务」
    （2026-11-17 用户实测：直跑模式报 ValueError）。
    """
    from . import copilot
    snap = None
    try:
        snap = accounts.get_account(file)
    except Exception:
        pass
    ops = []
    for st in stages:
        res = copilot.search(st, snap or {}, pages=1, limit=3)
        packed = None
        for p in (res.get('stages') or []):
            if ((p.get('stage') or {}).get('code') or '').upper() == st.upper():
                packed = p
                break
        if packed is None:
            packed = (res.get('stages') or [None])[0]
        if not packed or not packed.get('ops'):
            raise ValueError('%s 没搜到能用的作业（换 mode=fight 纯刷关，'
                             '或自己去作业站挑一份）' % st)
        op = packed['ops'][0]
        log('%s：用作业 #%s《%s》' % (st, op.get('copilotId'),
                                     (op.get('title') or '')[:36]))
        ops.append({'copilotId': op['copilotId'], 'stageCode': st,
                    'variant': (packed.get('stage') or {}).get('variant')})
    return maaplan.prepare_copilot(ops, _default_maa_dir(), log=log)


def skill_fight(params, log, tid=None):
    """skill 5：自动战斗——编队 / 借助战 / 吃理智药 / 开打

    stages 是关卡列表（一次 MAA 任务全挂上）；mode：
      copy   按作业站作业逐关抄（缺作业的关报错）
      fight  纯刷关（MAA 自己的编队/理智，不抄作业）
    编队（formation）/ 借助战（support）/ 吃理智药（sanityPotion）默认全开。

    **默认不唤醒、不导航**（noNav 直跑）：调用前界面在哪，Copilot 就从哪
    接手（当期活动 MAA 自己认路；老活动请先用 navigate skill 或手动到位）。
    用户明确要「连唤醒/导航一起」时传 wakeup=true 才走全套。
    """
    stages = [s for s in (params.get('stages') or []) if s]
    if not stages:
        raise ValueError('缺 stages（要打哪些关）')
    file = _account_file(params.get('account') or '')
    mode = (params.get('mode') or 'copy').strip()
    if mode not in ('copy', 'fight'):
        raise ValueError('mode 只能是 copy / fight')
    tail = (params.get('accountName') or params.get('tail')
            or _account_name_tail(file) or '').strip()
    cop = params.get('copilot') or {}
    # wakeup/navigate 显式给了才走全套；默认直跑（2026-11-17 用户要求）
    with_nav = bool(params.get('wakeup') or params.get('navigate'))
    spec = {
        'tasks': ['startup', 'copilot' if mode == 'copy' else 'fight'],
        'stages': stages,
        'accountName': tail,
        'maaDir': _default_maa_dir(),
        'noNav': not with_nav,
        'copilot': {
            'formation': cop.get('formation', True),        # 自动编队
            'support': cop.get('support', True),            # 借助战（缺人时借）
            'sanityPotion': cop.get('sanityPotion', True),  # 理智不够吃药
        },
        'profile': params.get('profile') or '',
    }
    # 抄作业必须有作业文件：要么调用方直接给 ops / copilotList，要么按
    # stages 自动搜。不给的话队列里 Copilot 会被 core_tasks 跳过（见
    # _prepare_ops 注释），noNav 模式下直接空队列报错。
    if mode == 'copy':
        if params.get('copilotList'):
            spec['copilotList'] = params['copilotList']
        elif params.get('ops'):
            spec['copilotList'] = maaplan.prepare_copilot(
                params['ops'], _default_maa_dir(), log=log)
        else:
            log('搜作业…')
            spec['copilotList'] = _prepare_ops(stages, file, log)
    log('自动战斗（%s）：%s' % (mode, ' '.join(stages)))
    if with_nav:
        log('带唤醒+导航的全套流程')
    else:
        log('直跑模式：不唤醒不导航，从当前界面直接接手'
            '（要全套流程就说「带唤醒」）')
    if tail:
        log('切号名：%s' % tail)
    maaplan.run_core(spec, tid=tid, timeout=params.get('timeout') or None)
    return {'ok': True, 'mode': mode, 'stages': stages, 'account': file,
            'noNav': not with_nav}


# ---------------------------------------------------------------- skip_story

def skill_skip_story(params, log, tid=None):
    """skill 6：过剧情关卡（mn-st-1 这类 ST 关）

    ST 关没战斗、作业站没作业，MAA 抄作业流程到这里必卡。做法：
      1) 先自己导航到该活动关卡列表（复用 goto_stage_list）；
      2) 让 MAA 的关卡导航/OCR 把 ST 关点开（走一次 fight 单关任务，
         MAA 找不到战斗界面时我们接管）；
      3) 检测到剧情播放页就连续跳幕（stagenav._skip_story_replay），
         跳完 = 该关完成，停掉 MAA。
    """
    stage = (params.get('stage') or '').strip()
    if not stage:
        raise ValueError('缺 stage（要过哪个剧情关）')
    file = _account_file(params.get('account') or '')
    d = _default_maa_dir()
    conn = maaplan._connection(d, None)
    client = _client_of(file)
    tail = (params.get('accountName') or params.get('tail')
            or _account_name_tail(file) or '').strip()

    # 第 2) 步用 MAA 的 fight 单关当「点关探针」：StageNavigation 点进 ST 关
    # 后剧情自动播，MAA 等不到战斗界面会失败——无所谓，剧情已经在播了，
    # 我们的跳幕线程接管（失败驱动的关键：别等 MAA 报错才开始跳）
    probe_done = threading.Event()

    def probe():
        spec = {'tasks': ['startup', 'fight'], 'stages': [stage],
                'accountName': tail, 'maaDir': d}
        try:
            maaplan.run_core(spec, tid=tid, timeout=900)
        except Exception as e:
            log('MAA 点关结束（%s），跳幕线程接管' % e)
        probe_done.set()

    th = threading.Thread(target=probe, daemon=True)
    th.start()

    # 第 3) 步：等剧情页出现（最多 8 分钟），然后连续跳幕
    t = stagenav.Touch(conn['adb_path'], conn['address'], log,
                       package=stagenav.game_package(client))
    alive, detail = t.ready()
    log('adb：%s' % detail)
    if not alive:
        raise RuntimeError(detail)
    t0 = time.time()
    jumped = False
    while time.time() - t0 < 480:
        time.sleep(3)
        if stagenav.in_story_dialog(t):
            log('检测到剧情播放，连续跳幕…')
            r = stagenav._skip_story_replay(t, log)
            jumped = r != 'stuck'
            if r == 'stuck':
                log('剧情循环重播，跳幕失败——重启回主界面并报失败')
                t.restart_app(log)
            break
        if probe_done.is_set():
            # MAA 那边已经收工还没见剧情页：多半是关卡没点开（未解锁/导航失败）
            log('MAA 已结束但没等到剧情页')
            break
    if not jumped:
        raise RuntimeError('没检测到剧情播放页（关卡可能没点开，或剧情已看完）。'
                           '如果 ST 关有解锁条件没满足，先打前面的主线关再试。')
    return {'ok': True, 'stage': stage, 'account': file}


# ---------------------------------------------------------------- signin

def skill_signin(params, log, tid=None):
    """skill 4：森空岛签到（file 空 = 全部账号）"""
    from . import signin
    file = (params.get('account') or '').strip()
    res = signin.run(file or '', log=log)
    return {'ok': res.get('counts', {}).get('fail', 0) == 0,
            'counts': res.get('counts', {}), 'day': res.get('day', '')}


# ---------------------------------------------------------------- weekly 编排

def _cycle_sort_key(code):
    """周目排序键：ST-N 最前，普通关按号，EX 最后（MN-ST-1 < MN-1…7 < MN-ST-2
    < MN-8 < MN-EX-1…，正好是用户给的周目顺序）"""
    up = (code or '').upper()
    m = re.match(r'^[A-Z0-9]+-ST-(\d+)$', up)
    if m:
        return (0, int(m.group(1)))
    m = re.match(r'^[A-Z0-9]+-EX-?(\d+)$', up)
    if m:
        return (2, int(m.group(1)))
    m = re.match(r'^[A-Z0-9]+-(\d+)$', up)
    if m:
        return (1, int(m.group(1)))
    return (3, 0)


def _cycle_rows(prefix):
    """活动全部关卡（含 ST 剧情关）：代号按周目顺序排好

    resolve() 的默认视图不含剧情关（story=True 的 208 条），周目编排必须
    看见 ST 关，所以直接用带剧情关的完整视图按代号前缀取，口径和搜索一致。
    """
    kw = prefix.strip().rstrip('-').lower()
    if not kw:
        raise ValueError('缺活动代号')
    rows, seen = [], set()
    for st in resources._stages(True):
        code = (st.get('code') or '').strip()
        low = code.lower()
        if not low.startswith(kw) or not re.search(r'\d', code):
            continue
        if st.get('difficulty') == 'easy' or code in seen:
            continue
        seen.add(code)
        rows.append({'code': code, 'story': bool(st.get('story'))})
    if not rows:
        raise ValueError('找不到活动关卡：%s（先确认活动代号）' % prefix)
    rows.sort(key=lambda r: _cycle_sort_key(r['code']))
    return rows


def skill_weekly(params, log, tid=None):
    """周目编排：唤醒 → （过剧情 → 打一段）× N → 导航 EX → 打 EX

    ST 关自动过剧情，连续的战斗关合并成一次 fight 批次；任何一步失败
    立即停 + 报告卡在哪关，由上层提醒用户。
    """
    prefix = (params.get('stage') or params.get('keyword') or '').strip()
    if not prefix:
        raise ValueError('缺 stage（活动代号，如 MN / TL）')
    file = _account_file(params.get('account') or '')
    rows = _cycle_rows(prefix)
    log('周目计划（%d 关）：%s' % (len(rows), ' '.join(r['code'] for r in rows)))

    done, batch = [], []
    for r in rows:
        code, story = r['code'], r['story']
        if story:
            # 冲掉攒着的战斗批次，先过剧情
            if batch:
                _run_fight_batch(batch, params, file, log, done)
                batch = []
            log('—— 过剧情关 %s ——' % code)
            skill_skip_story({'stage': code, 'account': file,
                              'accountName': params.get('accountName')}, log)
            done.append(code)
        else:
            batch.append(code)
    if batch:
        _run_fight_batch(batch, params, file, log, done)
    return {'ok': True, 'stages': done, 'account': file}


def _run_fight_batch(batch, params, file, log, done):
    log('—— 自动战斗 %s ——' % ' '.join(batch))
    r = skill_fight({'stages': batch, 'account': file,
                     'mode': params.get('mode') or 'copy',
                     'accountName': params.get('accountName')}, log)
    done.extend(r.get('stages') or [])


# ---------------------------------------------------------------- 调度

SKILLS = {
    'wakeup':    ('唤醒游戏到主界面（可选按尾号切号）', skill_wakeup),
    'navigate':  ('从主界面导航到指定活动',            skill_navigate),
    'fight':     ('自动战斗：编队/借助战/吃理智药/开打', skill_fight),
    'skip_story': ('过剧情关卡（ST 关）',              skill_skip_story),
    'signin':    ('森空岛签到（单号/全部）',            skill_signin),
    'weekly':    ('周目编排：剧情+战斗按序全自动',      skill_weekly),
}


def run_skill(name, params=None):
    """把一个 skill 跑成后台任务，返回 taskId（前端 / 助手轮询 /api/task）

    tid 透传给 skill：fight / wakeup / skip_story 里调 run_core(spec, tid=tid)，
    MAA 的事件日志（排队 / 逃跑 / 失败）会写进同一条任务日志——右栏日志栏
    和助手都能一眼看到卡在哪。
    """
    entry = SKILLS.get(name)
    if not entry:
        raise ValueError('没有这个 skill：%s（可选：%s）'
                         % (name, ' '.join(sorted(SKILLS))))
    desc, fn = entry

    def job():
        # tasks.create 把线程命名成 task-<tid>，从线程名取回自己的 tid，
        # 让 run_core 的 MAA 事件日志（排队/逃跑/失败）写进同一条任务日志
        tid = threading.current_thread().name.replace('task-', '', 1)
        log = tasks.log_factory(tid)
        log('[%s] %s' % (name, desc))
        return fn(params or {}, log, tid=tid)

    tid = tasks.create('skill:' + name, job)
    return tid
