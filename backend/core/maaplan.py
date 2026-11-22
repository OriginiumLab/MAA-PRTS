# -*- coding: utf-8 -*-
"""把「用户想怎么挂」翻译成一份完整的 MAA 配置档案 + 任务队列

为什么要建新档案
----------------
用户的档案（136 / 157 / moren …）里是他自己调了半天的基建排班、公招标签、
肉鸽策略。我们往里写东西，他下次打开 MAA 就会发现「我配的任务怎么变了」。
所以这里所有写操作只落在 ``PRTS-`` 开头的档案上，他原有的档案一个字段都不动，
想删的时候在 MAA 里删掉整个档案就行。

任务队列怎么拼
--------------
按类型从模板档案里取「原型」（这样用户已经调好的参数会被保留），再按需要
覆盖我们关心的字段：

- ``Fight`` → ``StagePlan`` 换成目标关卡。MAA 会走 ``StageNavigationBegin``
  那条链（先 OCR 找关卡名、找不到就 ``FullStageNavigation`` 全关卡导航），
  所以「导航到指定关卡」是 MAA 自己做的，我们只要把关卡名写对。
- ``StartUp`` → ``AccountName`` + ``AccountSwitchEnabled``，切号靠它。
  （这是 GUI 任务队列的口径；``core_tasks`` 直连走的是 MaaCore 任务参数，
  只有 ``account_name``，给了就切号——见下面的说明。）
- 模板里没有的类型（``Copilot`` / ``Reclamation``）用内置默认值。

为什么 CopilotTask 只有四个字段
------------------------------
实测 v6.20.1：往 TaskQueue 里写 ``CopilotTask``，MAA 只保留
``$type`` / ``Name`` / ``IsEnable`` / ``TaskType``，其余字段（UseFormation /
LoopTimes…）一律用它的默认值重写。真正能调的参数在 ``<档案>.Copilot``
这个对象里（UseFormation / SelectFormation / LoopTimes / SupportMode），
任务本身只是个「执行这个列表」的开关。

「自动按开始」怎么做的
--------------------
``<档案>.Gui.StartUpSettings.RunDirectly = true`` —— 就是 MAA 设置页里的
「启动后直接开始任务」。再配 ``SkipStartupAutoRunAfterUpdate = false``，
更新后也照跑。MAA 一启动就连模拟器、点开始，不需要人碰界面。

注意 ``Gui`` 是**档案级**的，所以只影响我们新建的那份，用户自己的档案
打开后行为不变。
"""
from __future__ import annotations

import copy
import json
import os
import re
import threading
import time

from . import acctname, maa, maalog, multiproc, settings, tasks

PROFILE_PREFIX = 'PRTS-'
_SAFE = re.compile(r'[\\/:*?"<>|]')

# 结算页「点一下继续」的位置（1280 口径）：MAA 自己的 ClickCorner，
# ``specificRect [1000, 20, 270, 50]`` 的中心。任务定义写明「点屏幕右边不会
# 点到掉落物品，也不会关掉关卡选择」，所以结算页上能推进、已经在地图上点了
# 也不误触（用户 2026-11-22 实测「失败后点屏幕任何位置都能立即返回关卡界面」，
# 取这个「不会误触」的点更稳）。
_RESULT_TAP = (1135, 45)

#: 多开时几条线程都会往 report 里加行，加锁免得丢（也顺手把日志顺序稳住）
_GROUPS_LOCK = threading.Lock()

# 任务清单：key → (TaskType, 中文名, 说明, 是否需要关卡)
# 顺序即「一键长草」里的推荐顺序，也是默认队列的顺序
TASK_DEFS = [
    ('startup',     'StartUp',     '开始唤醒', '启动游戏并登录账号（多账号切号的开关）'),
    ('recruit',     'Recruit',     '自动公招', '自动公开招募，按标签自动选'),
    ('infrast',     'Infrast',     '基建换班', '基建换班 + 收菜 + 无人机加速'),
    ('fight',       'Fight',       '理智作战', '按关卡刷理智，可排多关轮换（MAA 自动导航到该关）'),
    ('copilot',     'Copilot',     '自动战斗', '按作业列表逐关抄作业：自动导航 + 自动编队'),
    ('mall',        'Mall',        '信用收支', '领信用、信用商店、访友'),
    ('award',       'Award',       '领取奖励', '日常/周常奖励、邮件、限时开采'),
    ('reclamation', 'Reclamation', '生息演算', '自动打生息演算'),
    ('roguelike',   'Roguelike',   '自动肉鸽', '自动刷集成战略'),
]

TASK_MAP = {k: {'type': t, 'label': l, 'desc': d} for k, t, l, d in TASK_DEFS}

# 模板档案里没有该类型时用这套默认值（字段照 MAA 6.20.1 的结构抄）
_DEFAULTS = {
    'StartUp': {
        '$type': 'StartUpTask', 'AccountName': '', 'AccountSwitchEnabled': False,
        'Name': '开始唤醒', 'IsEnable': True, 'TaskType': 'StartUp',
    },
    'Recruit': {
        '$type': 'RecruitTask', 'MaxTimes': 4, 'ExtraTagMode': 0,
        'Level3PreferTags': [], 'PreferTagEnabled': True,
        'PreserveTagList': ['支援机械'], 'PreserveTagEnabled': True,
        'RefreshLevel3': True, 'ForceRefresh': True, 'Level3Choose': True,
        'Level3RecruitmentPermitReserveEnabled': False,
        'Level3RecruitmentPermitReserve': 8,
        'Level4Choose': True, 'Level5Choose': False, 'Level6Choose': False,
        'Level3Time': 540, 'Level4Time': 540,
        'Name': '自动公招', 'IsEnable': True, 'TaskType': 'Recruit',
    },
    'Infrast': {
        '$type': 'InfrastTask', 'Mode': 'Normal', 'UsesOfDrones': 'CombatRecord',
        'DormThreshold': 30, 'DormTrustEnabled': False,
        'OriginiumShardAutoReplenishment': True, 'DormFilterNotStationed': True,
        'ReceptionMessageBoard': True, 'ReceptionClueExchange': True,
        'SendClue': True, 'ContinueTraining': False, 'UseWeeklySchedule': False,
        'CustomFileType': 'user_defined', 'Filename': '', 'PlanSelect': -1,
        'RoomList': [{'Room': r} for r in
                     ('Mfg', 'Trade', 'Control', 'Power', 'Reception',
                      'Office', 'Dorm', 'Processing', 'Training')],
        'Name': '基建换班', 'IsEnable': True, 'TaskType': 'Infrast',
    },
    'Fight': {
        '$type': 'FightTask', 'UseMedicine': False, 'MedicineCount': 2,
        'UseStone': False, 'StoneCount': 5, 'EnableTargetDrop': False,
        'DropId': '', 'DropCount': 5, 'IsInventoryTarget': False,
        'EnableTimesLimit': False, 'TimesLimit': 4, 'Series': 0,
        'StagePlan': [], 'IsDrGrandet': False, 'UseExpiringMedicine': True,
        'MedicineExpireDays': 2, 'UseExpireMedicineForActivity': False,
        'UseCustomAnnihilation': False, 'AnnihilationStage': 'Annihilation',
        'HideUnavailableStage': True, 'IsStageManually': False,
        'UseOptionalStage': False, 'UseStoneAllowSave': False,
        'HideSeries': False, 'StageResetMode': 'Current',
        'UseWeeklySchedule': False,
        'Name': '理智作战', 'IsEnable': True, 'TaskType': 'Fight',
    },
    # 只写 MAA 认的四个字段，其余交给它自己补默认值
    'Copilot': {
        '$type': 'CopilotTask', 'Name': '自动战斗',
        'IsEnable': True, 'TaskType': 'Copilot',
    },
    'Mall': {
        '$type': 'MallTask', 'Shopping': True, 'CreditFight': True,
        'CreditFightFormation': 1, 'CreditFightOnceADay': True,
        'VisitFriends': True, 'VisitFriendsOnceADay': False,
        'FirstList': '招聘许可', 'BlackList': '碳;家具;加急许可',
        'ShoppingIgnoreBlackListWhenFull': False, 'OnlyBuyDiscount': False,
        'ReserveMaxCredit': False,
        'Name': '信用收支', 'IsEnable': True, 'TaskType': 'Mall',
    },
    'Award': {
        '$type': 'AwardTask', 'Award': True, 'Mail': True, 'FreeGacha': False,
        'Orundum': True, 'Mining': True, 'SpecialAccess': True,
        'SignInEvent': False,
        'Name': '领取奖励', 'IsEnable': True, 'TaskType': 'Award',
    },
    'Reclamation': {
        '$type': 'ReclamationTask',
        'Name': '生息演算', 'IsEnable': True, 'TaskType': 'Reclamation',
    },
}

# 不懂 MAA 的人最常要的那几套，直接给 preset，省得模型自己拼
PRESETS = [
    {'id': 'daily', 'label': '日常全清',
     'desc': '开始唤醒 → 自动公招 → 基建换班 → 信用收支 → 领取奖励',
     'tasks': ['startup', 'recruit', 'infrast', 'mall', 'award']},
    {'id': 'daily_fight', 'label': '日常 + 刷材料',
     'desc': '日常全清，中间插一段理智作战刷指定关卡',
     'tasks': ['startup', 'recruit', 'infrast', 'fight', 'mall', 'award']},
    {'id': 'copy', 'label': '只抄作业',
     'desc': '开始唤醒 → 自动战斗（按作业列表逐关抄）',
     'tasks': ['startup', 'copilot']},
    {'id': 'copy_full', 'label': '日常 + 抄作业',
     'desc': '开始唤醒 → 自动公招 → 基建换班 → 自动战斗 → 信用收支 → 领取奖励',
     'tasks': ['startup', 'recruit', 'infrast', 'copilot', 'mall', 'award']},
]


# ---------------------------------------------------------------- 小工具
def capabilities(maa_dir=None):
    """这台 MAA 支持配什么：任务清单 + 预设 + 模板档案里已有哪些原型

    ``available`` 为 False 的类型说明模板档案里没有原型、我们也没有内置默认，
    硬加进去 MAA 可能不认，所以先告诉调用方别选。
    """
    out = {'tasks': [], 'presets': PRESETS, 'maaDir': maa_dir or ''}
    prototypes = set()
    try:
        cfg = maa._load_cfg(maa_dir)
        prof = (cfg.get('Configurations') or {}).get(cfg.get('Current') or '') or {}
        prototypes = {t.get('TaskType') for t in (prof.get('TaskQueue') or [])}
    except Exception:
        pass
    for key, ttype, label, desc in TASK_DEFS:
        out['tasks'].append({
            'id': key, 'type': ttype, 'label': label, 'desc': desc,
            'available': ttype in prototypes or ttype in _DEFAULTS,
            'needsStage': ttype == 'Fight',
            'fromTemplate': ttype in prototypes,
        })
    out['prototypes'] = sorted(p for p in prototypes if p)
    out['coreVersion'] = core_version(out['maaDir'])
    return out


def core_version(maa_dir=None):
    """MaaCore 的版本号（拿不到就空串）

    放在这里是因为「到底能不能直连」是排查挂机问题的第一件事：GUI 是 6.20.1
    但核心被单独更新过，版本对不上时 ``AsstAppendTask`` 会静默不收。
    """
    if not maa_dir:
        return ''
    eng = None
    try:
        from . import maacore
        eng = maacore.Engine(maa_dir)
        eng.open()
        return eng.version()
    except Exception:
        return ''
    finally:
        try:
            if eng:
                eng.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 建队列
def _proto(template_queue, task_type):
    """从模板队列里取该类型的原型（同类型有多个时取第一个启用的）"""
    rows = [t for t in (template_queue or []) if t.get('TaskType') == task_type]
    return next((t for t in rows if t.get('IsEnable')), rows[0] if rows else None)


def _clean_stage(v):
    """关卡名归一：去掉多余空白，突袭后缀 #f# 去掉（那是作业的口径）"""
    s = str(v or '').strip()
    return re.sub(r'#f#$', '', s, flags=re.I)


def build_queue(spec, template_queue=None):
    """按 spec 拼任务队列

    spec 里关心的键：
    - ``tasks``   : ['startup','recruit',…] 有序，不在里面的任务直接不生成
    - ``stages``  : Fight 的关卡列表（代号，如 TL-5 / 1-7）
    - ``stagesPerRound`` : 每关刷几次（Fight.TimesLimit），不给就不限
    - ``accountName`` : StartUp 的切号名
    - ``switchAccount`` : 是否开切号（默认有 accountName 就开）
    """
    want = [k for k in (spec.get('tasks') or []) if k in TASK_MAP]
    if not want:
        raise ValueError('一个任务都没选，至少要「开始唤醒」才能挂')
    if 'startup' not in want:
        want.insert(0, 'startup')          # 没有唤醒就跑不了，强制补上

    stages = [_clean_stage(s) for s in (spec.get('stages') or []) if _clean_stage(s)]
    per_round = spec.get('stagesPerRound')
    acc = acctname.normalize(spec.get('accountName') or '')
    switch = spec.get('switchAccount')
    switch = bool(acc) if switch is None else bool(switch)

    out, skipped = [], []
    for key in want:
        ttype = TASK_MAP[key]['type']
        proto = _proto(template_queue, ttype)
        if proto:
            row = copy.deepcopy(proto)
        elif ttype in _DEFAULTS:
            row = copy.deepcopy(_DEFAULTS[ttype])
        else:
            skipped.append('%s（模板档案里没有，MAA 可能不认）' % TASK_MAP[key]['label'])
            continue

        row['IsEnable'] = True
        row['TaskType'] = ttype
        row.setdefault('Name', TASK_MAP[key]['label'])

        if ttype == 'StartUp':
            if acc:
                row['AccountName'] = acc
            row['AccountSwitchEnabled'] = switch
        elif ttype == 'Fight':
            if stages:
                row['StagePlan'] = list(stages)
            if per_round:
                row['EnableTimesLimit'] = True
                row['TimesLimit'] = int(per_round)
        elif ttype == 'Copilot':
            # **不能进 GUI 的任务队列**：MAA 的 TaskQueueViewModel.InitializeItems
            # 不认识 CopilotTask，塞进去会抛 NotImplementedException，整个任务
            # 列表变空白（连别的任务都看不见）。抄作业只写 Copilot.TaskList，
            # 真正要它自动跑得走 maacore 直连（见本文件下半部分）。
            skipped.append('自动战斗（MAA 任务队列不支持，走直连执行）')
            continue
        out.append(row)
    return out, skipped


# ---------------------------------------------------------------- 档案
def _profile_name(spec):
    """档案名：PRTS-<用户给的> / PRTS-<切号名> / PRTS-挂机

    只剔掉 JSON/文件名里真正有害的字符。`*` 要留着——官服的切号名就是
    ``181****5505``，抹掉星号会跟 AccountName 对不上，用户看着也别扭。
    """
    raw = (spec.get('profile') or '').strip()
    if not raw:
        raw = (spec.get('accountName') or '').strip() or '挂机'
    raw = raw[len(PROFILE_PREFIX):] if raw.startswith(PROFILE_PREFIX) else raw
    raw = re.sub(r'[\\/:?"<>|\s]', '', raw).strip()[:20] or '挂机'
    return PROFILE_PREFIX + raw


def _set_run_directly(profile_cfg, on=True):
    """把「启动后直接开始」写进档案自己的 Gui 设置

    这是「自动按开始」的唯一开关：MAA 启动 → 连模拟器 → 直接点开始。
    不动的话它停在主界面等人工点。
    """
    gui = profile_cfg.setdefault('Gui', {})
    su = gui.setdefault('StartUpSettings', {})
    su['RunDirectly'] = bool(on)
    # 更新后默认会跳过自动运行，一并关掉，否则每次更新完就哑了
    su['SkipStartupAutoRunAfterUpdate'] = False
    return su


