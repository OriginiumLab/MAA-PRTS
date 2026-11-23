# -*- coding: utf-8 -*-
"""MaaCore 直连：自己当 MAA 的壳，把任务交给核心执行

为什么不写配置让 GUI 自己跑
--------------------------
MAA 的「一键长草」任务队列**不支持 Copilot 任务**。v6.20.1 实测：往
``TaskQueue`` 里塞一个 ``CopilotTask``，MAA 启动时
``TaskQueueViewModel.InitializeItems()`` 直接抛 ``NotImplementedException``，
任务列表整块空白（连别的任务也看不见）。而「自动战斗」页的作业列表只能在
GUI 里手点「开始」。

所以「自动导入作业集 + 自动按开始」这件事，只能自己驱动 MaaCore ——
这也是 MAA GUI 本身的做法（``AsstAppendTask(Copilot, task)`` + ``AsstStart()``）。

顺带解决的两件事
----------------
- **小活动**：``Custom`` 任务类型（``task_names``）能跑小工具页那些功能，
  GUI 不给你加进队列，核心接受。
- **进度**：回调里有 ``SubTaskStart`` / ``TaskChain_Completed`` / 掉落统计，
  比读日志实时；同时 MaaCore 照样写 ``debug/asst.log``，M2 的停滞检测继续有效。

三个坑（都是实测出来的，别改）
----------------------------
1. ``Copilot`` 的 ``filename`` **必须是绝对路径**。相对路径不报错，就是
   ``set_params`` 返回 0，任务静默不入队。
2. ``copilot_list`` 每项的键是 ``filename`` / ``stage_name`` / ``is_raid``。
   写成 ``file_name`` 会抛 C++ 异常，整个进程 0xe06d7363 崩掉。
3. ``AsstCreateEx`` 必须在 ``AsstSetUserDir`` + ``AsstLoadResource`` 之后调，
   否则返回 NULL —— 而且没有回调你就看不到任何进度。
"""
from __future__ import annotations

import ctypes
import json
import os
import threading
import time

# 回调消息号。**必须照 MaaCore 的 AsstMsg 抄，别自己顺手排**：
# 10000 是 TaskChainError 不是 Start。之前整整错位一位，结果「开始」被报成
# 「完成」、「成功」被报成「失败」，日志里永远一片失败。
# 实测钉住（asst.log 的 append_callback 与收到的号一一对齐）：
#   TaskChainStart=10001  TaskChainCompleted=10002  TaskChainStopped=10004
#   TaskChainError=10000
#
# **子任务是 20000 段，不是 70000 段**（2026-11-22 用户报「战斗日志没有 MAA
# 那种提示」才查出来）：这个项目最初抄的是 MaaCore 4.x 的号，5.x/6.x 的
# AsstMsg 已经把 SubTask* 挪到 20000~20004。写了 70001 的后果是——任务链级
# 的「开始任务/任务失败」照常显示，但「开始编队 / 选择干员 / 当前步骤: 部署」
# 这些**全被子任务判断挡掉**（`_translate` 里一个都不匹配），一场仗打完日志
# 里一个字都没有。真机实测回调码：Depot 跑一趟 = 10001,10002,20001,20002,20003。
MSG_INTERNAL_ERROR = 0
MSG_INIT_FAILED = 1
MSG_CONNECTION_INFO = 2
MSG_ALL_TASKS_COMPLETED = 3
MSG_TASK_ERROR = 10000
MSG_TASK_START = 10001
MSG_TASK_COMPLETED = 10002
MSG_TASK_EXTRA = 10003
MSG_TASK_STOPPED = 10004
MSG_SUBTASK_ERROR = 20000
MSG_SUBTASK_START = 20001
MSG_SUBTASK_COMPLETED = 20002
MSG_SUBTASK_EXTRA = 20003
MSG_SUBTASK_STOPPED = 20004

# 老核心（4.x）用 70000 段发子任务：两段都认，换核心不至于又哑一次
_SUBTASK_OLD = {70000: MSG_SUBTASK_ERROR, 70001: MSG_SUBTASK_START,
                70002: MSG_SUBTASK_COMPLETED, 70003: MSG_SUBTASK_EXTRA,
                70004: MSG_SUBTASK_STOPPED}

