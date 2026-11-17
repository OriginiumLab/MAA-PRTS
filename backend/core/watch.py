# -*- coding: utf-8 -*-
"""M3 自愈层：盯住 MAA，卡住了就关掉重开接着跑

为什么是「重启」而不是「点一下跳过」
------------------------------------
MAA 处理剧情关的链条其实是全的（``SkipThePreBattlePlot`` 点右上角 → 确认框
→ ``EndOfPlot``），问题在导航层：``FullStageNavigation`` 的定义是「在关卡
列表里右滑 10 次 → 找关卡名 → 左滑」，**它假定起点已经在关卡列表上**。
游戏一停在剧情界面，这套局部搜索就是空转，``retry_times: 20``、每次约
0.7 秒，14 秒后判任务失败，然后整个队列停住——用户看到的「卡死」其实是
它早就放弃了，正等你去点「开始」。

重启会走 ``StartUp``（开始唤醒），把游戏拉回主界面。**用已知状态覆盖未知
状态**，一个动作同时覆盖剧情、弹窗、网络、编队所有卡住场景；而「盲点右上角」
只能救剧情这一种，还得赌位置对不对。

代价：当前那一关从头再打，最多浪费一关的理智。这条写进 UI 文案。
"""
from __future__ import annotations

import os
import threading
import time

from . import asstlog, maa

POLL_SECONDS = 5
# 同一关连续这么多次都救不回来就不再自动恢复，转人工
MAX_RESUME_PER_STAGE = 3
# 恢复后至少等这么久才允许再一次恢复，避免「重启—立刻判卡—又重启」的循环
RESUME_COOLDOWN = 90


def _key(d):
    return os.path.normcase(os.path.normpath(d or ''))


class Watch:
    """盯一个 MAA 目录

    多开时每个目录一个实例（``_watches`` 按目录去重），互不干扰。
    """

    def __init__(self, maa_dir=None, profile=None, auto_resume=True,
                 on_log=None, on_state=None, on_done=None):
        self.maa_dir = maa_dir or ''
        self.profile = profile or ''
        self.auto_resume = auto_resume
        self.on_log = on_log
        self.on_state = on_state      # (snap) → None，状态变化时回调
        self.on_done = on_done        # (snap) → None，整轮跑完时回调
        self.watcher = asstlog.Watcher(self.maa_dir or None)
        self._stop = threading.Event()
        self._thread = None
        self.resume_count = 0
        self.stage_fails = {}
        self.last_resume_at = 0.0
        self.last_state = 'idle'
        self.gave_up = ''
        self.snap = self.watcher.snapshot()

    # ---------------------------------------------------------- 生命周期
    def start(self):
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                       name='maa-watch')
        self._thread.start()
        return True

    def stop(self, quiet=True):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        if not quiet:
            self._log('已停止监控')

    def alive(self):
        return bool(self._thread and self._thread.is_alive())

    def _log(self, msg):
        if self.on_log:
            try:
                self.on_log(msg)
            except Exception:
                pass

    # ---------------------------------------------------------- 主循环
    def _loop(self):
        self._log('开始监控 MAA 运行状态（每 %d 秒看一次日志）' % POLL_SECONDS)
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:      # 监控线程绝不能因为一次异常就死掉
                self._log('监控出错（已忽略）：%s: %s' % (type(e).__name__, e))
            self._stop.wait(POLL_SECONDS)
        self._log('监控已停止')

    def _tick(self):
        snap = self.watcher.poll()
        self.snap = snap
        st = snap['state']
        if st != self.last_state:
            self._on_change(self.last_state, st, snap)
            self.last_state = st

    def _on_change(self, old, new, snap):
        line = asstlog.explain(snap)
        if new == 'stalled':
            where = snap.get('stage') or '未知关卡'
            self._log('检测到停滞：%s —— %s' % (where, line['text']))
            if self.auto_resume:
                self._try_resume(snap)
            else:
                self._log('（自动恢复已关闭，需要手动处理）')
        elif new == 'failed':
            self._log('MAA 已退出：%s' % (snap.get('error') or ''))
        elif new == 'done':
            self._log('本次作业已跑完' + ('（最后一关 %s）' % snap['stage']
                                          if snap.get('stage') else ''))
            if self.on_done:
                try:
                    self.on_done(snap)
                except Exception as e:
                    self._log('完成回调出错：%s' % e)
        elif new == 'running':
            self._log('MAA 开始跑' + ('：%s' % snap['stage'] if snap.get('stage') else ''))
        if self.on_state:
            try:
                self.on_state(snap)
            except Exception:
                pass

    # ---------------------------------------------------------- 恢复
    def _try_resume(self, snap):
        now = time.time()
        if now - self.last_resume_at < RESUME_COOLDOWN:
            self._log('刚恢复过一次，先等等再动（冷却 %d 秒）'
                      % int(RESUME_COOLDOWN - (now - self.last_resume_at)))
            return
        stage = snap.get('stage') or '?'
        fails = self.stage_fails.get(stage, 0)
        if fails >= MAX_RESUME_PER_STAGE:
            self.gave_up = stage
            self._log('「%s」已经连续 %d 次救不回来，停止自动恢复。'
                      '多半是这份作业本身有问题（站位/干员/关卡不匹配），'
                      '换一份作业试试' % (stage, fails))
            return
        try:
            self._resume(snap)
        except Exception as e:
            self._log('自动恢复失败：%s: %s' % (type(e).__name__, e))

    def _resume(self, snap):
        idx = snap.get('index')
        rows = maa.tasklist(self.profile or None, maa_dir=self.maa_dir).get('items') or []
        if not rows:
            self._log('作业列表是空的，没法续跑')
            return
        # 只保留「当前这一关及之后」。index 是 TaskList 下标；越界就从头再来
        cut = idx if isinstance(idx, int) and 0 <= idx < len(rows) else 0
        keep = rows[cut:]
        if not keep:
            self._log('当前已经是最后一关，没有可续跑的条目')
            return
        stage = snap.get('stage') or ''
        self.stage_fails[stage] = self.stage_fails.get(stage, 0) + 1
        self.resume_count += 1
        self.last_resume_at = time.time()

        names = [r.get('Name') or str(r.get('CopilotId') or '?') for r in keep]
        self._log('自动恢复 #%d：从「%s」起重开 MAA，剩下 %d 关（%s）'
                  % (self.resume_count, names[0], len(keep),
                     '、'.join(names[:5]) + ('…' if len(names) > 5 else '')))
        self._log('注意：卡住的那一关会从头打一遍（最多浪费一关的理智）')

        maa.close(self.maa_dir)
        maa.set_tasklist(keep, self.profile or None, maa_dir=self.maa_dir,
                         close_first=False)
        ok = maa.launch(self.maa_dir)
        if not ok:
            self._log('重启后没检测到 MAA 进程，请手动打开')
        # 新一次运行：状态机重置，日志游标会在文件变小时自动从头读
        self.watcher.state = 'running'
        self.watcher.done_at = None
        self.watcher.error = ''
        self.last_state = 'running'
        self._log('已重新启动，继续跑')


