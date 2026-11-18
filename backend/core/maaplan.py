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

from . import acctname, maa, maalog, settings, tasks

PROFILE_PREFIX = 'PRTS-'
_SAFE = re.compile(r'[\\/:*?"<>|]')

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


def prepare_copilot(ops, maa_dir, log=None):
    """下载并落盘作业，返回 MaaCore 的 copilot_list 条目

    ``filename`` **必须是绝对路径**（实测：相对路径不报错，但任务静默不入队）。

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
    os.makedirs(_copilot_dir(maa_dir), exist_ok=True)
    rows, done = [], {}
    for it in ops or []:
        cid = int(it['copilotId'])
        code = it.get('stageCode') or str(cid)
        if cid not in done:
            content, flags, sname, notes, pre = copilot.fetch_prepared(cid)
            fname = '%s_%s.json' % (_SAFE.sub('_', code).strip() or 'stage', cid)
            path = os.path.normpath(os.path.join(_copilot_dir(maa_dir), fname))
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(content, f, ensure_ascii=False, indent=2)
            done[cid] = (path, sname, flags)
            log('%s作业 #%s %s' % ('取预下载的' if pre else '下载', cid,
                                   it.get('title') or ''))
            for n in notes or []:
                log('    修正：%s' % n)
        path, sname, flags = done[cid]
        variants = copilot.entry_variants(flags, it.get('variant'), sname)
        for raid in variants:
            rows.append({'filename': path, 'stage_name': _clean_stage(code),
                         'is_raid': bool(raid)})
    return rows


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
            # 用户在 MAA「自动战斗」页常勾的这三项，默认都开：
            # formation          自动编队
            # support_unit_usage 借干员：0 不借 / 1 仅缺人时 / 2 始终
            # use_sanity_potion  理智不够时吃理智药
            try:
                support = int(form.get('support', 1))
            except (TypeError, ValueError):
                support = 1
            p = {'copilot_list': rows,
                 'formation': bool(form.get('formation', True)),
                 'add_trust': False,
                 'ignore_requirements': False,
                 'support_unit_usage': support if support in (0, 1, 2) else 1,
                 'use_sanity_potion': bool(form.get('sanityPotion', True)),
                 'formation_index': 1}
            out.append(('Copilot', p))
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


def _navigate(conn, stage, log, client=None):
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
                               package=stagenav.game_package(client))
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
    log('连接 %s（%s）…' % (conn['address'], conn['config']))
    # 抄作业时 MAA 每换一份作业就发一条 CopilotListLoadTaskFileSuccess，
    # 文件名是我们自己按代号存的（BB-S-3_10531.json）。记下来就能知道
    # 「跑到第几关、卡在哪一关」——预检说打不了的和实际卡住的才能对上。
    cp_rows = spec.get('copilotList') or []
    by_file = {os.path.basename(r.get('filename') or ''): r.get('stage_name')
               for r in cp_rows if r.get('filename')}
    # state 是回调线程和跑机线程共用的「进度便签」：
    #   stage/trail  跑到第几关（收尾结算用）
    #   last_log     最后一次往任务日志写字的时间（心跳线程用它算「多久没动静」）
    #   pause        材料不足之类的暂停原因（回调里置位，run_core 收尾时上报）
    #   done/summary 干员培养的成果（skill_train 用来算还剩哪些没做）
    state = {'stage': '', 'trail': [], 'by_file': by_file,
             'actions': {}, 'deployed': [], 'nodes': {},
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

        # 队列头是 StartUp 就先只跑它，剩下的一律等登录完成再排——**绝不能
        # 让导航和唤醒并发**：唤醒点 START / 登录 / 关公告的那几十秒里界面
        # 根本不在主界面，导航按下去全落在启动页甚至安卓桌面上（2026-11-17
        # 中午实测：nav-fail 现场图就是一张桌面截图，用户看到的是「到主页后
        # 没导航，游戏直接被关了」）。
        started_login = bool(seq) and seq[0][0] == 'StartUp'
        head, tail = (seq[:1], seq[1:]) if started_login else (seq, [])

        for typ, params in head:
            eng.append(typ, params)
            log('排队：%s %s' % (_label_of(typ), _brief(typ, params)))
        log('开始执行（%s）' % _label_of(head[0][0]))
        if not eng.start():
            raise RuntimeError('AsstStart 返回失败')

        if started_login:
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
                nav_ok, nav_why = _navigate(conn, first, log,
                                            client=spec.get('clientType'))
                if not nav_ok:
                    # 老故事集/自建模板的关卡，MAA 自己 100% 找不到（它的入口
                    # 模板在当前 UI 上全失效，只会横向滑主线）。这种情况宁可
                    # 明确报错，也别把作业排下去让它乱滑 30 秒再失败。
                    from . import stagenav
                    if stagenav.tpl_path_of(first) or stagenav.event_of(first)[0]:
                        raise RuntimeError('导航没到位（%s），不再把作业交给 MAA'
                                           % nav_why)
                    log('导航没成功，交给 MAA 自己试（当期活动它认得）')

            # ---- 第三段：排作业，开打 -----------------------------------------
            for typ, params in tail:
                eng.append(typ, params)
                log('排队：%s %s' % (_label_of(typ), _brief(typ, params)))
            if tail and not eng.start():
                raise RuntimeError('AsstStart 返回失败')
        elif first and not spec.get('noNav'):
            # 没有 StartUp 的任务链（core_tasks 会强制补，正常走不到）：
            # 先导航再跑，结果必须检查——游戏没开时导航失败要立刻暴露。
            # noNav 直跑模式例外：不导航，Copilot 从当前位置自己接手。
            nav_ok, nav_why = _navigate(conn, first, log,
                                        client=spec.get('clientType'))
            if not nav_ok:
                from . import stagenav
                if stagenav.tpl_path_of(first) or stagenav.event_of(first)[0]:
                    raise RuntimeError('导航没到位（%s），不再把作业交给 MAA'
                                       % nav_why)

        total = timeout or (60 * 60 * 8)
        finished = _wait_run(eng, total, log, state)
        if not finished and not state.get('pause'):
            log('等了 %d 分钟还没跑完，先停下' % (total // 60))
            eng.stop()
        if state.get('pause'):
            log('已暂停：%s' % state['pause'])
        # 成功与否只看有没有任务链报错：AllTasksCompleted 在链报错后照样会来，
        # 单纯等它等于「永远成功」。次数打满被 Stop 的那次是正常结束。
        bad = [c for c in eng.chains if not c.get('ok')]
        stopped = [c for c in eng.chains if c.get('stopped')]
        if stopped and not state.get('pause'):
            log('正常收尾：%s（次数打满 / 理智不足 / 手动停）'
                % '、'.join(c['chain'] for c in stopped[:4]))
        # 暂停是「用户该去补材料了」，不是失败：MAA 已经按计划把能做的都试过，
        # 这时报一串 [失败] 只会让人以为程序坏了。
        for c in bad:
            if not state.get('pause'):
                log('[失败] %s %s' % (c['chain'], c.get('why') or ''))
        failed_stage = state['stage'] if bad else ''
        failed_why = next((c.get('why') for c in bad if c.get('why')), '') if bad else ''
        return {'ok': (finished and not bad) or bool(state.get('pause')),
                'tasks': len(seq),
                'chains': eng.chains,
                'timed_out': not finished,
                'failed_stage': failed_stage,
                'failed_why': failed_why,
                'stage_trail': state['trail'],
                'paused': state.get('pause') or '',
                'pauseDetail': state.get('pause_detail') or '',
                'trained': state.get('done') or {},
                'trainSummary': state.get('summary') or {},
                'error': '; '.join(eng.errors) or eng.error or ''}
    finally:
        tap_stop.set()
        if tap_thread.is_alive():
            tap_thread.join(timeout=2)
        eng.close()


# MaaCore 的 CopilotAction：抄作业时每做一个动作就报一次
_ACTION_CN = {
    'Deploy': '部署', 'Retreat': '撤退', 'UseSkill': '开技能',
    'SwitchSpeed': '切二倍速', 'SkillDaemon': '技能就绪自动开',
    'BulletTime': '子弹时间', 'StartAction': '开始行动',
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
    'BattleStartPre': '准备编队',
    'BattleStartAll': '开始行动',
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

    why = _DETAIL_CN.get(detail, detail or '失败')
    tail = ('（计划 %s）' % plan_brief(plan)) if plan else ''
    log('✘ %s：%s%s' % (name, why, tail))
    if detail != 'ResourceInsufficient' or state is None:
        return
    # 材料不足不是「跑挂了」，是「该去补材料」：MAA 自己会接着试下一条计划，
    # 那样只会一路失败下去。这里把它停住（暂停），把缺什么写清楚。
    if not state.get('pause'):
        state['pause'] = '%s 材料不足' % name
        state['pause_detail'] = tail
        log('材料不足，暂停培养。补齐材料后说一声「继续培养」，'
            '会按没做完的部分接着来')
        stop = state.get('stop')
        if stop:
            # 不能在回调线程里同步 AsstStop（它会等工作线程收尾，有互锁风险），
            # 丢给一个短命线程去叫停
            threading.Thread(target=stop, daemon=True,
                             name='maa-pause-stop').start()


def _on_core_event(ev, log, tid, state=None):
    """把引擎事件转成任务日志（跑机面板实时能看到）"""
    kind = ev.get('kind')
    if kind == 'error':
        log('[错误] %s' % (ev.get('text') or ''))
    elif kind in ('chain_start', 'chain_done', 'chain_failed', 'chain_stop'):
        log(ev.get('text') or '')
    elif kind == 'connected':
        log('模拟器已连接')
    elif kind == 'done':
        log('全部任务跑完')
    elif kind == 'node':
        # 只报「换了一个阶段」，同一阶段反复来不写（SubTaskStart 一秒十几条）
        label = _subtask_label(ev.get('sub') or '')
        if not label:
            return
        now = time.time()
        last = (state or {}).get('nodes') or {}
        if last.get('label') == label and now - last.get('at', 0) < 20:
            return
        if state is not None:
            state['nodes'] = {'label': label, 'at': now}
        log('· %s' % label)
    elif kind == 'extra':
        what = ev.get('what')
        data = ev.get('data') or {}
        if what == 'CopilotListLoadTaskFileSuccess':
            code = _stage_of_file(data, state)
            if code:
                log('换作业：%s' % code)
                if state is not None:
                    state['stage'] = code
                    if code not in state['trail']:
                        state['trail'].append(code)
        elif what == 'CopilotAction':
            act = str(data.get('action') or '')
            target = str(data.get('target') or '')
            # 白名单外的动作（Output / SkillUsage 这些内部记账）不写进任务日志，
            # 否则面板上会冒出一串看不懂的英文
            label = _ACTION_CN.get(act)
            if not label:
                return
            now = time.time()
            if act == 'Deploy':
                done = (state or {}).setdefault('deployed', [])
                if target and target not in done:
                    done.append(target)
                    log('部署 %s' % target)
                return
            if act == 'UseSkill':
                # 一场仗要开十几次技能，15 秒内只说一次，不然日志全是「开技能」
                if now - (state or {}).get('skill_at', 0) < 15:
                    return
                if state is not None:
                    state['skill_at'] = now
                log('开技能')
                return
            seen = (state or {}).setdefault('actions', {})
            if act in seen:
                return
            seen[act] = now
            log(label + ((' %s' % target) if target else ''))
        elif what == 'BattleFormation':
            names = [str(x) for x in (data.get('formation') or []) if x]
            if names:
                log('编队：%s' % '、'.join(names[:12]))
        elif what == 'StageInfo':
            name = data.get('name') or data.get('stage') or ''
            if name:
                log('关卡：%s' % name)
        elif what == 'StageDrops':
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
    while True:
        left = deadline - time.time()
        if left <= 0:
            return False
        if eng.wait(timeout=min(60.0, left), poll=0.5):
            return True
        if not eng.running():
            return eng.wait(timeout=1.0, poll=0.2)
        if time.time() - (state or {}).get('last_log', t0) >= HEARTBEAT_SECONDS:
            cost = int(time.time() - t0)
            where = (state or {}).get('stage') or ''
            log('…还在跑%s（已 %d 分 %d 秒）'
                % (('：' + where) if where else '', cost // 60, cost % 60))
            if state is not None:
                state['last_log'] = time.time()


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


def run_plan(rows, spec_base, tid=None):
    """多账号串行跑：一个账号跑完再切下一个

    一个模拟器同一时刻只能登一个号，所以这里天然是串行的。每个账号都新建
    Engine（``CopilotTask`` 在同一个实例上不能二次 set_params，会报
    ``already set params``），跑完销毁，干净。
    """
    log = tasks.log_factory(tid) if tid else (lambda *_: None)
    d = spec_base.get('maaDir') or ''
    # 同一个模拟器不能两个人同时戳：MAA 自己在跑就先关掉，否则两条 adb 连接
    # 互相抢截图，表现成「点了没反应」。
    if d and maa.running_in(d):
        log('MAA 正在运行，先关掉（否则两边抢模拟器）…')
        maa.close(d)
    report = []
    for i, r in enumerate(rows, 1):
        log('=' * 34)
        log('[%d/%d] %s（切号名 %s）' % (i, len(rows), r.get('name') or '',
                                        r.get('accountName') or ''))
        cp = []
        if r.get('ops'):
            log('准备 %d 份作业…' % len(r['ops']))
            cp = prepare_copilot(r['ops'], d,
                                 log=lambda m: log('  ' + m))
        spec = dict(spec_base)
        spec.update({
            'accountName': r.get('accountName') or '',
            'switchAccount': True,
            'clientType': client_type_of(r.get('channel')),
            'copilotList': cp,
        })
        try:
            res = run_core(spec, tid)
            report.append({'name': r.get('name') or '',
                           'ok': bool(res.get('ok')),
                           'tasks': res.get('tasks'),
                           'error': res.get('error') or ''})
        except Exception as e:
            log('「%s」失败：%s: %s' % (r.get('name') or '', type(e).__name__, e))
            report.append({'name': r.get('name') or '', 'ok': False,
                           'error': '%s: %s' % (type(e).__name__, e)})
    done = sum(1 for x in report if x['ok'])
    log('=' * 34)
    log('跑完：%d/%d 个账号正常结束' % (done, len(report)))
    return {'report': report, 'done': done, 'total': len(report)}


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