CONNECT_FAILED = {MSG_INIT_FAILED, MSG_CONNECTION_INFO}

# 回调签名：void(int msg, const char* details_json, void* custom_arg)
ASST_CALLBACK = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p)


class EngineError(RuntimeError):
    pass


def _utf8(s):
    return str(s).encode('utf-8')


# 活着的实例：停挂机要能隔着线程把正在跑的那个叫停（这时 MAA 进程压根没起，
# 按进程名去关是关不掉的）
_LIVE = set()
_LIVE_LOCK = threading.Lock()


def stop_live(owners=None):
    """叫停直连实例（只 AsstStop，不销毁，让调用方的 wait() 自己收到结束）

    ``owners``：只停这些账号的实例（账号文件名；空 = 全停）。用户 2026-11-23
    报的原话「停止的话不能指定停止，会一起停其他任务」——之前只有一个全停
    入口，点一下两台模拟器一起断，另一台正在打的关就白烧了。

    匹配走 :func:`owner_match`，所以传文件名、``示例昵称#0384 bilibili服`` 这种
    去掉扩展名的写法、昵称都能对上。
    """
    with _LIVE_LOCK:
        live = [e for e in _LIVE if e._handle]
    n = 0
    for e in live:
        if getattr(e, 'stopped', False):
            continue                    # 已经停过的别重复计数
        if owners and not owner_match(e.owner, owners):
            continue
        try:
            e.stop(external=True)
            n += 1
        except Exception:
            pass
    return n


def owner_match(owner, wants):
    """这个实例的归属算不算 ``wants`` 里的一个

    ``owner`` 是引擎挂的账号文件名（``示例昵称#0384 bilibili服.json``，没挂就是
    空串）。比较时忽略扩展名与路径，并允许传昵称/别名——用户在聊天里说的是
    「b 服那个号」「示例昵称#0384」，不会带 ``.json``。
    """
    if not owner:
        return False
    o = os.path.basename(str(owner))
    stem = o[:-5] if o.lower().endswith('.json') else o
    for w in (wants or ()):
        s = os.path.basename(str(w or '').strip())
        if not s:
            continue
        s2 = s[:-5] if s.lower().endswith('.json') else s
        if o == s or stem == s2 or stem.split(' ')[0] == s2:
            return True
    return False


def owner_match_any(owners, wants):
    """一组账号里有任意一个被点名就算命中（多开子进程停止用）"""
    return any(owner_match(o, wants) for o in (owners or ()))


def stop_all_live():
    """叫停所有直连实例（旧入口，等价于 ``stop_live(None)``）"""
    return stop_live(None)


def live_others(exclude=None):
    """这个进程里还有几个开着的 MaaCore（不含 ``exclude``）→ 数量

    用来防「第二个实例把第一个跑挂」：MaaCore 的 ``AsstLoadResource`` /
    ``AsstCreateEx`` 动的是**进程级**全局资源，同一个进程里再开一个实例，
    前一个正在跑的任务链会被直接打断（还只发 ``TaskChainStopped``，不是
    TaskChainError——上层完全看不出异常）。
    """
    with _LIVE_LOCK:
        return len([e for e in _LIVE if e._handle and e is not exclude])


# 「本进程允许跑 MaaCore」的唯一名额。抢不到就说明已经有一路在这个进程里
# 跑着，新的一路必须换独立进程（见 multiproc 的模块注释）。
_INPROC = threading.Lock()


def claim_inproc():
    """抢本进程的跑机名额 → True 抢到 / False 已经有一路在跑

    抢到了**必须**在跑完后 ``release_inproc()``（用 try/finally）。这个名额
    和模拟器、账号都无关：它防的是「同一进程里两个 MaaCore 互相拆台」。
    """
    return _INPROC.acquire(blocking=False)


def release_inproc():
    try:
        _INPROC.release()
    except RuntimeError:                    # 没抢到过就释放：忽略，别炸调用方
        pass


