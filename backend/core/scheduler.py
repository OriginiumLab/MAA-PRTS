# -*- coding: utf-8 -*-
"""定时任务：按时间启动程序 + 链式启动

语义照搬三月七助手的「定时任务配置」，只保留在我们这儿能落地、也确实是
用户在用的那部分（朋友要搬的 autostart-138.exe / OneDragon-Launcher.exe /
cmd 批处理都属于这一类）：

  - 触发两种：time（每周指定几天、某个 HH:MM）与 chain（上一条退出后接着跑）
  - 动作一种：启动一个外部程序。三月七的「本体任务」我们不做——MAA 自己的
    任务链由 skills 那一层负责，这里只当启动器
  - 同一时刻只跑一个任务。冲突策略 stop（停掉在跑的）/ skip（跳过本次）
  - 链式任务失败后 continue（继续后面的）/ stop（停掉整条链）

为什么不用 Windows 计划任务：链式启动（等上一个进程退出）系统计划任务
表达不了；而且时间触发只在程序运行时生效——跟三月七一致，要开机自启
得用户把本程序放进启动项。

数据存 config/schedule.json（原子写）；日志写 debug/schedule.log，同时留
一段内存环形缓冲给界面读（含 seq，供前端轮询拿「刚跑完」的通知）。
"""
from __future__ import annotations

import collections
import datetime
import os
import queue
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid

from . import settings
from .atomicio import dump_json

VERSION = 1
MAX_TASKS = 60
CONFLICTS = ('stop', 'skip')
ON_FAIL = ('continue', 'stop')
_TICK = 5.0
_LOG_MAX = 1024 * 1024

_lock = threading.RLock()
_cache = None
_running = None            # {id,name,proc,startedAt,killed}
_queue = queue.Queue()
_fired = {}                # task id -> 'YYYY-MM-DD HH:MM'（同一分钟不重复触发）
_last = {}                 # task id -> {at,ok,code,text}
_events = collections.deque(maxlen=400)
_seq = 0
_started = False


# ---------------------------------------------------------------- 存储
def _path() -> str:
    # 每次取 settings 的当前值：冒烟测试会把 CONFIG_DIR 指到临时目录
    return os.path.join(settings.CONFIG_DIR, 'schedule.json')


def _defaults() -> dict:
    return {'version': VERSION, 'conflict': 'stop', 'onChainFail': 'continue', 'tasks': []}


def _new_id() -> str:
    return 's' + uuid.uuid4().hex[:8]


def _norm_time(v) -> str:
    m = re.match(r'^(\d{1,2}):(\d{1,2})$', str(v or '').strip())
    if not m:
        return ''
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return ''
    return '%02d:%02d' % (h, mi)


def _parse_time(v):
    s = _norm_time(v)
    if not s:
        return None
    return int(s[:2]), int(s[3:])


def _norm_weekdays(v):
    """1=周一 … 7=周日。缺省/脏数据一律当每天全选（宁可按天跑，不要静默不跑）"""
    if v is None:
        return list(range(1, 8))
    out = set()
    for d in (v if isinstance(v, (list, tuple, set)) else []):
        if isinstance(d, bool):
            continue
        try:
            n = int(d)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= 7:
            out.add(n)
    return sorted(out)


def _norm_timeout(v) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 0
    return max(0, min(n, 7 * 86400))