# ---------------------------------------------------------------- 注册表
_watches = {}
_reg_lock = threading.Lock()
# 没开监控时的被动观察者（只读查询用，按目录缓存，避免重复读日志）
_passive = {}


def start(maa_dir=None, profile=None, auto_resume=True, on_log=None,
          on_state=None, on_done=None):
    """开一个目录的监控（重复调用返回同一个实例，只是更新回调）"""
    d = maa_dir or ''
    k = _key(d)
    with _reg_lock:
        w = _watches.get(k)
        if w is None:
            w = Watch(d, profile, auto_resume, on_log, on_state, on_done)
            _watches[k] = w
        else:
            w.profile = profile or w.profile
            w.auto_resume = auto_resume
            if on_log:
                w.on_log = on_log
            if on_state:
                w.on_state = on_state
            if on_done:
                w.on_done = on_done
        w.start()
        return w


def stop(maa_dir=None):
    k = _key(maa_dir or '')
    with _reg_lock:
        w = _watches.pop(k, None)
    if w:
        w.stop()
    return bool(w)


def get(maa_dir=None):
    return _watches.get(_key(maa_dir or ''))


def all_snapshots():
    return [{'maaDir': w.maa_dir, **w.snap,
             'resumeCount': w.resume_count, 'gaveUp': w.gave_up,
             'watching': w.alive()}
            for w in list(_watches.values())]


def snapshot(maa_dir=None):
    w = get(maa_dir)
    if w:
        return {'maaDir': w.maa_dir, **w.snap, 'resumeCount': w.resume_count,
                'gaveUp': w.gave_up, 'watching': w.alive()}
    # 没开监控也要能给个实时读数（前端一打开页面就要显示）。
    # 这里缓存一个被动观察者：每次请求都新建 Watcher 会把 4MB 日志重读一遍，
    # 前端 15 秒轮询一次就是每秒几十 MB 的白读
    k = _key(maa_dir or '')
    with _reg_lock:
        p = _passive.get(k)
        if p is None:
            p = asstlog.Watcher(maa_dir or None, live=True)
            _passive[k] = p
    snap = p.poll()
    return {'maaDir': maa_dir or '', **snap, 'resumeCount': 0,
            'gaveUp': '', 'watching': False}


def watchers():
    """正在跑的监控数量（退出前判断要不要等）"""
    return [w for w in list(_watches.values()) if w.alive()]


def stop_all():
    for w in list(_watches.values()):
        try:
            w.stop()
        except Exception:
            pass
    _watches.clear()
    _passive.clear()