class Engine:
    """一个 MaaCore 实例

    生命周期：``open()`` → ``connect()`` → ``append()`` × N → ``start()``
    → 等 ``AllTasksCompleted`` → ``close()``。
    """

    def __init__(self, maa_dir, on_event=None, owner=''):
        self.maa_dir = os.path.abspath(maa_dir)
        self.dll_path = os.path.join(self.maa_dir, 'MaaCore.dll')
        if not os.path.isfile(self.dll_path):
            raise EngineError('找不到 MaaCore.dll：%s' % self.dll_path)
        self._lib = ctypes.CDLL(self.dll_path)
        self._bind()
        self._handle = None
        self._cb = None
        self._events = []
        # 事件是**累计发出过的条数**，不是列表长度：列表到 400 条会裁掉前面，
        # 下标会整体前移。用下标当游标的话，长任务跑到一半就再也读不到事件
        # （实测理智作战 150 秒零 subtask，就是这个）。
        self._seq = 0
        self._lock = threading.Lock()
        self._on_event = on_event
        self._done = threading.Event()
        self.error = ''
        # 每条任务链的最终状态：正文明细靠它判断「到底跑成功没有」。
        # AllTasksCompleted 在链报错后也会来，只看它就等于永远成功。
        self.chains = []
        self.errors = []
        # 最近一条子任务失败的原因（SubTaskError 的 why）：编队缺干员时
        # MaaCore 发的是 TaskChainStopped 而不是 TaskChainError，只能靠它
        # 回头补一句「为什么停的」（2026-11-23 实测：圣聆初雪 OperatorMissing）
        self._sub_why = ''
        self._cur_chain = ''
        # 收到过多少次 TaskChainStart。start() 用它判断「AsstStart 返回 false
        # 之后新任务到底有没有被接上」（见 start 的注释）。
        self._chain_starts = 0
        # 有人从外面叫停过（用户点「停止挂机」→ stop_all_live() → stop()）。
        # wait() 返回 False 时靠它区分「真跑满 / 任务卡死」和「用户按了停止」：
        # 2026-11-23 用户手动暂停，日志却写「这一关超时（1200 秒）」，完全对不上。
        self.stopped = False
        self.stopped_by_user = False   # 只有 stop_live（点名停）才会置位
        self.connected = False
        # 这个实例在替哪个账号干活（账号文件名）。「停止」要能只停某一路：
        # 多开时两台各一个实例，全停等于把另一台正在打的关也砍了。
        self.owner = str(owner or '')

    # ---------------------------------------------------------- 绑定
    def _bind(self):
        L = self._lib
        # CreateEx 的 restype 必须显式写 c_void_p：默认 c_int 会把 64 位句柄
        # 截成 32 位，后面每次 append 都 access violation
        L.AsstCreateEx.argtypes = [ASST_CALLBACK, ctypes.c_void_p]
        L.AsstCreateEx.restype = ctypes.c_void_p
        L.AsstSetUserDir.argtypes = [ctypes.c_char_p]
        L.AsstSetUserDir.restype = ctypes.c_bool
        L.AsstLoadResource.argtypes = [ctypes.c_char_p]
        L.AsstLoadResource.restype = ctypes.c_bool
        L.AsstSetConnectionExtras.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        L.AsstSetConnectionExtras.restype = None
        L.AsstSetInstanceOption.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                            ctypes.c_char_p]
        L.AsstSetInstanceOption.restype = ctypes.c_bool
        L.AsstConnect.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                  ctypes.c_char_p, ctypes.c_char_p]
        L.AsstConnect.restype = ctypes.c_bool
        L.AsstAppendTask.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
        L.AsstAppendTask.restype = ctypes.c_int
        L.AsstStart.argtypes = [ctypes.c_void_p]
        L.AsstStart.restype = ctypes.c_bool
        L.AsstStop.argtypes = [ctypes.c_void_p]
        L.AsstStop.restype = ctypes.c_bool
        L.AsstRunning.argtypes = [ctypes.c_void_p]
        L.AsstRunning.restype = ctypes.c_bool
        L.AsstDestroy.argtypes = [ctypes.c_void_p]
        L.AsstDestroy.restype = None
        L.AsstGetVersion.argtypes = []
        L.AsstGetVersion.restype = ctypes.c_char_p
        L.AsstBackToHome.argtypes = [ctypes.c_void_p]
        L.AsstBackToHome.restype = ctypes.c_bool
        # AsstAsyncClick(handle, x, y, block) -> AsstAsyncCallId
        L.AsstAsyncClick.argtypes = [ctypes.c_void_p, ctypes.c_int32,
                                     ctypes.c_int32, ctypes.c_bool]
        L.AsstAsyncClick.restype = ctypes.c_int32
        # AsstGetImage(handle, buff, buff_size) -> 写入的字节数（PNG），
        # 缓冲不够时返回 0。用来截图看导航点没点对。
        L.AsstGetImage.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                   ctypes.c_size_t]
        L.AsstGetImage.restype = ctypes.c_size_t

    def version(self):
        try:
            return (self._lib.AsstGetVersion() or b'').decode('utf-8', 'ignore')
        except Exception:
            return ''

    # ---------------------------------------------------------- 回调
    def _make_cb(self):
        CB = ASST_CALLBACK

        def _fn(msg, detail, arg):
            try:
                raw = (detail or b'').decode('utf-8', 'ignore')
                try:
                    data = json.loads(raw) if raw else {}
                except Exception:
                    data = {'raw': raw}
            except Exception:
                return
            # 翻译层自己抛异常绝不能把**整条**事件吞掉还无声无息：以前
            # `_translate` 裸调，子任务号写错之后一场仗的日志就是这么消失的
            # （面板上只有「开始任务/任务失败」两行）。现在出错记一条 error
            # 事件，至少面板上看得出「翻译炸了」。
            try:
                ev = self._translate(msg, data)
            except Exception as e:
                ev = {'t': time.time(), 'kind': 'error',
                      'text': '日志翻译出错（msg=%s）：%s' % (msg, e)}
            if ev is None:
                return
            with self._lock:
                self._events.append(ev)
                self._seq += 1
                if len(self._events) > 400:
                    del self._events[:120]
            if msg == MSG_ALL_TASKS_COMPLETED:
                self._done.set()
            if msg in (MSG_INTERNAL_ERROR, MSG_INIT_FAILED):
                self.error = ev.get('text') or 'MaaCore 报错'
                self._done.set()
            if self._on_event:
                try:
                    self._on_event(ev)
                except Exception:
                    pass

        return CB(_fn)

    def _translate(self, msg, data):
        """把 MaaCore 的回调翻成前端看得懂的一句话

        只挑对「现在跑到哪了」有用的：任务链开始/结束、子任务、掉落、报错。
        ``SubTaskStart`` 每秒能刷十几条，全推给前端会把面板刷爆，所以只在
        子任务名变化时才出一条。
        """
        msg = _SUBTASK_OLD.get(msg, msg)      # 4.x 老核心的 70000 段
        chain = data.get('taskchain') or ''
        sub = data.get('subtask') or ''
        what = data.get('what') or ''
        t = time.time()
        if msg == MSG_ALL_TASKS_COMPLETED:
            return {'t': t, 'kind': 'done', 'text': '全部任务跑完了'}
        if msg == MSG_INTERNAL_ERROR:
            return {'t': t, 'kind': 'error',
                    'text': 'MaaCore 内部错误：%s' % (data.get('what') or data.get('details') or '')}
        if msg == MSG_INIT_FAILED:
            return {'t': t, 'kind': 'error', 'text': 'MaaCore 初始化失败'}
        if msg == MSG_CONNECTION_INFO:
            info = data.get('what') or ''
            if info == 'Connected':
                self.connected = True
                return {'t': t, 'kind': 'connected', 'text': '模拟器已连接'}
            if info == 'ConnectFailed':
                return {'t': t, 'kind': 'error',
                        'text': '连不上模拟器（检查 MAA 设置里的 ADB 路径和地址）'}
            if info in ('UuidGot', 'Detected', 'UnsupportedResolution', 'ResolutionError'):
                return {'t': t, 'kind': 'info', 'text': '连接：%s' % info}
            return None
        if msg == MSG_TASK_START:
            self._cur_chain = chain
            self._sub_why = ''          # 新任务链开始，别带上一链的残留
            self._chain_starts += 1
            return {'t': t, 'kind': 'chain_start', 'chain': chain,
                    'text': '开始任务：%s' % _chain_cn(chain)}
        if msg == MSG_TASK_COMPLETED:
            self._sub_why = ''
            self.chains.append({'chain': chain, 'ok': True, 'stopped': False})
            return {'t': t, 'kind': 'chain_done', 'chain': chain,
                    'text': '完成任务：%s' % _chain_cn(chain)}
        if msg == MSG_TASK_STOPPED:
            # Stopped 一般不是失败：次数打满、理智不足、我方 AsstStop 都走这里。
            # 但**前面刚报过 SubTaskError 就是真失败**——MaaCore 在编队缺干员
            # （OperatorMissing）这类硬错误上不发 TaskChainError，只发
            # SubTaskError + TaskChainStopped。以前一律记 ok=True，于是
            # run_scatter 把它当成「打完了」（2026-11-23 实测：OS-S-2 突袭缺
            # 「圣聆初雪」，日志里一句「超时 1200 秒」骗了用户 20 分钟）。
            why = self._sub_why
            self._sub_why = ''
            if why:
                self.chains.append({'chain': chain, 'ok': False, 'stopped': True,
                                    'why': why})
                self.errors.append('%s：%s' % (_chain_cn(chain), why))
                return {'t': t, 'kind': 'chain_failed', 'chain': chain,
                        'why': why, 'text': '任务中止：%s（%s）'
                                            % (_chain_cn(chain), why)}
            self.chains.append({'chain': chain, 'ok': True, 'stopped': True})
            return {'t': t, 'kind': 'chain_stop', 'chain': chain,
                    'text': '结束任务：%s' % _chain_cn(chain)}
        if msg == MSG_TASK_ERROR:
            why = (data.get('why') or data.get('details') or '')
            self.chains.append({'chain': chain, 'ok': False, 'stopped': False,
                                'why': why})
            self.errors.append('%s%s' % (_chain_cn(chain),
                                         '：%s' % why if why else ''))
            # why 要带上：run_core 靠它说出「哪一关翻车、为什么」
            # （2026-11-19 用户要求：作业翻车时日志要看得出是哪关）
            return {'t': t, 'kind': 'chain_failed', 'chain': chain,
                    'why': why, 'text': '任务失败：%s' % _chain_cn(chain)}
        if msg == MSG_SUBTASK_START:
            # ``subtask`` 是插件类名（ProcessTask / BattleProcessTask），不是
            # 用户看得懂的阶段；真正的 pipeline 名在 ``first[0]``，形如
            # ``Copilot@WaitUntilEndOfAction`` / ``OperProgress@EliteUp``
            first = data.get('first') or []
            node = ''
            if isinstance(first, list) and first:
                node = str(first[0] or '')
            if not node:
                node = str((data.get('details') or {}).get('task') or sub or '')
            if node and '@' not in node and chain:
                node = '%s@%s' % (chain, node)
            return {'t': t, 'kind': 'node', 'chain': chain, 'sub': node}
        if msg == MSG_SUBTASK_ERROR:
            # 子任务失败：MaaCore 只给 pipeline 名，不给原因。最典型的是
            # ``Copilot@WaitUntilEndOfAction``——打仗打完了，但结算不是三星
            # （漏怪 / 作业没打过），MaaCore 反复认「三颗星」认不到 20 次就报
            # 这个。以前这层不接，用户只看到「任务失败：自动战斗」+ 一片空白
            # （2026-11-22 实测：OS-6 打了 2 星，日志里没有任何解释）。
            first = data.get('first') or []
            node = str(first[0] or '') if isinstance(first, list) and first else sub
            # why 是 MaaCore 给的机器可读原因（OperatorMissing / ...），
            # details.opers 里是缺哪些干员。2026-11-23 之前这里全丢了，
            # 用户只看到「结束了」却不知道为什么。
            why = str(data.get('why') or '')
            self._sub_why = why
            return {'t': t, 'kind': 'sub_error', 'chain': chain, 'sub': node,
                    'why': why, 'opers': _missing_opers(data)}
        if msg == MSG_SUBTASK_EXTRA:
            # 掉落 / 关卡识别 / 抄作业换关 / 理智 / 战斗动作 / 培养结果，
            # 这几类对「现在在干什么」有意义。2026-11-18 补 CopilotAction 与
            # 培养两条：以前只有换作业和掉落，一场战斗 3 分钟日志里一个字
            # 都没有，用户报「自动战斗没有日志」。
            if what in ('StageDrops', 'CopilotListLoadTaskFileSuccess',
                        'BattleFormation', 'BattleFormationSelected',
                        'StageInfo',
                        'SanityBeforeStage', 'FightTimes', 'CopilotAction',
                        'ExceededLimit', 'OperProgressDetail',
                        'OperProgressSummary'):
                return {'t': t, 'kind': 'extra', 'what': what,
                        'data': data.get('details') or {}}
            return None
        return None

    # ---------------------------------------------------------- 开关
    def open(self):
        """建实例 + 加载资源。必须在这之后才能 connect / append。"""
        # 同进程第二个实例会把第一个正在跑的链「无声打断」（只发 Stopped），
        # 2026-11-23 用户报「多开另一个号，他把我前面的停了」就是这条：
        # 5556 那组在跑，另一个号在**同一进程**里开第二个实例，5556 的唤醒链
        # 13 秒后就莫名结束，接着两边一起报登录失败。宁可这一路明确报错，
        # 也别把已经在跑的那一路拖死。
        busy = live_others(exclude=self)
        if busy:
            raise EngineError(
                '这个进程里已经有 %d 个 MaaCore 在跑，不能再开第二个'
                '（会互相把任务打断）。多开必须一路一个独立进程。' % busy)
        if not self._lib.AsstSetUserDir(_utf8(self.maa_dir)):
            raise EngineError('AsstSetUserDir 失败：%s' % self.maa_dir)
        if not self._lib.AsstLoadResource(_utf8(self.maa_dir)):
            raise EngineError('AsstLoadResource 失败：%s' % self.maa_dir)
        # MAA 自己也会再加载一次 cache（热更新的资源在这里），漏了活动关卡会认不出
        cache = os.path.join(self.maa_dir, 'cache')
        if os.path.isdir(cache):
            self._lib.AsstLoadResource(_utf8(cache))
        self._cb = self._make_cb()          # 必须留引用，被 GC 掉会直接崩
        self._handle = self._lib.AsstCreateEx(self._cb, None)
        if not self._handle:
            raise EngineError('AsstCreateEx 返回空（资源没加载好？）')
        with _LIVE_LOCK:
            _LIVE.add(self)
        return self

    def connect(self, adb_path, address, config='General', touch='MiniTouch'):
        if not self._handle:
            raise EngineError('还没 open()')
        # **别在这里调 AsstSetInstanceOption 改触摸模式**：MAA 6.x 的这个 C
        # 接口只有一个数字 key（TouchMode = 2），传字符串（MAA 官方 GUI 也是
        # 这么传的）会被读成指针值，asst.log 里固定留一行
        #   set_instance_option | key 1650568816 value MiniTouch
        #   Unknown key or value MiniTouch
        # 也就是**从来没生效过**，官方 GUI 同样天天报这句。MAA 自己按连接配置
        # 兜底成 MiniTouch，实测点击/滑动都正常，所以直接不调，省一行假报错。
        ok = self._lib.AsstConnect(self._handle, _utf8(adb_path or 'adb'),
                                   _utf8(address), _utf8(config or 'General'))
        self.connected = bool(ok)
        return ok

    def append(self, task_type, params):
        if not self._handle:
            raise EngineError('还没 open()')
        tid = self._lib.AsstAppendTask(self._handle, _utf8(task_type),
                                       _utf8(json.dumps(params or {}, ensure_ascii=False)))
        if tid <= 0:
            raise EngineError('MaaCore 不接受这个任务：%s %s'
                              % (task_type, json.dumps(params or {}, ensure_ascii=False)[:200]))
        return tid

    def start(self, settle=15.0):
        """开始跑队列；返回 False 表示 MaaCore 真的没接

        ``AsstStart`` 返回 false 有两种完全不同的处境，以前混在一起判，
        结果「任务明明跑起来了，日志却报 AsstStart 返回失败」
        （2026-11-19 用户实测，三次培养全中）：

        a) **上一批还在收尾**：``AllTasksCompleted`` 是在工作线程退出前发的，
           这时 ``AsstRunning`` 还是 true，MaaCore 打一行 ``Start | block``
           就把 ``AsstStart`` 挡了。可它自己还活着、会继续消费队列 ——
           我们刚 append 的那条照样在几十毫秒后开跑。asst.log 实证：
           ``00:25:09.674 Start | block`` → ``00:25:09.776 TaskChainStart``。
           判据：**新任务链开始了**（``self._chain_starts`` 增加）。
        b) **真的没接**：工作线程已经退干净（``running`` 变 false），队列里那
           一条没人取。这时才需要重试一次 ``AsstStart``。

        以前只处理 b)：等 running 变 false 再重试。碰上 a) 时 running 一直
        true（任务正在跑），等满 ``settle`` 秒后当失败抛错——用户看到的
        「X RuntimeError: AsstStart 返回失败」就是这么来的，任务其实在跑。
        """
        if not self._handle:
            raise EngineError('还没 open()')
        self._done.clear()
        if self._lib.AsstStart(self._handle):
            return True
        mark = self._chain_starts
        deadline = time.time() + max(0.0, settle)
        while time.time() < deadline:
            if self._chain_starts > mark:
                return True                 # a)：新任务链已经开跑
            if not self.running():
                break                       # b)：退干净了，重试
            time.sleep(0.1)
        if self._chain_starts > mark:
            return True
        if self.running():
            # 还在跑但没看到新链：交给 _wait_run 的心跳/重排兜底，
            # 不在这里判死（误判一次就是一整轮白跑）
            return True
        self._done.clear()
        return bool(self._lib.AsstStart(self._handle))

    def stop(self, external=False):
        """叫停这个实例

        ``external=True`` = 用户/上层点名停的（:func:`stop_live` 走的这条路）。
        我们自己的超时收尾也调 ``stop()``，但那是「跑完了/跑不动了」不是
        「用户按了停止」：以前两者共用 ``stopped``，日志就把超时写成
        「已停止（你按了停止）」，跟事实完全对不上（2026-11-23 用户报）。
        """
        self.stopped = True
        if external:
            self.stopped_by_user = True
        if self._handle:
            try:
                self._lib.AsstStop(self._handle)
            except Exception:
                pass

    def running(self):
        return bool(self._handle and self._lib.AsstRunning(self._handle))

    def click(self, x, y, block=True):
        """点一下（1280×720 坐标系，MaaCore 自己缩放到实际分辨率）

        活动关导航要用：MAA 的活动入口模板在当前 UI 上全失效，只能按固定
        位置自己点（见 stagenav.py 的说明）。

        ``block=True``（默认）等这一下点完再返回。实测连排四下 ``block=False``
        时后面几下会被吞掉——界面纹丝不动，所以导航一律用阻塞模式。
        """
        if not self._handle:
            return False
        try:
            return bool(self._lib.AsstAsyncClick(self._handle, int(x), int(y),
                                                 bool(block)))
        except Exception:
            return False

    def screencap(self, limit=6 * 1024 * 1024):
        """当前画面（PNG 字节），失败返回 b''

        只给调试/校验用：导航那三下要是点空了，存张图比猜快。
        MaaCore 的 AsstGetImage 是同步的，缓冲不够会返回 0。
        """
        if not self._handle:
            return b''
        buf = ctypes.create_string_buffer(limit)
        try:
            n = self._lib.AsstGetImage(self._handle, buf, limit)
        except Exception:
            return b''
        return buf.raw[:int(n)] if n else b''

    def back_to_home(self):
        """退回主界面

        抄作业的关卡导航（``<代号>@Copilot@StageNavigationBegin``）是从关卡列表
        界面开始滑的。上一步要是停在某个关卡详情页（比如刚刷完 1-7），直接
        导航就会在错的界面里滑，滑到底也找不到 TL-S-1。
        """
        if not self._handle:
            return False
        try:
            return bool(self._lib.AsstBackToHome(self._handle))
        except Exception:
            return False

    def wait(self, timeout=None, poll=1.0):
        """等全部任务结束。返回 True=跑完，False=超时/被停

        「running 变 False」只有在**见过 running=True** 之后才算停：AsstStart
        是异步生效的，刚 start 完的头几百毫秒 AsstRunning 还是 False——以前
        在这里直接 ``return self._done.is_set()``，把「还没开始跑」当成
        「已经跑完」，``_wait_run`` 两层立刻返回，run_core 就收尾 close，把刚
        排进队列的任务当场杀掉（2026-11-18 用户实测：培养干员 append 完
        start 后同一毫秒 Assistant 就被析构，任务报错但 OperProgress 一步
        都没跑）。没见到 running 的宽限给 15 秒，超过还没跑起来才认输。
        """
        deadline = time.time() + timeout if timeout else None
        seen_running = False
        quiet_since = None
        while True:
            if self._done.is_set():
                return True
            if self.running():
                seen_running = True
                quiet_since = None
            elif seen_running:
                # 跑过之后程序说停了：stop() 或异常退出
                return self._done.is_set()
            elif quiet_since is None:
                quiet_since = time.time()
            elif time.time() - quiet_since > 15:
                return self._done.is_set()
            if deadline and time.time() > deadline:
                return False
            time.sleep(poll)

    def events(self, since=0):
        """从第 ``since`` 条之后的事件

        ``since`` 是**累计条数**（上次返回值的 ``len`` + 起点），不是列表下标：
        缓冲会裁剪，用下标会错位。返回值最后一条的序号 = ``since + len(结果)``。
        """
        with self._lock:
            fresh = self._seq - since
            if fresh <= 0:
                return []
            fresh = min(fresh, len(self._events))
            return self._events[len(self._events) - fresh:]

    def close(self):
        if self._handle:
            try:
                self._lib.AsstDestroy(self._handle)
            except Exception:
                pass
            self._handle = None
        self._cb = None
        with _LIVE_LOCK:
            _LIVE.discard(self)

    def __enter__(self):
        return self.open()

    def __exit__(self, *a):
        self.close()