def normalize_task(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    mode = 'chain' if str(raw.get('mode') or '').strip().lower() == 'chain' else 'time'
    name = str(raw.get('name') or '').strip()[:40] or '未命名任务'
    return {
        'id': str(raw.get('id') or '').strip()[:24] or _new_id(),
        'enabled': raw.get('enabled') is not False,
        'name': name,
        'mode': mode,
        'time': _norm_time(raw.get('time')) or '08:00',
        'weekdays': _norm_weekdays(raw.get('weekdays')),
        'program': settings.norm_dir(str(raw.get('program') or '')),
        'args': str(raw.get('args') or '').strip()[:1000],
        'cwd': settings.norm_dir(str(raw.get('cwd') or '')),
        'timeout': _norm_timeout(raw.get('timeout')),
        'notify': bool(raw.get('notify')),
    }


def load() -> dict:
    global _cache
    with _lock:
        if _cache is not None:
            return _copy(_cache)
        data = _defaults()
        try:
            import io
            import json
            with io.open(_path(), encoding='utf-8') as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                if saved.get('conflict') in CONFLICTS:
                    data['conflict'] = saved['conflict']
                if saved.get('onChainFail') in ON_FAIL:
                    data['onChainFail'] = saved['onChainFail']
                tasks = saved.get('tasks')
                if isinstance(tasks, list):
                    data['tasks'] = [normalize_task(t) for t in tasks[:MAX_TASKS]]
        except Exception:
            pass          # 没文件 / 坏了都按默认跑，坏文件留给用户自己看
        _cache = data
        return _copy(data)


def _copy(cfg) -> dict:
    return {
        'version': VERSION,
        'conflict': cfg.get('conflict') or 'stop',
        'onChainFail': cfg.get('onChainFail') or 'continue',
        'tasks': [dict(t) for t in (cfg.get('tasks') or [])],
    }


def save(patch: dict) -> dict:
    """整表保存（tasks / conflict / onChainFail），返回落盘后的视图"""
    global _cache
    patch = patch or {}
    with _lock:
        cur = load()
        if 'conflict' in patch and patch['conflict'] in CONFLICTS:
            cur['conflict'] = patch['conflict']
        if 'onChainFail' in patch and patch['onChainFail'] in ON_FAIL:
            cur['onChainFail'] = patch['onChainFail']
        if isinstance(patch.get('tasks'), list):
            seen = set()
            tasks = []
            for raw in patch['tasks'][:MAX_TASKS]:
                t = normalize_task(raw)
                while t['id'] in seen:
                    t['id'] = _new_id()
                seen.add(t['id'])
                tasks.append(t)
            cur['tasks'] = tasks
        os.makedirs(settings.CONFIG_DIR, exist_ok=True)
        dump_json(_path(), cur)
        _cache = cur
        return _copy(cur)


# ---------------------------------------------------------------- 时间计算
def _due_slot(task, now_ts=None) -> str:
    """当前这一分钟正好是这个任务的计划点就返回槽位串，否则空。

    只认「当前分钟」，不回看上一分钟：程序关着的时候错过的点不补跑——
    补跑会在开机瞬间一口气把昨天所有的任务全拉起来，比漏一次更糟。
    """
    if task.get('mode') != 'time':
        return ''
    t = _parse_time(task.get('time'))
    wd = task.get('weekdays') or []
    if not t or not wd:
        return ''
    dt = datetime.datetime.fromtimestamp(now_ts if now_ts is not None else time.time())
    if dt.isoweekday() not in wd:
        return ''
    if (dt.hour, dt.minute) != t:
        return ''
    return dt.strftime('%Y-%m-%d %H:%M')


def next_run_ts(task, now_ts=None) -> int:
    if task.get('mode') != 'time':
        return 0
    t = _parse_time(task.get('time'))
    wd = task.get('weekdays') or []
    if not t or not wd:
        return 0
    now = datetime.datetime.fromtimestamp(now_ts if now_ts is not None else time.time())
    # 同一分钟内还没触发过的，就算「下一次就是现在」，界面上不会显示成下周
    if _due_slot(task, now.timestamp()) and _fired.get(task.get('id')) != \
            now.strftime('%Y-%m-%d %H:%M'):
        return int(now.replace(second=0, microsecond=0).timestamp())
    for off in range(0, 9):
        day = (now + datetime.timedelta(days=off)).date()
        if day.isoweekday() not in wd:
            continue
        cand = datetime.datetime.combine(day, datetime.time(*t))
        if cand > now:
            return int(cand.timestamp())
    return 0


def _chain_after(task, tasks) -> list:
    """紧跟在 task 后面的一串 chain 任务（中间夹一条 time 任务就断链）"""
    idx = next((i for i, t in enumerate(tasks) if t.get('id') == task.get('id')), -1)
    if idx < 0:
        return []
    out = []
    for t in tasks[idx + 1:]:
        if t.get('mode') != 'chain':
            break
        if t.get('enabled'):
            out.append(t)
    return out


# ---------------------------------------------------------------- 日志/事件
def _log_path() -> str:
    return os.path.join(settings.DEBUG_DIR, 'schedule.log')


def _rotate_log():
    try:
        if os.path.getsize(_log_path()) > _LOG_MAX:
            os.replace(_log_path(), _log_path() + '.1')
    except Exception:
        pass


def _emit(kind, text, level='info', task=None) -> dict:
    global _seq
    task = task or {}
    with _lock:
        _seq += 1
        ev = {
            'seq': _seq,
            'ts': int(time.time()),
            'time': time.strftime('%m-%d %H:%M:%S'),
            'kind': kind,
            'level': level,
            'text': text,
            'taskId': task.get('id') or '',
            'name': task.get('name') or '',
            'notify': bool(task.get('notify')),
        }
        _events.append(ev)
    try:
        os.makedirs(settings.DEBUG_DIR, exist_ok=True)
        _rotate_log()
        with open(_log_path(), 'a', encoding='utf-8') as f:
            f.write('%s  [%s] %s\n' % (ev['time'], level, text))
    except Exception:
        pass
    return ev


def log_lines(limit=80) -> list:
    with _lock:
        return list(_events)[-limit:]


# ---------------------------------------------------------------- 执行
def _split_args(s: str) -> list:
    s = str(s or '').strip()
    if not s:
        return []
    if os.name != 'nt':
        return shlex.split(s)
    # Windows 下跟 CreateProcess 用同一套解析（引号、反斜杠转义都能对上），
    # shlex(posix=False) 会把引号留在参数里
    try:
        import ctypes
        from ctypes import wintypes
        fn = ctypes.WinDLL('shell32', use_last_error=True).CommandLineToArgvW
        fn.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        fn.restype = ctypes.POINTER(wintypes.LPWSTR)
        free = ctypes.WinDLL('kernel32', use_last_error=True).LocalFree
        free.argtypes = [wintypes.HLOCAL]
        free.restype = wintypes.HLOCAL
        n = ctypes.c_int()
        arr = fn('m ' + s, ctypes.byref(n))
        if not arr:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return [arr[i] for i in range(1, n.value)]
        finally:
            free(ctypes.cast(arr, wintypes.HLOCAL))
    except Exception:
        return [p.strip('"') for p in re.findall(r'"[^"]*"|\S+', s)]


def _argv(task) -> list:
    program = settings.norm_dir(task.get('program') or '')
    if not program:
        raise ValueError('没有填要运行的程序')
    if not os.path.isabs(program):
        found = shutil.which(program)
        if found:
            program = found
    if not os.path.isfile(program):
        raise ValueError('程序不存在：%s' % program)
    args = _split_args(task.get('args'))
    ext = os.path.splitext(program)[1].lower()
    if ext in ('.bat', '.cmd'):
        comspec = os.environ.get('COMSPEC') or 'cmd.exe'
        # cmd /c 后面整条命令行要作为一个参数传，路径带空格时才不会被拆错
        return [comspec, '/d', '/c', subprocess.list2cmdline([program] + args)]
    return [program] + args


def _popen(argv, cwd):
    os.makedirs(settings.DEBUG_DIR, exist_ok=True)
    _rotate_log()
    log = open(_log_path(), 'a', encoding='utf-8')
    flags = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0) if os.name == 'nt' else 0
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT, creationflags=flags)
    finally:
        log.close()       # 子进程持有自己的句柄，父进程不需要一直开着
    return proc


