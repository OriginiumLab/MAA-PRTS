# -*- coding: utf-8 -*-
"""MAA 日志观测：把 debug/asst.log 与 debug/gui.log 变成结构化事件流

MAA 没有给我们任何接口去问「现在跑到第几关、卡在哪」，但它的日志里有两条
现成的结构化通道，一条像素都不用看：

1. ``debug/asst.log``
   MaaCore 的回调 JSON 被 GUI 原样打印（有的带 ``Assistant::append_callback |``
   前缀，有的是裸 ``{"class":...}``）。里面有：

       CopilotListLoadTaskFileSuccess {"id":4,"stage_name":"act55side_s03",...}
       {"cur_task":"Copilot@WaitUntilEndOfAction",
        "details":{"cur_retry":0,"retry_times":20,
                   "to_be_recognized":["Copilot@SkipThePreBattlePlot", ...]}}

   即「跑到第几个作业、哪一关、停在哪个 pipeline 节点、正在识别什么、
   重试到第几次」。

2. ``debug/gui.log``
   GUI 自己的任务链边界，比 asst.log 的信号更权威（asst.log 里没有
   「全部跑完」这种东西）：

       BeginRun: owner="Copilot"
       Start Task Chain: Copilot, Task ID: 1
       Completed Task Chain: Copilot, Task ID: 1
       Idle: false to true (called from SetStopped)

设计原则：**宽松解析**。MAA 升级改了日志格式，最坏结果只能是「事件变少」，
绝不能是「程序抛异常」。认不出的行一律跳过。
"""
from __future__ import annotations

import json
import os
import re
import time

from . import settings

# 首次打开一个几十 MB 的老日志时只回读尾部这么多，够拿到上下文又不卡启动
INITIAL_TAIL = 4 * 1024 * 1024
# 内存里最多留多少条事件（前端「刚才发生了什么」只看最近几十条）
KEEP_EVENTS = 400

_TS = re.compile(r'^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\.(\d{3})\]')

# gui.log 的关键行（不是 JSON，单独认）
_GUI_START = re.compile(r'Start Task Chain: (\w+), Task ID: (\d+)')
_GUI_DONE = re.compile(r'Completed Task Chain: (\w+), Task ID: (\d+)')
_GUI_BEGIN = re.compile(r'BeginRun: owner="(\w+)"')
_GUI_IDLE = re.compile(r'Idle: false to true')


def log_paths(maa_dir=None) -> dict:
    """MAA 的两份日志。目录没配或不存在时返回空串，调用方据此判「没有日志」。"""
    d = (maa_dir or settings.load().get('maaDir') or '').strip()
    if not d:
        return {'asst': '', 'gui': ''}
    dbg = os.path.join(d, 'debug')
    return {
        'asst': os.path.join(dbg, 'asst.log'),
        'gui': os.path.join(dbg, 'gui.log'),
    }


