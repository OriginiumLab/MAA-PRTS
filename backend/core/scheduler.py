# -*- coding: utf-8 -*-
"""定时任务：到点启动程序 + 链式启动

这里只当「启动器」，不做别的（MAA 自己的任务链归 skills 那一层管）：

  - 触发两种：time（每周指定几天、某个 HH:MM）与 chain（上一条退出后接着跑）
  - 动作一种：启动一个外部程序（exe / bat / cmd）
  - 程序可以从预设里挑：本程序自己、MAA 官方客户端，以及常见其它游戏的
    自动化工具（原神 / 绝区零 / 鸣潮 / 终末地 / 重返未来 / MFA）。预设只
    决定「先试哪几个 exe 名」和默认参数，路径一律现场探测、允许手改，探测
    不到就让用户点「浏览」——各家装在哪差别太大，猜错比不猜更烦人。
    config/schedule-presets.json 里还能补自己的预设，不用改代码
  - 同一时刻只跑一个任务。冲突策略 stop（停掉在跑的）/ skip（跳过本次）
  - 链式任务失败后 continue（继续后面的）/ stop（停掉整条链）

为什么不用 Windows 计划任务：链式启动（等上一个进程退出）系统计划任务
表达不了；而且时间触发只在程序运行时生效，要开机自启得用户把本程序放进
启动项。

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
import sys
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
_preset_cache = [None, None]   # [config/schedule-presets.json 的 mtime, 预设表]
_scan_cache = {}               # preset id -> (探测时间, 找到的路径)
_SCAN_TTL = 300                # 探测结果缓存 5 分钟：装完软件重开对话框够用了
_SCAN_BUDGET = 1.5             # 一次扫盘最多 1.5 秒，宁可不找也别让界面卡住


# ---------------------------------------------------------------- 程序预设
# 预设只说「启动哪个程序、带什么参数」，不写死路径：
#   exes —— 先按这些文件名去常见位置找，找不到留给用户手选
#   args —— 界面上「参数」为空时的兜底
# 想加自己的预设：config/schedule-presets.json 里写一条
#   [{"id": "xx", "name": "显示名", "exes": ["xx.exe"], "args": "--foo"}]
# 同 id 覆盖内置那条，不用改代码。
PRESETS = (
    {'id': 'custom', 'name': '自定义程序', 'exes': (), 'args': '',
     'note': '自己填 exe / bat / cmd 的路径'},
    {'id': 'self', 'name': 'MAA-PRTS 本体', 'exes': (), 'args': '',
     'note': '把本程序自己拉起来（打包后是 MAA-PRTS.exe，源码模式是 python main.py）'},
    {'id': 'maa', 'name': 'MAA 官方客户端', 'exes': ('MAA.exe',), 'args': '',
     'note': '按设置里的 MAA 目录找 MAA.exe'},
    {'id': 'bettergi', 'name': '原神 BetterGI', 'exes': ('BetterGI.exe',), 'args': '',
     'note': 'BetterGI 主程序'},
    {'id': 'onedragon', 'name': '绝区零 一条龙',
     'exes': ('OneDragon-Launcher.exe', 'OneDragon.exe'), 'args': '',
     'note': '一条龙启动器（OneDragon-Launcher.exe）'},
    {'id': 'okww', 'name': '鸣潮 ok-ww',
     'exes': ('ok-ww.exe', 'ok-script.exe'), 'args': '',
     'note': 'ok-ww 主程序（ok-script 打包的版本）'},
    {'id': 'maaend', 'name': '终末地 MaaEnd',
     'exes': ('MaaEnd.exe', 'mxu.exe'), 'args': '',
     'note': 'MaaEnd 主程序（有的版本叫 mxu.exe）'},
    {'id': 'm9a', 'name': '重返未来 M9A', 'exes': ('M9A.exe',), 'args': '',
     'note': 'M9A 主程序'},
    {'id': 'mfaa', 'name': 'MFA Avalonia', 'exes': ('MFAAvalonia.exe',), 'args': '',
     'note': 'MFAAvalonia 主程序'},
)

_PRESET_FILE = 'schedule-presets.json'
# 扫盘时别往这些目录里钻（系统目录/包管理器缓存，翻下去只有噪音）
_SKIP_DIRS = frozenset((
    'windows', 'system32', 'winsxs', 'windows.old', '$recycle.bin',
    'system volume information', 'recovery', 'perflogs', 'appdata',
    'node_modules', '.git', '__pycache__', 'cache', 'temp',
))


def _preset_path() -> str:
    return os.path.join(settings.CONFIG_DIR, _PRESET_FILE)


def presets() -> list:
    """内置预设 + config/schedule-presets.json（同 id 覆盖）；按文件 mtime 缓存"""
    try:
        mt = os.path.getmtime(_preset_path())
    except OSError:
        mt = 0.0
    if _preset_cache[1] is not None and _preset_cache[0] == mt:
        return [dict(p) for p in _preset_cache[1]]

    out = [dict(p, builtin=True) for p in PRESETS]
    idx = {p['id']: i for i, p in enumerate(out)}
    extra = []
    try:
        import io
        import json
        with io.open(_preset_path(), encoding='utf-8') as f:
            extra = json.load(f)
    except Exception:
        extra = []          # 没文件 / 写坏了都按内置表跑
    for raw in (extra if isinstance(extra, list) else []):
        if not isinstance(raw, dict):
            continue
        pid = str(raw.get('id') or '').strip().lower()[:24]
        if not pid or pid == 'custom':
            continue
        exes = raw.get('exes')
        if isinstance(exes, str):
            exes = [exes]
        row = {
            'id': pid,
            'name': str(raw.get('name') or pid)[:20],
            'exes': tuple(str(x)[:64].strip() for x in (exes or []) if str(x).strip()),
            'args': str(raw.get('args') or '')[:200],
            'note': str(raw.get('note') or '')[:120],
            'builtin': pid in idx,
        }
        if pid in idx:
            out[idx[pid]] = row
        else:
            out.append(row)
    _preset_cache[:] = [mt, out]
    return [dict(p) for p in out]


def preset_id(v) -> str:
    """收一个预设 id；不认识的（含空值）一律当自定义"""
    pid = str(v or '').strip().lower()
    ids = {p['id'] for p in presets()}
    return pid if pid in ids else 'custom'


def _preset_of(pid):
    return next((p for p in presets() if p['id'] == pid), None)


# ---------------------------------------------------------------- 找程序
def _self_defaults() -> dict:
    """本体：打包后就是本进程的 exe；源码模式＝同一个 python 跑 main.py"""
    if settings.FROZEN:
        return {'program': sys.executable, 'args': '',
                'cwd': os.path.dirname(os.path.abspath(sys.executable))}
    main_py = os.path.join(settings.RES, 'backend', 'main.py')
    if os.path.isfile(main_py):
        # 带引号传：路径里有空格时 _split_args 才会当一个参数
        return {'program': sys.executable, 'args': '"%s"' % main_py,
                'cwd': settings.RES}
    return {'program': '', 'args': '', 'cwd': ''}


def _maa_dirs() -> list:
    st = settings.load()
    out = []
    for d in [st.get('maaDir')] + list(st.get('maaDirs') or []):
        d = settings.norm_dir(str(d or ''))
        if d and d not in out:
            out.append(d)
    return out


def _find_maa() -> str:
    """先按设置里的 MAA 目录找，再问正在跑的 MAA 进程，最后才扫盘"""
    for d in _maa_dirs():
        p = os.path.join(d, 'MAA.exe')
        if os.path.isfile(p):
            return p
    try:
        from . import maa
        for row in maa.list_running():
            p = row.get('path') or ''
            if p and os.path.isfile(p):
                return p
    except Exception:
        pass
    return _scan_exe(('MAA.exe',))


def _scan_roots() -> list:
    out = []
    for letter in 'CDEFGH':
        p = '%s:\\' % letter
        if os.path.isdir(p):
            out.append(p)
    for key in ('LOCALAPPDATA', 'PROGRAMFILES', 'PROGRAMW6432',
                'PROGRAMFILES(X86)', 'USERPROFILE'):
        p = os.environ.get(key) or ''
        if p and os.path.isdir(p):
            out.append(p)
    return out


def _walk_files(root, depth=2, deadline=None, cap=4000):
    """限深、限量、限时地走一遍目录，逐个吐出文件路径

    「找程序」只值这么点代价：装在哪都有可能，但绝不能因为扫盘把界面卡死。
    """
    stack = [(root, 0)]
    seen = 0
    while stack:
        d, lvl = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for ent in it:
                if deadline and time.time() > deadline:
                    return
                seen += 1
                if seen > cap:
                    return
                try:
                    if ent.is_file(follow_symlinks=False):
                        yield ent.path
                    elif lvl < depth and ent.is_dir(follow_symlinks=False):
                        if ent.name.lower() in _SKIP_DIRS:
                            continue
                        stack.append((ent.path, lvl + 1))
                except OSError:
                    continue


def _scan_exe(names, roots=None, budget=_SCAN_BUDGET) -> str:
    """按文件名在常见位置找 exe，找到就返回全路径，找不到返回空串

    时间预算按根目录平分：C 盘文件多，独占整个预算的话 D 盘（游戏工具常装
    在那儿）就永远轮不到。
    """
    want = {str(n).lower() for n in (names or ())}
    if not want:
        return ''
    roots = list(roots if roots is not None else _scan_roots())
    if not roots:
        return ''
    budget = max(0.2, float(budget or _SCAN_BUDGET))
    end = time.time() + budget
    per = budget / len(roots)
    for root in roots:
        left = end - time.time()
        if left <= 0:
            break
        deadline = time.time() + min(per, left)
        for path in _walk_files(root, 2, deadline):
            if os.path.basename(path).lower() in want:
                return path
    return ''


def detect(pid, refresh=False) -> str:
    """某个预设在本机找到的程序路径（结果缓存几分钟，refresh 强制重找）"""
    pid = preset_id(pid)
    if pid == 'custom':
        return ''
    now = time.time()
    hit = _scan_cache.get(pid)
    if hit and not refresh and now - hit[0] < _SCAN_TTL:
        return hit[1]
    if pid == 'self':
        path = _self_defaults().get('program') or ''
    elif pid == 'maa':
        path = _find_maa()
    else:
        row = _preset_of(pid) or {}
        path = _scan_exe(row.get('exes') or ())
    _scan_cache[pid] = (now, path)
    return path


def fill(pid, refresh=False) -> dict:
    """选了某个预设时给界面回填的默认值；用户改过的一律以用户的为准"""
    pid = preset_id(pid)
    out = {'id': pid, 'program': '', 'args': '', 'cwd': ''}
    if pid == 'custom':
        return out
    row = _preset_of(pid) or {}
    out['args'] = row.get('args') or ''
    if pid == 'self':
        out.update(_self_defaults())
        out['id'] = pid
    else:
        out['program'] = detect(pid, refresh=refresh)
    return out


def resolve(task) -> dict:
    """把预设兜底进任务：program / args / cwd 只补空的

    这样「本体」这种预设即使换台机器、换个安装位置也能跑；用户填了值就
    绝不动他自己的。
    """
    out = dict(task)
    d = fill(out.get('preset'))
    if d['id'] == 'custom':
        return out
    for key in ('program', 'args', 'cwd'):
        if not str(out.get(key) or '').strip():
            out[key] = d.get(key) or ''
    return out


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
        'preset': preset_id(raw.get('preset')),
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
    task = resolve(task)
    program = settings.norm_dir(task.get('program') or '')
    if not program:
        raise ValueError('没有填要运行的程序（预设里也没找到，点「浏览」选一下）')
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
    task = resolve(job['task'])
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