def _set_copilot_opts(profile_cfg, spec):
    """写 <档案>.Copilot 的全局参数（真正生效的那几个）"""
    cop = profile_cfg.setdefault('Copilot', {})
    opt = spec.get('copilot') or {}
    if 'formation' in opt:
        cop['UseFormation'] = bool(opt.get('formation'))
    if 'loopTimes' in opt:
        try:
            cop['LoopTimes'] = max(1, min(99, int(opt.get('loopTimes'))))
        except (TypeError, ValueError):
            pass
    if 'support' in opt:
        cop['SupportMode'] = 'Never' if opt.get('support') is False else 'WhenNeeded'
    return cop


def ensure_profile(spec, maa_dir=None):
    """建/覆盖我们的档案（不动用户原有档案），返回档案名

    只写任务队列和几个开关，作业列表由调用方（maa.apply_plan）填，
    这样「只跑日常、不抄作业」的场景也能共用这条路。
    """
    d = maa_dir or spec.get('maaDir') or settings.load().get('maaDir') or ''
    cfg = maa._load_cfg(d)
    configs = cfg.get('Configurations') or {}
    if not configs:
        raise ValueError('这个 MAA 里一个配置档案都没有，先在 MAA 里配好一个能连上的')

    base = spec.get('baseProfile') or cfg.get('Current') or ''
    if base not in configs:
        base = next(iter(configs))
    if base.startswith(PROFILE_PREFIX):
        # 模板不能是上一轮我们自己建的档案：那样会越叠越脏
        base = next((n for n in configs if not n.startswith(PROFILE_PREFIX)), base)

    name = _profile_name(spec)
    tpl = configs[base]
    prof = copy.deepcopy(tpl)

    queue, skipped = build_queue(spec, tpl.get('TaskQueue'))
    prof['TaskQueue'] = queue
    _set_run_directly(prof, spec.get('runDirectly', True))
    _set_copilot_opts(prof, spec)
    # 作业列表先清空：要抄作业的话由 apply_plan 填，不抄就是空的（Copilot 任务已剔除）
    prof.setdefault('Copilot', {})['TaskList'] = []

    configs[name] = prof
    cfg['Configurations'] = configs
    cfg['Current'] = name
    maa._save_cfg(cfg, d)
    return {'profile': name, 'base': base, 'tasks': [t['TaskType'] for t in queue],
            'skipped': skipped, 'maaDir': d}


def remove_profile(profile, maa_dir=None):
    """删掉我们建的档案（只允许删 PRTS- 开头的）"""
    if not str(profile or '').startswith(PROFILE_PREFIX):
        raise ValueError('只删本程序建的档案（%s 开头的），你自己的档案请在 MAA 里删'
                         % PROFILE_PREFIX)
    d = maa_dir or settings.load().get('maaDir') or ''
    cfg = maa._load_cfg(d)
    configs = cfg.get('Configurations') or {}
    if profile not in configs:
        raise ValueError('档案不存在：%s' % profile)
    configs.pop(profile)
    if cfg.get('Current') == profile:
        cfg['Current'] = next(iter(configs), '')
    maa._save_cfg(cfg, d)
    return True


# ---------------------------------------------------------------- 全流程
def apply(spec, tid=None, start=True):
    """建档案 → 写作业 → 启动（MAA 自己点开始）

    返回 ``{profile, tasks, ops, started}``。耗时的大头是下载作业，
    所以调用方应该把它丢后台线程里跑（agent 工具就是这么做的）。
    """
    log = tasks.log_factory(tid) if tid else (lambda *_: None)
    d = spec.get('maaDir') or settings.load().get('maaDir') or ''

    ops = spec.get('ops') or []
    log('准备档案：%s' % (spec.get('profile') or '（自动命名）'))
    info = ensure_profile(spec, d)
    name = info['profile']
    log('模板档案「%s」→ 新档案「%s」' % (info['base'], name))
    log('任务队列：%s' % ' → '.join(
        TASK_MAP[k]['label'] for k in (spec.get('tasks') or []) if k in TASK_MAP))
    for s in info.get('skipped') or []:
        log('[跳过] %s' % s)

    added = 0
    if ops:
        items = [{'copilotId': int(o['copilotId']),
                  'stageCode': o.get('stageCode') or str(o['copilotId']),
                  'title': o.get('title') or '',
                  'variant': o.get('variant'),
                  'name': o.get('name') or o.get('stageCode')}
                 for o in ops]
        log('写作业列表：%d 份' % len(items))
        # auto_start=False：先把档案写完整，最后统一启动一次
        res = maa.apply_plan(items, profile=name, mode='replace',
                             auto_start=False, tid=tid, maa_dir=d)
        added = res.get('added') or 0
        log('作业已写入 %d 条' % added)

    started = False
    if start:
        log('启动 MAA（启动后会自动点开始）…')
        try:
            started = maa.launch(d)
            log('MAA 已启动' if started else 'MAA 启动后没检测到进程，请手动打开')
        except Exception as e:
            log('启动失败：%s' % e)
            raise
    return {'profile': name, 'tasks': info['tasks'], 'base': info['base'],
            'ops': added, 'started': started,
            'runDirectly': bool(spec.get('runDirectly', True))}


def start_apply(spec, start=True):
    """后台版本：前端/agent 拿到 taskId 就能轮询日志"""
    holder = {}

    def go():
        try:
            tasks.finish(holder['tid'], result=apply(spec, holder['tid'], start))
        except Exception as e:
            import traceback
            tasks.finish(holder['tid'], error='%s: %s' % (type(e).__name__, e),
                         result={'traceback': traceback.format_exc()})

    import threading
    tid = tasks.create('建档案并启动（%s）' % (spec.get('profile') or '挂机'))
    holder['tid'] = tid
    threading.Thread(target=go, daemon=True, name='maaplan-apply').start()
    return tid


def describe(profile=None, maa_dir=None):
    """看一个档案现在配了什么（给前端/助手解释用）"""
    cfg = maa._load_cfg(maa_dir)
    configs = cfg.get('Configurations') or {}
    name = profile or cfg.get('Current') or ''
    c = configs.get(name) or {}
    gui = (c.get('Gui') or {}).get('StartUpSettings') or {}
    queue = []
    for t in (c.get('TaskQueue') or []):
        tt = t.get('TaskType')
        row = {'type': tt, 'enable': bool(t.get('IsEnable')),
               'label': TASK_MAP.get(tt.lower(), {}).get('label') or t.get('Name') or tt}
        if tt == 'StartUp':
            row['accountName'] = t.get('AccountName') or ''
            row['switchEnabled'] = bool(t.get('AccountSwitchEnabled'))
        if tt == 'Fight':
            row['stages'] = list(t.get('StagePlan') or [])
        queue.append(row)
    cop = c.get('Copilot') or {}
    return {'profile': name, 'isOurs': name.startswith(PROFILE_PREFIX),
            'runDirectly': bool(gui.get('RunDirectly')),
            'queue': queue,
            'ops': len(cop.get('TaskList') or []),
            'formation': bool(cop.get('UseFormation')),
            'loopTimes': cop.get('LoopTimes') or 1}


# ================================================================ 直连执行
# 下面这条线走 MaaCore（见 maacore.py），因为 GUI 的任务队列不支持 Copilot。
# 好处是能抄作业、能跑 Custom 小活动、能自己按开始；代价是 MAA 界面不会显示
# 进度 —— 进度由我们的跑机面板给。

CLIENT_TYPES = {
    '官服': 'Official', 'official': 'Official', '': 'Official',
    'b服': 'Bilibili', 'bilibili': 'Bilibili', 'bilibili服': 'Bilibili',
    'bilibili服（国际）': 'Bilibili',
    '日服': 'YoStarJP', '美服': 'YoStarEN', '韩服': 'YoStarKR',
    '台服': 'txwy', '国际服': 'YoStarEN',
}


def client_type_of(channel):
    """渠道名 → MaaCore 的 client_type"""
    k = str(channel or '').strip().lower()
    for name, code in CLIENT_TYPES.items():
        if name and name.lower() == k:
            return code
    if 'bili' in k:
        return 'Bilibili'
    if '日' in k or 'jp' in k:
        return 'YoStarJP'
    if '美' in k or 'en' in k:
        return 'YoStarEN'
    if '韩' in k or 'kr' in k:
        return 'YoStarKR'
    if '台' in k or 'txwy' in k:
        return 'txwy'
    return 'Official'


def _copilot_dir(maa_dir):
    return os.path.join(maa_dir, 'config', 'copilot')


def prepare_copilot(ops, maa_dir, log=None, account=None):
    """下载并落盘作业，返回 MaaCore 的 copilot_list 条目

    ``filename`` **必须是绝对路径**（实测：相对路径不报错，但任务静默不入队）。

    ``account`` 是账号文件名：给了就按该账号练度做干员替换（#92）再落盘。

    ``stage_name`` 是 **导航名**，必须是**关卡代号**（``TL-S-1`` / ``1-7``），
    不是作业里的关卡内部 id。看 MaaCore 的 MultiCopilotTaskPlugin::_run：

        Task.get(config.nav_name + "@Copilot@ClickStageName")->text = { config.nav_name };
        ProcessTask(*this, { config.nav_name + "@Copilot@StageNavigationBegin" }).run();

    它拿 nav_name 去拼 pipeline 名字、再塞进 ClickStageName 的 OCR 文本里。
    传内部 id（``act55side_s01``）拼出来的 pipeline 不存在、OCR 也永远匹配不
    到界面上显示的「TL-S-1」，于是导航一路滑到底就失败了 —— 抄作业「点了没
    反应」的真正原因。作业文件里的 stageId 由 MaaCore 自己从 json 里读
    （``Copilot.get_stage_name()``），不需要我们传。
    """
    from . import copilot

    log = log or (lambda *_: None)
    owned = copilot.owned_of(account)
    os.makedirs(_copilot_dir(maa_dir), exist_ok=True)
    rows, done = [], {}
    for it in ops or []:
        cid = int(it['copilotId'])
        code = it.get('stageCode') or str(cid)
        if cid not in done:
            content, flags, sname, notes, pre = copilot.fetch_prepared(cid)
            sub_notes = []
            if owned:
                content, sub_notes = copilot.apply_substitutions(content, owned)
            fname = '%s_%s.json' % (_SAFE.sub('_', code).strip() or 'stage', cid)
            path = os.path.normpath(os.path.join(_copilot_dir(maa_dir), fname))
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(content, f, ensure_ascii=False, indent=2)
            done[cid] = (path, sname, flags)
            log('%s作业 #%s %s' % ('取预下载的' if pre else '下载', cid,
                                   it.get('title') or ''))
            for n in notes or []:
                log('    修正：%s' % n)
            for n in sub_notes or []:
                log('    替换：%s' % n)
        path, sname, flags = done[cid]
        nav = _clean_stage(code)
        variants = copilot.entry_variants(flags, it.get('variant'), sname)
        for raid in variants:
            # 不再给突袭条目自动垫一条普通（2026-11-19 用户实测纠正）：
            # MaaCore 拿到 is_raid=true 会自己点 ChangeToRaidDifficulty 切难度
            # （asst.log 01:42 实证），垫普通只会让用户看到「又从头打了一遍
            # 普通 EX-1」。普通真没通关时要一起打通，由调用方显式要 both
            # （skill_fight 的 stages 写法 SV-EX-1@both）。
            if raid and not str(sname).endswith('#s'):
                log('    %s 打突袭（MAA 自己切突袭难度；普通没通关请用 @both）' % nav)
            rows.append({'filename': path, 'stage_name': nav,
                         'is_raid': bool(raid)})
    return rows


def _copilot_params(rows, spec):
    """Copilot 任务的参数（首轮和「换作业重挂」共用一份，别两处各写一遍）

    用户在 MAA「自动战斗」页常勾的三项默认都开：
    formation 自动编队 / support_unit_usage 借干员（1=仅缺人时）/
    use_sanity_potion 理智不够时吃药。
    """
    form = spec.get('copilot') or {}
    try:
        support = int(form.get('support', 1))
    except (TypeError, ValueError):
        support = 1
    return {'copilot_list': rows,
            'formation': bool(form.get('formation', True)),
            'add_trust': False,
            'ignore_requirements': False,
            'support_unit_usage': support if support in (0, 1, 2) else 1,
            'use_sanity_potion': bool(form.get('sanityPotion', True)),
            'formation_index': 1}


def core_tasks(spec, maa_dir=None):
    """spec → MaaCore 的 ``[(任务类型, 参数), …]``

    这块跟 ``build_queue`` 是两条路：那个写给 GUI 的 TaskQueue（能看界面，
    但抄不了作业），这个直接喂核心（能抄作业 / 小活动 / 自己按开始）。
    """
    d = maa_dir or spec.get('maaDir') or ''
    want = [k for k in (spec.get('tasks') or [])
            if k in TASK_MAP or k == 'operprogress']
    # noStartUp：培养干员时用户说「不用切号、游戏已经开着」，可以跳过唤醒——
    # OperProgress 自己会走 OperBoxBegin 从任意页面进干员列表（见 MaaCore 的
    # OperProgressProcessTask::find_and_open_operator）
    if 'startup' not in want and not spec.get('noStartUp'):
        want.insert(0, 'startup')

    stages = [_clean_stage(s) for s in (spec.get('stages') or []) if _clean_stage(s)]
    acc = acctname.normalize(spec.get('accountName') or '')
    switch = spec.get('switchAccount')
    switch = bool(acc) if switch is None else bool(switch)
    client = spec.get('clientType') or client_type_of(spec.get('channel'))
    form = (spec.get('copilot') or {})
    per_round = spec.get('stagesPerRound')

    # 每关打几次（MaaCore 的 Fight.times）：
    # - 用户说了次数就照办
    # - 只挂一关：刷到理智不足为止（MAA 的 FightTimesTaskPlugin 到 999 之前就会
    #   因为理智不够自己停，日志里能看到 SanityBeforeStage）
    # - 挂了多关：每关打一遍。否则第一关就把理智吃光，后面七关形同虚设
    if per_round:
        times = max(1, int(per_round))
    else:
        times = 999 if len(stages) <= 1 else 1

    out = []
    for key in want:
        if key == 'operprogress':
            # 干员培养（MaaCore 6.20+ 的 OperProgress）：plans 直接原样喂，
            # 字段口径见 skills._train_plans（name / role / elite /
            # skill_level / skill_mastery，专精是长度 3 的数组）
            plans = spec.get('plans') or []
            if plans:
                out.append(('OperProgress', {'plans': plans}))
            continue
        ttype = TASK_MAP[key]['type']
        if ttype == 'StartUp':
            p = {'client_type': client, 'start_game_enabled': True}
            # MaaCore 的 StartUpTask::set_params 只认 account_name /
            # client_type / start_game_enabled 三个键：**给了非空
            # account_name 就切号**，没有 account_switch_enabled 这个参数
            # （核对过 6.20.1 源码，塞进去只会被原样忽略）
            if acc and switch:
                p['account_name'] = acc
            out.append(('StartUp', p))
        elif ttype == 'Fight':
            for st in (stages or []):
                p = {'stage': st, 'medicine': 0, 'stone': 0,
                     'times': times, 'series': 0}
                out.append(('Fight', p))
        elif ttype == 'Copilot':
            rows = spec.get('copilotList') or []
            if not rows:
                continue
            out.append(('Copilot', _copilot_params(rows, spec)))
        elif ttype == 'Recruit':
            out.append(('Recruit', {'max_times': 4, 'refresh': True,
                                    'select': [3, 4], 'confirm': [3, 4],
                                    'expedite': False}))
        elif ttype == 'Infrast':
            out.append(('Infrast', {
                'mode': 0,
                'facility': ['Mfg', 'Trade', 'Control', 'Power', 'Reception',
                             'Office', 'Dorm', 'Processing', 'Training'],
                'drones': 'Money', 'threshold': 0.3, 'replenish': True}))
        elif ttype == 'Mall':
            out.append(('Mall', {'shopping': True, 'buy_first': ['招聘许可'],
                                 'blacklist': ['碳', '家具', '加急许可'],
                                 'force_shopping_if_credit_full': True,
                                 'visit_friends': True}))
        elif ttype == 'Award':
            out.append(('Award', {'award': True, 'mail': True, 'recruit': True,
                                  'orundum': True, 'mining': True,
                                  'specialaccess': True}))
        elif ttype == 'Roguelike':
            out.append(('Roguelike', {}))
        elif ttype == 'Reclamation':
            out.append(('Reclamation', {'theme': 1, 'mode': 0}))
    return out


