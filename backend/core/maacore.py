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
MSG_INTERNAL_ERROR = 0
MSG_INIT_FAILED = 1
MSG_CONNECTION_INFO = 2
MSG_ALL_TASKS_COMPLETED = 3
MSG_TASK_ERROR = 10000
MSG_TASK_START = 10001
MSG_TASK_COMPLETED = 10002
MSG_TASK_EXTRA = 10003
MSG_TASK_STOPPED = 10004
MSG_SUBTASK_ERROR = 70000
MSG_SUBTASK_START = 70001
MSG_SUBTASK_COMPLETED = 70002
MSG_SUBTASK_EXTRA = 70003
MSG_SUBTASK_STOPPED = 70004

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


def stop_all_live():
    """叫停所有直连实例（只 AsstStop，不销毁，让调用方的 wait() 自己收到结束）"""
    with _LIVE_LOCK:
        live = [e for e in _LIVE if e._handle]
    n = 0
    for e in live:
        try:
            e.stop()
            n += 1
        except Exception:
            pass
    return n


class Engine:
    """一个 MaaCore 实例

    生命周期：``open()`` → ``connect()`` → ``append()`` × N → ``start()``
    → 等 ``AllTasksCompleted`` → ``close()``。
    """

    def __init__(self, maa_dir, on_event=None):
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
        self._cur_chain = ''
        self.connected = False

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
            ev = self._translate(msg, data)
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
            return {'t': t, 'kind': 'chain_start', 'chain': chain,
                    'text': '开始：%s' % _chain_cn(chain)}
        if msg == MSG_TASK_COMPLETED:
            self.chains.append({'chain': chain, 'ok': True, 'stopped': False})
            return {'t': t, 'kind': 'chain_done', 'chain': chain,
                    'text': '完成：%s' % _chain_cn(chain)}
        if msg == MSG_TASK_STOPPED:
            # Stopped 不是失败：次数打满、理智不足、我方 AsstStop 都会走这里。
            # 真正的失败只有 TaskChainError。把两者混起来，用户就会看到
            # 「打完了却报失败」——这一条踩过。
            self.chains.append({'chain': chain, 'ok': True, 'stopped': True})
            return {'t': t, 'kind': 'chain_stop', 'chain': chain,
                    'text': '结束：%s' % _chain_cn(chain)}
        if msg == MSG_TASK_ERROR:
            why = (data.get('why') or data.get('details') or '')
            self.chains.append({'chain': chain, 'ok': False, 'stopped': False,
                                'why': why})
            self.errors.append('%s%s' % (_chain_cn(chain),
                                         '：%s' % why if why else ''))
            return {'t': t, 'kind': 'chain_failed', 'chain': chain,
                    'text': '失败：%s' % _chain_cn(chain)}
        if msg == MSG_SUBTASK_START:
            return {'t': t, 'kind': 'node', 'chain': chain, 'sub': sub}
        if msg == MSG_SUBTASK_EXTRA:
            # 掉落 / 关卡识别 / 抄作业换关 / 理智，这几类对用户有意义
            if what in ('StageDrops', 'CopilotListLoadTaskFileSuccess',
                        'BattleFormation', 'StageInfo',
                        'SanityBeforeStage', 'FightTimes'):
                return {'t': t, 'kind': 'extra', 'what': what,
                        'data': data.get('details') or {}}
            return None
        return None

    # ---------------------------------------------------------- 开关
    def open(self):
        """建实例 + 加载资源。必须在这之后才能 connect / append。"""
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
        try:
            self._lib.AsstSetInstanceOption(self._handle, b'TouchMode', _utf8(touch))
        except Exception:
            pass
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

    def start(self):
        if not self._handle:
            raise EngineError('还没 open()')
        self._done.clear()
        return bool(self._lib.AsstStart(self._handle))

    def stop(self):
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
        """等全部任务结束。返回 True=跑完，False=超时/被停"""
        deadline = time.time() + timeout if timeout else None
        while True:
            if self._done.is_set():
                return True
            if not self.running():
                # 已经在跑但程序说停了：可能是 stop() 或异常退出
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
    'CloseDown': '关闭游戏',
}


def _chain_cn(chain):
    return _CHAIN_CN.get(chain) or chain or '任务'
