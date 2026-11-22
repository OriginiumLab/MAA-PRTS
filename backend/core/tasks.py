# -*- coding: utf-8 -*-
"""后台任务：跑耗时操作（拉 token、写 MAA）并让前端轮询进度"""
from __future__ import annotations

import os
import re
import threading
import time
import traceback
import uuid

from . import applog
from . import settings

_tasks = {}
_lock = threading.Lock()
_keep = 40

# 每个任务的日志同时落一份到 debug/tasks/<tid>.log（2026-11-22 加）：
# 以前只存在内存里，窗口一关就全没了——用户报「多开只有一台在跑」时，
# 5556 那组一行日志都没有，想查也没现场。文件很小（几 KB），顺手就写。
_TASK_LOG_DIR = os.path.join(settings.DEBUG_DIR, 'tasks')


def _open_log(tid, title, path=None):
    try:
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        else:
            os.makedirs(_TASK_LOG_DIR, exist_ok=True)
            path = os.path.join(_TASK_LOG_DIR, '%s.log' % tid)
        fh = open(path, 'w', encoding='utf-8', newline='')
        fh.write('# %s  %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), title))
        return fh
    except Exception:                           # noqa: BLE001 日志落不下来不能拖垮任务
        return None


def _decorate(text, prefix):
    """给一行日志补 ``[账号]`` 前缀；已有前缀原样，``[组 ...]`` 改写成账号

    多开时两台模拟器的日志会被 tail 到同一个任务里交错显示，前端右栏按账号
    分栏只能靠这个前缀认人。写在 ``log()`` 这一层而不是每个调用点：MAA 的
    「MaaCore 已加载 / 模拟器已连接」是 ``maaplan.run_core`` 直接调
    ``tasks.log_factory`` 写的，包装 worker 的 log 函数根本拦不到它。
    """
    s = str(text)
    if not prefix:
        return s
    m = re.match(r'^\[([^\]]{1,60})\]\s*(.*)$', s, re.S)
    if not m:
        return prefix + ' ' + s
    label, rest = m.group(1), m.group(2)
    if label.startswith('组 '):
        return prefix + ' ' + rest
    return s


def create(title: str, fn=None, owners=None, log_path=None, prefix='') -> str:
    """建一个后台任务，返回 taskId

    ``owners``：这个任务属于哪些账号（账号文件名，单个字符串或列表）。
    前端右栏按账号分栏显示日志，要靠它知道「这个任务的日志该出现在哪个
    tab 里」；不传就当全局任务（所有 tab 都能看到）。

    ``log_path``：日志文件落到别处（默认 ``debug/tasks/<tid>.log``）。多开
    子进程用它把自己的日志写到父进程约定的位置，父进程 tail 文件转发。

    ``prefix``：形如 ``[昵称]``，这个任务的每一行日志自动带上它（多开子
    进程专用，见 :func:`_decorate`）。
    """
    if isinstance(owners, str):
        owners = [owners]
    owners = [str(x) for x in (owners or []) if str(x).strip()]
    tid = uuid.uuid4().hex[:12]
    fh = _open_log(tid, title, log_path)
    with _lock:
        _tasks[tid] = {
            'id': tid, 'title': title, 'status': 'running',
            'logs': [], 'result': None, 'error': None,
            'owners': owners,
            'startedAt': time.time(), 'endedAt': None, '_fh': fh,
            '_prefix': str(prefix or ''),
        }
        # 清理旧任务
        if len(_tasks) > _keep:
            for k in sorted(_tasks, key=lambda x: _tasks[x]['startedAt'])[:len(_tasks) - _keep]:
                if _tasks[k]['status'] != 'running':
                    old = _tasks.pop(k, None)
                    if old and old.get('_fh'):
                        try:
                            old['_fh'].close()
                        except Exception:
                            pass

    if fn is None:
        return tid

    def run():
        try:
            res = fn() if fn else None
            finish(tid, result=res)
        except Exception as e:
            finish(tid, error='%s: %s' % (type(e).__name__, e),
                   result={'traceback': traceback.format_exc()})

    threading.Thread(target=run, daemon=True, name='task-' + tid).start()
    return tid


def finish(tid, result=None, error=None):
    with _lock:
        t = _tasks.get(tid)
        if not t:
            return
        was_running = t['status'] == 'running'
        t['status'] = 'error' if error else 'done'
        t['result'] = result
        t['error'] = error
        t['endedAt'] = time.time()
        fh = t.get('_fh')
        if fh:
            try:
                fh.write('# %s %s%s\n'
                         % ('失败' if error else '结束',
                            time.strftime('%H:%M:%S'),
                            ('：' + error) if error else ''))
                fh.flush()
            except Exception:
                pass
    if error and was_running:
        # 失败任务此前只存在于内存（前端还能看，重启即没）。摘要 + traceback
        # 落一份到 debug/app.log，排障才有现场——文件 IO 放锁外，别卡任务队列
        tb = ''
        if isinstance(result, dict):
            tb = (result.get('traceback') or '').strip()
        applog.log(('[任务失败] %s：%s' % (t['title'], error))
                   + (('\n' + tb) if tb else ''))


def log(tid, line: str):
    with _lock:
        t = _tasks.get(tid)
        if t is not None:
            text = _decorate(str(line).rstrip('\n'), t.get('_prefix'))
            t['logs'].append(text)
            if len(t['logs']) > 800:
                del t['logs'][:200]
            fh = t.get('_fh')
            if fh:
                try:
                    fh.write(text + '\n')
                    fh.flush()
                except Exception:
                    pass


def log_factory(tid):
    return lambda line: log(tid, line)


# 对外只给这些字段。/api/task 直接把这份 dict 丢给 json.dumps，**绝不能带
# 文件句柄**：2026-11-22 加日志落盘时把 fh 塞进了任务 dict，`dict(t)` 原样
# 带出去 → `TypeError: Object of type TextIOWrapper is not JSON serializable`
# → 接口 500 → 前端每 700ms 收到一次失败，右栏只剩一行任务名、一条日志都
# 没有（用户报「右边日志都消失了」）。内部字段一律下划线开头，这里过滤。
_PUBLIC = ('id', 'title', 'status', 'logs', 'result', 'error',
           'owners', 'startedAt', 'endedAt')


def _public(t, since=0):
    out = {k: t.get(k) for k in _PUBLIC}
    logs = t.get('logs') or []
    out['logs'] = logs[since:]
    out['logCount'] = len(logs)
    return out


def get(tid, since=0):
    with _lock:
        t = _tasks.get(tid)
        if not t:
            return None
        return _public(t, since)


def for_owner(file, limit=8):
    """某个账号相关的任务（新的在前，含完整日志）

    前端切账号时要立刻把「这个号自己的日志」摆出来——只在内存里记新写入
    的行不够，切回来得能看到切换前已经跑完的那些。
    """
    f = str(file or '').strip()
    with _lock:
        rows = [_public(t) for t in _tasks.values()
                if not f or not t.get('owners') or f in t['owners']]
    rows.sort(key=lambda r: r.get('startedAt') or 0, reverse=True)
    return rows[:max(1, int(limit or 8))]


def running():
    with _lock:
        return [t for t in _tasks.values() if t['status'] == 'running']


def wait_all(timeout=5.0):
    """等所有在跑的任务结束（进程退出前调用）。

    返回超时后仍未结束的任务标题列表；全部跑完则返回 []。
    """
    deadline = time.time() + timeout
    while True:
        left = running()
        if not left:
            return []
        if time.time() >= deadline:
            return [t['title'] for t in left]
        time.sleep(0.1)
