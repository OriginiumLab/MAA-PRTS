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
  navigate    从主界面导航到任意活动（含 EX / S / MO）     stagenav.goto_stage_list
  fight       自动战斗：编队 / 借助战 / 吃理智药 / 开打    maaplan.run_core
  skip_story  过剧情关（mn-st-1 这类 ST 关）               stagenav + 跳幕
  signin      森空岛签到（单号 / 全部）                    signin.run
  weekly      周目编排：把上面几个按关卡顺序串起来         本文件 _plan_cycle
  train       培养干员：唤醒（可切号）→ MAA 干员培养        maaplan.run_core

除 signin 外，每个 skill 起跑前都会先过一次环境闸（``envcheck.ensure``）：
模拟器没开就替用户启动，起来才往下走；见 ``_NEEDS_GAME`` 和 envcheck 的
模块注释。它不是 skill，``/api/skill/list`` 里看不到。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

from . import accounts
from . import atomicio
from . import envcheck
from . import maa
from . import maaplan
from . import resources
from . import settings
from . import stageplan
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
    """账号的切号名（MAA StartUp 切号用的那个）

    来源优先级：
      1. 设置页「MAA 路径」给账号填的 ``maa.accountName``（按游戏账号绑定，
         最准——程序里两个游戏账号对 MAA 里 5 个尾号档案，只有这里能说清
         谁是谁）；
      2. MAA 当前档案「开始唤醒」任务里配的 ``AccountName``（用户在 MAA 里
         填过 3635 这类尾号，直连跑也应遵从同一设置）；
      3. 当前档案名本身就是纯数字尾号（136 / 157 / 165…，用户的档案就是
         这么命名的）时直接用档案名。

    都没有给空串——StartUp 照跑，只是不开切号开关，调用方要提示用户去
    设置页填。**昵称绝不能拿来顶**：官服切号认的是手机号，拿昵称 MAA 在
    账号列表里翻不到，acctname.check 会当场拒。

    以前读 ``snap['accountName'] / ['nickName']``——**顶层根本没有这两个
    字段**（昵称在 ``account.nickName``，切号名在 ``maa.accountName``），切号名
    永远拿不到，StartUp 不带 account_name：游戏已在主界面时这条链 2 秒就
    「完成」，什么都没做，用户看到的就是「唤醒根本没跑」（2026-11-18 实测：
    19:26 挂 IW-EX、17:02 培养两次的 StartUp 参数里都没有切号名，而旧路径
    02:28 的有 3635）。
    """
    try:
        snap = accounts.get_account(file)
    except Exception:
        snap = {}
    name = ((snap.get('maa') or {}).get('accountName') or '').strip()
    if name:
        return name
    try:
        d = _default_maa_dir()
        cfg = maa._load_cfg(d) if d else {}
        prof_name = cfg.get('Current') or ''
        prof = (cfg.get('Configurations') or {}).get(prof_name) or {}
        for t in (prof.get('TaskQueue') or []):
            if t.get('TaskType') == 'StartUp':
                name = (t.get('AccountName') or '').strip()
                if name:
                    return name
                break
        # 档案名就是尾号（136/157/181 这类纯数字），直接当切号名
        if re.fullmatch(r'\d{3,4}', prof_name or ''):
            return prof_name
    except Exception:
        pass                    # MAA 配置读不了就算了，别拖垮任务
    return ''


def _client_of(file):
    try:
        snap = accounts.get_account(file)
        return (snap.get('clientType') or snap.get('channelName') or '').strip()
    except Exception:
        return ''