def _connection(maa_dir, profile=None):
    """从 MAA 配置里抄连接参数（ADB 路径 / 地址 / 连接方式）

    用户已经在 MAA 里调通过一次连接的，直接复用，不用再问一遍。
    """
    cfg = maa._load_cfg(maa_dir)
    configs = cfg.get('Configurations') or {}
    name = profile or cfg.get('Current') or ''
    c = configs.get(name) or (next(iter(configs.values())) if configs else {}) or {}
    cs = ((c.get('Gui') or {}).get('ConnectSettings') or {})
    return {
        'adb_path': cs.get('AdbPath') or 'adb',
        'address': cs.get('Address') or '127.0.0.1:5555',
        'config': cs.get('Config') or 'General',
        'touch': cs.get('TouchMode') or 'MiniTouch',
        'maa_dir': maa_dir,          # 导航失败时往它的 debug/ 存现场截图
    }


# ---------------------------------------------------------------- 分段导航
# MaaCore 的多关 Copilot 是**一条任务链里连着跑**的，关卡之间它只会
# ``ClickStageName`` 在**当前这一页**上 OCR 找关卡名。它不会去点活动地图上的
# 分区节点（圣巡之旅 / 变革之路 / 喀兰之心），也不会返回上一层换页。所以打完
# OS-10 之后界面还停在基础区列表，OS-EX-1 根本不在这一页上——它会一直找，
# 整轮卡死在那儿（2026-11-22 用户实测：「挂完 OS-10 后，没有切换我们的导航到
# EX 的关卡界面，就直接挂 OS-EX-1 了，导致 MAA 一直找不到」）。
#
# 解法：把作业按「活动 + 分区」切成若干段，段与段之间停一下 MAA，点掉结算页、
# 重新导航到下一段的分区，再把那一段排进去。多花 10~15 秒，换「作业永远排在
# 它该在的那一页上」。
def _zone_of(stage_code):
    """关卡代号 → 分区类型（``base`` / ``ex`` / ``s`` / ``mo``）

    **口径必须和 ``eventnav.run`` 选分区节点时完全一致**：那边也是
    ``zone_kind`` 算完再套一份白名单，``st``（剧情关）/``f``（额外关）都归
    基础区——它们在游戏里本来就待在基础区列表里。这里如果另分一档出来，就会
    把作业切成「没标定过分区节点」的段，导航必然失败。
    """
    try:
        from . import stagenav
        k = stagenav.zone_kind(stage_code) or 'base'
    except Exception:
        k = 'base'
    return k if k in ('base', 'ex', 's', 'mo') else 'base'


def _seg_key(stage_code):
    """一关属于哪一段：``活动前缀/分区``，例如 ``OS/ex``、``/base``（主线）"""
    try:
        from . import eventnav
        pre = eventnav.prefix_of(stage_code) or ''
    except Exception:
        pre = ''
    return '%s/%s' % (pre, _zone_of(stage_code))


def segments_of(rows):
    """作业列表 → 按「活动 + 分区」切好的段

    每段是 ``{'key', 'rows', 'from', 'to'}``；``rows`` 原样保留（喂给
    ``_copilot_params`` 的就是它）。

    **只合并连续的同类，绝不重排**：用户要是把顺序排成 OS-1 → OS-EX-1 →
    OS-2，那就老实切三段来回跑。偷偷重排等于替他改打关顺序，出问题全是
    「为什么先打了后面那关」这种没法解释的疑问。
    """
    segs = []
    for r in (rows or []):
        code = str((r or {}).get('stage_name') or '').strip()
        if not code:
            continue
        key = _seg_key(code)
        if segs and segs[-1]['key'] == key:
            segs[-1]['rows'].append(r)
            continue
        segs.append({'key': key, 'rows': [r]})
    for s in segs:
        s['from'] = str(s['rows'][0].get('stage_name') or '')
        s['to'] = str(s['rows'][-1].get('stage_name') or '')
    return segs


def _seg_label(seg):
    """日志里怎么写这一段：``OS 基础区 10 关（OS-1~OS-10）``"""
    pre, _, kind = (seg.get('key') or '/base').partition('/')
    name = {'base': '基础区', 'ex': 'EX 区', 's': 'S 区',
            'mo': 'MO 区'}.get(kind, kind.upper() or '基础区')
    a, b = seg.get('from') or '?', seg.get('to') or '?'
    span = a if a == b else '%s~%s' % (a, b)
    head = ('%s %s' % (pre, name)) if pre else name
    return '%s %d 关（%s）' % (head, len(seg.get('rows') or []), span)


def _navigate(conn, stage, log, client=None, strict=False):
    """把界面带到关卡列表，返回 ``(ok, 说明)``

    导航要连点好几下，而 MaaCore 的 ``AsstAsyncClick`` 一次连接只能点中第一下
    （实测），所以点击/截图都走 adb，见 ``stagenav.Touch``。B 服账号要传
    B 服的包名，否则「游戏进程不在」的检测永远为真。

    失败会往 ``<MAA>/debug/nav-fail-<关卡>.png`` 存一张现场截图：MAA 自己的
    日志只会说「滑到底没找到」，有一张图才知道是点空了、卡弹窗、还是模板过期。
    """
    try:
        from . import stagenav
        touch = stagenav.Touch(conn['adb_path'], conn['address'], log,
                               package=stagenav.game_package(client),
                               strict=strict)
        # 先打一发 get-state：打包版里子进程没带 CREATE_NO_WINDOW 时 adb 会开出
        # 一串黑框、命令还静默失败（2026-11-17 用户报的），不先自检就会一路点空，
        # 最后误报「翻了几十屏没找到这张卡片」。
        alive, detail = touch.ready()
        log('adb 自检：%s' % detail)
        if not alive:
            return False, detail
        ok, why = stagenav.goto_stage_list(touch, stage, log)
        log('导航%s：%s' % ('完成' if ok else '失败', why))
        if not ok:
            d = str(conn.get('maa_dir') or '')
            path = os.path.join(d or '.', 'debug',
                                'nav-fail-%s.png' % _SAFE.sub('_', stage))
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                if stagenav.dump(touch, path):
                    log('现场截图：%s' % path)
            except Exception:
                pass
        return ok, why
    except Exception as e:
        log('导航出错：%s' % e)
        return False, '%s: %s' % (type(e).__name__, e)


