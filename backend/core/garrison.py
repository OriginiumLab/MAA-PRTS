# -*- coding: utf-8 -*-
"""卫戍协议·策略刻章速刷：Node 宿主进程管理 + HTTP 转发

移植自独立版 ``garrison-standalone``（小黑盒扩展分离产物）。核心
``service.mjs``（GPL-3.0-or-later，含 MaaFramework 原生模块，~114MB）
**不进仓库、不进安装包**——它是被 ``node host.mjs`` 以子进程方式加载的
独立程序，Python 这边只做进程编排与代理转发。核心目录按优先级检测：

1. ``settings.garrisonCoreDir``（用户手动指定）
2. ``<运行根>/garrison/core``（把 ``medal-farm-v1.1.4/core`` 拷到这里即可）
3. ``<运行根>/garrison/medal-farm-v1.1.4/core``（整个独立包解压进 garrison/）
4. ``<运行根>/garrison-standalone/medal-farm-v1.1.4/core``（解压即用）

多账号：每个账号一个 Node 宿主进程（懒启动、随机端口 + token 握手），
各自连各自 MAA 配置里的模拟器地址，互不干扰；检查点按账号存
``config/garrison/<账号文件名>.checkpoint.json``。宿主带 --parent-pid
看门狗，Python 崩溃后它们会自己退出，不会变成孤儿继续点游戏。

连接默认值从**当前账号绑定 MAA** 的 gui.new.json 解析（ConnectSettings）：
MuMu → 设备 mumu + MuMu Extras 快速截图；雷电 → ldplayer；PC 官方客户端
→ pc；模拟模式默认极速、操作速度 1.5、目标 3 胜。
"""
from __future__ import annotations

import io
import json
import os
import queue
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid

from . import applog, garrisondl, maa, settings

CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0

HOST_JS = os.path.join(settings.RES, 'resources', 'garrison', 'host.mjs')
PATCH_FILE = os.path.join(settings.RES, 'resources', 'garrison', 'patches.json')

DEVICES = ('mumu', 'ldplayer', 'pc')
SHOT_METHODS = ('auto', 'fast', 'adb')
BUSY = ('running', 'starting', 'stopping')

# MAA ConnectSettings.Config（模拟器类型）→ 核心的 device / 截图方式。
# MuMu（含 ARM 版）走 MuMu Extras（快速截图）；雷电的 Extras 需 9.1.32+
# 且实例由核心自找，稳妥起见 auto；其余 ADB 类一律当 mumu（核心的默认）。
_KIND_MAP = {
    'MuMuEmulator12': ('mumu', 'fast'),
    'MuMuArm': ('mumu', 'fast'),
    'LDPlayer': ('ldplayer', 'auto'),
    'PC': ('pc', 'auto'),
}


# ---------------------------------------------------------------- 环境
def node_exe() -> str:
    """Node.js 可执行文件（核心是 ESM + 原生模块，需要 Node ≥ 20）"""
    try:
        return shutil.which('node') or ''
    except Exception:
        return ''


def _latest_pkg_core(base: str) -> str:
    """``<base>/medal-farm-v<版本>/core`` 里挑版本最高的一个，没有返回 ''"""
    try:
        names = os.listdir(base)
    except OSError:
        return ''
    best = None
    for name in names:
        m = garrisondl.PKG_DIR_RE.match(name)
        if not m:
            continue
        core = os.path.join(base, name, 'core')
        if not os.path.isfile(os.path.join(core, 'service.mjs')):
            continue
        if best is None or garrisondl.ver_key(m.group(1)) > garrisondl.ver_key(best[0]):
            best = (m.group(1), core)
    return best[1] if best else ''


def core_dir() -> str:
    """按约定位置找卫戍协议核心（含 service.mjs 的目录），找不到返回 ''

    目录名里的版本号**不能写死**：核心包由 garrisondl 按本机小黑盒记录安装，
    版本跟着上游走（v1.1.4 → v1.1.5 → …）。这里先扫 ``medal-farm-v*`` 取
    版本最高的那份，再退回无版本号的固定位置，避免「下载器装好了新版、
    探测却只认旧目录名」导致前端一直卡在「核心就绪，正在启动」。
    """
    cands = []
    try:
        cands.append(settings.norm_dir(settings.load().get('garrisonCoreDir') or ''))
    except Exception:
        pass
    root = settings.project_root()
    for base in (os.path.join(root, 'garrison'),
                 os.path.join(root, 'garrison-standalone')):
        cands.append(_latest_pkg_core(base))
    cands += [
        os.path.join(root, 'garrison', 'core'),
        os.path.join(root, 'garrison-standalone', 'core'),
    ]
    for d in cands:
        if d and os.path.isfile(os.path.join(d, 'service.mjs')):
            return d
    return ''