def _kill(entry, why='') -> None:
    entry['killed'] = True
    proc = entry.get('proc')
    if not proc:
        return
    if why:
        _emit('kill', '停止「%s」：%s' % (entry.get('name') or '', why), 'warn',
              {'id': entry.get('id'), 'name': entry.get('name')})
    try:
        if os.name == 'nt':
            # .bat 是 cmd 的子进程，只 terminate cmd 会留下孤儿；整棵树一起收
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                           capture_output=True, timeout=10)
        else:
            proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _now_running_view():
    with _lock:
        if not _running:
            return None
        return {
            'id': _running.get('id'),
            'name': _running.get('name'),
            'pid': _running.get('pid'),
            'startedAt': _running.get('startedAt'),
            'seconds': int(time.time() - (_running.get('startedAt') or time.time())),
        }


def _enqueue(task, reason, respect_conflict=True) -> bool:
    cfg = load()
    victim = None
    with _lock:
        if _running and respect_conflict:
            if cfg['conflict'] == 'skip':
                _emit('skip', '跳过「%s」：任务「%s」还在跑（冲突策略＝跳过本次）'
                      % (task.get('name'), (_running or {}).get('name')), 'warn', task)
                return False
            victim = _running
    if victim:
        _kill(victim, '被「%s」顶掉（冲突策略＝停止正在运行的）' % task.get('name'))
    _queue.put({'task': dict(task), 'reason': reason})
    _emit('queue', '排队「%s」（%s）' % (task.get('name'), reason), 'info', task)
    return True