def _stamp(line):
    """行首时间戳 → epoch 秒。取不到返回 None（调用方用「现在」兜底）。"""
    m = _TS.match(line)
    if not m:
        return None
    try:
        return time.mktime(time.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')) \
            + int(m.group(2)) / 1000.0
    except (ValueError, OverflowError):
        return None


def _json_of(line):
    """抠出日志行里的 JSON 对象；不是 JSON 行返回 None。

    尾部可能跟着别的东西（GUI 有时会在同一行补字），所以解析失败时
    截到最后一个 ``}`` 再试一次。
    """
    i = line.find('{"')
    if i < 0:
        return None
    try:
        return json.loads(line[i:])
    except ValueError:
        j = line.rfind('}')
        if j <= i:
            return None
        try:
            return json.loads(line[i:j + 1])
        except ValueError:
            return None


class Tail:
    """一个日志文件的增量游标

    处理三件事：只读新增、文件被轮转（asst.log → asst.bak.log 后新建）、
    最后一行还没写完（下次接着读）。
    """

    def __init__(self, path, initial_tail=INITIAL_TAIL):
        self.path = path or ''
        self.initial_tail = initial_tail
        self.offset = 0
        self._started = False
        self._skip_first = False
        self._fp = None

    def size(self):
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def _fingerprint(self):
        """文件开头 96 字节，用来认出「这不是刚才那个文件了」

        只靠「变小了」判不出轮转：新文件恰好和旧的同样大小（或更大）时，
        偏移量还在旧位置上，读到的就是从半行开始的垃圾。MAA 每次启动都会
        把 asst.log 重命名成 asst.bak.log 再建新的，这个场景很常见。
        """
        try:
            with open(self.path, 'rb') as f:
                return f.read(96)
        except OSError:
            return b''

    def _begin(self):
        """首次定位：老日志从尾部开始，避免一次读几十 MB"""
        self._started = True
        n = self.size()
        if n > self.initial_tail:
            self.offset = n - self.initial_tail
            self._skip_first = True    # 第一行大概率是半截，丢掉
        else:
            self.offset = 0
        self._fp = self._fingerprint()

    def rewind(self):
        """从头开始读（调用方想回放整段历史时用）"""
        self.offset = 0
        self._started = True
        self._skip_first = False
        self._fp = self._fingerprint()

    def read_new(self):
        """返回新增的完整行。没有新增返回 []。"""
        if not self.path:
            return []
        if not self._started:
            self._begin()
        n = self.size()
        fp = self._fingerprint()
        # 文件变小，或者开头那段内容变了 = 被轮转 / 重写，从头来
        if n < self.offset or (self._fp is not None and fp and fp != self._fp):
            self.offset = 0
            self._skip_first = False
        self._fp = fp
        if n <= self.offset:
            return []
        try:
            with open(self.path, 'rb') as f:
                f.seek(self.offset)
                chunk = f.read(n - self.offset)
        except OSError:
            return []
        end = chunk.rfind(b'\n')
        if end < 0:
            # 一行都没写完整，指针不动，等下次
            return []
        self.offset += end + 1
        lines = chunk[:end].decode('utf-8', 'ignore').splitlines()
        if self._skip_first and lines:
            lines = lines[1:]
            self._skip_first = False
        return [ln for ln in lines if ln.strip()]


# ---------------------------------------------------------------- 事件归一化
def norm_asst(line):
    """asst.log 一行 → 事件 dict；不是我们关心的事件返回 None

    统一形状：``{t, kind, ...}``。kind 取值见 _KIND_DOC。
    """
    obj = _json_of(line)
    if not isinstance(obj, dict):
        return None
    t = _stamp(line)
    what = obj.get('what') or ''
    det = obj.get('details') if isinstance(obj.get('details'), dict) else {}
    chain = obj.get('taskchain') or ''
    tid = obj.get('taskid')

    if what == 'CopilotListLoadTaskFileSuccess':
        # 多作业列表每换一份作业都会报一次，id 就是 TaskList 里的下标
        return {'t': t, 'kind': 'stage', 'index': _int(det.get('id')),
                'stage': str(det.get('stage_name') or ''),
                'file': os.path.basename(str(det.get('file_name') or ''))}

    if what == 'CopilotAction':
        return {'t': t, 'kind': 'action', 'action': str(det.get('action') or ''),
                'target': str(det.get('target') or ''), 'chain': chain}

    if what in ('BattleFormation', 'BattleFormationSelected'):
        return {'t': t, 'kind': 'formation', 'detail': det.get('formation') or det}

    if what == 'Connected':
        return {'t': t, 'kind': 'connected'}

    # ProcessTask 的进度行：带 cur_task / cur_retry，是最能说明「卡在哪」的
    cur = obj.get('cur_task')
    if cur:
        total = _int(det.get('retry_times'))
        retry = _int(det.get('cur_retry'))
        return {'t': t, 'kind': 'node', 'node': str(cur), 'retry': retry,
                'total': total, 'targets': list(det.get('to_be_recognized') or []),
                'chain': chain, 'taskid': tid}

    sub = obj.get('subtask') or ''
    if what in ('SubTaskStart', 'SubTaskCompleted') and sub:
        return {'t': t, 'kind': 'subtask', 'name': sub,
                'phase': 'start' if what == 'SubTaskStart' else 'done',
                'task': str(det.get('task') or ''), 'chain': chain}

    return None


def norm_gui(line):
    """gui.log 一行 → 事件；无关行返回 None

    gui.log 里只有任务链边界是可靠的，别的（HTTP、配置保存）一律不要。
    """
    if 'Task Chain' not in line and 'BeginRun' not in line and 'Idle:' not in line:
        return None
    t = _stamp(line)
    m = _GUI_START.search(line)
    if m:
        return {'t': t, 'kind': 'chain_start', 'chain': m.group(1),
                'taskid': _int(m.group(2))}
    m = _GUI_DONE.search(line)
    if m:
        return {'t': t, 'kind': 'chain_done', 'chain': m.group(1),
                'taskid': _int(m.group(2))}
    m = _GUI_BEGIN.search(line)
    if m:
        return {'t': t, 'kind': 'chain_begin', 'chain': m.group(1)}
    if _GUI_IDLE.search(line):
        return {'t': t, 'kind': 'idle'}
    return None


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 状态机
# 阈值说明：MAA 自己的 retry_times 是 20、每次约 0.7 秒，也就是 14 秒就放弃
# 并终止整个队列；它自带的「停滞提醒」默认 30 分钟。中间这一大段空白正是
# 我们要补的窗口。设太短会和 MAA 自己的重试打架，180 秒是折中。
STALL_SECONDS = 180
ALIVE_SECONDS = 60          # 这个窗口内有事件就算「在动」

# 这些节点出现说明 MAA 自己在推进战斗，不是卡住
_PROGRESS_NODES = ('Copilot@WaitUntilEndOfAction', 'Copilot@BattleStartPreFlag',
                   'Copilot@ClickCornerUntilStartButton', 'Copilot@EndOfAction')


class Watcher:
    """盯一个 MAA 目录的运行状态

    用法::

        w = Watcher(maa_dir)
        snap = w.poll()      # 每 5 秒调一次
        snap['state']        # idle / running / stalled / failed / done
        snap['stage']        # 当前关卡（act55side_s03 / TL-5）
        snap['events']       # 最近事件

    这不是线程，poll 要由调用方驱动（watch.py 的后台线程 / 前端手动刷新）。
    """

    def __init__(self, maa_dir=None, stall_seconds=STALL_SECONDS, live=True):
        self.maa_dir = maa_dir
        paths = log_paths(maa_dir)
        self.asst = Tail(paths['asst'])
        self.gui = Tail(paths['gui'])
        self.stall_seconds = stall_seconds
        # live=False 用于回放历史日志（测试 / 事后分析）：这时不拿「MAA 进程
        # 在不在」判死活，否则翻一份昨天的日志会得出「MAA 崩了」的结论
        self.live = live
        self.events = []
        self.state = 'idle'
        self.stage = ''
        self.index = 0
        self.node = ''
        self.retry = (0, 0)
        self.chain = ''
        self.started_at = None      # 最近一次 chain_start / chain_begin
        self.ended_at = None        # 最近一次 chain_done / idle
        self.done_at = None
        self.last_event_at = None
        self.error = ''

    # -------------------------------------------------- 读
    def _push(self, ev):
        if not ev:
            return
        if ev.get('t') is None:
            ev['t'] = time.time()
        self.events.append(ev)
        if len(self.events) > KEEP_EVENTS:
            del self.events[:len(self.events) - KEEP_EVENTS]
        if self.last_event_at is None or ev['t'] > self.last_event_at:
            self.last_event_at = ev['t']

    def drain(self):
        """读完两个日志的新增部分，返回本次新增的事件条数"""
        n = 0
        for line in self.asst.read_new():
            ev = norm_asst(line)
            if ev:
                self._apply(ev)
                n += 1
        for line in self.gui.read_new():
            ev = norm_gui(line)
            if ev:
                self._apply(ev)
                n += 1
        return n

    def _apply(self, ev):
        """只记事实，不判状态——状态在 poll() 里统一推导

        这样翻一份三小时前的旧日志不会得出「MAA 崩了」的结论：事实是
        「上次跑完了，之后没再跑」，推导出来的就是 idle。
        """
        self._push(ev)
        kind = ev['kind']
        if kind in ('chain_begin', 'chain_start'):
            self.chain = ev['chain']
            self.started_at = ev['t']
            self.error = ''
        elif kind == 'chain_done':
            self.ended_at = ev['t']
            self.chain = ev.get('chain') or self.chain
        elif kind == 'idle':
            # MAA 回到空闲 = 这一轮结束。没有 chain_done 就是被停了
            self.ended_at = ev['t']
        elif kind == 'stage':
            self.index = ev['index']
            self.stage = ev['stage']
            self.retry = (0, 0)
            self.node = ''
        elif kind == 'node':
            self.node = ev['node']
            self.retry = (ev['retry'], ev['total'])

    # -------------------------------------------------- 判
    def _derive(self, now):
        """按事实推状态：idle / running / stalled / done / failed"""
        gap = (now - self.last_event_at) if self.last_event_at else None
        active = bool(self.started_at) and (
            not self.ended_at or self.started_at > self.ended_at)

        if not active:
            # 没有正在进行的运行。刚结束（2 分钟内）显示 done，否则就是空闲
            if self.ended_at and gap is not None and gap < 120:
                return 'done'
            return 'idle'
        if gap is None:
            return 'running'
        return 'running' if gap < self.stall_seconds else 'stalled'

    def poll(self):
        """读增量 + 重算状态，返回快照"""
        before = len(self.events)
        self.drain()
        now = time.time()
        state = self._derive(now)
        gap = (now - self.last_event_at) if self.last_event_at else None

        if self.live:
            from . import maa as _maa
            try:
                alive = _maa.running_in(self.maa_dir) if self.maa_dir else _maa.is_running()
            except Exception:
                alive = False
            if state in ('running', 'stalled'):
                if not alive:
                    # 进程没了：刚断的才算崩；早就断了的（翻旧日志）是历史，
                    # 不该在界面上显示成「MAA 崩了」
                    if self.error:
                        state = 'failed'
                    elif gap is not None and gap < self.stall_seconds:
                        state = 'failed'
                        self.error = self.error or 'MAA 进程已退出'
                    else:
                        state = 'idle'
            elif state == 'idle' and alive and gap is not None \
                    and gap < self.stall_seconds:
                # 进程在、日志在动，但没看到链边界（比如用户手动点的开始）
                state = 'running'

        if state != 'done':
            # done 只在结束那一刻显示，之后回归 idle；这里只保留最后一次的完成时刻
            self.done_at = self.ended_at if state == 'done' else self.done_at
        self.state = state
        return self.snapshot(new=(len(self.events) - before))

    def _retry_heavy(self):
        retry, total = self.retry
        return bool(total) and retry >= total * 0.7

    def snapshot(self, new=0):
        return {
            'state': self.state,
            'chain': self.chain,
            'stage': self.stage,
            'index': self.index,
            'node': self.node,
            'retry': self.retry[0],
            'retryTotal': self.retry[1],
            'since': (time.time() - self.last_event_at) if self.last_event_at else None,
            'startedAt': self.started_at,
            'doneAt': self.done_at,
            'error': self.error,
            'newEvents': new,
            'events': list(self.events[-60:]),
        }


# ---------------------------------------------------------------- 现场取证
def explain(snap, min_gap=180):
    """把快照翻成一句人话，给 UI 提示和 agent 读

    返回 ``{level, text, hint}``；level 取 ok / warn / bad。
    """
    st = snap.get('state')
    stage = snap.get('stage') or ''
    node = snap.get('node') or ''
    retry, total = snap.get('retry') or 0, snap.get('retryTotal') or 0

    if st == 'done':
        return {'level': 'ok', 'text': '已跑完', 'hint': ''}
    if st == 'idle':
        return {'level': 'ok', 'text': '空闲中（MAA 没在跑）', 'hint': ''}
    if st == 'failed':
        return {'level': 'bad', 'text': 'MAA 退出了：%s' % (snap.get('error') or '未知原因'),
                'hint': '点「启动 MAA」重新开始'}
    if st == 'stalled':
        where = ('卡在 %s' % stage) if stage else '卡住了'
        at = ('停在 %s' % node.split('@')[-1]) if node else '没有任何新日志'
        return {'level': 'bad', 'text': '%s，%s' % (where, at),
                'hint': '多半是剧情关或弹窗挡住了，自动恢复会关掉 MAA 重开接着跑'}
    if st == 'running':
        if retry and total:
            return {'level': 'warn', 'text': '%s 正在找 %s（重试 %d/%d）'
                    % (stage or '当前关', node.split('@')[-1] or '目标', retry, total),
                    'hint': ''}
        return {'level': 'ok', 'text': '正在跑 %s' % (stage or '…'), 'hint': ''}
    return {'level': 'ok', 'text': '未知状态', 'hint': ''}


def history(maa_dir=None, limit=200, include_bak=True, tail_bytes=8 * 1024 * 1024):
    """回放日志能还原出的事件（另开游标，不影响实时 Watcher）

    给「昨天那几关为什么失败」这种问题用的。``asst.bak.log`` 是上一次运行
    留下的（MAA 每次启动把 asst.log 转存成 bak），要一起读——只读当前那份
    的话，重启过就什么都查不到了。

    只读**尾部** ``tail_bytes``：完整的 asst.log 能到 60MB+，从头扫要几秒，
    而我们要的只是最近几次运行。
    """
    out = []
    paths = log_paths(maa_dir)
    files = [(paths['asst'], norm_asst)]
    if include_bak and paths['asst']:
        # MAA 把上一轮写成 asst.bak.log（不是 asst.log.bak）
        p = paths['asst']
        bak = (p[:-4] + '.bak.log') if p.lower().endswith('.log') else (p + '.bak')
        files.insert(0, (bak, norm_asst))
    files.append((paths['gui'], norm_gui))
    for path, norm in files:
        if not path or not os.path.isfile(path):
            continue
        t = Tail(path, initial_tail=tail_bytes)   # 首次 read_new 会自己定位到尾部
        for line in t.read_new():
            ev = norm(line)
            if ev:
                ev.setdefault('src', os.path.basename(path))
                out.append(ev)
    out.sort(key=lambda e: e.get('t') or 0)
    return out[-limit:]


def failures(maa_dir=None, limit=60):
    """从历史里挑出「跑过哪几关、哪几关没跑完」

    返回 ``[{stageId, code, name, index, startedAt, endedAt, ok}]``。

    靠 stage 事件切段：每出现一次新的 stage 就把上一段收尾。同一关连着
    报好几次（MAA 换作业时会重复上报）只算一段。

    这份记录能直接看出「卡住 → 重启 → 从头重跑」的老毛病：日志里会出现
    ``s03 未完成`` 后面跟着 ``s01``，说明前两关白打了一遍。
    """
    evs = history(maa_dir, limit=6000, tail_bytes=64 * 1024 * 1024)
    rows, cur = [], None

    def close(row):
        if row:
            rows.append(row)

    for ev in evs:
        if ev['kind'] == 'stage':
            sid = ev.get('stage') or ''
            if cur and cur['stageId'] == sid:
                continue          # 同一关的重复上报
            close(cur)
            cur = {'stageId': sid, 'code': '', 'name': '',
                   'index': ev.get('index'), 'startedAt': ev.get('t'),
                   'endedAt': None, 'ok': False}
        elif ev['kind'] == 'chain_done' and cur:
            cur['endedAt'] = ev.get('t')
            cur['ok'] = True
            close(cur)
            cur = None
        elif ev['kind'] in ('chain_start', 'chain_begin') and cur:
            cur['startedAt'] = cur['startedAt'] or ev.get('t')
    close(cur)

    # 补上人类看得懂的关卡代号（act55side_s03 → TL-S-3）
    # 资源包偶尔缺新活动的隐藏关（TL-S-x 就没收录），查不到就退回 stageId，
    # 总比显示空白强——用户至少能看出卡在哪
    for r in rows:
        try:
            from . import resources
            d = resources.stage_detail(r['stageId'])
            if d:
                r['code'] = d.get('code') or ''
                r['name'] = d.get('name') or ''
        except Exception:
            pass
        if not r['code']:
            r['code'] = r['stageId']
    return rows[-limit:]
