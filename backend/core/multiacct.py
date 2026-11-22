# -*- coding: utf-8 -*-
"""多账号编排：一次给左侧多个账号各挂一套作业

MAA 的多账号机制
----------------
MAA 里 **一个配置档案（Configuration）= 一个账号**。档案的 ``TaskQueue[0]``
是 StartUpTask，里面两个字段决定切不切号：

    {"$type": "StartUpTask", "AccountName": "181****5505",
     "AccountSwitchEnabled": true, ...}

``AccountName`` 的取值就是游戏登录界面上显示的那个名字。官服是打码手机号
（``181****5505``），B 服是用户名。**这个信息森空岛给不了、MAA 也给不了**，
只能问用户——这就是绕不开的那个坎。

所以本模块干四件事：

1. ``candidates()``  把左侧账号里能挂的挑出来（只取明日方舟，终末地不掺和）
2. ``plan()``        按账号练度各挑一份能打的作业，算出要建哪些档案
3. ``apply()``       复制档案、改 AccountName、写作业列表
4. ``run()``         串行跑：切档案 → 启动 → 等跑完 → 切下一个

单 MAA 串行 vs 多 MAA 并行
--------------------------
一个模拟器同一时刻只能登一个号，所以「多账号」本质是**串行**：A 跑完 →
退出 → 切号 → B。用户有多个 MAA 目录 + 多个模拟器时才谈得上并行。

默认一律串行。并行只在用户明确有多个 MAA 目录时提供（``mode='parallel'``）。
"""
from __future__ import annotations

import copy
import os
import re
import time

from . import acctname, accounts, asstlog, copilot, maa, settings, tasks, watch

# 官服账号名形如 181****5505：前 3 位 + 中间打码 + 后 4 位
_SAFE = re.compile(r'[\\/:*?"<>|]')
# 一个账号要跑多久的粗估（每关 10 分钟 + 切号开销），只用于超时兜底
MINUTES_PER_STAGE = 10


def norm_account_name(v) -> str:
    """把用户给的东西整理成 MAA 认的 AccountName

    规则统一在 ``acctname.normalize``（这里只是历史入口，别在本模块另写一份）：
    ``1815505`` / ``18112345505`` / ``181****5505`` / ``181＊5505`` /
    ``181 5505`` 都会变成 ``181****5505``；``5505`` 这种片段原样保留
    （MAA 是子串匹配，官方说明允许只给后四位）。
    """
    from . import acctname
    return acctname.normalize(v)


def candidates():
    """左侧账号里能挂机的那些（只取明日方舟）

    终末地账号（``game != 'arknights'``）MAA 根本不管，混进来会让后面
    对着它的练度去找明日方舟作业，结果一片「全部打不了」。
    """
    from . import acctbind

    out = []
    for row in accounts.list_accounts():
        if (row.get('game') or 'arknights') != 'arknights':
            continue
        # 绑了配置文件的账号：切号名以那份档案里的为准（用户 2026-11-22 要求，
        # 规则在 acctbind）。没绑就用手填的，别为此去读 MAA 配置——列表接口
        # 每次启动都会走一遍，能省一次文件读取就省一次。
        nm = row.get('maaAccountName') or ''
        if row.get('maaProfile'):
            try:
                nm, _src = acctbind.resolve(row['file'])
            except Exception:
                pass
        out.append({
            'file': row['file'],
            'name': row.get('alias') or row.get('nickName') or row['file'],
            'nickName': row.get('nickName') or '',
            'channel': row.get('channelName') or '',
            'level': row.get('level'),
            'maaProfile': row.get('maaProfile') or '',
            'accountName': nm,
            'maaDir': row.get('maaDir') or '',
            'progress': row.get('progress') or {},
        })
    return out


def _profile_name(account_name, nick, taken):
    """档案名：一律带 PRTS- 前缀

    以前是直接用 AccountName 当档案名（moren / 136 / 181），好处是跟用户
    自己的命名习惯一致，坏处是**会盖掉他自己那份档案**——他调了半天的基建
    排班、公招标签就没了。现在改成永远新建一份 PRTS-xxx，用户的档案一个
    字段都不动，不想要了在 MAA 里删掉整个档案就行。
    """
    from . import maaplan
    # 档案名是 JSON 的 key，不是文件名，`*` 完全合法（官服的 181****5505 就靠它）
    base = re.sub(r'[\\/:?"<>|\s]', '', str(account_name or nick or '')).strip()
    if not base:
        base = 'acct'
    base = base[:24]
    name, i = maaplan.PROFILE_PREFIX + base, 2
    while name in taken:
        name = '%s%s_%d' % (maaplan.PROFILE_PREFIX, base, i)
        i += 1
    taken.add(name)
    return name