def _patch_rules():
    try:
        with io.open(PATCH_FILE, encoding='utf-8') as f:
            return (json.load(f) or {}).get('patches') or []
    except (OSError, ValueError):
        return []


def apply_core_patches(core: str) -> dict:
    """给核心 service.mjs 打最小文本补丁（幂等，失败不影响启动）

    核心是第三方 GPL 混淆包，不进仓库、由 garrisondl.py 下载；它偶尔认不出
    游戏新弹窗（如「确认准备就绪吗？当前仍处于临时整备区…」），落进 unknown
    空转 90 秒后报错。补这种缺口走规则文件里的纯文本替换，比在 MAA-PRTS 侧
    再挂一个 adb 看门狗去跟核心抢截图/输入安全得多：点哪儿仍由核心自己按
    OCR 文本算，分辨率与缩放天然自适应。

    首次改动前留 ``service.mjs.orig`` 备份；核心升级后规则脱靶（find 找不到）
    只记日志跳过，绝不动文件。返回 ``{applied, skipped, miss, error}``。
    """
    rules = _patch_rules()
    out = {'applied': 0, 'skipped': 0, 'miss': 0, 'error': ''}
    if not rules:
        return out
    path = os.path.join(core, 'service.mjs')
    try:
        with io.open(path, encoding='utf-8') as f:
            src = f.read()
    except OSError as e:
        out['error'] = str(e)
        return out
    text = src
    for r in rules:
        find = r.get('find') or ''
        repl = r.get('replace') or ''
        if not find or not repl:
            continue
        if repl in text:                     # 已经打过了
            out['skipped'] += 1
            continue
        if find not in text:                 # 核心换了写法：不硬来
            out['miss'] += 1
            applog.log('[卫戍协议] 核心补丁「%s」未命中，跳过（核心版本可能变了）'
                       % (r.get('id') or '?'))
            continue
        text = text.replace(find, repl, 1)
        out['applied'] += 1
    if text == src:
        return out
    try:
        bak = path + '.orig'
        if not os.path.isfile(bak):
            shutil.copy2(path, bak)
        tmp = path + '.tmp'
        with io.open(tmp, 'w', encoding='utf-8', newline='') as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError as e:
        out['error'] = str(e)
        applog.log('[卫戍协议] 核心补丁写入失败：%s' % e)
        return out
    applog.log('[卫戍协议] 核心补丁已应用 %d 条（已在位 %d，未命中 %d）'
               % (out['applied'], out['skipped'], out['miss']))
    return out


def _maa_dir_for(file: str) -> str:
    """账号绑定的 MAA 根目录，没有就回退全局默认（与 api._maa_dir_for 同口径，
    这里不能 import api——api 反过来要 import 本模块）"""
    if file:
        try:
            from . import accounts
            snap = accounts.get_account(file)
            d = settings.norm_dir((snap.get('maa') or {}).get('dir') or '')
            if d:
                return d
        except Exception:
            pass
    return settings.load()['maaDir']


def _maa_defaults(file: str):
    """从账号绑定 MAA 的连接配置里抄出来的裸默认值（不含用户偏好 overlay）"""
    opts = {
        'device': 'mumu', 'mode': 'quick', 'speed': 1.5, 'medalWins': 3,
        'adbAddress': '127.0.0.1:16384', 'adbPath': '',
        'screenshotMethod': 'auto', 'pcUiScale': 90,
    }
    src = {'kind': '', 'profile': '', 'maaDir': ''}
    try:
        d = _maa_dir_for(file)
        if d:
            cfg = maa._load_cfg(d)
            configs = cfg.get('Configurations') or {}
            name = cfg.get('Current') or ''
            c = (configs.get(name)
                 or (next(iter(configs.values())) if configs else {}) or {})
            cs = ((c.get('Gui') or {}).get('ConnectSettings') or {})
            kind = str(cs.get('Config') or '')
            dev, shot = _KIND_MAP.get(kind, ('mumu', 'auto'))
            opts['device'] = dev
            opts['screenshotMethod'] = shot
            addr = str(cs.get('Address') or '').strip()
            if addr:
                opts['adbAddress'] = addr
            opts['adbPath'] = str(cs.get('AdbPath') or '').strip()
            src = {'kind': kind, 'profile': name, 'maaDir': d}
    except Exception:
        pass
    return opts, src


