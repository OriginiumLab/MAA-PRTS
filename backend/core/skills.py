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

    **唯一来源：账号自己绑定的 ``maa.accountName``**（设置 → MAA 路径里填的
    手机尾号，或对话里用户报给助手后回存的）。给空串就退回 adb 重启那条路，
    调用方负责提示去设置页填。

    用户口径（2026-11-18）：agent 模式跑的是**临时配置**——StartUp 任务参数
    只有「开始唤醒 + 手机号」，MaaCore 按参数自己换号，**不读也不改用户的
    MAA 配置档案**。以前这里会退回读 MAA 当前档案的 ``AccountName`` / 档案名
    （moren / 136 / 157…）来猜，两个问题：一是违背上面的口径（agent 不该碰
    用户的 MAA 配置）；二是猜错代价大——程序里两个游戏账号对 MAA 里五个尾号
    档案，档案名和 AccountName 根本说不清谁是谁，错配了 MAA 在账号列表里
    死找 4 分钟超时。谁要用哪个尾号，只有账号绑定这一个可信来源。

    **昵称绝不能拿来顶**：官服切号认的是手机号，拿昵称 MAA 在账号列表里
    翻不到，acctname.check 会当场拒。
    """
    try:
        snap = accounts.get_account(file)
    except Exception:
        snap = {}
    maa = snap.get('maa') if isinstance(snap, dict) else None
    maa = maa if isinstance(maa, dict) else {}
    return (maa.get('accountName') or '').strip()


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

    # 「最新活动 / 当期活动」这类指代：走当期活动表（用户 2026-11-19 要求，
    # 活动一换就让助手直接挂新的，不用他每次都报代号）
    try:
        from . import eventnav
        hit = eventnav.match_name(s)
        if hit:
            log('「%s」→ %s（当期活动）' % (s, hit))
            return hit, ''
    except Exception:
        pass

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
        # 临时配置：StartUp 任务参数只有「开始唤醒 + 切号名」，MaaCore 按参数
        # 自己换号——不读也不改用户的 MAA 配置档案
        spec = {'tasks': ['startup'], 'accountName': tail,
                'maaDir': _default_maa_dir()}
        log('临时配置唤醒：只跑开始唤醒，切到 %s' % tail)
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
    log('这个账号没绑定切号名（设置 → MAA 路径里填手机尾号，'
        '或对话里直接告诉我），只能重启当前账号的游戏')
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

_VARIANT_ALIASES = {
    'raid': 'raid', '突袭': 'raid', 'only': 'raid', 'onlyraid': 'raid',
    'normal': 'normal', '普通': 'normal',
    'both': 'both', 'all': 'both', '都要': 'both', 'allraid': 'both',
}


def _parse_stages(raw):
    """stages 支持逐关指定难度：``'SV-EX-1'`` / ``'SV-EX-1@raid'`` /
    ``'SV-EX-1@both'`` / ``{'stage': 'SV-EX-1', 'raid': 'both'}``

    返回 ``(codes, wants)``；wants 只收明确标注过的关卡（大写代号 → 难度），
    没标注的走全局 ``raid`` 参数。用户 2026-11-19 明确要求的用法：同一个
    计划里「EX-1~5 只打突袭、EX-6/7 普通+突袭」，没有逐关标注就做不到。
    """
    codes, wants = [], {}
    for it in raw or []:
        if isinstance(it, dict):
            code = str(it.get('stage') or it.get('code')
                       or it.get('name') or '').strip()
            w = it.get('raid', it.get('variant'))
            if isinstance(w, bool):
                w = 'both' if w else 'normal'
        else:
            code = str(it or '').strip()
            w = None
            if '@' in code:
                code, w = code.rsplit('@', 1)
                code, w = code.strip(), w.strip().lower()
        if not code:
            continue
        codes.append(code)
        key = _VARIANT_ALIASES.get(w) if w else None
        if key:
            wants[code.upper()] = key
    return codes, wants


def _select_ops(stages, file, log, raid='auto', wants=None, fixed=None,
                depth=3, strict=False, prereq=None):
    """挑选每关要用的作业 → ``(picks, pool)``

    ``picks`` 是首轮要排的 ``[{'copilotId','stageCode','variant'}]``；
    ``pool`` 是同一关同一档的**备选作业** ``{'SV-EX-1@raid': [41623, 41624]}``，
    给 ``maaplan.run_core`` 在翻车后换一份重挂用（2026-11-19 用户需求 3：
    「某关战斗失败了，换其他作业自动挂这关」）。

    ``depth`` 是每关备选几份（默认 3：首轮 + 两轮重挂）；
    ``strict=True`` 时只排指定档位（``wants`` 里写 normal 就不拿突袭作业顶），
    扫图 skill 用它保证「普通段只打普通、突袭段只打突袭」。

    mode=copy：把关卡代号列表变成 MaaCore 的 copilotList

    每个 stage 按代号搜作业站，挑**这号能抄的**那份下载落盘（prepare_copilot）。
    以前直接拿 ``ops[0]``——那是按评分排的，不管练度，于是经常挑到「缺三个
    干员」的作业，MAA 编队阶段就失败，用户只看到「跑了 8 分钟 0/1」。
    现在优先 ready（可直接抄）→ borrow（借一人）→ 其它，并如实告警。

    **默认每关只排真正要打的那一档**（2026-11-19 用户实测口径）：

    - ``raid='auto'``（默认）→ 有突袭作业就**只排突袭**（IsRaid=true 一条）。
      MaaCore 拿到 is_raid=true 会自己点「ChangeToRaidDifficulty」切难度
      （asst.log 01:42 实证），不需要前面垫一条普通。以前排「普通 → 突袭」
      两条，用户看到的就是「又从头打了一遍普通 EX-1」，白烧一次理智。
    - ``raid='both'`` → 普通 + 突袭两条（普通还没通关、要一次打通时用）。
    - ``raid=False`` / ``'normal'`` → 只排普通（刷材料、不打突袭）。
    - 单关可以用 ``stages=['SV-EX-1@raid', 'SV-EX-6@both']`` 覆盖全局（见
      ``_parse_stages``）。

    主线「磨难 / 险地」不是突袭（用户 2026-11-19 明确），它们是独立关卡 id，
    各排一条、不展开。

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
    if isinstance(raid, bool):
        gw = 'both' if raid else 'normal'
    else:
        gw = {'auto': 'raid', 'raid': 'raid', 'true': 'both', 'both': 'both',
              'normal': 'normal', 'false': 'normal', '0': 'normal',
              'no': 'normal'}.get(str(raid).strip().lower(), 'raid')
    wants = {str(k).upper(): v for k, v in (wants or {}).items()}
    fixed = {str(k).upper(): int(v) for k, v in (fixed or {}).items() if v}
    prereq = {str(k).upper() for k in (prereq or ()) if k}
    picks, pool = [], {}

    def _ranked(row):
        """这一行里能抄的作业，按「可直接抄 → 借一人 → 练度不足」排好"""
        if not row:
            return []
        cands = sorted(list(row.get('ops') or []),
                       key=lambda o: (band.get(o.get('mode'), 9),
                                      -(o.get('rating') or 0)))
        usable = [o for o in cands if (o.get('mode') or '') != 'blocked']
        return usable or cands

    def _add(st, variant, cands):
        """首轮用 cands[0]，其余存进 pool 当重挂的备选"""
        if not cands:
            return
        key = '%s@%s' % (st.upper(), variant)
        ids = []
        for o in cands[:max(1, depth)]:
            cid = o.get('copilotId')
            if cid and int(cid) not in ids:
                ids.append(int(cid))
        if ids:
            pool[key] = ids
        picks.append({'copilotId': cands[0]['copilotId'], 'stageCode': st,
                      'variant': variant})

    def _note(st, op):
        mode = op.get('mode')
        if mode not in ('ready', 'borrow'):
            log('注意：%s 只搜到「%s」的作业（%s），照排有可能打不过'
                % (st, {'train': '练度不足', 'blocked': '缺干员'}.get(mode, mode),
                   '、'.join((op.get('missing') or []) + (op.get('training') or []))[:40]))

    for st in stages:
        # 用户点名「用 41624 那份」：直接钉住作业 id，不再去作业站重挑。
        # 以前没有这个入口，助手嘴上说换了、实际每次重新搜索都搜回同一份
        # （2026-11-19 02:59 用户：「切换作业它并没有成功，而是反复用这个」）。
        cid = fixed.get(st.upper())
        if cid:
            w = wants.get(st.upper()) or gw
            if w == 'both':
                _add(st, 'normal', [{'copilotId': cid, 'mode': 'ready'}])
                _add(st, 'raid', [{'copilotId': cid, 'mode': 'ready'}])
                log('%s：按指定作业 #%s 打普通 + 突袭' % (st, cid))
            else:
                _add(st, 'raid' if w == 'raid' else 'normal',
                     [{'copilotId': cid, 'mode': 'ready'}])
                log('%s：按指定作业 #%s 打%s'
                    % (st, cid, '突袭' if w == 'raid' else '普通'))
            continue
        try:
            res = copilot.search(st, snap or {}, pages=1, limit=6)
        except Exception as e:                  # noqa: BLE001 一关搜不到别拖垮整批
            log('跳过 %s：搜作业失败（%s）' % (st, type(e).__name__))
            continue
        packeds = [p for p in (res.get('stages') or [])
                   if ((p.get('stage') or {}).get('code') or '').upper()
                   == st.upper()]
        if not packeds:
            packeds = [p for p in (res.get('stages') or []) if p.get('ops')]
        normal_row = next((p for p in packeds
                           if (p.get('stage') or {}).get('variant') != 'raid'), None)
        raid_row = next((p for p in packeds
                         if (p.get('stage') or {}).get('variant') == 'raid'), None)
        n_cands = _ranked(normal_row)
        r_cands = _ranked(raid_row)
        n_op = n_cands[0] if n_cands else None
        r_op = r_cands[0] if r_cands else None
        if not (n_op or r_op):
            # 作业站没有这关的作业（或被黑名单滤空）：跳过它比整批报错好，
            # 别的关照打；最后一份都没有才报错
            log('跳过 %s：没搜到能用的作业（可换 mode=fight 纯刷关）' % st)
            continue

        # 磨难 / 险地 / 简单是主线自己的难度（独立关卡 id），不展开成两条
        st_info = (raid_row or normal_row or {}).get('stage') or {}
        mainline = st_info.get('difficulty') in ('easy', 'tough', 'risky')
        want = wants.get(st.upper()) or gw
        # 补进来的前置关只为「打通解锁下一关」，打突袭是白白多烧一倍理智
        # （2026-11-19 用户实测：只点了 S-3/S-4，S-1、S-2 也按突袭打了一遍）
        if st.upper() in prereq and st.upper() not in wants:
            want = 'normal'
            log('%s 是补的前置关，只打普通（打通就解锁）' % st)
        if mainline:
            want = 'normal'
        # 「这关的突袭作业到底有没有」和「抄不抄得动」是两件事：
        # 2026-11-19 用户实测「不会选突袭」——突袭作业练度要求比普通高，被判
        # 「缺干员」后程序自己默默改打普通，用户看到的就是「我要突袭它打普通」。
        # 现在只要**有**突袭作业就照排（`_note` 会如实告警），不偷偷降级；
        # 真要降级是「这关压根没有突袭作业」。
        has_raid = r_op is not None
        raid_ok = (has_raid
                   and (r_op.get('mode') or 'blocked') in ('ready', 'borrow',
                                                           'unknown'))

        def _alt(n):
            return '（另有 %d 份备选，翻车会自动换）' % (n - 1) if n > 1 else ''

        if want == 'raid':
            if has_raid:
                _note(st, r_op)
                log('%s：只打突袭（作业 #%s《%s》）%s'
                    % (st, r_op.get('copilotId'),
                       (r_op.get('title') or '')[:30], _alt(min(len(r_cands), depth))))
                _add(st, 'raid', r_cands)
                continue
            if n_op is None:
                log('跳过 %s：这关没有突袭作业，也没有普通版' % st)
                continue
            if strict:
                log('跳过 %s：这关没有突袭作业（这一轮只要突袭）' % st)
                continue
            log('%s：作业站这关没有突袭作业，只打普通' % st)
        elif want == 'both':
            if has_raid and n_op is not None:
                log('%s：普通 + 突袭（作业 #%s → #%s）'
                    % (st, n_op.get('copilotId'), r_op.get('copilotId')))
                _note(st, n_op)
                _add(st, 'normal', n_cands)
                _note(st, r_op)
                _add(st, 'raid', r_cands)
                continue
            if has_raid and n_op is None:
                _note(st, r_op)
                log('%s：只打突袭（没有普通版作业）%s'
                    % (st, _alt(min(len(r_cands), depth))))
                _add(st, 'raid', r_cands)
                continue
            if n_op is None:
                log('跳过 %s：这轮要普通+突袭，但两份都没有' % st)
                continue
            if strict:
                log('跳过 %s：这关没有突袭作业（这一轮只要突袭）' % st)
                continue
            log('%s：作业站这关没有突袭作业，只排普通' % st)
        # 走到这里就是「打普通」
        if n_op is None:
            if strict:
                log('跳过 %s：没有能抄的普通作业（这一轮只要普通）' % st)
                continue
            _note(st, r_op)
            log('%s：没有普通版作业，改用突袭作业 #%s《%s》'
                % (st, r_op.get('copilotId'), (r_op.get('title') or '')[:30]))
            _add(st, 'raid', r_cands)
            continue
        _note(st, n_op)
        log('%s：用作业 #%s《%s》%s'
            % (st, n_op.get('copilotId'), (n_op.get('title') or '')[:36],
               _alt(min(len(n_cands), depth))))
        _add(st, 'normal', n_cands)
    if not picks:
        raise ValueError('这几关一份能用的作业都没搜到（换 mode=fight 纯刷关，'
                         '或自己去作业站挑一份）')
    return picks, pool