def _snap_of(file):
    return accounts.get_account(file)


def _maa_dir(req=None):
    """本次要操作的 MAA 目录：显式给 → 全局默认 → 第一个账号绑的

    用户可能只在账号里绑了目录、没设全局默认，这时也不能空着。
    """
    d = str((req or {}).get('maaDir') or '').strip()
    if d:
        return d
    d = settings.load().get('maaDir') or ''
    if d:
        return d
    for c in candidates():
        if c.get('maaDir'):
            return c['maaDir']
    return ''


def plan(req):
    """生成多账号方案（纯只读，不碰磁盘、不碰 MAA）

    ``req`` 字段：

    ============  ================================================
    accounts      账号文件名列表；空 = 左侧全部能挂的
    names         {文件: '1815505'} —— 缺的进 ``missing``，要问用户
    keyword       关卡关键词（TL- / 15-3 / act55side / 活动名）
    kind          activity / permanent，只用于提示文案
    limit         每关最多挑几份候选（默认取最优一份）
    limitStages   最多挂几关（防止一口气挂 40 关）
    ============  ================================================

    返回里 ``accounts[].ops`` 是每个账号各关挑中的作业，
    ``missing`` 是必须先问用户的东西。
    """
    cands = candidates()
    want = [str(x) for x in (req.get('accounts') or []) if str(x).strip()]
    if want:
        by_file = {c['file']: c for c in cands}
        picked = [by_file[f] for f in want if f in by_file]
        unknown = [f for f in want if f not in by_file]
    else:
        picked, unknown = cands, []
    if not picked:
        return {'ok': False, 'error': '左侧没有可挂的明日方舟账号',
                'accounts': [], 'missing': [], 'warnings': []}

    # 原样保留用户/助手给的值，归一化与校验统一交给 acctname.check（下面按账号做）。
    # 别在这里先 norm 一遍：两层各自改写同一串，第二层看到的已是半成品，
    # 星号数量被改坏后再去卡严格正则，就成了「用户照文档填却被判非法」。
    names = {str(k): str(v or '').strip() for k, v in (req.get('names') or {}).items()}
    keyword = str(req.get('keyword') or '').strip()
    limit_stages = max(1, min(20, int(req.get('limitStages') or 8)))
    kind = str(req.get('kind') or '').strip().lower()

    # MAA 里已经配好的档案：能直接对上就不用问用户了。
    # 用户通常已经把账号配过一次（档案名 136 / moren / 181 这种），
    # AccountName 和森空岛昵称对得上的就复用，一个字段都不用填。
    md = _maa_dir(req)
    try:
        existing = maa.profiles(md).get('profiles') or []
    except Exception:
        existing = []
    by_account = {}
    for pf in existing:
        an = (pf.get('accountName') or '').strip()
        if an:
            by_account.setdefault(an, pf)

    def _match(c):
        """昵称 → MAA 档案：容忍「示例昵称#6408」对上「示例昵称」"""
        nick = (c.get('nickName') or '').strip()
        for key in (nick, nick.split('#')[0].strip(), (c.get('name') or '').strip()):
            if key and key in by_account:
                return by_account[key]
        return None

    missing, warnings, rows = [], [], []
    if not keyword:
        missing.append({'what': 'keyword',
                        'question': '要挂哪些关？说个关键词就行（例如 TL-、15-3、act55side、或者活动名）'})

    # 已有档案名，避免新档案把用户原来的覆盖掉
    md = _maa_dir(req)
    try:
        taken = {p['name'] for p in maa.profiles(md).get('profiles') or []}
    except Exception:
        taken = set()

    for c in picked:
        nm, why = acctname.check(names.get(c['file']) or '', c.get('channel'))
        reuse = ''
        if not nm:
            # 设置里绑过的直接用（手填的尾号 / 选了配置文件带出来的账号名）：
            # candidates 给的已经是生效值，用户配过一次就不该再问（2026-11-22）
            nm, _ = acctname.check(c.get('accountName') or '', c.get('channel'))
        if not nm:
            # 还没绑：看 MAA 里有没有现成的档案对得上。
            # 注意取的是档案里的 **AccountName**，不是档案名：`maaProfile`
            # 是 moren / 136 这种档案名，拿它当账号名写进 StartUp，MAA 切号
            # 必然失败（切号要的是登录界面那行 181****5505）。
            # 2026-11-17 这里还写着 `or c.get('maaProfile')`，等于把档案名
            # 当账号名用，整轮挂机死在「登录」这一步。
            hit = _match(c)
            if hit:
                nm, _ = acctname.check(hit.get('accountName') or '', c.get('channel'))
                reuse = (hit.get('name') or '') if nm else ''
        if not nm:
            missing.append({
                'what': 'accountName', 'file': c['file'], 'name': c['name'],
                'question': '账号「%s」（%s）的手机号后 4 位是多少？给这四位就行'
                            '（如 5505）；前 3 位 + 后 4 位也行（如 157****3635）。'
                            'MAA 切号用的就是它；B 服填游戏里显示的用户名。%s'
                            % (c['name'], c['channel'] or '未知渠道',
                               ('你给的「%s」%s。' % (names.get(c['file']), why))
                               if names.get(c['file']) and why else ''),
            })
            rows.append({'file': c['file'], 'name': c['name'], 'channel': c['channel'],
                         'accountName': '', 'profile': '', 'ops': [], 'needName': True,
                         'stages': [], 'reuse': False})
            continue
        if reuse:
            # 只是「切号名不用问用户」——MAA 里已有档案的 AccountName 对上了。
            # 但档案仍然另建一份，不动他原来那份。
            prof = _profile_name(nm, c['name'], taken)
        elif nm in taken:
            prof = _profile_name(nm, c['name'], taken)
        else:
            prof = _profile_name(nm, c['name'], taken)
        taken.add(prof)
        rows.append({'file': c['file'], 'name': c['name'], 'channel': c['channel'],
                     'accountName': nm, 'profile': prof, 'ops': [],
                     'needName': False, 'stages': [], 'reuse': bool(reuse),
                     'matchedProfile': reuse})

    if not keyword:
        return {
            'ok': False, 'error': '还差信息才能排计划',
            'keyword': '', 'kind': kind, 'mode': 'serial',
            'accounts': rows, 'missing': missing, 'warnings': warnings,
            'totalStages': 0, 'totalOps': 0,
        }

    # 每个账号搜一次就够：search 会把关键词展开成整段关卡（TL- → 11 关）
    total_ops = 0
    for row in rows:
        if row['needName']:
            continue
        try:
            snap = _snap_of(row['file'])
        except Exception as e:
            warnings.append('账号「%s」的数据读不出来：%s' % (row['name'], e))
            continue
        try:
            res = copilot.search(keyword, snap, pages=1, limit=8)
        except Exception as e:
            warnings.append('账号「%s」搜作业失败：%s' % (row['name'], e))
            continue
        for packed in (res.get('stages') or [])[:limit_stages]:
            st = packed.get('stage') or {}
            best = None
            for op in (packed.get('ops') or []):
                if op.get('mode') != 'blocked':
                    best = op
                    break
            if not best:
                continue
            row['ops'].append({
                'copilotId': best.get('copilotId'),
                'stageCode': st.get('code') or '',
                'stageId': st.get('stageId') or '',
                'title': best.get('title') or '',
                'rating': best.get('rating'),
                'mode': best.get('mode'),
                'difficulty': copilot.difficulty_text_for(
                    best.get('stageName') or '', best.get('flags') or 0)
                if hasattr(copilot, 'difficulty_text_for') else '',
                'variant': st.get('variant'),
            })
            total_ops += 1
        row['stages'] = [o['stageCode'] for o in row['ops']]
        if not row['ops']:
            warnings.append('账号「%s」在这批关卡里一份能打的作业都没找到'
                            '（练度差太多，或者关键词不对）' % row['name'])

    rows = [r for r in rows]
    stages = sorted({o['stageCode'] for r in rows for o in r['ops']})
    dirs = [d for d in (settings.load().get('maaDirs') or []) if d]
    if md and md not in dirs:
        dirs.insert(0, md)
    usable = [d for d in dirs if os.path.isfile(os.path.join(d, 'MAA.exe'))]
    mode = str(req.get('mode') or '').strip().lower()
    if mode not in ('serial', 'parallel'):
        mode = 'serial'
    if mode == 'parallel' and len(usable) < 2:
        warnings.append('只找到 %d 个可用的 MAA 目录，并行跑不起来，改回串行'
                        % len(usable))
        mode = 'serial'

    if kind == 'activity':
        warnings.append('活动关是有时限的，挂之前确认一下还剩几天')
    elif kind == 'permanent':
        warnings.append('常驻关卡不限时，可以随时挂')

    return {
        'ok': bool(total_ops),
        'keyword': keyword, 'kind': kind, 'mode': mode,
        'maaDir': md,
        'accounts': rows, 'missing': missing, 'warnings': warnings,
        'totalStages': len(stages), 'totalOps': total_ops,
        'stages': stages,
        'maaDirs': usable,
        'summary': _summary(rows, len(stages), missing),
    }