# 可持久化的速刷选项键（账号 json 里的 garrisonOpts 只认这些）
OPT_KEYS = ('device', 'mode', 'adbAddress', 'adbPath', 'medalWins', 'speed',
            'screenshotMethod', 'pcUiScale')


def _stored_opts(file: str) -> dict:
    """账号 json 里存过的用户改动（garrisonOpts），按白名单取"""
    if not file:
        return {}
    try:
        from . import accounts
        snap = accounts.get_account(file)
        st = snap.get('garrisonOpts') or {}
        return {k: st[k] for k in OPT_KEYS
                if isinstance(st, dict) and k in st}
    except Exception:
        return {}


def defaults(file: str):
    """速刷默认参数：MAA 配置抄来的基底 + 账号里存过的用户改动。

    返回 ``(options, 来源)``。用户在面板改过的选项按账号持久化
    （save_opts 只存与 MAA 基底不同的键），重启不丢；没改过的键
    继续跟随 MAA 配置。
    """
    opts, src = _maa_defaults(file)
    stored = _stored_opts(file)
    for k in OPT_KEYS:
        if k in stored and k in opts:
            opts[k] = stored[k]
    return opts, src


def save_opts(file: str, options: dict) -> dict:
    """用户改的速刷选项写回账号 json。

    以账号**当前生效值**为基底叠加传入项再清洗（只传一个键不会把别的
    键锁到全局兜底值），然后与「MAA 抄来的裸默认」逐键比对，只存不同
    的键——改回默认值的键自动剔除，没动过的键永远跟着 MAA 配置走。
    返回合并后的生效默认值（含实际存了哪些键，便于排查）。
    """
    cur, _src0 = defaults(file)
    clean = sanitize({**cur, **(options or {})})
    base, _src = _maa_defaults(file)
    diff = {k: clean[k] for k in OPT_KEYS if clean.get(k) != base.get(k)}
    from . import accounts as _acc
    _acc.save_pref(file, {'garrisonOpts': diff})
    merged, src = defaults(file)
    return {'defaults': merged, 'fromMaa': src, 'saved': diff}


def sanitize(body: dict) -> dict:
    """前端提交的选项清洗：只放行核心认识的枚举与范围，其余回默认"""
    body = body or {}
    dft, _src = defaults('')   # 兜底基底（读不到 MAA 配置时的那套）
    device = body.get('device') if body.get('device') in DEVICES else dft['device']
    try:
        speed = min(5.0, max(0.5, float(body.get('speed') or 1.5)))
    except (TypeError, ValueError):
        speed = 1.5
    return {
        'device': device,
        'mode': body.get('mode') if body.get('mode') in ('quick', 'standard') else 'quick',
        'medalWins': 6 if str(body.get('medalWins')) == '6' else 3,
        'speed': speed,
        'adbAddress': str(body.get('adbAddress') or dft['adbAddress']).strip() or dft['adbAddress'],
        'adbPath': str(body.get('adbPath') or '').strip(),
        'screenshotMethod': (body.get('screenshotMethod')
                             if body.get('screenshotMethod') in SHOT_METHODS
                             else dft['screenshotMethod']),
        'pcUiScale': 100 if str(body.get('pcUiScale')) == '100' else 90,
    }


# ---------------------------------------------------------------- 宿主进程
# file → {'proc', 'port', 'token', 'core', 'started', 'logPath'}
_HOSTS: dict[str, dict] = {}
_lock = threading.Lock()
_SPAWN_LOCK = threading.Lock()   # 同一账号并发首启只放一个进去


def _stem(file: str) -> str:
    return os.path.splitext(os.path.basename(file or 'account'))[0]