def _tidy_stage_list(stages, asked, log):
    """预检后的名单核对：只留「用户点名的关 + 游戏内前置关」

    2026-11-19 用户实测：他说「SV-EX-1 ~ SV-EX-6」，助手排的是
    ``SV-EX-2 ~ SV-EX-7``（跳掉 EX-1 之后又往后补了一关凑数），白烧一整轮
    理智。这里按白名单过滤，名单外的一律剔掉并写日志；用户点名却没排进去的
    （预检判定打不了）也点名说明。

    白名单**先用代号规则自己算**（同活动同分区的序号 ≤ 点名最大序号），再
    并上 ``stageplan.expand`` 的链：资源包里没有当期活动 EX / S 关时 expand
    认不出前置，只靠它会漏。
    """
    asked = [str(s).upper() for s in (asked or []) if s]
    if not asked or not stages:
        return stages
    allow = set(asked)

    def _key(code):
        m = re.match(r'^([A-Za-z][A-Za-z0-9]{0,11})-(EX|HX|MO|S|F)-?(\d{1,2})$',
                     str(code or '').upper())
        return (m.group(1), m.group(2), int(m.group(3))) if m else None

    top = {}
    for a in asked:
        k = _key(a)
        if k:
            top[(k[0], k[1])] = max(top.get((k[0], k[1]), 0), k[2])
    if top:
        for s in stages:
            k = _key(s)
            if k and k[2] <= top.get((k[0], k[1]), 0):
                allow.add(str(s).upper())
    try:
        allow |= {x['code'].upper()
                  for x in stageplan.expand(list(asked), with_prereq=True,
                                            fill_base=True)}
    except Exception:
        pass
    extra = [s for s in stages if str(s).upper() not in allow]
    if extra:
        log('名单核对：%s 不在用户点名的关卡里（也不是前置关），已剔除'
            % '、'.join(str(x) for x in extra))
        stages = [s for s in stages if str(s).upper() in allow]
        if not stages:
            raise ValueError('名单核对后一关都不剩（用户点名的是 %s）'
                             % '、'.join(asked))
    got = {str(x).upper() for x in stages}
    missed = [s for s in asked if s not in got]
    if missed:
        log('名单核对：用户点名的 %s 没在计划里（预检判定打不了，见上面说明）'
            % '、'.join(missed))
    return stages