def _summary(rows, stage_count, missing):
    usable = [r for r in rows if r.get('ops')]
    if not usable:
        return '没有可执行的作业'
    if missing:
        return '还差 %d 个账号的切号名，补齐后才能挂' % len(missing)
    return '%d 个账号 × %d 关，共 %d 份作业' % (
        len(usable), stage_count, sum(len(r['ops']) for r in usable))


# ---------------------------------------------------------------- 落盘
def apply(plan_data, tid=None):
    """把方案写进 MAA：一个账号一份 PRTS- 档案 + 完整任务队列 + 各自的作业列表

    档案一律**新建**（模板是用户现有的某个档案），所以他的日常配置一点不动。
    任务队列由 ``maaplan`` 拼：开始唤醒 / 自动公招 / 基建换班 / 自动战斗 /
    信用收支 / 领取奖励，外加 ``RunDirectly``（MAA 启动后自己点开始）。
    """
    from . import maaplan

    log = tasks.log_factory(tid) if tid else (lambda *_: None)
    d = plan_data.get('maaDir') or ''
    base = plan_data.get('baseProfile') or ''
    ok_rows = [r for r in plan_data.get('accounts') or [] if r.get('ops')]
    if not ok_rows:
        raise ValueError('没有可写入的账号（每个账号都得至少有一份能打的作业）')

    cfg = maa._load_cfg(d)
    configs = cfg.get('Configurations') or {}
    if not configs:
        raise ValueError('MAA 配置里一个档案都没有，先在 MAA 里配好一个能用的')
    base = base or cfg.get('Current') or ''
    if base not in configs:
        base = next(iter(configs))
    if base.startswith(maaplan.PROFILE_PREFIX):
        base = next((n for n in configs if not n.startswith(maaplan.PROFILE_PREFIX)), base)
    log('以「%s」为模板新建档案（它的日常任务配置会被复制过去）' % base)

    tasks_want = plan_data.get('tasks') or ['startup', 'copilot']
    cop_opt = plan_data.get('copilot') or {}

    written = []
    for r in ok_rows:
        spec = {
            'profile': r['profile'],
            'baseProfile': base,
            'accountName': r['accountName'],
            'switchAccount': True,
            'tasks': tasks_want,
            'stages': plan_data.get('fightStages') or [],
            'copilot': cop_opt,
            'ops': r['ops'],
            'runDirectly': plan_data.get('runDirectly', True),
            'maaDir': d,
        }
        info = maaplan.ensure_profile(spec, d)
        log('建档案「%s」（切号名 %s，%d 个任务）'
            % (info['profile'], r['accountName'], len(info['tasks'])))
        for s in info.get('skipped') or []:
            log('[跳过] %s' % s)
        written.append((info['profile'], r))

    total = 0
    for i, (name, r) in enumerate(written, 1):
        items = [{'copilotId': o['copilotId'], 'stageCode': o['stageCode'],
                  'title': o['title'], 'variant': o.get('variant'),
                  'name': o['stageCode']}
                 for o in r['ops']]
        log('[%d/%d] 给「%s」写 %d 份作业（切号名 %s）'
            % (i, len(written), name, len(items), r['accountName']))
        # auto_start=False：全部写完再统一启动，不然每写一个就拉起一次 MAA；
        # account 带上：写盘时按各账号练度做干员替换（#92）
        res = maa.apply_plan(items, profile=name, mode='replace',
                             auto_start=False, tid=tid, maa_dir=d,
                             account=r.get('file'))
        total += res.get('added') or 0
    log('全部写完，共 %d 份作业；MAA 启动后会自动开始' % total)
    return {'profiles': [n for n, _ in written], 'ops': total}