def _checkpoint_path(file: str) -> str:
    return os.path.join(settings.CONFIG_DIR, 'garrison',
                        '%s.checkpoint.json' % _stem(file))


def _log_path(file: str) -> str:
    return os.path.join(settings.DEBUG_DIR, 'garrison-%s.log' % _stem(file))


def _alive(h) -> bool:
    try:
        return bool(h) and h['proc'].poll() is None
    except Exception:
        return False


def host_of(file: str):
    """该账号的宿主进程信息（可能为 None，不启动）"""
    with _lock:
        return _HOSTS.get(file)


def _reap(file, h):
    with _lock:
        if _HOSTS.get(file) is h:
            _HOSTS.pop(file, None)
    try:
        if _alive(h):
            h['proc'].terminate()
    except Exception:
        pass


def ensure_host(file: str) -> dict:
    """拿到该账号的宿主（没有就启动一个）；起不来抛 RuntimeError"""
    with _SPAWN_LOCK:
        h = host_of(file)
        if _alive(h):
            return h
        if h:
            _reap(file, h)
        return _spawn(file)


def _spawn(file: str) -> dict:
    node = node_exe()
    if not node:
        raise RuntimeError('找不到 Node.js：卫戍协议核心需要 Node.js 20 以上，'
                           '装好后再来（https://nodejs.org）')
    core = core_dir()
    if not core:
        raise RuntimeError('核心包还没下载好：打开卫戍协议页会自动下载安装')
    apply_core_patches(core)
    if not os.path.isfile(HOST_JS):
        raise RuntimeError('缺少宿主脚本：%s' % HOST_JS)

    ckpt = _checkpoint_path(file)
    os.makedirs(os.path.dirname(ckpt), exist_ok=True)
    os.makedirs(settings.DEBUG_DIR, exist_ok=True)
    token = uuid.uuid4().hex
    argv = [node, HOST_JS,
            '--core', core, '--checkpoint', ckpt,
            '--token', token, '--parent-pid', str(os.getpid()), '--port', '0']
    logp = _log_path(file)
    try:
        logf = io.open(logp, 'ab', buffering=0)
    except OSError:
        logf = subprocess.DEVNULL
    try:
        proc = subprocess.Popen(
            argv, cwd=core, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=logf,
            creationflags=CREATE_NO_WINDOW)
    except OSError as e:
        raise RuntimeError('速刷后端起不来：%s' % e)

    # stdout 只等一行握手：READY <port> <token>。加载原生模块（sharp/onnx）
    # 首次可能要几秒，给 45 秒；放队列里等，超时按行读余量防止卡死。
    q: queue.Queue = queue.Queue()

    def reader():
        try:
            for line in proc.stdout:          # type: ignore[union-attr]
                q.put(line.decode('utf-8', 'ignore').strip())
        except Exception:
            pass
        finally:
            q.put(None)                       # 流关闭（进程退出）

    threading.Thread(target=reader, daemon=True,
                     name='garrison-%s' % _stem(file)).start()
    deadline = time.time() + 45
    ready = None
    while time.time() < deadline:
        try:
            line = q.get(timeout=1.0)
        except queue.Empty:
            if not _alive({'proc': proc}):
                break
            continue
        if line is None:
            break
        if line.startswith('READY '):
            ready = line.split()
            break
    if not ready or len(ready) < 2:
        rc = proc.poll()
        _reap(file, {'proc': proc})
        raise RuntimeError('速刷后端没就绪（%s）。排查：%s'
                           % ('退出码 %s' % rc if rc is not None else '45 秒内没握手',
                              logp))
    try:
        port = int(ready[1])
    except (IndexError, ValueError):
        _reap(file, {'proc': proc})
        raise RuntimeError('速刷后端握手行不认识：%r' % ' '.join(ready))

    h = {'proc': proc, 'port': port, 'token': token, 'core': core,
         'started': time.time(), 'logPath': logp}
    with _lock:
        _HOSTS[file] = h
    applog.log('[卫戍协议] %s 的速刷后端已启动（端口 %d，核心 %s）'
               % (_stem(file), port, core))
    return h