# 关卡代号形态：CW-1 / MN-EX-1 / OF-EX1 / 15-3 / TR-1。**这是唯一能被导航
# 直接消化的形态**，其余写法都要先换（见 norm_stage）。EX 与数字之间那道
# 横杠是可选的：火蓝之心 / 喧闹法则写成 OF-EX1、CB-EX1。
_STAGE_CODE_RE = re.compile(r'^(?:\d{1,2}|[A-Za-z][A-Za-z0-9]*)(?:-[A-Za-z]+)?-?\d+')


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
    tail = (params.get('accountName') or params.get('tail')
            or _account_name_tail(file)).strip()
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
    log('没找到切号名（设置 → MAA 路径里给这个账号填手机尾号就能切号），'
        '直接重启游戏')
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

    每个 stage 按代号搜作业站，挑**这号能抄的**那份下载落盘（prepare_copilot）。
    以前直接拿 ``ops[0]``——那是按评分排的，不管练度，于是经常挑到「缺三个
    干员」的作业，MAA 编队阶段就失败，用户只看到「跑了 8 分钟 0/1」。
    现在优先 ready（可直接抄）→ borrow（借一人）→ 其它，并如实告警。

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
    band = {'ready': 0, 'borrow': 1, 'train': 2, 'blocked': 3}
    ops = []
    for st in stages:
        res = copilot.search(st, snap or {}, pages=1, limit=6)
        packeds = [p for p in (res.get('stages') or [])
                   if ((p.get('stage') or {}).get('code') or '').upper()
                   == st.upper()]
        if not packeds:
            packeds = [p for p in (res.get('stages') or []) if p.get('ops')]
        # 行间挑选**不能只取第一行**（第一行永远是普通难度）：同样能打时优先
        # 突袭行，理由见 stageplan._lookup 的注释——通用位作业以前永远落
        # normal，MAA 打的永远是普通难度（2026-11-18 用户实测）
        packed = None
        if packeds:
            packed = min(packeds, key=lambda p: (
                min((band.get(o.get('mode'), 9) for o in (p.get('ops') or [])),
                    default=9),
                0 if (p.get('stage') or {}).get('variant') == 'raid' else 1))
        candidates = [p for p in (packed.get('ops') or [])] if packed else []
        if not candidates:
            raise ValueError('%s 没搜到能用的作业（换 mode=fight 纯刷关，'
                             '或自己去作业站挑一份）' % st)
        candidates.sort(key=lambda o: (band.get(o.get('mode'), 9),
                                       -(o.get('rating') or 0)))
        op = candidates[0]
        mode = op.get('mode')
        if mode not in ('ready', 'borrow'):
            log('注意：%s 只搜到「%s」的作业（%s），照排有可能打不过'
                % (st, {'train': '练度不足', 'blocked': '缺干员'}.get(mode, mode),
                   '、'.join((op.get('missing') or []) + (op.get('training') or []))[:40]))
        log('%s：用作业 #%s《%s》' % (st, op.get('copilotId'),
                                     (op.get('title') or '')[:36]))
        ops.append({'copilotId': op['copilotId'], 'stageCode': st,
                    'variant': (packed.get('stage') or {}).get('variant')})
    return maaplan.prepare_copilot(ops, _default_maa_dir(), log=log)