_CHAIN_CN = {
    'StartUp': '开始唤醒',
    'Fight': '理智作战',
    'Recruit': '自动公招',
    'Infrast': '基建换班',
    'Mall': '信用收支',
    'Award': '领取奖励',
    'Roguelike': '自动肉鸽',
    'Copilot': '自动战斗',
    'SSSCopilot': '保全派驻',
    'Paradox': '悖论模拟',
    'Reclamation': '生息演算',
    'Custom': '小工具',
    'Depot': '仓库识别',
    'OperBox': '干员识别',
    'OperProgress': '干员培养',
    'CloseDown': '关闭游戏',
}


def _chain_cn(chain):
    return _CHAIN_CN.get(chain) or chain or '任务'


# SubTaskError 的 why 是 MaaCore 的机器码，日志里直接甩给用户看不懂。
_WHY_CN = {
    'OperatorMissing': '编队缺干员',
    'InsufficientSanity': '理智不足',
    'UnsupportedStage': '这一关不支持',
    'NotEnoughMedicine': '理智药不够',
    'RoguelikeSquadNotFound': '没找到指定分队',
}


def why_cn(why):
    """``OperatorMissing`` → ``编队缺干员``；认不出的保留原文"""
    w = str(why or '').strip()
    if not w:
        return ''
    return _WHY_CN.get(w) or w


def _missing_opers(data):
    """SubTaskError 的 ``details.opers`` → 缺的干员名（去重保序）

    形如 ``{"圣聆初雪": [{"name": "圣聆初雪", "reason": "Unchecked"}]}``。
    2026-11-23 用户排 5 关普通，实际被排成突袭、第二关就缺干员卡死，日志里
    一个名字都没有——就是因为这里没解析。
    """
    out = []
    try:
        opers = (data.get('details') or {}).get('opers') or {}
        for k, v in opers.items():
            name = ''
            if isinstance(v, list) and v:
                name = str((v[0] or {}).get('name') or '')
            name = name or str(k or '')
            if name and name not in out:
                out.append(name)
    except Exception:                                  # noqa: BLE001 回调字段可能变形
        pass
    return out