def _prepare_ops(stages, file, log, raid='auto', wants=None, fixed=None,
                 depth=3, strict=False, prereq=None):
    """``_select_ops`` + 落盘 → ``(copilotList, retry)``

    ``retry`` 直接塞进 ``spec['retry']``，run_core 据此在翻车后换作业重挂。
    每关只有一份候选时不给 retry（没得换，省一轮空转）。
    """
    picks, pool = _select_ops(stages, file, log, raid=raid, wants=wants,
                              fixed=fixed, depth=depth, strict=strict,
                              prereq=prereq)
    rows = maaplan.prepare_copilot(picks, _default_maa_dir(), log=log)
    retry = {}
    if any(len(v) > 1 for v in pool.values()):
        retry = {'pool': pool, 'maxRounds': max(2, min(4, depth))}
    return rows, retry


def skill_fight(params, log, tid=None):
    """skill 5：自动战斗——开打前预检 / 编队 / 借助战 / 吃理智药 / 开打

    stages 是关卡列表；mode：
      copy   按作业站作业逐关抄（缺作业的关报错）
      fight  纯刷关（MAA 自己的编队/理智，不抄作业）
    编队（formation）/ 借助战（support）/ 吃理智药（sanityPotion）默认全开。

    **开打前先预检**（``precheck=True``，默认开，见 stageplan）：把关卡按游戏
    顺序排，再拿作业要求比对账号练度。练度不够 / 缺干员 / 没作业的关卡
    **不排队**，只在日志和返回值里说明原因——用户实测过的坑：练度不够还硬挂
    S1~S4，MAA 跑 8 分钟第一关就过不去。

    **默认不补前置**（``autoPrereq=False``，2026-11-19 用户口径：「我让他挂
    的关卡就是他点名的关」）：要打 S-3 就只排 S-3，不会顺手把 S-1、S-2 也打
    一遍。确实没解锁、要一起补时传 ``autoPrereq=true``（或 'auto' = 只补
    EX/S/MO 的前置）；补进来的前置关**只打普通**（打通就解锁，不打突袭）。

    **默认不唤醒、不导航**（noNav 直跑）：调用前界面在哪，Copilot 就从哪
    接手（当期活动 MAA 自己认路；老活动请先用 navigate skill 或手动到位）。
    用户明确要「连唤醒/导航一起」时传 wakeup=true 才走全套。

    **突袭（raid）**：默认每关**只打真正要打的那一档**（见 ``_prepare_ops``）：
    ``raid='auto'``（默认）有突袭作业就只排突袭；``raid='both'`` 普通+突袭
    两条（普通还没通关、要一次打通用）；``raid=False`` 只排普通（刷材料）。
    单关可以写 ``stages=['SV-EX-1@raid', 'SV-EX-6@both']`` 逐关覆盖。
    主线磨难 / 险地不是突袭，各排一条。
    """
    stages, wants = _parse_stages(params.get('stages'))
    if not stages:
        raise ValueError('缺 stages（要打哪些关）')
    # 用户点名某份作业（「用 41624 那份」「换成令的单核」）时钉住作业 id。
    # 以前没有这个入口，助手只能重新搜，排序总是回到同一份（评分最高的），
    # 用户就看到「换作业没生效，还是同一份」。
    fixed = {}
    for it in (params.get('ops') or []):
        if not isinstance(it, dict):
            continue
        code = str(it.get('stage') or it.get('stageCode')
                   or it.get('code') or '').strip()
        try:
            cid = int(it.get('copilotId') or it.get('id') or 0)
        except (TypeError, ValueError):
            cid = 0
        if not (code and cid):
            continue
        fixed[code.upper()] = cid
        w = _VARIANT_ALIASES.get(str(it.get('raid') or it.get('variant')
                                     or '').strip().lower())
        if w:
            wants[code.upper()] = w
        if code.upper() not in {s.upper() for s in stages}:
            stages.append(code)
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
    # 用户点名的关（预检会往里补前置关，比对时要用原样的这一份）
    asked = [s.upper() for s in stages]
    plan = None
    prereq = set()
    with_pre = False
    if params.get('precheck', True):
        # autoPrereq：**默认「不补」**（2026-11-19 用户实测口径）。
        # 以前默认把前置关一起补进计划，用户只点名 S-3/S-4，结果 S-1、S-2
        # 也从头打了一遍（还按突袭打，白烧一倍理智）。他明确要求「我让他挂
        # 的关卡就是他点名的关」，所以补前置改成显式开关：
        #   'auto'（老行为，S/EX/MO 的前置一起补）/ True 全补 / False 不补
        auto = params.get('autoPrereq', False)
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
                # 「缺干员」不等于打不了：MAA 会借助战（support_unit_usage=1），
                # 而作业站最新的作业常常用当期新干员，预检拿它一比就全判缺人，
                # 于是这号明明手动能打，程序却一关都不排（2026-11-19 用户口径：
                # 「我手动跑 MAA 都能正常打」）。只要**全都是缺人**就照排；
                # 练度不足（train）/ 这关没作业 仍然拒绝。
                lack = [b for b in plan['blocked']
                        if '缺' in (b.get('reason') or '')
                        and '练度' not in (b.get('reason') or '')]
                if lack and len(lack) == len(plan['blocked']):
                    log('预检说这几关缺干员（%s），但缺人可以借助战，照排试试'
                        % '、'.join(b['code'] for b in lack))
                    stages = [b['code'] for b in lack]
                else:
                    raise ValueError('这号一关都打不了：\n' + '\n'.join(
                        stageplan.details(plan)) + '\n' + '\n'.join(plan['notes']))
            else:
                stages = plan['playable']
            prereq = {str(i['code']).upper()
                      for i in (plan.get('plan') or []) if i.get('prereq')}
            if prereq:
                log('补前置：%s（只打普通，打通就解锁；不想补就说「只挂我'
                    '点名的关」）' % '、'.join(sorted(prereq)))

    # ---- 关卡名单核对：只能比用户点名的多「前置关」，不能冒出别的编号 ----
    stages = _tidy_stage_list(stages, asked, log)

    # 没让补前置时提醒一句：EX-3 / S-4 这类在游戏里要先通前面的关，点不开
    # 关卡时用户能对上原因（不自动补，是因为默认「点名的关就是全部」）
    if not with_pre:
        need = [a for a in asked
                if stageplan.kind_of(a) in ('ex', 's', 'mo')
                and not re.search(r'-1$', a)]
        if need:
            log('不补前置：%s 在游戏里要先通同分区前面的关；要一起补就说'
                '「把前置一起补」' % '、'.join(need))

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
    # 抄作业必须有作业文件：按 stages 现搜（预检那轮已经拉过作业站，这里命中
    # 缓存，不再发请求）。**不能拿预检挑好的那一条直接用**——预检每关只挑
    # 一份，而且不看要打普通还是突袭（`@both` 那种要两条），得在这里按变体拆。
    # 不给 copilotList 的话队列里 Copilot 会被 core_tasks 跳过（见
    # _prepare_ops 注释），noNav 模式下直接空队列报错。
    if mode == 'copy':
        if params.get('copilotList'):
            spec['copilotList'] = params['copilotList']
        else:
            # 每关多挑几份备选：翻车时 run_core 会自动换下一份重挂
            # （2026-11-19 需求 3），不用整轮重来、也不重打已成功的关
            spec['copilotList'], retry = _prepare_ops(
                stages, file, log, raid=params.get('raid', 'auto'),
                wants=wants, fixed=fixed,
                depth=int(params.get('swapDepth') or 3),
                prereq=prereq)
            if retry:
                spec['retry'] = retry
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

    # ---- 收尾结算：逐关算账（哪几关过了、哪几关翻车、换过几次作业）----
    got_ok = [str(x) for x in ((r or {}).get('stages_ok') or [])]
    got_bad = (r or {}).get('stages_failed') or []
    swaps = (r or {}).get('swaps') or []
    failed = (r or {}).get('failed_stage') or ''
    trail = (r or {}).get('stage_trail') or []
    bad_u = {str(x.get('stage') or '').upper() for x in got_bad}
    ok_u = {x.upper() for x in got_ok}
    trail_u = {str(x).upper() for x in trail}
    rest = [s for s in stages
            if str(s).upper() not in ok_u | bad_u | trail_u]
    if got_ok:
        log('结算：成功 %d 关 —— %s' % (len(got_ok), '、'.join(got_ok)))
    if got_bad:
        for x in got_bad:
            log('结算：%s 翻车 —— %s'
                % (x.get('stage'), (x.get('why') or 'MAA 没给原因')[:60]))
    if swaps:
        log('这次自动换过 %d 次作业（换哪份都记在上面）' % len(swaps))
    if failed and not got_bad:
        why = stageplan.explain_failure((r or {}).get('failed_why') or '')
        log('卡在 %s：%s' % (failed, why or 'MAA 没给原因，看上面的报错'))
    if rest and (got_bad or failed):
        log('没打的关：%s（前面卡住了，这些关排不进去）' % '、'.join(rest))
    elif rest and not (r or {}).get('timed_out') and (r or {}).get('ok'):
        log('这些关没有任何战斗记录，可能被跳过了：%s' % '、'.join(rest))
    elif (r or {}).get('timed_out'):
        log('跑满 %d 分钟还没结束，已停下%s'
            % (int((params.get('timeout') or 2 * 60 * 60) // 60),
               ('；最后一关是 %s' % trail[-1]) if trail else ''))
    elif not (r or {}).get('ok', True) and not got_ok:
        log('一关都没打到就失败了：%s'
            % ((r or {}).get('error') or '看上面的报错')[:120])
    return {'ok': bool((r or {}).get('ok', True)), 'mode': mode, 'stages': stages,
            'account': file, 'noNav': not with_nav,
            'prereq': sorted(prereq),
            'played': got_ok or trail, 'failed': failed,
            'failedReason': stageplan.explain_failure((r or {}).get('failed_why') or ''),
            'stagesOk': got_ok,
            'stagesFailed': [{'stage': x.get('stage'), 'why': x.get('why')}
                             for x in got_bad],
            'swaps': swaps,
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


# ---------------------------------------------------------------- sweep

_SWEEP_TIER_CN = {'base': '普通关', 'ex': 'EX 关', 's': 'S 关',
                  'mo': 'MO 关', 'f': 'F 关'}


def _sweep_segments(keyword, log, tiers=None, raid='both'):
    """活动 → 分段计划：普通关 / EX 普通 / EX 突袭 / S 关 …

    为什么要分段（用户 2026-11-19 需求 4：「挂完普通导航到突袭，再挂突袭的
    作业集合，再导航到 S，再挂 S 关卡」）：EX 和 S 是活动地图里不同的分区，
    MAA 的 Copilot 只认**当前分区**上的关卡名，中间不重新导航就会「找不到
    关卡」（见 ``_run_fight_batch`` 的注释）。同一段内换难度（普通 ↔ 突袭）
    不用重导航——MaaCore 拿到 ``is_raid=true`` 自己点「切换突袭难度」。
    """
    from . import resources
    res = resources.resolve(keyword)
    stages = [s for s in (res.get('stages') or []) if not s.get('story')]
    if not stages:
        raise ValueError('认不出「%s」有哪些关（说活动名或代号前缀，如 覆潮之下 / SV）'
                         % keyword)
    groups = {}
    for s in stages:
        groups.setdefault(stageplan.kind_of(s.get('code')), []).append(s)
    out = []
    for k in (tiers or ['base', 'ex', 's']):
        rows = groups.get(k) or []
        if not rows:
            continue
        codes = [s['code'] for s in rows]
        label = _SWEEP_TIER_CN.get(k, k.upper())
        if k == 'ex' and raid != 'normal':
            if raid in ('both', 'auto'):
                out.append({'name': label + '·普通', 'codes': codes,
                            'nav': codes[0], 'variant': 'normal'})
            out.append({'name': label + '·突袭', 'codes': codes,
                        'nav': codes[0], 'variant': 'raid'})
        else:
            # 险地（15+ 章 #s）在 MaaCore 里就是 is_raid=true
            variant = 'raid' if (k == 's' and raid == 'raid') else 'normal'
            out.append({'name': label, 'codes': codes, 'nav': codes[0],
                        'variant': variant})
    return out, (res.get('title') or keyword)


def skill_sweep(params, log, tid=None):
    """一键扫图：普通关 → EX 普通 → EX 突袭 → S 关，逐段导航 + 挂机

    用户 2026-11-19 的期望 4：「skill 联动可以做到挂完普通导航到突袭，再挂
    突袭的作业集合，再导航到 S，再挂 S 关卡」。三件事这里一起做掉：

    1. **分段**：每段开始前重新导航（EX / S 是不同分区，不导航 MAA 认不出关卡）
    2. **逐段作业池**：每关多挑几份，翻车自动换一份重挂该关，已成功的关不重打
    3. **逐关播报**：``✔ X 战斗成功`` / ``✘ X 翻车：原因``，最后一段结算

    段落可以用 ``tiers`` 挑（base / ex / s / mo / f），``raid`` 控制 EX 段：
    ``both``（默认，普通 + 突袭）/ ``raid``（只突袭）/ ``normal``（只普通）。
    """
    raw = (params.get('stage') or params.get('keyword') or '').strip()
    if not raw:
        raise ValueError('缺 stage（活动名或代号前缀，如 覆潮之下 / SV）')
    file = _account_file(params.get('account') or '')
    d = _default_maa_dir()
    tiers = params.get('tiers')
    if isinstance(tiers, str):
        tiers = [x.strip() for x in tiers.replace('，', ',').split(',')
                 if x.strip()]
    segs, title = _sweep_segments(raw, log, tiers=tiers,
                                  raid=str(params.get('raid') or 'both'))
    if not segs:
        raise ValueError('「%s」里没有可挂的关卡' % title)
    log('扫图计划（%s）：%d 段' % (title, len(segs)))
    for i, seg in enumerate(segs, 1):
        more = (' 等 %d 关' % len(seg['codes']) if len(seg['codes']) > 8 else '')
        log('  %d) %s：%s%s' % (i, seg['name'],
                               '、'.join(seg['codes'][:8]), more))

    tail = (params.get('accountName') or params.get('tail')
            or _account_name_tail(file) or '').strip()
    if params.get('wakeup', True):
        skill_wakeup({'account': file, 'accountName': tail}, log, tid=tid)

    done, bad, all_swaps = [], [], []
    total = params.get('timeout') or 3 * 60 * 60
    for i, seg in enumerate(segs, 1):
        log('=' * 32)
        log('第 %d/%d 段：%s（%d 关）'
            % (i, len(segs), seg['name'], len(seg['codes'])))
        want = {c.upper(): seg['variant'] for c in seg['codes']}
        try:
            # 这一段只要这一档：普通段不会拿突袭作业顶，反之亦然
            picks, pool = _select_ops(seg['codes'], file, log, wants=want,
                                      strict=True, depth=3)
        except ValueError as e:
            log('这一段跳过：%s' % e)
            continue
        rows = maaplan.prepare_copilot(picks, d, log=log)
        if params.get('nav', True):
            # 段与段之间必须重新导航：EX / S 在活动地图里是不同分区
            try:
                skill_navigate({'stage': seg['nav'], 'account': file,
                                'accountName': tail}, log)
            except Exception as e:
                log('导航没成功（%s），这一段先跳过' % e)
                continue
        spec = {'tasks': ['copilot'],
                'copilotList': rows,
                'noNav': True,          # 导航已经由上面做过
                'maaDir': d,
                'accountName': tail,
                'clientType': _client_of(file),
                'copilot': {'formation': params.get('formation', True),
                            'support': True, 'sanityPotion': True},
                'retry': {'pool': pool, 'maxRounds': 3}}
        # 每段分一份时间预算（段数不定，均分最稳；单段至少 10 分钟）
        share = max(600, int(total / max(1, len(segs))))
        try:
            r = maaplan.run_core(spec, tid=tid, timeout=share)
        except Exception as e:
            log('第 %d 段失败：%s: %s' % (i, type(e).__name__, e))
            bad.append({'stage': '、'.join(seg['codes'][:3]) + '…',
                        'why': '%s: %s' % (type(e).__name__, e),
                        'segment': seg['name']})
            continue
        ok = [str(x) for x in (r.get('stages_ok') or [])]
        fail = r.get('stages_failed') or []
        done.extend(ok)
        bad.extend([{'stage': x.get('stage'), 'why': x.get('why'),
                     'segment': seg['name']} for x in fail])
        all_swaps.extend(r.get('swaps') or [])
        log('第 %d 段结束：成功 %d 关%s'
            % (i, len(ok), ('，翻车 %d 关' % len(fail)) if fail else ''))

    log('=' * 32)
    log('扫图结束：成功 %d 关%s'
        % (len(done), ('，翻车 %d 关' % len(bad)) if bad else ''))
    if done:
        log('成功：%s' % '、'.join(done))
    for x in bad:
        log('翻车：%s（%s）—— %s'
            % (x.get('stage'), x.get('segment'),
               (x.get('why') or 'MAA 没给原因')[:50]))
    if all_swaps:
        log('全程自动换过 %d 次作业' % len(all_swaps))
    return {'ok': bool(done) and not bad, 'title': title,
            'segments': len(segs),
            'stagesOk': done, 'stagesFailed': bad, 'swaps': all_swaps,
            'account': file}


# ---------------------------------------------------------------- 小工具（牛杂）

def skill_tool(params, log, tid=None):
    """跑 MAA「小工具 → 牛杂」里的功能：战时新闻委员会 / 活动商店

    两项的共同点：入口都在**当期活动地图**上，点进去之后由 MAA 的 ``Custom``
    任务接手（怎么调见 :mod:`maatools` 顶部注释）。所以这里只有三步：

        1. adb 导航：终端 → 活动地图 → 点入口节点
        2. 交给 MaaCore 跑 Custom 任务
        3. 把「成功没有 / 卡在哪 / 要不要手动过教程」讲清楚

    用户 2026-11-19 的原话：「然后运行 MAA 中小工具牛杂中的这个，并告诉用户
    如果被教程卡住，手动过一下教程再挂一遍」。所以卡住时不能只报失败。
    """
    from . import eventnav, maatools

    key, ent = maatools.resolve(params.get('name') or params.get('tool') or '')
    file = _account_file(params.get('account') or '')
    d = _default_maa_dir()
    log('小工具：%s（MAA 牛杂里的同名项）' % ent.get('name'))

    # ---- 1) 导航到活动地图上的入口 ----
    conn = maaplan._connection(d, None)
    client = _client_of(file)
    t = stagenav.Touch(conn['adb_path'], conn['address'], log,
                       package=stagenav.game_package(client))
    alive, detail = t.ready()
    log('adb：%s' % detail)
    if not alive:
        raise RuntimeError(detail)
    if not t.game_alive():
        raise RuntimeError('游戏没在跑（模拟器连着但客户端没开）——先调 skill_wakeup '
                           '唤醒，再挂')

    prefix = (params.get('prefix') or params.get('activity') or '').strip().upper()
    ok, why = eventnav.run_tool(t, ent.get('nav'), prefix=prefix or None, log=log)
    if not ok:
        raise RuntimeError('导航没到位：%s' % why)
    log(why)

    # ---- 2) 交给 MAA ----
    bl = params.get('blacklist')
    if isinstance(bl, str):
        bl = [x for x in re.split(r'[,，;；、\s]+', bl) if x]
    task = maatools.build_params(key, blacklist=bl if bl else None)
    if key == 'event_shop':
        log('黑名单：%s（这些不买，无限池也不碰）'
            % '、'.join(task['params']['event_shop']['blacklist']))
    if ent.get('tutorial'):
        log('提醒：%s' % ent['tutorial'])

    spec = {'maaDir': d, 'title': ent.get('name'),
            'task': task, 'tutorial': ent.get('tutorial') or '',
            'profile': params.get('profile')}
    r = maaplan.run_custom(spec, tid=tid, timeout=int(ent.get('timeout') or 1800))

    out = {'ok': bool(r.get('ok')),
           'tool': key, 'name': ent.get('name'),
           'account': file,
           'stuck': r.get('stuck') or '',
           'timed_out': bool(r.get('timed_out')),
           'tutorial': r.get('tutorial') or '',
           'why': r.get('error') or ''}
    if not out['ok']:
        if r.get('stuck') or r.get('timed_out'):
            out['hint'] = ('看起来卡住了：%s'
                           % (r.get('tutorial') or '手动看一眼模拟器现在停在哪一页'))
            log('没跑完。%s' % out['hint'])
        else:
            out['hint'] = 'MAA 报错了，看上面几行日志'
            log('没跑完：%s' % (out['why'] or 'MAA 报错'))
    return out


# ---------------------------------------------------------------- 调度

SKILLS = {
    'wakeup':    ('唤醒游戏到主界面（可选按尾号切号）', skill_wakeup),
    'navigate':  ('从主界面导航到指定活动',            skill_navigate),
    'fight':     ('自动战斗：预检练度/前置 → 编队/借人/开打（失败自动换作业）',
                  skill_fight),
    'skip_story': ('过剧情关卡（ST 关）',              skill_skip_story),
    'signin':    ('森空岛签到（单号/全部）',            skill_signin),
    'weekly':    ('周目编排：剧情+战斗按序全自动',      skill_weekly),
    'train':     ('培养干员：精英化/技能/专精，材料不足暂停', skill_train),
    'sweep':     ('一键扫图：普通→突袭→S 逐段导航挂机（失败自动换作业）',
                  skill_sweep),
    'tool':      ('MAA 牛杂小工具：战时新闻委员会 / 活动商店', skill_tool),
}

# 哪些 skill 跑之前要先确认模拟器在。signin 走的是森空岛网页，跟游戏无关，
# 不查——否则用户只想签个到，却被要求开模拟器。这张表另用一个常量而不是
# 塞进 SKILLS 元组：/api/skill/list 和助手的工具列表都直接吃 SKILLS 的形状，
# 加字段会把两处都牵连一遍。
_NEEDS_GAME = frozenset(['wakeup', 'navigate', 'fight', 'skip_story', 'weekly',
                         'train', 'sweep', 'tool'])


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