def _exec(job) -> None:
    global _running
    task = job['task']
    name = task.get('name') or '未命名任务'
    try:
        argv = _argv(task)
    except Exception as e:
        _emit('finish', '「%s」没跑起来：%s' % (name, e), 'err', task)
        _last[task.get('id')] = {'at': int(time.time()), 'ok': False, 'code': None, 'text': str(e)}
        return

    program = argv[0] if argv else ''
    cwd = task.get('cwd') or ''
    if not cwd:
        cwd = os.path.dirname(settings.norm_dir(task.get('program') or '')) or settings.project_root()
    if not os.path.isdir(cwd):
        cwd = settings.project_root()

    _emit('start', '启动「%s」：%s' % (name, subprocess.list2cmdline(argv)), 'info', task)
    try:
        proc = _popen(argv, cwd)
    except Exception as e:
        _emit('finish', '「%s」启动失败：%s' % (name, e), 'err', task)
        _last[task.get('id')] = {'at': int(time.time()), 'ok': False, 'code': None, 'text': str(e)}
        return

    entry = {'id': task.get('id'), 'name': name, 'proc': proc,
             'pid': proc.pid, 'startedAt': time.time(), 'killed': False}
    with _lock:
        _running = entry

    started = time.time()
    timeout = task.get('timeout') or 0
    timed_out = False
    while True:
        try:
            code = proc.wait(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            if timeout and time.time() - started > timeout:
                timed_out = True
                _kill(entry, '超过 %d 秒，强制结束' % timeout)
                try:
                    code = proc.wait(timeout=5)
                except Exception:
                    code = None
                break

    elapsed = int(time.time() - started)
    killed = bool(entry.get('killed')) and not timed_out
    ok = (code == 0) and not killed and not timed_out
    with _lock:
        _running = None
        _last[task.get('id')] = {
            'at': int(time.time()), 'ok': ok, 'code': code,
            'text': ('已停止' if killed else ('超时' if timed_out else '退出码 %s' % code)),
        }

    took = '%d 分 %d 秒' % (elapsed // 60, elapsed % 60) if elapsed >= 60 else '%d 秒' % elapsed
    if killed:
        text = '「%s」已停止（用时 %s）' % (name, took)
        level = 'warn'
    elif timed_out:
        text = '「%s」超时被结束（%s）' % (name, took)
        level = 'err'
    elif ok:
        text = '「%s」完成，用时 %s' % (name, took)
        level = 'ok'
    else:
        text = '「%s」失败，退出码 %s（用时 %s）' % (name, code, took)
        level = 'err'
    _emit('finish', text, level, task)

    # 被顶掉/手动停止的任务不再往下链，否则会跟新任务打架
    if killed:
        return
    cfg = load()
    if ok or cfg['onChainFail'] == 'continue':
        # 只排下一个：链上的每个任务跑完都会接着排它后面那个。一次性把整条链
        # 全排进去的话，后一个会被「上一条的链」和「自己前面那条的链」各排一次，
        # 同一个任务跑两遍（实测踩过）
        nxt = _chain_after(task, cfg['tasks'])[:1]
        for item in nxt:
            _enqueue(item, '链式', respect_conflict=False)
    remaining = len(_chain_after(task, cfg['tasks']))
    if not ok and cfg['onChainFail'] == 'stop' and remaining:
        _emit('chain', '「%s」失败，链式后续 %d 个任务不再执行' % (name, remaining),
              'warn', task)


def _worker() -> None:
    while True:
        job = _queue.get()
        try:
            _exec(job)
        except Exception as e:      # 单条任务炸了不能把整条调度线程带走
            _emit('error', '调度出错：%s' % e, 'err', job.get('task'))
        finally:
            _queue.task_done()


def _loop() -> None:
    last_tick = time.time()
    while True:
        try:
            now = time.time()
            # 系统休眠/改时间后往回跳：把已触发记录清掉，否则同一分钟会再触发一次
            if now < last_tick - 90:
                _fired.clear()
            last_tick = now
            for t in load()['tasks']:
                if not t.get('enabled') or t.get('mode') != 'time':
                    continue
                slot = _due_slot(t, now)
                if not slot or _fired.get(t['id']) == slot:
                    continue
                _fired[t['id']] = slot
                _enqueue(t, '定时')
        except Exception as e:
            _emit('error', '调度循环出错：%s' % e, 'err')
        time.sleep(_TICK)


def start() -> bool:
    """起调度线程（幂等）。必须在数据根确定之后调用。"""
    global _started
    with _lock:
        if _started:
            return False
        _started = True
    for fn, nm in ((_worker, 'sched-worker'), (_loop, 'sched-loop')):
        threading.Thread(target=fn, daemon=True, name=nm).start()
    _emit('boot', '定时任务已就绪（%d 条）' % len(load()['tasks']))
    return True


# ---------------------------------------------------------------- 对外
def status(since=0) -> dict:
    cfg = load()
    now = time.time()
    tasks = []
    for i, t in enumerate(cfg['tasks']):
        row = dict(t)
        row['index'] = i
        ts = next_run_ts(t, now)
        row['nextRun'] = time.strftime('%m-%d %H:%M', time.localtime(ts)) if ts else ''
        row['nextRunTs'] = ts
        row['last'] = _last.get(t['id'])
        tasks.append(row)
    with _lock:
        seq = _seq
        events = [e for e in _events if since and e['seq'] > int(since)]
    return {
        'ok': True,
        'tasks': tasks,
        'conflict': cfg['conflict'],
        'onChainFail': cfg['onChainFail'],
        'running': _now_running_view(),
        'seq': seq,
        'events': events,
        'log': log_lines(80),
        'logPath': _log_path(),
    }


def run_now(task_id) -> dict:
    cfg = load()
    task = next((t for t in cfg['tasks'] if t.get('id') == task_id), None)
    if not task:
        raise ValueError('任务不存在：%s' % (task_id or '（没选）'))
    _enqueue(task, '手动')
    return status()


def stop_current() -> dict:
    with _lock:
        entry = _running
    if entry:
        _kill(entry, '手动停止')
    return status()