# ---------------------------------------------------------------- 执行
def run(plan_data, tid=None):
    """串行跑：切档案 → 启动 → 等跑完 → 切下一个

    返回 ``[{profile, accountName, state, stage, resumed, seconds}]``。

    等待靠 watch（M2/M3）：它盯着 asst.log 判完成与卡住，卡住时自动重启续跑。
    这里只负责「什么时候换下一个账号」。
    """
    log = tasks.log_factory(tid) if tid else (lambda *_: None)
    d = plan_data.get('maaDir') or ''
    rows = [r for r in plan_data.get('accounts') or [] if r.get('ops')]
    report = []
    for i, r in enumerate(rows, 1):
        name = r['profile']
        log('=' * 40)
        log('[%d/%d] 切换到账号「%s」（%s，切号名 %s）'
            % (i, len(rows), r['name'], r['channel'] or '未知渠道', r['accountName']))
        t0 = time.time()
        try:
            maa.set_current(name, d)          # 内部会先关掉正在跑的 MAA
            ok = maa.launch(d)
        except Exception as e:
            log('切换失败：%s: %s' % (type(e).__name__, e))
            report.append({'profile': name, 'accountName': r['accountName'],
                           'state': 'failed', 'error': str(e), 'seconds': 0})
            continue
        if not ok:
            log('MAA 没起来，跳过这个账号')
            report.append({'profile': name, 'accountName': r['accountName'],
                           'state': 'failed', 'error': 'MAA 启动失败', 'seconds': 0})
            continue

        w = watch.start(d, profile=name, auto_resume=True,
                        on_log=lambda m: log('  ' + m))
        deadline = time.time() + max(15, MINUTES_PER_STAGE * len(r['ops']) + 10) * 60
        snap = {}
        while time.time() < deadline:
            time.sleep(5)
            snap = w.snap or {}
            st = snap.get('state')
            if st == 'done':
                break
            if st in ('failed', 'stalled') and w.gave_up:
                # 同一关救不回来：别在这个账号上耗着
                log('  「%s」救不回来，跳过这个账号的剩余关卡' % w.gave_up)
                break
        else:
            log('  等了 %d 分钟还没跑完，先跳到下一个账号'
                % int((time.time() - t0) / 60))
        w.stop()
        secs = int(time.time() - t0)
        state = 'done' if snap.get('state') == 'done' else (snap.get('state') or 'timeout')
        log('[%d/%d] 「%s」结束：%s，用了 %d 分 %d 秒，自动恢复 %d 次'
            % (i, len(rows), r['name'], state, secs // 60, secs % 60, w.resume_count))
        report.append({'profile': name, 'accountName': r['accountName'],
                       'state': state, 'stage': snap.get('stage') or '',
                       'resumed': w.resume_count, 'seconds': secs})
    try:
        maa.close(d)
    except Exception:
        pass
    done = sum(1 for x in report if x['state'] == 'done')
    log('=' * 40)
    log('多账号跑完：%d/%d 个账号正常结束' % (done, len(report)))
    return report


# ---------------------------------------------------------------- 进度对照
def coverage(plan_data):
    """方案里每个账号各关的作业，跟森空岛进度对比一下

    只做提示，不改方案：用户说「挂所有账号的 TL 关」时，有的账号其实
    已经打完了，没必要陪着跑一遍。
    """
    out = []
    for r in plan_data.get('accounts') or []:
        try:
            snap = _snap_of(r['file'])
            prog = snap.get('progress') or {}
        except Exception:
            prog = {}
        out.append({'file': r['file'], 'name': r['name'],
                    'main': (prog.get('main') or {}).get('nextCode') or '',
                    'campaignLeft': sum(1 for c in (prog.get('campaign') or [])
                                        if not c.get('full')),
                    'activityLeft': sum(a.get('left') or 0
                                        for a in (prog.get('activities') or []))})
    return out