def skill_fight(params, log, tid=None):
    """skill 5：自动战斗——开打前预检 / 编队 / 借助战 / 吃理智药 / 开打

    stages 是关卡列表；mode：
      copy   按作业站作业逐关抄（缺作业的关报错）
      fight  纯刷关（MAA 自己的编队/理智，不抄作业）
    编队（formation）/ 借助战（support）/ 吃理智药（sanityPotion）默认全开。

    **开打前先预检**（``precheck=True``，默认开，见 stageplan）：把关卡按游戏
    顺序排、补齐前置关，再拿作业要求比对账号练度。练度不够 / 缺干员 / 没作业
    的关卡**不排队**，只在日志和返回值里说明原因——用户实测过的坑：练度不够
    还硬挂 S1~S4，MAA 跑 8 分钟第一关就过不去，后面三关根本没解锁。

    要打 S3 但 S1、S2 没打时，预检会把 S1、S2 一起补进计划（``autoPrereq``，
    默认开）：不打前置，目标关在地图上是灰的。

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

    # ---- 开打前预检：能打哪几关、哪几关打不了、为什么 ----
    plan = None
    if params.get('precheck', True):
        # autoPrereq：默认 'auto' = 只补 EX / S / MO 的前置（这几关才是「没通
        # 就锁着」的高频场景，且最多 8 关）；True = 基础关也一起补；False = 不补。
        # **纯刷关（fight）默认不补**：那是「刷 S-3 的材料」，补上前置就变成
        # 每关打一遍，理智全花在已经通关的关上。
        auto = params.get('autoPrereq', 'auto')
        with_pre = (auto is not False) and (mode == 'copy' or auto is True)
        try:
            plan = stageplan.review(
                stages, file, mode=mode, log=log,
                with_prereq=with_pre, fill_base=auto is True)
        except Exception as e:                  # noqa: BLE001 预检不能拖垮开打
            log('预检没做成（%s），按原计划直接打' % e)
            plan = None
        else:
            stageplan.log_report(plan, log)
            if not plan['playable']:
                raise ValueError('这号一关都打不了：\n' + '\n'.join(
                    stageplan.details(plan)) + '\n' + '\n'.join(plan['notes']))
            stages = plan['playable']

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
    # 抄作业必须有作业文件：预检挑好的 ops 直接用（省一轮作业站查询），
    # 没有预检结果时再按 stages 现搜。不给的话队列里 Copilot 会被
    # core_tasks 跳过（见 _prepare_ops 注释），noNav 模式下直接空队列报错。
    if mode == 'copy':
        pre_ops = (plan or {}).get('ops') or {}
        if params.get('copilotList'):
            spec['copilotList'] = params['copilotList']
        elif params.get('ops'):
            spec['copilotList'] = maaplan.prepare_copilot(
                params['ops'], _default_maa_dir(), log=log)
        elif pre_ops and all(s.upper() in pre_ops for s in stages):
            spec['copilotList'] = maaplan.prepare_copilot(
                [pre_ops[s.upper()] for s in stages], _default_maa_dir(), log=log)
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
    else:
        log('没找到切号名，唤醒不切号（要切号去设置 → MAA 路径里给账号填'
            '手机尾号）')
    # 兜底超时：MAA 卡在编队/加载界面时不会自己停（用户实测挂过 480 分钟），
    # 两小时还没跑完就停下，别让整个任务僵尸化。
    r = maaplan.run_core(spec, tid=tid,
                         timeout=params.get('timeout') or 2 * 60 * 60)

    # ---- 收尾结算：实际打到第几关、卡在哪 ----
    failed = (r or {}).get('failed_stage') or ''
    trail = (r or {}).get('stage_trail') or []
    trail_u = [str(x).upper() for x in trail]
    rest = [s for s in stages if str(s).upper() not in trail_u]
    if failed:
        raw_why = (r or {}).get('failed_why') or ''
        why = stageplan.explain_failure(raw_why)
        log('卡在 %s：%s' % (failed, why or 'MAA 没给原因，看上面的报错'))
        if rest:
            log('后面 %s 没打（前一关没过，这些关在游戏里还没解锁）'
                % '、'.join(rest))
    elif trail:
        log('打完了：%s' % '、'.join(trail))
    elif (r or {}).get('timed_out'):
        log('跑满 %d 分钟还没结束，已停下%s'
            % (int((params.get('timeout') or 2 * 60 * 60) // 60),
               ('；最后一关是 %s' % trail[-1]) if trail else ''))
    elif not (r or {}).get('ok', True):
        log('一关都没打到就失败了：%s'
            % ((r or {}).get('error') or '看上面的报错')[:120])
    return {'ok': bool((r or {}).get('ok', True)), 'mode': mode, 'stages': stages,
            'account': file, 'noNav': not with_nav,
            'played': trail, 'failed': failed,
            'failedReason': stageplan.explain_failure((r or {}).get('failed_why') or ''),
            'notPlayed': rest,
            'blocked': (plan or {}).get('blocked') or [],
            'precheck': plan,
            'summary': (plan or {}).get('summary') or ''}


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


# ---------------------------------------------------------------- train

# 账号快照里的职业（中文）→ MaaCore 的 battle::Role。
# 带上职业能让 MAA 少翻一遍干员列表（它按职业 tab 过滤后再 OCR 找名字），
# 同名不同职业的干员（无人机 / 召唤物）也不会认错。
_TRAIN_ROLE = {
    '先锋': 'Pioneer', '近卫': 'Warrior', '重装': 'Tank', '狙击': 'Sniper',
    '术师': 'Caster', '医疗': 'Medic', '辅助': 'Support', '特种': 'Special',
}

_CN_NUM = {'零': 0, '一': 1, '二': 2, '两': 2, '三': 3,
           '0': 0, '1': 1, '2': 2, '3': 3}


def _to_int(v):
    """'2' / 'Lv.7' / '精二' / 7 → int；认不出返回 None"""
    if v is None or v == '' or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    m = re.search(r'\d+', s)
    if m:
        return int(m.group())
    for k, n in _CN_NUM.items():
        if k in s:
            return n
    return None


def _norm_mastery(v):
    """专精目标 → ``[技1, 技2, 技3]``（0~3，是**绝对等级**不是增量）

    用户/模型给法五花八门，这里统一吃下：
        [0, 3, 0]                        列表（最标准）
        {"2": 3} / {"技能2": 3}           字典：技能号 → 专精等级
        "2:3" / "技能2专精3" / "二技能专三"
    """
    if v is None or v == '' or v == [] or v == {}:
        return None
    arr = [0, 0, 0]
    if isinstance(v, dict):
        for k, lv in v.items():
            i = _to_int(k)
            n = _to_int(lv)
            if not i or i < 1 or i > 3:
                raise ValueError('专精技能号只能 1/2/3（收到 %s）' % k)
            if n is None or n < 0 or n > 3:
                raise ValueError('专精等级只能 0~3（收到 %s）' % lv)
            arr[i - 1] = n
        return arr if any(arr) else None
    if isinstance(v, (list, tuple)):
        for i, lv in enumerate(list(v)[:3]):
            n = _to_int(lv) or 0
            if n < 0 or n > 3:
                raise ValueError('专精等级只能 0~3（收到 %s）' % lv)
            arr[i] = n
        return arr if any(arr) else None
    s = str(v)
    pairs = re.findall(r'([123一二三])\D{0,6}?([0-3零一二三])', s)
    for a, b in pairs:
        arr[_CN_NUM[a] - 1] = _CN_NUM[b]
    if not any(arr):
        raise ValueError('看不懂专精目标「%s」，写成 [0,3,0]（技能2 专三）这种' % v)
    return arr


def _train_rows(params):
    """把入参摊平成 ``[{name, elite, skillLevel, mastery}]``

    支持三种写法：
      operators: [{name, elite, skillLevel, mastery}, …]   ← 标准
      operators: ["泥岩", "银灰"]                          ← 只给名字
      operator: "泥岩" + 顶层 elite/skillLevel/mastery     ← 单个干员简写
    顶层字段会兜底给每个没写目标的干员（同名批量培养时省事）。
    """
    raw = params.get('operators')
    if raw is None:
        raw = params.get('opers')
    if isinstance(raw, str):
        raw = [x for x in re.split(r'[,，、;；\s]+', raw) if x]
    rows = []
    for it in (raw or []):
        rows.append({'name': it} if isinstance(it, str) else dict(it or {}))

    if not rows and params.get('operator'):
        rows = [{'name': params.get('operator')}]

    fallback = {k: params.get(k) for k in
                ('elite', 'skillLevel', 'skill_level', 'mastery', 'skillMastery')
                if params.get(k) not in (None, '')}
    out = []
    for row in rows:
        for k, v in fallback.items():
            if row.get(k) in (None, ''):
                row[k] = v
        name = str(row.get('name') or '').strip()
        if not name:
            continue
        out.append({
            'name': name,
            'elite': row.get('elite'),
            'skillLevel': row.get('skillLevel', row.get('skill_level')),
            'mastery': row.get('mastery', row.get('skill_mastery')),
        })
    return out


def _train_plans(rows, file, log):
    """入参 + 账号练度 → MaaCore 的 plans（只留还没达标的项）

    「导入用户需要培养的练度」实际做的是：拿账号快照对一遍，已经达标的项
    直接从计划里删掉——MAA 会自己在档案页现场识别，但让它跑一遍「已精二
    的干员升精二」纯属浪费时间，日志里还会多一条看不懂的「失败」。
    """
    snap = None
    try:
        snap = accounts.get_account(file)
    except Exception:
        pass
    chars = (snap or {}).get('chars') or []
    out = []
    for row in rows:
        raw_name = str(row.get('name') or '').strip()
        if not raw_name:
            continue
        char = next((c for c in chars
                     if str(c.get('name') or '') == raw_name), None)
        if char is None:
            char = next((c for c in chars
                         if raw_name and raw_name in str(c.get('name') or '')), None)
        if char is None and chars:
            raise ValueError('账号快照里没有「%s」——名字写错了？还是这号没这个干员'
                             '（先在设置里同步一次账号）' % raw_name)
        name = str((char or {}).get('name') or raw_name)
        plan = {'name': name}
        role = _TRAIN_ROLE.get(str((char or {}).get('profession') or '').strip())
        if role:
            plan['role'] = role

        cur_e = int((char or {}).get('elite') or 0)
        cur_sl = int((char or {}).get('skillLevel') or 0)
        cur_m = [int((s or {}).get('mastery') or 0)
                 for s in ((char or {}).get('skills') or [])] + [0, 0, 0]

        elite = _to_int(row.get('elite'))
        if elite:
            if elite not in (1, 2):
                raise ValueError('%s 的精英化目标只能填 1 或 2（0 / 不填 = 不升精英化）'
                                 % name)
            if char and cur_e >= elite:
                log('%s 已经是精%s，跳过精英化'
                    % (name, '二' if elite == 2 else '一'))
            else:
                plan['elite'] = elite

        sl = _to_int(row.get('skillLevel')
                     if row.get('skillLevel') is not None else row.get('skill_level'))
        if sl:
            if sl < 2 or sl > 7:
                raise ValueError('%s 的技能基础等级只能 2~7（MAA 的下限是 2，收到 %s）'
                                 % (name, sl))
            if char and cur_sl >= sl:
                log('%s 技能已经是 %d 级，跳过' % (name, cur_sl))
            else:
                plan['skill_level'] = sl

        arr = _norm_mastery(row.get('mastery')
                            if row.get('mastery') is not None
                            else row.get('skill_mastery'))
        if arr:
            for i in range(3):
                if arr[i] and cur_m[i] >= arr[i]:
                    log('%s 技能%d 已经专%s，跳过' % (name, i + 1, arr[i]))
                    arr[i] = 0
            if any(arr):
                plan['skill_mastery'] = arr

        if len(plan) <= (2 if role else 1):
            log('%s 的目标都已达标，不用培养' % name)
            continue
        out.append(plan)
    return out


def _remain_plans(plans, done):
    """计划 − 已完成 → 还没做完的部分（也用来续跑）"""
    out = []
    for p in plans:
        rec = (done or {}).get(p['name']) or {}
        left = {'name': p['name']}
        if p.get('role'):
            left['role'] = p['role']
        if p.get('elite') and int(rec.get('elite') or 0) < int(p['elite']):
            left['elite'] = int(p['elite'])
        if p.get('skill_level') and int(rec.get('skill_level') or 0) < int(p['skill_level']):
            left['skill_level'] = int(p['skill_level'])
        arr = list(p.get('skill_mastery') or [0, 0, 0]) + [0, 0, 0]
        got = list(rec.get('skill_mastery') or [0, 0, 0]) + [0, 0, 0]
        rem = [0, 0, 0]
        for i in range(3):
            if arr[i] and int(got[i] or 0) < int(arr[i]):
                rem[i] = int(arr[i])
        if any(rem):
            left['skill_mastery'] = rem
        if len(left) > 1:
            out.append(left)
    return out


def _train_plan_path():
    return os.path.join(settings.CONFIG_DIR, 'train-plan.json')


def _load_train_plan():
    """上次没做完的培养计划；没有/坏了都返回 None"""
    p = _train_plan_path()
    try:
        with open(p, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not (data.get('plans') or []):
        return None
    return data


def _save_train_plan(file, plans):
    """没做完就落盘（材料不足暂停后「继续培养」要用）；做完了删掉"""
    p = _train_plan_path()
    if not plans:
        try:
            os.remove(p)
        except OSError:
            pass
        return
    atomicio.dump_json(p, {
        'account': file,
        'savedAt': time.strftime('%Y-%m-%d %H:%M:%S'),
        'plans': plans,
    })


def skill_train(params, log, tid=None):
    """skill 7：培养干员——升精英化 / 技能等级 / 专精（MAA 的 OperProgress）

    流程：开始唤醒（可选，切号可选）→ MAA 干员培养 → 材料不足就暂停

    - ``operators``：``[{name, elite, skillLevel, mastery}, …]``；``mastery``
      是**绝对专精等级**（``[0,3,0]`` = 技能2 专三），也吃 ``"2:3"`` 这种写法
    - 只给 ``name`` 也行（只补专精 / 只升精英化时另传 elite / skillLevel）
    - ``wakeup`` 默认 true；``switchAccount`` 默认「给了切号名才切」，用户说
      不用切号就传 false——StartUp 仍然跑，只是不开切号开关
    - ``resume``：接着上次没做完的做（材料不足暂停后用它）
    """
    saved = None
    if params.get('resume'):
        saved = _load_train_plan()
        if not saved:
            raise ValueError('没有没做完的培养计划（上次的都已经做完了）')
        if not params.get('account') and saved.get('account'):
            params = dict(params, account=saved['account'])
    file = _account_file(params.get('account') or '')
    tail = (params.get('accountName') or params.get('tail')
            or _account_name_tail(file) or '').strip()
    switch = params.get('switchAccount')
    if switch is None:
        switch = bool(tail)          # 只有显式给了切号名才切号
    switch = bool(switch) and bool(tail)
    wakeup = params.get('wakeup', True)
    if isinstance(wakeup, str):
        wakeup = wakeup.strip().lower() not in ('0', 'false', 'no', 'off')

    rows = saved['plans'] if saved else _train_rows(params)
    if not rows:
        raise ValueError('缺 operators（要培养谁、升到什么程度）')
    plans = _train_plans(rows, file, log)
    if not plans:
        _save_train_plan(file, [])
        log('没有需要培养的目标（都已经达标了）')
        return {'ok': True, 'account': file, 'plans': [], 'skipped': True,
                'remaining': []}

    if saved:
        log('续跑上次没做完的计划（%s 保存，%d 名）'
            % (saved.get('savedAt') or '', len(plans)))
    log('培养计划（%d 名）：' % len(plans))
    for p in plans:
        log('  · %s %s' % (p['name'], maaplan.plan_brief(p)))
    if not wakeup:
        log('不唤醒：直接从当前界面进干员列表（游戏得开着）')
    elif switch:
        log('先开始唤醒，切到 %s' % tail)
    else:
        log('先开始唤醒（不切号%s）' % ('；要切号去设置 → MAA 路径里给账号'
            '填手机尾号' if not tail else ''))

    spec = {
        'tasks': (['startup', 'operprogress'] if wakeup else ['operprogress']),
        'plans': plans,
        'accountName': tail,
        'switchAccount': switch,
        'noStartUp': not wakeup,
        'maaDir': _default_maa_dir(),
    }
    r = maaplan.run_core(spec, tid=tid,
                         timeout=params.get('timeout') or 3 * 60 * 60)
    done = r.get('trained') or {}
    summary = r.get('trainSummary') or {}
    remaining = _remain_plans(plans, done)
    _save_train_plan(file, remaining)

    if summary:
        log('成绩：成功 %s，失败 %s，跳过 %s'
            % (summary.get('success'), summary.get('failed'),
               summary.get('skipped')))
    if r.get('paused'):
        log('暂停原因：%s%s' % (r['paused'], r.get('pauseDetail') or ''))
    if remaining:
        log('还没做完 %d 项：' % len(remaining))
        for p in remaining:
            log('  · %s %s' % (p['name'], maaplan.plan_brief(p)))
        log('补齐材料后说一声「继续培养」，会接着做这些（不用重新报练度）')
    else:
        log('培养计划全部完成')
    return {'ok': bool(r.get('ok')), 'account': file, 'plans': plans,
            'paused': r.get('paused') or '', 'summary': summary,
            'trained': done, 'remaining': remaining,
            'wakeup': wakeup, 'switchAccount': switch}


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
    """周目排序键：ST-N 最前，然后 TR 教学 → 普通关按号 → EX → S → MO
    （MN-ST-1 < MN-1…7 < MN-ST-2 < MN-8 < MN-EX-1…，正好是用户给的周目顺序；
    S / MO 排在 EX 之后，BB 这种「EX 打完开 S、最后 MO」的活动才不会乱序）"""
    up = (code or '').upper()
    m = re.match(r'^[A-Z0-9]+-ST-(\d+)$', up)
    if m:
        return (0, int(m.group(1)))
    m = re.match(r'^[A-Z0-9]+-TR-?(\d+)$', up)
    if m:
        return (1, 0)
    m = re.match(r'^[A-Z0-9]+-EX-?(\d+)$', up)
    if m:
        return (3, int(m.group(1)))
    m = re.match(r'^[A-Z0-9]+-S-?(\d+)$', up)
    if m:
        return (4, int(m.group(1)))
    m = re.match(r'^[A-Z0-9]+-MO-?(\d+)$', up)
    if m:
        return (5, int(m.group(1)))
    m = re.match(r'^[A-Z0-9]+-(\d+)$', up)
    if m:
        return (2, int(m.group(1)))
    return (6, 0)


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
    cur_kind = None
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
            # **跨分区必须断批**：基础关和 EX 是两张地图，MAA 的 Copilot 只认
            # 当前地图上的关卡名，中间不重新导航就会「找不到 BB-EX-1」。
            # 以前只按 ST 关断批，活动没有 ST 时整个活动挤成一批，EX 全废。
            k = stageplan.kind_of(code)
            if batch and k != cur_kind:
                _run_fight_batch(batch, params, file, log, done)
                batch = []
            batch.append(code)
            cur_kind = k
    if batch:
        _run_fight_batch(batch, params, file, log, done)
    return {'ok': True, 'stages': done, 'account': file}


def _run_fight_batch(batch, params, file, log, done):
    # **周目必须先自己走到位**：老的常驻活动 MAA 资源里没有寻路配置，Copilot
    # 的 ClickStageName 只在「已经在正确分区的地图上」时才 OCR 得到关卡名；
    # 直跑的话 EX / S 批次会一直报「找不到关卡」。所以每个批次开打前导航一次
    # （navigate 内部会连带切分区，见 stagenav.goto_subzone）。
    if params.get('nav', True):
        log('—— 导航到 %s ——' % batch[0])
        try:
            skill_navigate({'stage': batch[0], 'account': file,
                            'accountName': params.get('accountName')}, log)
        except Exception as e:
            raise RuntimeError('周目导航到 %s 失败：%s' % (batch[0], e))
    log('—— 自动战斗 %s ——' % ' '.join(batch))
    r = skill_fight({'stages': batch, 'account': file,
                     'mode': params.get('mode') or 'copy',
                     'accountName': params.get('accountName')}, log)
    done.extend(r.get('stages') or [])
    # 打不了就停，别把后面的批次继续排下去：周目是按解锁顺序走的，这一关
    # 过不去，后面同活动的 EX / S 全是灰的，排了也是白跑（用户实测 8 分钟
    # 0/1）。原因原样抛给上层，助手要把「哪几关打不了、为什么」讲清楚。
    blocked = r.get('blocked') or []
    if blocked:
        raise RuntimeError('这些关打不了，周目停下：' + '；'.join(
            '%s %s' % (b['code'], b['reason']) for b in blocked))
    if r.get('failed'):
        raise RuntimeError('卡在 %s，后面的关没打（%s）'
                           % (r['failed'], '、'.join(r.get('notPlayed') or []) or '无'))


# ---------------------------------------------------------------- 调度

SKILLS = {
    'wakeup':    ('唤醒游戏到主界面（可选按尾号切号）', skill_wakeup),
    'navigate':  ('从主界面导航到指定活动',            skill_navigate),
    'fight':     ('自动战斗：预检练度/前置 → 编队/借人/开打', skill_fight),
    'skip_story': ('过剧情关卡（ST 关）',              skill_skip_story),
    'signin':    ('森空岛签到（单号/全部）',            skill_signin),
    'weekly':    ('周目编排：剧情+战斗按序全自动',      skill_weekly),
    'train':     ('培养干员：精英化/技能/专精，材料不足暂停', skill_train),
}

# 哪些 skill 跑之前要先确认模拟器在。signin 走的是森空岛网页，跟游戏无关，
# 不查——否则用户只想签个到，却被要求开模拟器。这张表另用一个常量而不是
# 塞进 SKILLS 元组：/api/skill/list 和助手的工具列表都直接吃 SKILLS 的形状，
# 加字段会把两处都牵连一遍。
_NEEDS_GAME = frozenset(['wakeup', 'navigate', 'fight', 'skip_story', 'weekly',
                         'train'])


def run_skill(name, params=None):
    """把一个 skill 跑成后台任务，返回 taskId（前端 / 助手轮询 /api/task）

    tid 透传给 skill：fight / wakeup / skip_story 里调 run_core(spec, tid=tid)，
    MAA 的事件日志（排队 / 逃跑 / 失败）会写进同一条任务日志——右栏日志栏
    和助手都能一眼看到卡在哪。

    **起任务前先过环境闸**（:mod:`envcheck`）：模拟器没开就替用户启动，
    起来之后才真正开跑；起不来直接让任务失败，不往下走到「翻了几十屏没
    找到卡片」那种更看不懂的报错。一切正常时日志里不会多一行。
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
        if name in _NEEDS_GAME:
            ok, why = envcheck.ensure(log)
            if not ok:
                raise RuntimeError('环境没准备好：%s' % why)
        return fn(params or {}, log, tid=tid)

    tid = tasks.create('skill:' + name, job)
    return tid