def _call(h: dict, method: str, path: str, body=None, timeout=30.0) -> dict:
    url = 'http://127.0.0.1:%d%s' % (h['port'], path)
    data = json.dumps(body).encode('utf-8') if body is not None else None
    headers = {'x-garrison-token': h['token']}
    if data is not None:
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode('utf-8')).get('error') or ''
        except Exception:
            msg = ''
        raise RuntimeError(msg or ('速刷后端返回 %d' % e.code))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise RuntimeError('速刷后端没响应（%s），详情见 %s'
                           % (getattr(e, 'reason', e), h.get('logPath', '')))


def _state_of(h: dict) -> dict:
    return _call(h, 'GET', '/api/state', timeout=10) or {}


# ---------------------------------------------------------------- 对外（api 层调）
def status(file: str) -> dict:
    """卫戍协议面板的一揽子状态：环境 + 默认值 + 当前运行态（懒启动宿主）。

    核心缺失时带 ``needsDownload`` + 下载进度（garrisondl 的状态机），
    前端据此自动开始下载并显示进度条；下载是全局一份（不分账号）。
    """
    node = node_exe()
    core = core_dir()
    dft, src = defaults(file)
    dl = garrisondl.state()
    if not core and dl.get('stage') == 'done':
        # 下载器说「装好了」、core_dir 却仍找不到：别把前端留在
        # 「核心就绪，正在启动…」里无限重试，直接把矛盾摆出来让用户重试/重下。
        dl = dict(dl, stage='error', error=(
            '核心包 v%s 已下载，但没找到可用的 core/service.mjs。请点重试；'
            '仍然不行就把 %s 整个删掉再重下。'
            % (dl.get('version') or '未知版本',
               os.path.join(settings.project_root(), 'garrison'))))
    out = {
        'nodeOk': bool(node), 'coreOk': bool(core), 'coreDir': core,
        'defaults': dft, 'fromMaa': src,
        'ready': False, 'running': False, 'state': None,
        'hasCheckpoint': os.path.isfile(_checkpoint_path(file)),
        'needsDownload': not core,
        'download': dl,
    }
    if node and core:
        try:
            h = ensure_host(file)
            st = _state_of(h)
            out['state'] = st.get('state')
            out['hasCheckpoint'] = bool(st.get('hasCheckpoint', out['hasCheckpoint']))
            status_ = (out['state'] or {}).get('status')
            out['running'] = status_ in BUSY
            out['ready'] = True
        except Exception as e:
            out['error'] = str(e)
    return out


def start_download() -> dict:
    """开始（或继续）核心包下载；幂等，返回当前下载状态"""
    return garrisondl.start()


def test(file: str, options: dict) -> dict:
    h = ensure_host(file)
    return _call(h, 'POST', '/api/test', sanitize(options), timeout=120)


def start(file: str, options: dict) -> dict:
    h = ensure_host(file)
    return _call(h, 'POST', '/api/start', sanitize(options), timeout=30)


def resume(file: str, options: dict) -> dict:
    h = ensure_host(file)
    return _call(h, 'POST', '/api/resume', sanitize(options), timeout=30)


def stop(file: str) -> dict:
    h = host_of(file)
    if not _alive(h):
        raise ValueError('当前没有速刷任务在跑')
    return _call(h, 'POST', '/api/stop', {}, timeout=15)   # type: ignore[arg-type]


def stop_all(timeout=6.0):
    """程序退出前收尾：通知各宿主自行 stop+dispose，等不及再硬杀。

    正在跑的对局点掉就停（核心的 stop 是可恢复的，检查点已落盘），
    所以这里不做长等待——用户关窗口的耐心比一局速刷宝贵。
    """
    with _lock:
        hosts = list(_HOSTS.items())
    if not hosts:
        return []
    for _file, h in hosts:
        try:
            _call(h, 'POST', '/api/shutdown', {}, timeout=3)
        except Exception:
            pass
    deadline = time.time() + timeout
    for _file, h in hosts:
        left = max(0.1, deadline - time.time())
        try:
            h['proc'].wait(timeout=left)
        except subprocess.TimeoutExpired:
            try:
                h['proc'].terminate()
            except Exception:
                pass
        except Exception:
            pass
    applog.log('[卫戍协议] 已停止 %d 个速刷后端' % len(hosts))
    with _lock:
        for file, h in hosts:
            if _HOSTS.get(file) is h:
                _HOSTS.pop(file, None)
    return [f for f, _h in hosts]