def run_core(spec, tid=None, timeout=None):
    """直连执行：建实例 → 连模拟器 → 逐个 append → start → 等跑完

    长跑任务（几个小时）不要在前台等，调用方应该丢后台线程。这里给的
    ``timeout`` 只是兜底，避免模拟器掉线后线程永远挂着。
    """
    from . import maacore

    log = tasks.log_factory(tid) if tid else (lambda *_: None)
    d = spec.get('maaDir') or settings.load().get('maaDir') or ''
    if not d:
        raise ValueError('还没设置 MAA 目录')

    # 同一台模拟器只能有一个 MAA 在操作，先把自己（GUI）关掉
    mine = maa.running_in(d)
    if mine:
        log('先关掉正在运行的 MAA 窗口（避免和直连抢模拟器）…')
        if not maa.close(d):
            raise RuntimeError('MAA 关不掉，请手动退出后重试')

    conn = _connection(d, spec.get('profile'))
    # 多开：用户/批量任务点名的模拟器地址优先（账号档案里可能写着同一台）
    if spec.get('adbAddress'):
        conn = dict(conn, address=spec['adbAddress'])
    log('连接 %s（%s）…' % (conn['address'], conn['config']))
    # 抄作业时 MAA 每换一份作业就发一条 CopilotListLoadTaskFileSuccess，
    # 文件名是我们自己按代号存的（BB-S-3_10531.json）。记下来就能知道
    # 「跑到第几关、卡在哪一关」——预检说打不了的和实际卡住的才能对上。
    cp_rows = spec.get('copilotList') or []
    by_file = {os.path.basename(r.get('filename') or ''): r.get('stage_name')
               for r in cp_rows if r.get('filename')}
    cid_of = {os.path.basename(r.get('filename') or ''): _cid_of(r.get('filename'))
              for r in cp_rows if r.get('filename')}
    # 首轮已经在用哪份作业：重试时靠它挑「下一份」，否则会挑回同一份
    tried = {}
    for r in cp_rows:
        key = '%s@%s' % (str(r.get('stage_name') or '').upper(),
                         'raid' if r.get('is_raid') else 'normal')
        cid = cid_of.get(os.path.basename(r.get('filename') or ''))
        if cid:
            tried[key] = cid
    # state 是回调线程和跑机线程共用的「进度便签」：
    #   stage/trail  跑到第几关（收尾结算用）
    #   results      逐关战斗结果 [{'stage','ok','why','cid','at'}]，重试和结算都看它
    #   cleared      已打过的关（同一关刷多次只喊一次「战斗成功」）
    #   last_log     最后一次往任务日志写字的时间（心跳线程用它算「多久没动静」）
    #   pause        材料不足之类的暂停原因（回调里置位，run_core 收尾时上报）
    #   done/summary 干员培养的成果（skill_train 用来算还剩哪些没做）
    state = {'stage': '', 'trail': [], 'by_file': by_file, 'cid_of': cid_of,
             'tried': tried, 'results': [], 'cleared': {}, 'cur_cid': 0,
             'actions': {}, 'deployed': {}, 'nodes': {},
             'last_log': time.time(), 'pause': '', 'pause_detail': '',
             'done': {}, 'summary': {}, 'stop': None,
             'plans': spec.get('plans') or []}

    def evlog(msg):
        state['last_log'] = time.time()
        log(msg)

    eng = maacore.Engine(d, on_event=lambda ev: _on_core_event(ev, evlog, tid, state))
    state['stop'] = eng.stop
    # MAA 自己写的 asst.log 里有些话不走回调（认不出关卡名、干员不存在、参数
    # 不合法…）。起一个轻量线程跟着读，翻成人话补进同一条任务日志——用户
    # 2026-11-18 报「自动战斗没有日志」，一半原因就是这些行没人读。
    try:
        tap = maalog.Tap(d)
    except Exception:
        tap = None            # 日志读不了不影响跑机，只是少一层说明
    tap_stop = threading.Event()

    def pump_maa_log():
        if tap is None:
            return
        while not tap_stop.wait(1.0):
            try:
                for _lvl, text in tap.poll():
                    log('[MAA] %s' % text)
            except Exception:
                pass

    tap_thread = threading.Thread(target=pump_maa_log, daemon=True,
                                  name='maa-log-tap')
    try:
        eng.open()
        log('MaaCore %s 已加载' % eng.version())
        # conn 里除了连接参数还带了 maa_dir（导航失败存截图用），不能整个
        # 展开传给 connect()，显式点名
        if not eng.connect(adb_path=conn['adb_path'], address=conn['address'],
                           config=conn['config'], touch=conn['touch']):
            raise RuntimeError('连不上模拟器：%s（检查 MAA 设置里的 ADB 路径和地址）'
                               % conn['address'])
        log('模拟器已连接')
        tap_thread.start()

        seq = core_tasks(spec, d)
        # 直跑模式（noNav）：**不唤醒、不导航**——调用方（skill_fight）已经把
        # 界面停在想要的位置（或用户手动导航好了），Copilot 自己会从当前位置
        # 接手。core_tasks 会强制补 StartUp（见它的注释：没唤醒跑不了），这里
        # 摘掉，否则又走一遍「等登录→停 MAA→导航」的全套（2026-11-17 用户实测
        # 吐槽：skill 化了怎么还在唤醒+导航）。
        if spec.get('noNav'):
            seq = [(t, p) for t, p in seq if t != 'StartUp']
        if not seq:
            raise ValueError('没有可执行的任务')

        # 抄作业前先把界面带到关卡列表：MAA 的活动入口模板在当前 UI 上全失效，
        # Copilot 自己又只会横向滑动切章节（这版游戏改成底部点 EPISODE 了），
        # 所以这一段只能我们自己点（详见 stagenav.py 顶部注释）。
        #
        # **必须放在 StartUp 之后**：唤醒会把界面拉回主界面，先导航等于白点。
        cp_rows = spec.get('copilotList') or []
        first = next((r.get('stage_name') for r in cp_rows if r.get('stage_name')), '')
        # 作业列表给了、但任务队列没勾「抄作业」：补一条。不补的话 StartUp 跑完
        # 就收工，导航到位了也没人开打（2026-11-17 中午实测）。
        if first and not any(t == 'Copilot' for t, _ in seq):
            log('[补] 任务队列没勾「抄作业」，按作业列表自动补上')
            form = spec.get('copilot') or {}
            try:
                support = int(form.get('support', 1))
            except (TypeError, ValueError):
                support = 1
            seq.append(('Copilot', {
                'copilot_list': cp_rows,
                'formation': bool(form.get('formation', True)),
                'add_trust': False,
                'ignore_requirements': False,
                'support_unit_usage': support if support in (0, 1, 2) else 1,
                'use_sanity_potion': bool(form.get('sanityPotion', True)),
                'formation_index': 1}))

        # 整轮的时间预算（分段抄作业、换作业重挂都用它算「还剩多少时间」）
        total = timeout or (60 * 60 * 8)
        budget = time.time() + total
        finished = True

        # 队列头是 StartUp 就先只跑它，剩下的一律等登录完成再排——**绝不能
        # 让导航和唤醒并发**：唤醒点 START / 登录 / 关公告的那几十秒里界面
        # 根本不在主界面，导航按下去全落在启动页甚至安卓桌面上（2026-11-17
        # 中午实测：nav-fail 现场图就是一张桌面截图，用户看到的是「到主页后
        # 没导航，游戏直接被关了」）。
        started_login = bool(seq) and seq[0][0] == 'StartUp'
        head, tail = (seq[:1], seq[1:]) if started_login else (seq, [])

        if spec.get('noNav'):
            # 直跑模式（调用方已经把界面停在想要的位置）：不唤醒、不导航。
            # 但**跨分区还是要分段**——MaaCore 不会点活动地图的分区节点，一把
            # 排到 EX 就是「一直找不到 OS-EX-1」（2026-11-22 用户实测）。
            # can_reuse_nav=True = 首段沿用调用方已经做好的那次导航。
            log('开始执行（%s）' % _label_of(seq[0][0]))
            finished = _run_tail(eng, seq, spec, state, log, conn, d, budget,
                                 can_reuse_nav=True)
        elif started_login:
            for typ, params in head:
                eng.append(typ, params)
                log('排队：%s %s' % (_label_of(typ), _brief(typ, params)))
            log('开始执行（%s）' % _label_of(head[0][0]))
            if not eng.start():
                raise RuntimeError('AsstStart 返回失败')
            # ---- 第一段：登录 -------------------------------------------------
            # 等「开始唤醒」这条链跑完（不是等全部，后面还没排）。
            # 登录失败就别往下走了：MaaCore 会一直重试切号/点开始，白烧时间。
            log('等登录（开始唤醒）跑完…')
            _wait_chain(eng, head[0][0], log, timeout=300)
            failed = [c for c in eng.chains
                      if c.get('chain') == head[0][0] and not c.get('ok')]
            if failed:
                raise RuntimeError('登录没成功：%s'
                                   % (failed[0].get('why') or '唤醒链报错'))
            log('登录完成，界面应该在主界面')

            # ---- 第二段：把 MAA 停住，界面交给我们的导航 ----------------------
            # 用户要求（2026-11-17）：MAA 登录完先停，导航由我们点，点到位再让
            # MAA 开打。不停的话 MaaCore 还在按自己的逻辑截屏/滑动，和我们的
            # adb 点击抢界面——表现就是「点了没反应」或者刚点进去又被它滑走。
            #
            # 只有真要抄作业（有 first）才需要停 + 导航：培养干员这类任务由
            # MaaCore 自己走 OperBoxBegin 进干员列表，多停一次只是白等 1 秒，
            # 日志里还会冒出「开始我们的导航」这种看不懂的话。
            if first:
                eng.stop()
                time.sleep(1.0)
                log('MAA 已停下，开始我们的导航')
                nav_ok, nav_why = _navigate(
                    conn, first, log, client=spec.get('clientType'),
                    strict=bool(spec.get('profile') or spec.get('adbAddress')))
                if not nav_ok:
                    # 老故事集/自建模板的关卡，MAA 自己 100% 找不到（它的入口
                    # 模板在当前 UI 上全失效，只会横向滑主线）。这种情况宁可
                    # 明确报错，也别把作业排下去让它乱滑 30 秒再失败。
                    from . import stagenav, eventnav
                    if (stagenav.tpl_path_of(first) or stagenav.event_of(first)[0]
                            or eventnav.has(first)):
                        raise RuntimeError('导航没到位（%s），不再把作业交给 MAA'
                                           % nav_why)
                    log('导航没成功，交给 MAA 自己试（当期活动它认得）')

            # ---- 第三段：排作业，开打 -----------------------------------------
            # 抄作业可能跨分区（基础区 → EX 区），_run_tail 会在段与段之间
            # 停一下、导航过去再排（见文件顶部「分段导航」）。
            finished = _run_tail(
                eng, tail, spec, state, log, conn, d, budget,
                can_reuse_nav=bool(first) and not spec.get('noNav'))
        else:
            # 防御分支：seq 里没有 StartUp（core_tasks 会强制补，正常走不到）。
            # 整队原样排，先导航再跑——游戏没开时导航失败要立刻暴露。
            for typ, params in head:
                eng.append(typ, params)
                log('排队：%s %s' % (_label_of(typ), _brief(typ, params)))
            log('开始执行（%s）' % _label_of(head[0][0]))
            if not eng.start():
                raise RuntimeError('AsstStart 返回失败')
            if first:
                nav_ok, nav_why = _navigate(
                    conn, first, log, client=spec.get('clientType'),
                    strict=bool(spec.get('profile') or spec.get('adbAddress')))
                if not nav_ok:
                    from . import stagenav, eventnav
                    if (stagenav.tpl_path_of(first) or stagenav.event_of(first)[0]
                            or eventnav.has(first)):
                        raise RuntimeError('导航没到位（%s），不再把作业交给 MAA'
                                           % nav_why)
            finished = _wait_run(eng, max(1.0, budget - time.time()), log, state)
        # ---- 换作业重挂：翻车的那几关再试一次（已成功的关不重打）----------
        # 用户 2026-11-19 需求 3：作业集里这一关的作业不行就换一份别的，
        # 自动接着挂，并且日志里说清楚换了哪份。
        swapped, chains_mark = [], None
        failed = any(not r.get('ok') for r in (state.get('results') or []))
        if failed and spec.get('copilotList'):
            # 用户 2026-11-22「我刚刚确实是 2 星，但是 agent 并没有帮我换作业
            # 重新挂」：旧条件要求 spec['retry'] 非空，而每关只有一份候选时
            # _prepare_ops 根本不给 retry，于是翻车后什么都不做。现在只要翻
            # 过车就交给 _retry_failed——里面没得换会同一份再试一次。
            swapped, chains_mark = _retry_failed(eng, spec, state, log, d, budget,
                                                 conn=conn)
        if not finished and not state.get('pause'):
            log('等了 %d 分钟还没跑完，先停下' % (total // 60))
            eng.stop()
        if state.get('pause'):
            log('已暂停：%s' % state['pause'])
        # 培养中途失败 / 暂停时，MaaCore 停在干员档案的精英化页（用户看到的
        # 「卡在升级等级的界面」就是这个）。等它真的停下来，再把界面退回
        # 主界面——不然用户切回模拟器，看到游戏卡在半开的页面，只会以为
        # 程序把游戏点坏了。
        train_bad = [c for c in eng.chains
                     if c.get('chain') == 'OperProgress' and not c.get('ok')]
        if state.get('pause') or train_bad:
            t_end = time.time() + 5
            while eng.running() and time.time() < t_end:
                time.sleep(0.2)
            if eng.back_to_home():
                log('界面已退回主界面')
        # 成功与否只看有没有任务链报错：AllTasksCompleted 在链报错后照样会来，
        # 单纯等它等于「永远成功」。次数打满被 Stop 的那次是正常结束。
        # 重试过的话只看重试那一轮的链，否则第一轮的失败会把结果盖掉。
        tail_chains = (eng.chains[chains_mark:] if chains_mark is not None
                       else eng.chains)
        bad = [c for c in tail_chains if not c.get('ok')]
        stopped = [c for c in eng.chains if c.get('stopped')]
        if stopped and not state.get('pause'):
            log('正常收尾：%s（次数打满 / 理智不足 / 手动停）'
                % '、'.join(c['chain'] for c in stopped[:4]))
        # 暂停是「用户该去补材料了」，不是失败：MAA 已经按计划把能做的都试过，
        # 这时报一串 [失败] 只会让人以为程序坏了。
        for c in bad:
            if not state.get('pause'):
                log('[失败] %s %s' % (c['chain'], c.get('why') or ''))
        # 逐关成败**不再统计**（2026-11-22 用户口径）：MaaCore 在 Copilot 任务里
        # 只报「换了哪份作业」和任务链成败，没有逐关结果，硬算出来的「成功 0 关」
        # 是假消息。这里只给「跑过哪些关」（trail）+ 真实的任务链失败。
        _, still = _tally(state.get('results') or [])
        failed_stage = state['stage'] if bad else ''
        failed_why = next((c.get('why') for c in bad if c.get('why')), '') if bad else ''
        # 用户点「停止挂机」时 wait 也返回 False，但这不是超时：以前 timed_out
        # 一律为 True，上层就报「跑满 120 分钟还没结束」（2026-11-23 用户手动
        # 暂停，日志跟事实完全对不上）。
        user_stop = bool(getattr(eng, 'stopped', False)) and not finished
        if user_stop:
            log('已停止（你按了停止），不再往下排')
        return {'ok': (finished and not bad) or bool(state.get('pause')),
                'tasks': len(seq),
                'chains': eng.chains,
                'timed_out': (not finished) and not user_stop,
                'failed_stage': failed_stage,
                'failed_why': failed_why,
                'stage_trail': state['trail'],
                'stages_failed': still,
                'swaps': swapped,
                'paused': (state.get('pause')
                           or ('手动停止' if user_stop else '')),
                'pauseDetail': state.get('pause_detail') or '',
                'trained': state.get('done') or {},
                'trainSummary': state.get('summary') or {},
                'stageReport': 'trail',   # 告诉调用方：这是「跑过的关」，不是成败
                'error': '; '.join(eng.errors) or eng.error or ''}
    finally:
        tap_stop.set()
        if tap_thread.is_alive():
            tap_thread.join(timeout=2)
        eng.close()


def _single_params(row, spec):
    """单作业模式的 Copilot 参数：只给 ``filename``，**不给** ``copilot_list``

    MaaCore 里那句是 ``CopilotTask set_params failed, stage_name or filename
    not found``——filename 单独就能成立。给了 copilot_list 它就走 MultiCopilot，
    每关都要 ``ClickStageName`` 在关卡列表里 OCR 找名字；而散点关卡（OS-S-*）
    根本不在那种列表里，认不出来（用户 2026-11-23：「MAA 无法识别 S 关卡」）。
    只给 filename 它就不导航，界面在哪从哪开始——正好我们从编队页交给它。
    """
    p = _copilot_params([row], spec)
    p.pop('copilot_list', None)
    p['filename'] = row.get('filename')
    return p


def run_scatter(spec, tid=None, timeout=None):
    """散点式关卡（S 关这类）：一关一关点进去，用**单作业模式**打

    和 :func:`run_core` 唯一的区别：作业不是「整队排给 MAA 让它自己找关」，而是
    我们自己点到干员编队页、一关排一个 Copilot 任务。原因见 :mod:`scatter`：
    OS 的 S 关浮在雪山地图上（积雪清理系统），MaaCore 的 ``ClickStageName``
    在关卡列表里 OCR，这种界面一个都认不出。

    返回结构刻意和 ``run_core`` 对齐（``ok`` / ``stage_trail`` /
    ``stages_failed`` / ``error``），调用方的收尾逻辑不用改。

    ``spec`` 除了 run_core 那些，还要 ``copilotList``：**每一条就是一趟**
    （普通/突袭各一条），``is_raid`` 决定点「开始行动」还是「开始突袭」。
    """
    from . import maacore, scatter, eventnav, stagenav

    log = tasks.log_factory(tid) if tid else (lambda *_: None)
    d = spec.get('maaDir') or settings.load().get('maaDir') or ''
    if not d:
        raise ValueError('还没设置 MAA 目录')

    rows = [r for r in (spec.get('copilotList') or []) if r.get('filename')]
    if not rows:
        raise ValueError('散点关卡要作业文件（copilotList 是空的）')
    first = str(rows[0].get('stage_name') or '')
    pre = eventnav.prefix_of(first)
    lay = scatter.layout_of(pre)
    if not lay:
        raise ValueError('「%s」没有散点关卡的标定（navtpl/scatter）' % (pre or first))
    log('%s：%s，一关一个作业（单作业模式）'
        % (lay.get('label') or pre, '、'.join(
            '%s%s' % (r.get('stage_name'), '突袭' if r.get('is_raid') else '')
            for r in rows)))

    mine = maa.running_in(d)
    if mine:
        log('先关掉正在运行的 MAA 窗口（避免和直连抢模拟器）…')
        if not maa.close(d):
            raise RuntimeError('MAA 关不掉，请手动退出后重试')

    conn = _connection(d, spec.get('profile'))
    if spec.get('adbAddress'):
        conn = dict(conn, address=spec['adbAddress'])
    log('连接 %s（%s）…' % (conn['address'], conn['config']))

    total = timeout or (2 * 60 * 60)
    deadline = time.time() + total
    state = {'trail': [], 'results': [], 'nodes': {}, 'stage': '',
             'by_file': {}, 'cid_of': {}, 'cur_cid': 0}
    trail, failed = [], []
    touched = 0

    touch = stagenav.Touch(conn['adb_path'], conn['address'], log,
                           package=stagenav.game_package(spec.get('clientType')))
    alive, detail = touch.ready()
    if not alive:
        log('连不上模拟器：%s' % detail)
        return {'ok': False, 'error': detail, 'stage_trail': [], 'stages_failed': [],
                'chains': [], 'timed_out': False, 'failed_stage': '',
                'failed_why': '', 'swaps': [], 'paused': '', 'pauseDetail': '',
                'trained': {}, 'trainSummary': {}, 'stageReport': 'trail'}

    # 先到 S 关界面：复用活动导航（点「喀兰之心」那个分区节点），它认得出
    # 主页 / 终端页 / 活动地图，比我们自己拼一条路稳。
    # 已经在 S 关界面就别再导航一次：那页右下角也摆着三个分区节点，
    # eventnav 的「已经在活动地图上」会成立，接着去点「喀兰之心」，画面不变
    # 就判失败——多绕一圈还报个假错误。
    data = touch.shot()
    if scatter.is_here(data, pre):
        log('已经在 S 关界面，跳过导航')
    else:
        nav_ok, nav_why = eventnav.run(touch, first, log)
        if not nav_ok:
            log('导航没到 S 关界面：%s' % nav_why)
            return {'ok': False, 'error': nav_why, 'stage_trail': [],
                    'stages_failed': [], 'chains': [], 'timed_out': False,
                    'failed_stage': '', 'failed_why': '', 'swaps': [],
                    'paused': '', 'pauseDetail': '', 'trained': {},
                    'trainSummary': {}, 'stageReport': 'trail'}
        time.sleep(1.5)
    # 详情面板如果是开着的（上一轮点开过），先点掉再认格子
    scatter.back_to_cells(touch, pre, log)
    data = touch.shot()
    if not scatter.is_here(data, pre):
        log('到了「%s」但没认出任何关卡按钮（模板过期？）' % (lay.get('label') or pre))
        return {'ok': False, 'error': 'S 关按钮一个都没认出来',
                'stage_trail': [], 'stages_failed': [], 'chains': [],
                'timed_out': False, 'failed_stage': '', 'failed_why': '',
                'swaps': [], 'paused': '', 'pauseDetail': '', 'trained': {},
                'trainSummary': {}, 'stageReport': 'trail'}
    cells = scatter.cells_of(data, pre)
    log('认到 %d 个关卡按钮：%s'
        % (len(cells), '、'.join('%s-S-%d' % (pre, i) for i in sorted(cells))))

    eng = maacore.Engine(d, on_event=lambda ev: _on_core_event(ev, log, tid, state))
    eng.open()
    try:
        if not eng.connect(conn['adb_path'], conn['address'],
                           conn.get('config') or 'General',
                           conn.get('touch') or 'MiniTouch'):
            log('MaaCore 连不上模拟器')
            return {'ok': False, 'error': 'MaaCore 连不上模拟器',
                    'stage_trail': [], 'stages_failed': [], 'chains': [],
                    'timed_out': False, 'failed_stage': '', 'failed_why': '',
                    'swaps': [], 'paused': '', 'pauseDetail': '', 'trained': {},
                    'trainSummary': {}, 'stageReport': 'trail'}
        for i, row in enumerate(rows, 1):
            if time.time() > deadline:
                log('跑满 %d 分钟，剩下的关不打了' % int(total // 60))
                break
            if state.get('pause'):
                log('已暂停（%s）' % state['pause'])
                break
            code = str(row.get('stage_name') or '')
            idx = scatter.cell_index(code)
            raid = bool(row.get('is_raid'))
            label = '%s%s' % (code, ' 突袭' if raid else ' 普通')
            log('---- [%d/%d] %s ----' % (i, len(rows), label))
            if not idx or idx not in (lay.get('cells') or {}):
                log('跳过 %s：标定里没有这个编号' % code)
                continue
            # 格子只在进界面时认一次：详情面板一展开，整屏会蒙一层暗色，
            # 所有模板的分数一起升高（实测 S2/S4 从 0 分涨到 11 分），这时
            # 重新识别会把「有」看成「没有」。坐标是钉死的，认一次就够。
            if idx not in cells:
                log('跳过 %s：进界面时没看到这个按钮（没解锁）' % code)
                continue
            ok_open, why = scatter.open_stage(touch, pre, idx, raid, log,
                                           pos=cells.get(idx))
            if not ok_open:
                log('跳过 %s：%s' % (code, why))
                failed.append({'stage': code, 'why': why})
                continue
            touched += 1
            state['stage'] = code
            state['nodes'] = {}
            mark = len(eng.chains)
            try:
                eng.append('Copilot', _single_params(row, spec))
            except Exception as e:                  # noqa: BLE001 参数不对就别硬跑
                log('MAA 不接受这份作业：%s' % e)
                failed.append({'stage': code, 'why': str(e)})
                scatter.back_to_cells(touch, pre, log)
                continue
            if not eng.start():
                log('AsstStart 返回失败')
                failed.append({'stage': code, 'why': 'AsstStart 返回失败'})
                scatter.back_to_cells(touch, pre, log)
                continue
            left = max(120.0, deadline - time.time())
            budget = min(1200.0, left)
            t0 = time.time()
            done = eng.wait(timeout=budget)
            spent = time.time() - t0
            if not done and getattr(eng, 'stopped', False):
                # 用户自己点了「停止挂机」（stop_all_live → AsstStop）：这不是
                # 超时也不是失败。2026-11-23 用户手动暂停，日志却写「这一关
                # 超时（1200 秒）」，他当场就问「前后矛盾」。
                log('已停止（你按了停止），剩下的关不打了')
                state['user_stop'] = True
                break
            bad = [c for c in eng.chains[mark:] if not c.get('ok')]
            if bad:
                # 失败优先看链本身。MaaCore 在编队缺干员这类硬错误上不发
                # TaskChainError，只发 SubTaskError + TaskChainStopped，
                # maacore 那边已经把 why 补进 chains 了（2026-11-23）。
                why = bad[0].get('why') or ''
                opers = (state.get('last_sub_opers') or []) if state else []
                cn = maacore.why_cn(why) or 'MAA 报任务链失败'
                tail = '（缺：%s）' % '、'.join(opers) if opers else ''
                log('%s 失败：%s%s' % (label, cn, tail))
                failed.append({'stage': code, 'why': (cn + tail)})
                # 缺干员是作业本身不匹配，重试同一份没用，直接跳过这关
                if why:
                    log('（%s 是作业和账号不匹配，这一关跳过，接着打下一关）'
                        % cn)
            elif not done:
                # wait 返回 False 有三种：真跑满 budget、MAA 自己停了、
                # 根本没跑起来。以前一律报「超时 1200 秒」，实测只等了 8 秒
                # 就回来了，日志骗人 20 分钟（2026-11-23 用户报障）。
                if spent >= budget - 5:
                    log('这一关跑了 %d 秒还没结束，先停下' % int(spent))
                    failed.append({'stage': code, 'why': '跑满 %d 秒' % int(spent)})
                    eng.stop()
                    break
                else:
                    sub = (state.get('last_sub_error') or '') if state else ''
                    log('这一关 %.0f 秒就被中止了（MAA 没报完任务链就停了%s）'
                        % (spent, ('，卡在 %s' % sub.split('@')[-1]) if sub else ''))
                    failed.append({'stage': code, 'why': '任务被中止'})
                eng.stop()
            else:
                log('%s 打完了（单作业模式不判三星，界面停在结算页）' % label)
                trail.append(code + ('(突袭)' if raid else ''))
                state.setdefault('results', []).append(
                    {'stage': code, 'ok': True, 'why': '', 'at': time.time(),
                     'cid': _cid_of(row.get('filename'))})
            if not scatter.back_to_cells(touch, pre, log):
                log('打完没能回到 S 关界面，剩下的关不打了')
                break
    finally:
        eng.close()

    return {'ok': not failed,
            'tasks': touched,
            'chains': [],
            'timed_out': time.time() > deadline,
            'failed_stage': failed[0]['stage'] if failed else '',
            'failed_why': failed[0]['why'] if failed else '',
            'stage_trail': trail,
            'stages_failed': failed,
            'swaps': [],
            'paused': (state.get('pause')
                       or ('手动停止' if state.get('user_stop') else '')),
            'pauseDetail': state.get('pause_detail') or '',
            'trained': {}, 'trainSummary': {},
            'stageReport': 'trail',
            'error': ('; '.join('%s：%s' % (x['stage'], x['why']) for x in failed)
                      if failed else '')}


def run_custom(spec, tid=None, timeout=None):
    """直连跑一条 ``Custom`` 任务（小工具 / 牛杂里的那几项）

    ``spec`` 字段：
        ``maaDir`` / ``profile`` —— 和 :func:`run_core` 一样从 MAA 配置里抄；
        ``task`` —— ``{'task_names': [...], 'params': {...}}``，见 :mod:`maatools`；
        ``title`` —— 日志里怎么称呼它（「战时新闻委员会」）；
        ``tutorial`` —— 卡住时给用户的操作建议（小游戏第一次要过教程）。

    为什么不复用 ``run_core``：那条链是给「抄作业」写的，会强制补 StartUp、
    会做关卡导航、会排队 Copilot、还要重挂——牛杂这两项一个都不需要，走一遍
    只会多花十几秒，还会在日志里灌一堆「排队：开始唤醒」。

    卡住检测：小游戏首次进入会停在教程页，MAA 的 pipeline 认不出教程对话，
    会安静地重试同一个节点。这里每轮看一眼「多久没新事件」，超过 120 秒就
    把 ``spec['tutorial']`` 那句话打出来——用户才知道该手动过一下教程。
    """
    from . import maacore

    log = tasks.log_factory(tid) if tid else (lambda *_: None)
    d = spec.get('maaDir') or settings.load().get('maaDir') or ''
    if not d:
        raise ValueError('还没设置 MAA 目录')
    task = spec.get('task') or {}
    if not task.get('task_names'):
        raise ValueError('没有要跑的任务（task_names 是空的）')
    title = spec.get('title') or '小工具'

    # 同一台模拟器只能有一个 MAA 在操作，先把自己（GUI）关掉
    if maa.running_in(d):
        log('先关掉正在运行的 MAA 窗口（避免和直连抢模拟器）…')
        if not maa.close(d):
            raise RuntimeError('MAA 关不掉，请手动退出后重试')

    conn = _connection(d, spec.get('profile'))
    # 多开：用户/批量任务点名的模拟器地址优先（账号档案里可能写着同一台）
    if spec.get('adbAddress'):
        conn = dict(conn, address=spec['adbAddress'])
    log('连接 %s（%s）…' % (conn['address'], conn['config']))

    state = {'last': time.time(), 'node': '', 'node_log': 0.0, 'warned': 0,
             'stuck': ''}

    def evlog(msg):
        state['last'] = time.time()
        log(msg)

    eng = maacore.Engine(d, on_event=lambda ev: _on_custom_event(ev, evlog, state))
    try:
        tap = maalog.Tap(d)
    except Exception:
        tap = None
    tap_stop = threading.Event()

    def pump():
        if tap is None:
            return
        while not tap_stop.wait(1.0):
            try:
                for _lvl, text in tap.poll():
                    line = _custom_tap_line(text)
                    if line:
                        evlog('[MAA] %s' % line)
            except Exception:
                pass

    th = threading.Thread(target=pump, daemon=True, name='maa-tool-tap')
    total = timeout or 1800
    try:
        eng.open()
        log('MaaCore %s 已加载' % eng.version())
        if not eng.connect(adb_path=conn['adb_path'], address=conn['address'],
                           config=conn['config'], touch=conn['touch']):
            raise RuntimeError('连不上模拟器：%s（检查 MAA 设置里的 ADB 路径和地址）'
                               % conn['address'])
        log('模拟器已连接')
        th.start()
        eng.append('Custom', task)
        log('交给 MAA：%s' % title)
        if not eng.start():
            raise RuntimeError('AsstStart 返回失败')

        deadline = time.time() + total
        finished = False
        while time.time() < deadline:
            if eng.wait(timeout=min(10.0, max(1.0, deadline - time.time()))):
                finished = True
                break
            idle = time.time() - state['last']
            if idle > 120 and state['warned'] < 2:
                state['warned'] += 1
                log('已经 %d 秒没有新动静了' % int(idle))
                if spec.get('tutorial'):
                    log(spec['tutorial'])
                    state['stuck'] = 'idle'
        if not finished:
            log('等了 %d 分钟还没跑完，先停下' % (total // 60))
            eng.stop()

        bad = [c for c in eng.chains if not c.get('ok')]
        for c in bad:
            log('[失败] %s %s' % (c['chain'], c.get('why') or ''))
        if finished and not bad:
            log('「%s」跑完了' % title)
        return {'ok': bool(finished and not bad),
                'title': title,
                'chains': eng.chains,
                'timed_out': not finished,
                'stuck': state.get('stuck') or '',
                'tutorial': spec.get('tutorial') or '',
                'error': '; '.join(eng.errors) or eng.error or ''}
    finally:
        tap_stop.set()
        if th.is_alive():
            th.join(timeout=2)
        eng.close()


def _on_custom_event(ev, log, state):
    """Custom 任务的回调 → 人话

    节点名（``MiniGame@WartimePressCommittee@Day1-Article2``）对用户没意义，
    但它是「有没有在动」的唯一信号：卡教程时节点会一直不变。所以每 60 秒
    最多报一次当前节点。
    """
    k = ev.get('kind') or ''
    if k == 'chain_start':
        log('MaaCore 开始跑这条任务')
    elif k == 'chain_done':
        log('MaaCore 报完成')
    elif k == 'chain_failed':
        log('[失败] %s' % (ev.get('why') or ''))
    elif k == 'node':
        node = str(ev.get('sub') or '')
        if not node:
            return
        state['node'] = node.rsplit('@', 1)[-1] or node
        now = time.time()
        if now - float(state.get('node_log') or 0) > 60:
            state['node_log'] = now
            log('进行中：%s' % state['node'])


#: MaaCore 活动商店插件说的话 → 用户看得懂的
_CUSTOM_TAP = (
    ('event shop skipping blacklisted product', '跳过黑名单商品：%s'),
    ('event shop selected product', '选中商品：%s'),
    ('event shop reached an unlimited commodity', '到无限池了，不买（停手）'),
    ('event shop currency is insufficient', '代币不够了，收工'),
    ('event shop reached the end after skipping blacklisted', '商店到底了（黑名单以外都买完）'),
    ('event shop purchase count exceeded', '买的数量到上限，停手'),
    ('event shop failed to swipe', '商店滑不动了'),
)


def _custom_tap_line(text):
    """一行 asst.log → 一条中文说明；不关心的返回空串"""
    t = str(text or '')
    low = t.lower()
    for key, tpl in _CUSTOM_TAP:
        if key in low:
            if '%s' in tpl:
                tail = t.split(':', 1)[-1].strip() if ':' in t else ''
                return tpl % tail if tail else tpl.replace('：%s', '')
            return tpl
    return ''


# MaaCore 的 CopilotAction：抄作业时每做一个动作就报一次
_ACTION_CN = {
    'Deploy': '部署', 'Retreat': '撤退', 'UseSkill': '使用技能',
    'SwitchSpeed': '切换倍速', 'SkillDaemon': '开始挂机',
    'BulletTime': '子弹时间', 'StartAction': '已开始行动',
}

# 子任务（pipeline 节点）→ 人话。只挑用户能看懂的，其余一律不写：
# SubTaskStart 一秒能来十几条，全推上去等于刷屏。
#
# 键统一用**裸 pipeline 名**（不带 ``<TaskChain>@`` 前缀）：maacore._translate
# 把 SubTaskStart 的 ``first[0]`` 取出来时，有的本来就带前缀
# （``Copilot@WaitUntilEndOfAction``），有的不带（``BattleStartAll``，会被补成
# ``Copilot@BattleStartAll``）。查表时先按整串找、再按 @ 后最后一段找，
# 两种都能命中。这些名字是从 <MAA>/debug/asst.log 里实际统计出来的，
# 不是照着源码猜的——2026-11-18 就是猜名对不上，导致战斗过程一整段没日志。
_SUBTASK_CN = {
    # 找关卡 / 导航
    'FullStageNavigation': '找关卡',
    'StageNavigationBegin': '关卡导航',
    'StageNavigationSlowlySwipeLeft': '滑动找关卡',
    'ChapterSwipeToTheRightAndPlot': '翻章节',
    'ToChapterNew': '进入章节',
    'StageBegin': '进入关卡',
    # 编队 / 开打
    'BattleStartPre': '开始编队',
    'BattleStartAll': '已开始行动',
    'BattleQuickFormation': '快速编队',
    'BattleQuickFormationConfirm': '确认编队',
    'BattleQuickFormationSkillPage': '编队·技能页',
    'BattleQuickFormationModulePage': '编队·模组页',
    'BattleSelectFormation1': '选择编队',
    'BattleFormationOperListSlowlySwipeToTheRight': '翻干员列表',
    'BattleAvatarDialog': '关闭干员提示',
    'Formation-EnterQuickFormation': '进入快速编队',
    'Formation-AddSupportUnit-EnterSupportList': '打开助战列表',
    'SupportList-DetailPanel-Flag': '查看助战详情',
    'BattleCancelSelection': '取消选择',
    'SkipForbiddenOperConfirm': '确认禁用干员',
    'StartAction': '开始行动',
    'ClickCornerUntilStartButton': '点「开始行动」',
    'FightBegin': '开始战斗',
    'BattleSpeedUp': '切二倍速',
    'UseMedicine': '吃理智药',
    # 战斗中 / 结算
    'WaitUntilEndOfAction': '战斗中',
    'BattlePause': '战斗暂停',
    'EndOfAction': '战斗结算',
    'RaidConfirm': '确认剿灭',
    # 启动 / 切号
    'StartUpBegin': '开始唤醒',
    'StartAtHome': '回到主界面',
    'AccountListSwipe': '翻账号列表',
    'AccountListSwipeToTop': '账号列表回顶',
    'AccountManagerLoginButton': '点登录',
    'AccountManagerListAccount': '打开账号列表',
    # 培养干员（OperProgress，名字取自 MaaCore OperProgressProcessTask.cpp）
    'OperFiles': '打开干员档案',
    'ReturnToOperFilesPage': '返回干员档案',
    'ReturnToOperBox': '返回干员列表',
    'CurrentElite': '读取精英化等级',
    'EliteUp': '精英化',
    'EliteUpPageConfirm': '确认精英化',
    'EliteUpMaterialMissing': '缺精英化材料',
    'LevelUp': '升级',
    'CurrentSkillLevel': '读取技能等级',
    'SkillUpgrade': '技能升级',
    'SkillUpConfirm': '确认技能升级',
    'MasteryPageEnter': '进入专精页',
    'MasterySelectSkill': '选择专精技能',
    'MasterySelectSkillMaxAlready': '该技能已专满',
    'MasteryMaterialMissing': '缺专精材料',
    'MfgCatalystMissing': '缺催化剂',
    'TrainingSkill': '训练室专精中',
}


def _subtask_label(sub):
    """SubTaskStart 的 pipeline 名 → 中文；认不出返回 ''（静默跳过）

    maacore 给的名字有的带 ``<Chain>@`` 前缀、有的不带，这里统一按裸名查。
    ``BattleQuickFormationRole-Caster`` 这类按职业筛选的节点有十几种后缀，
    逐条列进表太啰嗦，统一归成「编队·按职业筛选」；配合下面的 20 秒去重，
    一轮编队只出一条。
    """
    name = sub or ''
    key = name.rsplit('@', 1)[-1]
    label = _SUBTASK_CN.get(name) or _SUBTASK_CN.get(key)
    if label:
        return label
    if key.startswith('BattleQuickFormationRole-'):
        return '编队·按职业筛选'
    return ''


# OperProgress 的 result_detail（MaaCore 里按枚举序号序列化，老版本给数字）
_DETAIL_ORDER = ['Completed', 'AlreadySatisfied', 'ResourceInsufficient',
                 'OperatorNotFound', 'PrerequisiteNotMet', 'ChipNotCraftable',
                 'FormulaLocked', 'Unsupported', 'RecognitionFailed',
                 'TrainingRoomBusy', 'PrerequisiteTraining', 'Interrupt']
_DETAIL_CN = {
    'ResourceInsufficient': '材料不足',
    'OperatorNotFound': '翻遍干员列表没找到这个干员',
    'PrerequisiteNotMet': '前置不满足（专精要先精二，技能 7 要先精一）',
    'ChipNotCraftable': '芯片没法合成（5/6 星精二要制造站双芯片）',
    'FormulaLocked': '材料配方还没解锁',
    'Unsupported': '这条计划 MAA 不支持',
    'RecognitionFailed': '页面识别失败',
    'TrainingRoomBusy': '训练室被别的干员占着',
    'PrerequisiteTraining': '在练前置专精',
    'Interrupt': '被中断',
    'AlreadySatisfied': '已达标',
    'Completed': '完成',
}

# 「该去补东西」的结果 → (暂停原因, 怎么补)。命中就暂停，不再让 MAA 一条条
# 试下去（精英化没成，后面的技能/专精必然 PrerequisiteNotMet，刷屏没意义）。
#
# ChipNotCraftable 的来历（2026-11-19 用户实测「卡在升级界面报错」）：
# MaaCore 6.20.1 的 OperProgressProcessTask 在 E0→E1 缺普通芯片时直接
# ``return ChipNotCraftable``，不去走加工站合成（上游 issue #18612）。
# 也就是说 MAA 自己的 GUI 手动跑同样会停在这一步 —— 用户手动成功的那几次
# 都是芯片齐备的。别让日志把它说成「程序坏了」，要说成「缺什么、怎么补」。
_TRAIN_NEED_MORE = {
    'ResourceInsufficient': (
        '材料不足',
        '龙门币 / 经验卡 / 精英化材料不够。补齐后说一声「继续培养」接着做'),
    'ChipNotCraftable': (
        '缺精英化芯片',
        'MAA 这一版（6.20.1）缺芯片时不会自己去加工站合成，会直接停下'
        '（上游 issue #18612，MAA 自己的界面跑也一样）。'
        '最简单的办法：在游戏里手动精英化这张卡一次，再说「继续培养」；'
        '或者先去加工站把芯片做出来'),
    'FormulaLocked': (
        '材料配方没解锁',
        '加工站里的合成配方还没解锁。先解锁配方，再说「继续培养」'),
}


def _detail_key(v):
    """result_detail → 枚举名字（字符串直接用，数字按 MaaCore 的声明顺序换）"""
    if isinstance(v, bool) or v is None:
        return ''
    if isinstance(v, int):
        return _DETAIL_ORDER[v] if 0 <= v < len(_DETAIL_ORDER) else ''
    s = str(v).strip()
    if s.isdigit():
        i = int(s)
        return _DETAIL_ORDER[i] if 0 <= i < len(_DETAIL_ORDER) else ''
    return s


def plan_brief(p):
    """计划条目 → 「精英2 / 技能7 / 技能2 专精3」这种人话"""
    bits = []
    if p.get('elite'):
        bits.append('精英化 %s' % p['elite'])
    if p.get('skill_level'):
        bits.append('技能 %s' % p['skill_level'])
    arr = p.get('skill_mastery') or []
    for i, lv in enumerate(list(arr)[:3]):
        if lv:
            bits.append('技能%d 专精%d' % (i + 1, lv))
    return ' / '.join(bits) or '（没有目标）'


def _on_train_detail(data, log, state):
    """干员培养的一步结果（精英化 / 技能升级 / 专精各报一条）"""
    name = str(data.get('name') or '干员')
    result = str(data.get('result') or '')
    detail = _detail_key(data.get('result_detail'))
    plan = next((p for p in (state.get('plans') or [])
                 if str(p.get('name') or '') == name), {}) if state else {}

    what = ''
    if data.get('elite'):
        what = '精英化 %s' % data['elite']
    elif data.get('skill_level'):
        what = '技能等级 %s' % data['skill_level']
    elif data.get('skill_mastery'):
        arr = list(data['skill_mastery'] or []) + [0, 0, 0]
        idx = max(range(3), key=lambda i: int(arr[i] or 0))
        what = '技能%d 专精 %s' % (idx + 1, arr[idx])

    if result == 'Success':
        log('✔ %s：%s 完成' % (name, what or _DETAIL_CN.get(detail, '已达目标')))
        if state is not None:
            rec = state.setdefault('done', {}).setdefault(name, {})
            if data.get('elite'):
                rec['elite'] = int(data['elite'])
            if data.get('skill_level'):
                rec['skill_level'] = int(data['skill_level'])
            if data.get('skill_mastery'):
                arr = list(data['skill_mastery'] or []) + [0, 0, 0]
                old = list(rec.get('skill_mastery') or [0, 0, 0]) + [0, 0, 0]
                rec['skill_mastery'] = [max(int(arr[i] or 0), int(old[i] or 0))
                                        for i in range(3)]
        return

    if result == 'Skipped':
        log('· %s：跳过（%s）' % (name, _DETAIL_CN.get(detail, detail or '训练室占用')))
        return

    # 已经因为「该去补东西」暂停过了：MaaCore 会把同一名干员的后续子目标
    # 一条条报下来（精英化失败 → 技能/专精全 PrerequisiteNotMet 刷屏），
    # 这些不是新信息，静默掉。
    if state and state.get('pause'):
        return

    why = _DETAIL_CN.get(detail, detail or '失败')
    tail = ('（计划 %s）' % plan_brief(plan)) if plan else ''
    log('✘ %s：%s%s' % (name, why, tail))
    if detail not in _TRAIN_NEED_MORE or state is None:
        return
    # 这三种不是「跑挂了」，是「该去补东西」：MAA 自己会接着试下一条计划，
    # 那样只会一路失败下去（精英化没成，专精必然 PrerequisiteNotMet）。
    # 直接暂停，把要补的东西写清楚。
    #
    # ChipNotCraftable 尤其要说清楚：MAA 6.20.1 在 E0→E1 缺普通芯片时直接
    # 放弃、不去加工站合成（上游 issue #18612），MAA 自己的 GUI 跑也一样。
    # 用户手动能成功的那几次都是芯片齐的，容易误以为我们这边坏了。
    label, tip = _TRAIN_NEED_MORE[detail]
    state['pause'] = '%s %s' % (name, label)
    state['pause_detail'] = tail
    log('%s，暂停培养。%s' % (label, tip))
    log('后面没做完的部分已经存下来了，补完之后说一声「继续培养」，'
        '不用重新报练度')
    stop = state.get('stop')
    if stop:
        # 不能在回调线程里同步 AsstStop（它会等工作线程收尾，有互锁风险），
        # 丢给一个短命线程去叫停
        threading.Thread(target=stop, daemon=True,
                         name='maa-pause-stop').start()


def _on_core_event(ev, log, tid, state=None):
    """把引擎事件转成任务日志（跑机面板实时能看到）"""
    from . import maacore
    kind = ev.get('kind')
    if kind == 'error':
        log('[错误] %s' % (ev.get('text') or ''))
    elif kind in ('chain_start', 'chain_done', 'chain_failed', 'chain_stop'):
        log(ev.get('text') or '')
        # 失败要落到「哪一关」上：Copilot 链一挂，后面的关就不会再打了，
        # 用户需要立刻知道是这关的锅（2026-11-19 需求 1）
        if kind == 'chain_failed':
            _mark_fail(ev, log, state)
    elif kind == 'connected':
        log('模拟器已连接')
    elif kind == 'done':
        log('全部任务跑完')
    elif kind == 'node':
        # MAA 的日志面板就是一行一个阶段（开始编队 / 已开始行动 / 战斗中…），
        # 这里照同样的口径输出，不再加「·」前缀。
        # SubTaskStart 一秒能来十几条，所以同一阶段只报一次；跨关卡时把去重
        # 记录清掉（state['stage'] 变化），否则第二关的「已开始行动」会被上
        # 一关的 20 秒窗口吞掉（2026-11-22 用户要求「像 MAA 那样每关都有」）。
        label = _subtask_label(ev.get('sub') or '')
        if not label:
            return
        now = time.time()
        last = (state or {}).get('nodes') or {}
        if last.get('label') == label and now - last.get('at', 0) < 8:
            return
        if state is not None:
            state['nodes'] = {'label': label, 'at': now}
        log(label)
    elif kind == 'sub_error':
        # 子任务失败：MaaCore 只说「哪个 pipeline 卡住」，原因要我们自己翻。
        sub = ev.get('sub') or ''
        key = sub.rsplit('@', 1)[-1]
        why = ev.get('why') or ''
        opers = ev.get('opers') or []
        if why == 'OperatorMissing' or opers:
            # 编队缺干员：作业要的干员账号里没有 / 没解锁。以前只说「选择干员
            # 没成功」，用户根本猜不到是哪个人（2026-11-23 实测：圣聆初雪）。
            log('缺干员：%s（作业要的干员账号里没有，换一份作业或先练）'
                % ('、'.join(opers) or '未知'))
        elif key in ('WaitUntilEndOfAction', 'EndOfAction'):
            log('作战结束了，但结算不是三星（漏怪 / 作业没打过这关）')
        else:
            label = _subtask_label(sub)
            if label:
                log('%s 没成功' % label)
            if why:
                log('　原因：%s' % maacore.why_cn(why))
        if state is not None:
            state['last_sub_error'] = sub
            state['last_sub_why'] = why
            state['last_sub_opers'] = opers
    elif kind == 'extra':
        what = ev.get('what')
        data = ev.get('data') or {}
        if what == 'CopilotListLoadTaskFileSuccess':
            code = _stage_of_file(data, state)
            if code:
                prev = (state or {}).get('stage') or ''
                # 能在同一批里换到下一个作业，说明**上一关已经打完了**：
                # MultiCopilot 是顺序执行的，翻车会走 TaskChainError 而不是
                # 换关。MaaCore 在 Copilot 任务里不发 StageDrops，拿这个推
                # 是最可靠的「每关成功」信号（2026-11-22 用户要求）。
                if prev and prev.upper() != code.upper():
                    log('✓ %s 完成' % prev)
                    if state is not None:
                        # 顺便记进 results：重挂（_retry_failed）靠它判断
                        # 「这关到底过没过」。MaaCore 在 Copilot 里不发
                        # StageDrops，换关是唯一可靠的「上一关三星通过」
                        # 信号（2026-11-22 用户报「2 星后不换作业」时补的）。
                        state.setdefault('results', []).append(
                            {'stage': prev, 'ok': True, 'why': '',
                             'at': time.time(),
                             'cid': state.get('cur_cid') or 0})
                log('换作业：%s' % code)
                if state is not None:
                    state['stage'] = code
                    state['nodes'] = {}      # 新一关，阶段去重重新开始
                    fn = os.path.basename(str(data.get('file_name') or ''))
                    state['cur_cid'] = (state.get('cid_of') or {}).get(fn) or 0
                    if code not in state['trail']:
                        state['trail'].append(code)
        elif what == 'BattleFormationSelected':
            # MAA 面板里一行一条「选择干员: 芬」。以前没接这个 what，
            # 编队阶段只能看到一行笼统的「编队：…」（2026-11-22 用户对比
            # MAA 日志后指出）。
            sel = str(data.get('selected') or data.get('group_name') or '')
            if sel:
                log('选择干员: %s' % sel)
        elif what == 'CopilotAction':
            act = str(data.get('action') or '')
            target = str(data.get('target') or '')
            # 白名单外的动作（Output / SkillUsage 这些内部记账）不写进任务日志，
            # 否则面板上会冒出一串看不懂的英文
            label = _ACTION_CN.get(act)
            if not label:
                return
            now = time.time()
            # MAA 面板的统一前缀是「当前步骤: 」。StartAction 例外——
            # 它单独一行「已开始行动」。
            if act == 'StartAction':
                log(label)
                return
            step = '当前步骤: %s' % label
            if target:
                step += ' %s' % target
            if act == 'Deploy':
                # 同一个干员 6 秒内只报一次（撤退再部署是正常的，别永久吞掉）
                done = (state or {}).setdefault('deployed', {})
                if target and now - done.get(target, 0) < 6:
                    return
                if target:
                    done[target] = now
                log(step)
                return
            if act == 'UseSkill':
                # 一场仗要开十几次技能，15 秒内只说一次，不然日志全是「开技能」
                if now - (state or {}).get('skill_at', 0) < 15:
                    return
                if state is not None:
                    state['skill_at'] = now
                log(step)
                return
            seen = (state or {}).setdefault('actions', {})
            if act in seen:
                return
            seen[act] = now
            log(step)
        elif what == 'BattleFormation':
            # MAA 是先把干员名单单独打一行：``[芬, 斑点, 黑角, 安赛尔, 克洛丝]``
            names = [str(x) for x in (data.get('formation') or []) if x]
            if names:
                log('[%s]' % ', '.join(names[:12]))
        elif what == 'StageInfo':
            name = data.get('name') or data.get('stage') or ''
            if name:
                log('关卡：%s' % name)
        elif what == 'StageDrops':
            # 2026-11-22 起这里**不再拿掉落当「这关成功」的证据**：实测这台
            # MAA（6.20.1）在 Copilot 任务里根本不发 StageDrops 回调
            # （asst.log 里 250 多条 SubTaskExtraInfo 全是 CopilotAction /
            # BattleFormation，一条 StageDrops 都没有），拿它判成功等于永远
            # 「一关都没挂」。留着这段只为万一某个版本又发了：有就写掉落明细，
            # 不写「战斗成功」。
            items = data.get('stats') or []
            if items:
                log('掉落：' + '、'.join(
                    '%s×%s' % (i.get('itemName'), i.get('addQuantity'))
                    for i in items[:4]))
        elif what == 'SanityBeforeStage':
            cur, mx = data.get('current_sanity'), data.get('max_sanity')
            if cur is not None:
                log('理智 %s/%s' % (cur, mx))
        elif what == 'FightTimes':
            if data.get('finished'):
                log('这一轮打满 %s 次' % (data.get('times_finished') or ''))
        elif what == 'ExceededLimit':
            task = str(data.get('task') or data.get('pre_task') or '')
            n = data.get('max_times') or data.get('exec_times') or '?'
            log('[警告] MAA 在「%s」上重试到上限（%s 次）'
                % (task.split('@')[-1] or '当前步骤', n))
        elif what == 'OperProgressDetail':
            _on_train_detail(data, log, state)
        elif what == 'OperProgressSummary':
            ok = int(data.get('success') or 0)
            bad_n = int(data.get('failed') or 0)
            skip = int(data.get('skipped') or 0)
            if state is not None:
                state['summary'] = {'success': ok, 'failed': bad_n, 'skipped': skip}
            log('培养结束：成功 %d，失败 %d，跳过 %d' % (ok, bad_n, skip))


HEARTBEAT_SECONDS = 90


def _wait_run(eng, total, log, state):
    """等任务跑完，长时间没动静就补一句「还在跑」

    以前直接 ``eng.wait(timeout=total)``：一场仗三分钟、一小时八个作业，
    日志面板上始终只有最开头那行「开始执行」，用户只能盯着 MAA 窗口猜。
    现在每 90 秒报一次进度（有心跳才说明真的还在动，不是卡死）。
    """
    deadline = time.time() + total
    t0 = time.time()
    restarts = 0
    while True:
        left = deadline - time.time()
        if left <= 0:
            return False
        if eng.wait(timeout=min(60.0, left), poll=0.5):
            return True
        if not eng.running():
            # 队列没人跑、也还没收到 AllTasksCompleted：可能是 start 撞上
            # 「上一批收尾」窗口时判断成「已被接上」，其实那条任务没人取
            # （见 Engine.start 的 a)/b) 说明）。补一次 start，最多一次。
            # **暂停（材料不足 / 缺芯片）时绝不重排**——那是我们主动叫停的，
            # 重排等于把刚停下的任务又拉起来。
            if (restarts < 1 and not (state or {}).get('pause')
                    and not eng.wait(timeout=1.0, poll=0.2)):
                restarts += 1
                log('队列没跑起来，重新排一次…')
                if eng.start():
                    continue
            return False
        if time.time() - (state or {}).get('last_log', t0) >= HEARTBEAT_SECONDS:
            cost = int(time.time() - t0)
            where = (state or {}).get('stage') or ''
            log('…还在跑%s（已 %d 分 %d 秒）'
                % (('：' + where) if where else '', cost // 60, cost % 60))
            if state is not None:
                state['last_log'] = time.time()


def _run_tail(eng, tail, spec, state, log, conn, d, deadline, can_reuse_nav=False):
    """排 tail 并开打；抄作业跨分区时中间停下来导航，返回「最后是否跑完」

    普通任务（Fight / Recruit / Infrast…）各是一条独立链，原样排进队列。
    抄作业不一样：**一条链里连着跑多关**，中途出不了手。所以这里把它前后的
    任务分开，Copilot 自己按分区一段一段 ``append + start``，段与段之间停
    MAA、点掉结算页、导航到下一段的分区（见文件顶部「分段导航」的说明）。

    ``can_reuse_nav``：首关是不是**刚刚**导航过。只有「调用方已经导航且中间
    没跑别的任务」时才为 True——这时第一段不用再导航，省 15 秒；其余情况
    （前面排了收菜/招募之类）界面早就不在关卡列表上了，必须重新导航。
    """
    idx = next((i for i, (t, _) in enumerate(tail) if t == 'Copilot'), -1)
    if idx < 0:
        # 没有抄作业：整队原样排，行为和以前完全一样
        for typ, params in tail:
            eng.append(typ, params)
            log('排队：%s %s' % (_label_of(typ), _brief(typ, params)))
        if tail and not eng.start():
            raise RuntimeError('AsstStart 返回失败')
        return _wait_run(eng, max(1.0, deadline - time.time()), log, state)

    before, (_, cop_params), after = tail[:idx], tail[idx], tail[idx + 1:]

    if before:
        for typ, params in before:
            eng.append(typ, params)
            log('排队：%s %s' % (_label_of(typ), _brief(typ, params)))
        if not eng.start():
            raise RuntimeError('AsstStart 返回失败')
        if not _wait_run(eng, max(1.0, deadline - time.time()), log, state):
            log('抄作业前面的任务没跑完，先停下')
            return False
        can_reuse_nav = False       # 前面的任务跑完，界面早就不在关卡列表了

    segs = segments_of(spec.get('copilotList') or [])
    if not segs:
        # 作业列表是空的（core_tasks 已经把空 Copilot 跳过了，正常走不到）
        eng.append('Copilot', cop_params)
        log('排队：%s %s' % (_label_of('Copilot'), _brief('Copilot', cop_params)))
        if not eng.start():
            raise RuntimeError('AsstStart 返回失败')
        return _wait_run(eng, max(1.0, deadline - time.time()), log, state)

    if len(segs) > 1:
        log('作业跨 %d 个分区，分段跑：%s'
            % (len(segs), ' → '.join(_seg_label(s) for s in segs)))

    finished = True
    for i, seg in enumerate(segs):
        left = deadline - time.time()
        if left <= 60:
            log('没时间跑剩下的 %d 段了（总时长到上限），停下' % (len(segs) - i))
            finished = False
            break
        if state.get('pause'):
            log('已暂停（%s），后面的段不跑了' % state['pause'])
            break
        if i:
            # 上一段最后一关打完停在结算页：先点一下回到活动关卡界面，再去
            # 新分区。顺序不能反——结算页上 MAA 的 ClickStageName OCR 不到
            # 关卡名，排上去就是白跑（用户 2026-11-22 的硬条件）。
            if not _leave_result(conn, spec, segs[i - 1]['to'], log):
                log('没能离开结算页，剩下的 %d 段先不跑了' % (len(segs) - i))
                finished = False
                break
            nav_ok, nav_why = _navigate(conn, seg['from'], log,
                                       client=spec.get('clientType'))
            log('切到新分区%s：%s' % ('完成' if nav_ok else '失败', nav_why))
            if not nav_ok:
                # 前面几关已经打完了，这里 raise 会把整轮记成失败。照样排下去
                # 让 MAA 自己试一把（它有时能从列表滑过去），日志留痕就够。
                log('警告：没导航到「%s」，这一段的作业可能全找不到关卡'
                    % seg['from'])
        elif not can_reuse_nav:
            nav_ok, nav_why = _navigate(conn, seg['from'], log,
                                       client=spec.get('clientType'))
            log('导航%s：%s' % ('完成' if nav_ok else '失败', nav_why))

        log('第 %d/%d 段：%s' % (i + 1, len(segs), _seg_label(seg)))
        eng.append('Copilot', _copilot_params(seg['rows'], spec))
        if not eng.start():
            raise RuntimeError('AsstStart 返回失败')
        if not _wait_run(eng, max(1.0, deadline - time.time()), log, state):
            log('第 %d 段没跑完就停了，后面的段不跑了' % (i + 1))
            finished = False
            break

    if after and finished and not state.get('pause'):
        for typ, params in after:
            eng.append(typ, params)
            log('排队：%s %s' % (_label_of(typ), _brief(typ, params)))
        if not eng.start():
            raise RuntimeError('AsstStart 返回失败')
        finished = _wait_run(eng, max(1.0, deadline - time.time()), log, state)
    return finished


def _nav_of(data):
    """换作业日志该显示哪个名字

    ``CopilotListLoadTaskFileSuccess`` 里的 ``stage_name`` 是作业内部的关卡 id
    （act55side_s01），用户看不懂；文件名是我们自己按代号存的
    （``TL-S-1_110074.json``），拿它前半段最直观。
    """
    fn = os.path.basename(str(data.get('file_name') or ''))
    if fn:
        return fn.rsplit('_', 1)[0] or fn
    return data.get('nav_name') or data.get('stage_name') or ''


def _cid_of(filename):
    """作业文件名 → copilotId（``SV-EX-1_41623.json`` → 41623）

    prepare_copilot 按「代号_id.json」落盘，所以这里能反查。重试要「换一份」
    就得知道当前用的是哪一份。
    """
    base = os.path.basename(str(filename or ''))
    if '_' not in base:
        return 0
    tail = base.rsplit('_', 1)[-1].rsplit('.', 1)[0]
    try:
        return int(tail)
    except ValueError:
        return 0


def _stage_of_file(data, state=None):
    """作业文件名 → 关卡代号（预检说打不了的和实际卡住的要能对上）

    优先查 ``spec['copilotList']`` 的 filename→stage_name 映射（准确）；
    没有映射时退回文件名前半段（``BB-S-3_10531.json`` → ``BB-S-3``）。
    """
    fn = os.path.basename(str(data.get('file_name') or ''))
    table = (state or {}).get('by_file') or {}
    if fn and fn in table:
        return table[fn]
    return (_nav_of(data) or '').strip()


def _mark_clear(data, log, state):
    """**已废弃**：以前靠 StageDrops 判定「这关打完了」

    2026-11-22 实测：MaaCore 6.20.1 在 Copilot 任务里不发 StageDrops 回调，
    这个函数永远进不来，结果就是每次挂完都显示「一关都没挂」。现在改成只
    报**跑过哪些关**（``CopilotListLoadTaskFileSuccess``，可靠）+ 任务链
    结果是成功还是失败，不再假装知道逐关成败。保留这个空壳是为了让老调用方
    （如果有）不炸，别再往里加判据。
    """
    return


def _mark_fail(ev, log, state):
    """任务链失败 → 明确指出「哪一关翻车、为什么」

    用户 2026-11-19 的要求：作业翻车时日志要看得见是哪关。以前收尾只有
    一句「[失败] Copilot」，得自己翻 asst.log 才知道卡在哪。
    """
    if state is None:
        return
    chain = str(ev.get('chain') or '')
    if 'Copilot' not in chain:
        return
    cur = (state.get('stage') or '').strip()
    if not cur:
        return
    if state.get('fail_stage') == cur and time.time() - state.get('fail_at', 0) < 10:
        return
    state['fail_stage'] = cur
    state['fail_at'] = time.time()
    why = str(ev.get('why') or '').strip()
    if why:
        try:
            from . import stageplan
            why = stageplan.explain_failure(why) or why
        except Exception:
            pass
    else:
        # MaaCore 的 TaskChainError 只带 taskchain/taskid，**没有原因字段**
        # （2026-11-22 实测：details 是空的）。只能拿刚才那条 SubTaskError 推：
        # Copilot@WaitUntilEndOfAction = 结算页认了 20 次没认出三星。不说这句
        # 的话日志只有「MAA 没给原因」，用户根本不知道干嘛要换作业重挂。
        sub = str(state.get('last_sub_error') or '')
        if sub.rsplit('@', 1)[-1] in ('WaitUntilEndOfAction', 'EndOfAction'):
            why = '结算不是三星（漏怪 / 作业没打过这关）'
    state.setdefault('results', []).append(
        {'stage': cur, 'ok': False, 'why': why, 'at': time.time(),
         'cid': state.get('cur_cid') or 0})
    log('✘ %s 翻车：%s' % (cur, why or 'MAA 没给原因（看上面的 [MAA] 报错）'))


def _tally(results):
    """逐关结果 → (成功的关, 仍然失败的关)；同一关以最后一次为准"""
    ok, bad = [], {}
    for r in results or []:
        name = str(r.get('stage') or '').strip()
        if not name:
            continue
        k = name.upper()
        if r.get('ok'):
            if k not in {x.upper() for x in ok}:
                ok.append(name)
            bad.pop(k, None)
        else:
            bad[k] = r
    return ok, list(bad.values())


def _leave_result(conn, spec, stage, log):
    """翻车后把界面退回**活动关卡界面**

    「这一局翻车了」这件事**只认 MaaCore 的回调**（``TaskChainError``，或
    ``WaitUntilEndOfAction`` 子任务失败 = 结算不是三星），不靠自己截图猜
    ——用户 2026-11-22 的原话：「失败识别应该以 maa 的失败通知为准，而不是
    自己识别有没有失败」。

    翻车之后界面一定停在「行动结束」结算页。用户同一天的实测口径：**点屏幕
    任意位置都会立刻回到活动关卡界面**，在那个界面重排作业集就能换作业开打，
    不需要重新唤醒/登录，也不需要写导航。所以这里**不认结算页**（星星和
    「行动结束」标题都是自识别，1 星 / 直接失败 / 活动间 UI 差异都可能失效），
    只做三步：

    1. 点一下 MAA 自己的 ClickCorner（``_RESULT_TAP``）——任务定义里写明
       「点屏幕右边不会点到掉落物品，也不会关掉关卡选择」，所以结算页上能
       推进，万一已经在地图上了也不会误触。
    2. 再判 ``stagenav.probe_bytes`` 是不是活动关卡地图（底部 tab 暗）。
       **第一帧不判**：结算页底部也是暗的，probe 会把它当成地图，于是「还没
       点就放行」，作业排在结算页上白跑一整轮——这正是用户说的「在失败界面
       重新挂作业是不会成功的」。点过一次之后结算页必然消失，probe 才有意义。
    3. 点三次还不在关卡界面（落到主页 / 基建 / 剧情）→ ``to_home`` + 完整
       导航兜底。这一段只保证「界面能接受新的作业集」，失败也不抛错——真开
       不了打，下一轮还有日志。
    """
    try:
        from . import pages, stagenav
    except Exception:
        return False
    if not conn:
        return False
    try:
        touch = stagenav.Touch(conn['adb_path'], conn['address'], log,
                               package=stagenav.game_package(spec.get('clientType')))
        alive, detail = touch.ready()
        if not alive:
            log('点结算页失败：adb 不可用（%s）' % detail)
            return False
        tapped = 0
        for _i in range(3):
            cur = touch.shot()
            if cur and tapped and stagenav.probe_bytes(cur) == 'map':
                log('已回到活动关卡界面，在这里重排作业')
                return True
            touch.tap(*_RESULT_TAP)
            tapped += 1
            time.sleep(1.4)
        cur = touch.shot()
        where = stagenav.probe_bytes(cur) if cur else '截不到图'
        log('点了 %d 次还不在活动关卡界面（现在是「%s」），回主页重新导航'
            % (tapped, where or '未知'))
        # 兜底：先回主页（认得基建确认框/剧情页，不会盲按返回），再走一遍
        # 完整导航。多花十几秒，换「一定在关卡菜单上」。
        pages.to_home(touch, log)
        ok, why = _navigate(conn, stage, log, client=spec.get('clientType'))
        log('重挂前重新导航%s：%s' % ('完成' if ok else '失败', why))
        return ok
    except Exception as e:              # noqa: BLE001 点结算页不能拖垮挂机
        log('点结算页出错：%s: %s' % (type(e).__name__, e))
        return False

def _retry_failed(eng, spec, state, log, d, deadline, conn=None):
    """翻车后换作业重挂：**只改翻车那一关，其余关原样**

    用户 2026-11-19 需求 3 的原话是「某关战斗失败了，修改作业集中该关卡的作业，
    换其他作业自动挂这关」；2026-11-22 又补了实测口径：「我刚刚确实是 2 星，但是
    agent 并没有帮我换作业重新挂……翻车后 agent 应该知道我翻车了，然后回到对应的
    关卡界面，更换作业集（只改那关，其他不变，帮我重新挂），然后任务继续」。

    所以这里做四件事：

    1. **只动翻车那一关**：从候选池取该关「下一份没试过」的作业；翻车关之后
       **还没打**的关沿用原来那份（旧实现会顺手把它们也换成下一份，白冒风险）。
    2. **没别的作业就同一份再试一次**：漏怪是偶发的，2 星重打一次经常就三星。
       以前「候选都试过」直接收工，把后面几关一起丢下——用户看到的「翻车后
       什么都不做」有一半是这个。同一份只补试一次（记在 ``same_retry``），
       不会死循环。
    3. **回到活动关卡界面**：翻车时游戏停在「行动结束」结算页，先点一下把界面
       带回活动关卡地图（``_leave_result``）。不点的话 MaaCore 的 ClickStageName
       在结算页上 OCR 不到关卡名，整轮重挂白跑。
    4. **重挂后按最新结果继续**：每轮结束重看 ``state['results']``，哪关还失败才
       继续重挂；都过了立刻收手，不会把打过的关再打一遍（旧实现每轮都从同一个
       起点重排，第二轮会白烧一遍理智）。

    返回 ``(swapped, mark)``；``mark`` 是重试前 ``eng.chains`` 的长度（可能是 0），
    收尾判失败要从它往后看，否则第一轮的失败会把重试成功的结果盖掉。真的没
    进入重试时返回 ``([], None)``——0 是合法的分界，不能拿它当「没重试」。
    """
    retry = spec.get('retry') or {}
    pool = retry.get('pool') or {}
    # 没有 retry（每关都只有一份作业、或上层没给候选池）也要走一轮：用户报的
    # 就是「2 星之后什么都不做」。maxRounds 缺省 2 = 首轮 + 一次同份重挂。
    rounds = int(retry.get('maxRounds') or 2)
    if state.get('pause'):
        log('已暂停（%s），不重挂' % state['pause'])
        return [], None
    # 重挂也没用的原因（理智不够 / 关卡没解锁 / 作业与关卡对不上），别烧时间
    why_all = ' '.join(str(r.get('why') or '')
                       for r in (state.get('results') or []) if not r.get('ok'))
    for k in ('理智不够', '关卡还没解锁', 'MAA 不认这个关卡'):
        if k in why_all:
            log('翻车原因不值得重挂（%s），跳过' % k)
            return [], None
    order = []
    for r in (spec.get('copilotList') or []):
        code = str(r.get('stage_name') or '').strip()
        if code:
            order.append({'stage': code,
                          'variant': 'raid' if r.get('is_raid') else 'normal',
                          'cid': _cid_of(r.get('filename'))})
    if not order:
        return [], None

    mark = len(eng.chains)
    # 首轮已经在用哪份作业：换过的不再换回来，否则 #102 → #202 → #102 打转
    tried = {}
    for k, v in (state.get('tried') or {}).items():
        tried[k] = {v}
    same = set(state.get('same_retry') or [])      # 已用「同一份」补试过的关
    skip = set(state.get('skip_retry') or [])      # 候选都试过、放弃的关
    swapped = []
    for rnd in range(2, rounds + 2):
        left = deadline - time.time()
        if left <= 30:
            log('没时间再重挂了（总时长到上限），停下')
            break
        bad = [b for b in _tally(state.get('results') or [])[1]
               if str(b.get('stage') or '').strip().upper() not in skip]
        if not bad:
            break                     # 上一轮重挂成功（或没有失败关）
        bad_up = {str(b.get('stage') or '').strip().upper() for b in bad}
        idx_bad = [i for i, o in enumerate(order) if o['stage'].upper() in bad_up]
        if not idx_bad:
            break
        start_idx = min(idx_bad)
        log('翻车了：%s，先回关卡界面再换个作业重挂'
            % '、'.join(order[i]['stage'] for i in idx_bad))
        new_ops, picked = [], []
        for i, o in enumerate(order):
            if i < start_idx:
                continue              # 前面已经打完的关不再重打
            key = '%s@%s' % (o['stage'].upper(), o['variant'])
            # 「当前在用的是哪份」= state['tried']（每换一次就更新）。order 里的
            # cid 只是首轮那份，拿它当「同一份」会退回更早的作业（实测：第二轮
            # 换回 #102 而不是刚在用的 #202，等于又换了一次却记成「同一份」）。
            cid = (state.get('tried') or {}).get(key) or o['cid']
            if i in idx_bad:
                cids = [x for x in (pool.get(key) or []) if x]
                used = tried.setdefault(key, {cid})
                nxt = next((c for c in cids if c not in used), None)
                if nxt is None:
                    if key in same or not cid:
                        skip.add(o['stage'].upper())
                        log('跳过 %s：候选作业都试过了，再挂也是同样结果'
                            % o['stage'])
                        continue
                    nxt = cid          # 没有别的作业了：同一份再试一次
                    same.add(key)
                    state['same_retry'] = sorted(same)
                    log('翻车重挂：%s 没有别的作业，同一份 #%s 再试一次'
                        % (o['stage'], nxt))
                else:
                    used.add(nxt)
                    state.setdefault('tried', {})[key] = nxt
                    log('换作业重挂：%s %s #%s → #%s'
                        % (o['stage'],
                           '突袭' if o['variant'] == 'raid' else '普通',
                           cid or '?', nxt))
                picked.append({'stage': o['stage'], 'variant': o['variant'],
                               'from': cid, 'to': nxt})
                cid = nxt
            new_ops.append({'copilotId': cid, 'stageCode': o['stage'],
                            'variant': o['variant']})
        if skip:
            state['skip_retry'] = sorted(skip)
        if not new_ops:
            log('没有能重挂的作业了，停下（翻车：%s）'
                % '、'.join(order[i]['stage'] for i in idx_bad))
            break
        # 翻车之后游戏停在**失败结算页**（「作战失败 / 行动结束」那一层）。
        # 用户 2026-11-19：「随便点一下回关卡界面，在那个界面重新运行作业集
        # 就能换作业，不需要重新唤醒登录」；2026-11-22 又补了硬条件：「在失败
        # 界面重新挂作业是不会成功的，需要在关卡菜单界面重挂」。
        # 所以**先离开结算页、再下载/排作业**——顺序反了的话，界面还停在
        # 结算页时 MaaCore 的 ClickStageName OCR 不到关卡名，整轮白跑。
        # _leave_result 点一下屏幕，再用 probe 确认回到了活动关卡界面；实在
        # 回不去就回主页重走一遍导航，仍然不行才放弃这一轮。
        if not _leave_result(conn, spec, order[start_idx]['stage'], log):
            log('没能回到活动关卡界面，这轮不重挂了（在结算页上排作业一定白跑）')
            break
        rows = prepare_copilot(new_ops, d, account=spec.get('accountFile'),
                               log=lambda m: log('  ' + m))
        swapped.extend(picked)
        state['stage'] = ''
        state['cur_cid'] = 0
        state['by_file'] = {os.path.basename(r.get('filename') or ''):
                            r.get('stage_name') for r in rows if r.get('filename')}
        state['cid_of'] = {os.path.basename(r.get('filename') or ''):
                           _cid_of(r.get('filename')) for r in rows if r.get('filename')}
        segs = segments_of(rows)
        if not segs:                    # 正常走不到（new_ops 非空就该有行）
            log('重挂的作业一份都没落盘，停下')
            break
        if len(segs) > 1:
            log('重挂的关也跨 %d 个分区（%s），同样分段跑'
                % (len(segs), ' → '.join(_seg_label(s) for s in segs)))
        log('第 %d 轮：从「%s」起重挂 %d 关%s'
            % (rnd, order[start_idx]['stage'], len(new_ops),
               '（前面 %d 关不再重打）' % start_idx if start_idx else ''))
        timed_out = False
        for si, seg in enumerate(segs):
            if si:
                # 上一段打完停在结算页：先点一下回关卡界面，再导航到新分区
                # （和首轮分段同一个道理，见 _run_tail）
                if not _leave_result(conn, spec, segs[si - 1]['to'], log):
                    log('重挂时没能离开结算页，剩下的段不跑了')
                    break
                nav_ok, nav_why = _navigate(conn, seg['from'], log,
                                           client=spec.get('clientType'))
                log('重挂切到新分区%s：%s'
                    % ('完成' if nav_ok else '失败', nav_why))
                if state.get('pause'):
                    log('已暂停（%s），后面的段不跑了' % state['pause'])
                    break
            eng.append('Copilot', _copilot_params(seg['rows'], spec))
            if not eng.start():
                raise RuntimeError('重挂时 AsstStart 返回失败')
            if not _wait_run(eng, max(60.0, deadline - time.time()), log, state):
                log('第 %d 轮第 %d 段超时，先停下' % (rnd, si + 1))
                eng.stop()
                timed_out = True
                break
        if timed_out:
            break
        # 这一轮的链一条都没报错 = 重挂成功。最后那一关没有「下一关」可以换，
        # 所以 states['results'] 里那条旧的失败记录不会被 ok 覆盖——不看链的话
        # 会接着再排一轮（把刚打完的关又打一遍）。
        if not [c for c in eng.chains[mark:] if not c.get('ok')]:
            log('第 %d 轮重挂没再报失败，继续往下走' % rnd)
            break
    return swapped, mark


def _wait_chain(eng, chain, log, timeout=300):
    """等某一条任务链跑完（不是等全部任务）

    抄作业的两段式执行要用：唤醒完了才点导航，导航完了才把作业排进去。
    等 AllTasksCompleted 会一直等到超时——那时后面还没排任务。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(c.get('chain') == chain for c in eng.chains):
            return True
        if not eng.running() and eng.chains:
            return True
        if eng.error:
            return False
        time.sleep(0.5)
    log('等「%s」超时（%d 秒），继续往下走' % (_label_of(chain), timeout))
    return False


def plan_groups(rows, spec_base=None):
    """按「MAA 目录 + adb 地址」把账号分组：同组=同一台模拟器

    2026-11-22 用户要求：每个账号可以绑自己的 MAA（也就连着不同的模拟器）。

    - **不同模拟器** → 各开一条线程，真正同时跑（多开）；
    - **同一台模拟器** → 只能排队，一个号跑完切下一个（一台模拟器同一时刻
      只能登一个号）。

    返回 ``[(key, {'dir','address','rows'})]``，顺序按账号出现顺序。

    ``r['address']``（用户点名的模拟器）优先于档案里的地址：MAA 里八份档案
    可能全指向同一台，光看档案分不出两台（2026-11-22 用户实测）。
    """
    spec_base = spec_base or {}
    groups = {}
    for r in rows or []:
        d = str(r.get('maaDir') or spec_base.get('maaDir') or '').strip()
        try:
            conn = _connection(d, r.get('profile') or None)
        except Exception:
            conn = {'address': '', 'adb_path': ''}
        addr = str(r.get('address') or conn.get('address') or '')
        key = (os.path.normcase(os.path.abspath(d or '.')), addr)
        g = groups.setdefault(key, {'dir': d, 'address': addr,
                                    'rows': []})
        g['rows'].append(r)
    return list(groups.items())


def _run_group(g, spec_base, tid, log):
    """一个模拟器上的账号排队跑（组内串行）"""
    out = []
    d = g['dir'] or spec_base.get('maaDir') or ''
    # 同一个模拟器不能两个人同时戳：MAA 自己在跑就先关掉，否则两条 adb 连接
    # 互相抢截图，表现成「点了没反应」。
    if d and maa.running_in(d):
        log('MAA 正在运行，先关掉（否则两边抢模拟器）…')
        maa.close(d)
    for i, r in enumerate(g['rows'], 1):
        log('=' * 20 + ' [%d/%d] %s ' % (i, len(g['rows']),
                                         r.get('name') or '') + '=' * 20)
        log('模拟器 %s（%s）；切号名 %s'
            % (g['address'] or '默认', d or '默认 MAA',
               r.get('accountName') or '无'))
        cp = []
        if r.get('ops'):
            log('准备 %d 份作业…' % len(r['ops']))
            cp = prepare_copilot(r['ops'], d, account=r.get('file'),
                                 log=lambda m: log('  ' + m))
        spec = dict(spec_base)
        spec.update({
            'maaDir': d,                    # 这个号的 MAA（run_core 按它连 adb）
            'accountName': r.get('accountName') or '',
            'switchAccount': True,
            'clientType': client_type_of(r.get('channel')),
            'copilotList': cp,
            # 重挂（_retry_failed）换作业时按同一账号练度做替换（#92）
            'accountFile': r.get('file') or '',
        })
        try:
            res = run_core(spec, tid)
            out.append({'name': r.get('name') or '',
                        'file': r.get('file') or '',
                        'emulator': g['address'] or '',
                        'ok': bool(res.get('ok')),
                        'tasks': res.get('tasks'),
                        'ran': res.get('stage_trail') or [],
                        'error': res.get('error') or ''})
        except Exception as e:
            log('「%s」失败：%s: %s' % (r.get('name') or '', type(e).__name__, e))
            out.append({'name': r.get('name') or '', 'file': r.get('file') or '',
                        'emulator': g['address'] or '',
                        'ok': False, 'error': '%s: %s' % (type(e).__name__, e)})
    return out


def run_plan(rows, spec_base, tid=None):
    """多账号挂机：同一台模拟器排队切号，不同模拟器并行多开

    用户 2026-11-22 的口径：
      - 五个号共用一个 MAA / 一台模拟器 → 一个号跑完切下一个；
      - 两个号各自绑了不同 MAA（不同模拟器）→ 直接多开，两边同时动。

    每个账号都新建 Engine（``CopilotTask`` 在同一个实例上不能二次
    set_params，会报 ``already set params``），跑完销毁，干净。
    """
    log = tasks.log_factory(tid) if tid else (lambda *_: None)
    groups = plan_groups(rows, spec_base)
    if len(groups) > 1:
        log('检测到 %d 台模拟器，多开并行：%s'
            % (len(groups), '、'.join('%s（%d 个号）'
                                     % (g['address'] or '默认', len(g['rows']))
                                     for _, g in groups)))
    report = []

    def run_one(g):
        got = _run_group(g, spec_base, tid, log)
        with _GROUPS_LOCK:
            report.extend(got)

    def run_one_proc(idx, g):
        """多开：这台模拟器交给独立进程（同进程两个 MaaCore 会抢命名管道，
        见 multiproc 的模块注释——这是「说好并行、另一台不动」的真因）"""
        _res, got = multiproc.run_group(
            'plan', tid, idx, {'group': g, 'spec': spec_base}, log,
            who=g.get('address') or '默认模拟器')
        with _GROUPS_LOCK:
            report.extend(got or [])
        if not got:
            with _GROUPS_LOCK:
                for r in (g.get('rows') or []):
                    report.append({'file': r.get('file'), 'name': r.get('name'),
                                   'ok': False,
                                   'error': '子进程异常（没交回结果）'})

    if len(groups) <= 1:
        for _, g in groups:
            run_one(g)
    else:
        log('（每个模拟器一个独立进程）')
        ths = []
        for i, (_k, g) in enumerate(groups, 1):
            th = threading.Thread(target=run_one_proc, args=(i, g), daemon=True,
                                  name='plan-%s' % (g['address'] or 'emu'))
            th.start()
            ths.append(th)
        for th in ths:
            th.join()
    done = sum(1 for x in report if x['ok'])
    log('=' * 34)
    log('跑完：%d/%d 个账号正常结束（%d 台模拟器）'
        % (done, len(report), len(groups)))
    return {'report': report, 'done': done, 'total': len(report),
            'groups': len(groups)}


def _label_of(ttype):
    if ttype == 'OperProgress':
        # 不在 TASK_DEFS 里：它只走 MaaCore 直连，不进 GUI 的一键长草队列
        return '干员培养'
    for key, t, label, _ in TASK_DEFS:
        if t == ttype:
            return label
    return ttype


def _brief(ttype, params):
    if ttype == 'StartUp':
        return '（%s%s）' % (
            '官服' if params.get('client_type') == 'Official' else params.get('client_type'),
            '，切到 ' + params['account_name'] if params.get('account_name') else '')
    if ttype == 'Fight':
        t = params.get('times')
        return '%s ×%s' % (params.get('stage') or '',
                           '刷到理智不足' if (t or 0) >= 999 else t)
    if ttype == 'Copilot':
        n = len(params.get('copilot_list') or [])
        names = [it.get('stage_name') for it in (params.get('copilot_list') or [])][:6]
        return '%d 份作业%s%s' % (n, '：' + '、'.join(x for x in names if x) if names else '',
                                 '，自动编队' if params.get('formation') else '')
    if ttype == 'OperProgress':
        plans = params.get('plans') or []
        names = [str(p.get('name') or '') for p in plans if p.get('name')]
        return '（%d 名：%s）' % (len(plans), '、'.join(names[:6]))
    return ''
