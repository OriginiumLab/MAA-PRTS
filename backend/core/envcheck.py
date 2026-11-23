# -*- coding: utf-8 -*-
"""开跑前把环境确认一遍：模拟器开着没有、adb 通不通

为什么要单独一个前置闸：所有 skill 最终都要连模拟器，而「模拟器没开」是
最常见的一次失败——MaaCore 只会报「adb 连不上」这类底层话术，用户看到
第一反应是程序坏了。这里在任务真正开跑前先确认，没启动就**替用户启动**，
并且只在真动过手的时候往日志里写一句（一切正常时完全静默，不刷屏）。

路径从哪来（按优先级）：

1. ``settings.emulatorPath`` —— 用户手动指定的覆盖（设置页可写）
2. MAA 配置 ``<maaDir>/config/gui.new.json`` 里当前配置的
   ``Gui.ConnectSettings``：``Config``（模拟器类型）、``AdbPath``、
   ``Address``、``Extras.<类型>.EmulatorPath`` / ``InstanceIndex``
3. 从 adb 路径反推（``<模拟器目录>/adb.exe`` 的上一级）

只用 ``settings.emulatorAutoStart``（默认开）控制要不要替用户启动；关掉时
只提示不动手。

**多开时启动哪一台**：调用方点名了地址（``address='emulator-5556'``）就
按那台来，不是按 MAA 档案里的 ``InstanceIndex``。2026-11-23 用户报「只能
帮我启动 5554，启动不了 5556」——档案里一律写 ``InstanceIndex: 0``，所以
不管你要哪台，拉起来的永远是第一个实例。现在用
``ldconsole adb --index N --command get-serialno`` 反问「这个地址是第几个
实例」（实例没开时那行报错里也写着序列号，照样能解析），拿到序号再
``launch --index N``。

不放进 ``skills.SKILLS``：它不是用户可点的任务，是 ``skills.run_skill``
起任务时自动调的一次前检——所以也不会出现在 ``/api/skill/list`` 和助手的
工具列表里。
"""
from __future__ import annotations

import os
import re
import subprocess
import time

from . import settings

_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0

# 各模拟器：显示名 / 进程名 / 启动器（按优先级，前面的不行换后面）
# 启动参数里的 {idx} 换成实例序号，{dir} 换成安装目录（含尾分隔符）
_KINDS = {
    'LDPlayer': {
        'name': '雷电模拟器',
        'procs': ['dnplayer.exe', 'ldplayer.exe'],
        'launchers': [
            ('ldconsole.exe', ['launch', '--index', '{idx}']),
            ('dnplayer.exe', ['-index', '{idx}']),
        ],
    },
    'MuMuEmulator12': {
        'name': 'MuMu 模拟器 12',
        'procs': ['MuMuPlayer.exe', 'MuMuVMMHeadless.exe', 'MuMuManager.exe'],
        'launchers': [
            (os.path.join('shell', 'MuMuManager.exe'),
             ['control', '-v', '{idx}', 'launch']),
            (os.path.join('shell', 'MuMuPlayer.exe'), ['-v', '{idx}']),
            ('MuMuManager.exe', ['control', '-v', '{idx}', 'launch']),
        ],
    },
    'BlueStacks': {
        'name': '蓝叠模拟器',
        'procs': ['HD-Player.exe'],
        'launchers': [('HD-Player.exe', [])],
    },
    'Nox': {
        'name': '夜神模拟器',
        'procs': ['Nox.exe'],
        'launchers': [('Nox.exe', []), ('Nox.exe', ['-clone:Nox_{idx}'])],
    },
    'MEmu': {
        'name': '逍遥模拟器',
        'procs': ['MEmu.exe', 'MEmuHeadless.exe'],
        'launchers': [('MEmuConsole.exe', ['-v', '{idx}']),
                      ('MEmu.exe', ['-v', '{idx}'])],
    },
}

# 从 adb 路径都反推不到目录时，按常见的安装位置猜一把（只用于提示，不启动）
_GUESS_DIRS = [
    r'D:\LDPlayer\LDPlayer14',
    r'C:\LDPlayer\LDPlayer9',
    r'D:\Program Files\Netease\MuMuPlayer-12.0',
]

WAIT_DEFAULT = 90          # 启动模拟器后等 adb 就绪的上限（秒）
_TICK = 2.0                # 轮询间隔


# ---------------------------------------------------------------- 小工具
def _run(cmd, timeout=15):
    """跑一条外部命令，返回 ``(returncode, 输出)``；异常统一成 (-1, 说明)"""
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           stdin=subprocess.DEVNULL,
                           creationflags=_NO_WINDOW)
        out = (r.stdout or b'') + (r.stderr or b'')
        return r.returncode, out.decode('utf-8', 'ignore')
    except FileNotFoundError:
        return -1, '找不到文件：%s' % cmd[0]
    except Exception as e:                      # noqa: BLE001
        return -1, '%s: %s' % (type(e).__name__, e)


def _proc_alive(names):
    """这些进程名里有任何一个在跑吗（tasklist，Windows 专用）"""
    if os.name != 'nt':
        return False
    for name in names:
        rc, out = _run(['tasklist', '/FI', 'IMAGENAME eq %s' % name, '/NH'],
                       timeout=8)
        if rc == 0 and name.lower() in out.lower():
            return True
    return False


def _ld_instance_up(emu_dir, index):
    """雷电专用：``ldconsole list2`` 里这个实例是不是已经起来了

    为什么要更准的一层：进程名只能说明「有某个实例在跑」。用户多开时
    实例 1 开着、MAA 配的是实例 0，光看进程会以为「已在启动中」，白等
    45 秒还连不上。``list2`` 每行是
    ``序号,标题,顶层窗口,绑定窗口,安卓是否已启动,pid,vbox pid,宽,高,dpi``，
    第 5 个字段是 0 / 1。拿不到（旧版雷电 / 命令失败）返回 None，由调用方
    退回进程判断。
    """
    exe = os.path.join(emu_dir or '', 'ldconsole.exe')
    if not os.path.isfile(exe):
        return None
    rc, out = _run([exe, 'list2'], timeout=10)
    if rc != 0:
        return None
    for line in out.splitlines():
        parts = line.split(',')
        if len(parts) < 5:
            continue
        try:
            idx = int(parts[0].strip())
        except (TypeError, ValueError):
            continue
        if idx == int(index or 0):
            return parts[4].strip() == '1'
    return None


_SERIAL_RE = re.compile(r'emulator-(\d{4,6})')

#: ldconsole 一次调用 ~100ms，而设置页一次要问「有哪些实例、各自序列号」，
#: 八张账号卡就是几十次。3 秒内复用同一份结果，别让界面卡成幻灯片。
_LD_CACHE = {}
_LD_TTL = 3.0


def _cached(key, make):
    """``make()`` 的结果缓存 3 秒（只用于 ldconsole 的只读查询）"""
    hit = _LD_CACHE.get(key)
    if hit and time.time() - hit[0] < _LD_TTL:
        return hit[1]
    val = make()
    _LD_CACHE[key] = (time.time(), val)
    return val


def ld_serial(emu_dir, index):
    """雷电：实例序号 → adb 序列号（``emulator-5556``），拿不到返回空串

    ``ldconsole adb --index N --command get-serialno``：实例开着就回序列号，
    实例没开时回 ``error: device 'emulator-5558' not found``——**错误行里也
    写着序列号**，照样能解析出来。所以这个调用给出的「序号 ↔ 地址」对应
    关系不依赖实例是否在运行，比按端口公式猜可靠（多开时序号和端口错开
    一位就会点错台，那是「说好开第二台，结果第一台又弹出来一个」）。
    """
    exe = os.path.join(emu_dir or '', 'ldconsole.exe')
    if not os.path.isfile(exe):
        return ''
    try:
        idx = int(index or 0)
    except (TypeError, ValueError):
        return ''
    rc, out = _cached((emu_dir, 'serial', idx),
                      lambda: _run([exe, 'adb', '--index', str(idx),
                                    '--command', 'get-serialno'], timeout=10))
    if rc != 0:
        return ''
    m = _SERIAL_RE.search(out or '')
    return 'emulator-%s' % m.group(1) if m else ''


def ld_instances(emu_dir):
    """雷电：这台机器上有哪些实例 → ``[(序号, 安卓起没起来)]``

    ``ldconsole`` 读不到（旧版 / 目录不对）时返回 ``[]``——调用方据此退回
    老办法，别因为读不到就当成「一个实例都没有」。
    """
    exe = os.path.join(emu_dir or '', 'ldconsole.exe')
    if not os.path.isfile(exe):
        return []
    rc, out = _cached((emu_dir, 'instances'),
                      lambda: _run([exe, 'list2'], timeout=10))
    if rc != 0:
        return []
    got = []
    for line in (out or '').splitlines():
        parts = line.split(',')
        if len(parts) < 5:
            continue
        try:
            got.append((int(parts[0].strip()), parts[4].strip() == '1'))
        except (TypeError, ValueError):
            continue
    return got


def adb_port(addr):
    """``emulator-5556`` → 5556；``127.0.0.1:5555`` → 5555；其余 0

    雷电同一台机器有两个名字：控制台口（``emulator-5554``，MAA 档案里常
    写这个）和 adb 口（``127.0.0.1:5555``，adb 自己连的时候常用）。判
    「是不是同一台」时两个都要认，否则用户按 ip:port 填就永远对不上。
    """
    a = str(addr or '').strip()
    m = re.match(r'^emulator-(\d{2,6})$', a)
    if m:
        return int(m.group(1))
    m = re.match(r'^[^:]+:(\d{2,5})$', a)
    if m:
        return int(m.group(1))
    return 0


def index_of_address(cfg, address):
    """这个 adb 地址是**第几个实例** → ``(序号 or None, 序列号)``

    只对雷电能精确反查（``ldconsole adb --index N`` 把序号换成序列号，反
    过来就能问「5556 是第几个」）。其他模拟器没有这种反查手段，返回
    ``(None, '')``，调用方按老口径「只检查、不代启动」处理。

    序号 = 实例在自己那台机器上的排位（``ldconsole list2`` 第一列），也就
    是 ``launch --index`` 要的值；**不是** MAA 档案里那个 ``InstanceIndex``
    （八份档案常常全写 0）。
    """
    addr = str(address or '').strip()
    if not addr:
        return None, ''
    edir = cfg.get('emu_dir') or ''
    kind = cfg.get('kind') or ''
    if kind and kind != 'LDPlayer':
        # 明确是别的模拟器就别拿 ldconsole 去套（目录里恰好有雷电也只是巧合）
        return None, ''
    if not os.path.isfile(os.path.join(edir, 'ldconsole.exe')):
        return None, ''
    port = adb_port(addr)
    insts = ld_instances(edir)
    if not insts:
        return None, ''
    # 先问「开着的那几台」，再问没开的：对上了就少几次 subprocess
    guess = -1
    if port >= 5554 and (port - 5554) % 2 == 0:
        guess = (port - 5554) // 2
    order = sorted(insts, key=lambda x: (x[0] != guess, not x[1], x[0]))
    for idx, _up in order:
        ser = ld_serial(edir, idx)
        if not ser:
            continue
        if ser == addr:
            return idx, ser
        sp = adb_port(ser)
        # adb 口的端口 = 控制台口 + 1，两个写法指向同一台
        if port and sp and port in (sp, sp + 1):
            return idx, ser
    return None, ''


# ---------------------------------------------------------------- 读配置
def read_config(maa_dir=None, profile=None, address=''):
    """收集连接参数。读不到 MAA 配置时返回空壳，调用方据此跳过检查。

    多开时每个账号绑的是不同 MAA（也往往连不同模拟器），所以这里能指定
    看哪一份 MAA 配置——不指定就看全局默认那份。

    ``address`` 是调用方点名的那台（``emulator-5556``）：给了就以它为准反查
    实例序号，**压过**档案里的 ``InstanceIndex``。档案里那个值在多开用户那
    儿常年是 0（八份档案抄来抄去），照它启动永远拉起第一个实例。
    返回里 ``indexFrom`` 说明序号是哪来的（``地址`` / ``档案``），``serial``
    是序号换回来的 adb 序列号，用来「等它上线」时两种写法都认。
    """
    out = {'adb': '', 'address': '', 'kind': '', 'emu_dir': '', 'index': 0,
           'indexFrom': '档案', 'serial': '', 'name': '', 'source': ''}

    # 1) MAA 的 gui.new.json
    try:
        from . import maa
        cfg = maa._load_cfg(maa_dir or None)
    except Exception:                           # noqa: BLE001 配置缺失不算错
        cfg = None
    if cfg:
        configs = cfg.get('Configurations') or {}
        name = profile or cfg.get('Current') or ''
        conf = (configs.get(name)
                or (next(iter(configs.values())) if configs else {}) or {})
        cs = ((conf.get('Gui') or {}).get('ConnectSettings') or {})
        kind = str(cs.get('Config') or '')
        extras = (cs.get('Extras') or {}).get(kind) or {}
        out['kind'] = kind
        out['adb'] = str(cs.get('AdbPath') or '')
        out['address'] = str(cs.get('Address') or '')
        out['emu_dir'] = str(extras.get('EmulatorPath') or '')
        try:
            out['index'] = int(extras.get('InstanceIndex') or 0)
        except (TypeError, ValueError):
            out['index'] = 0
        out['name'] = (_KINDS.get(kind) or {}).get('name') or kind
        out['source'] = 'MAA 配置'

    # 2) 用户手动覆盖
    try:
        st = settings.load()
    except Exception:                           # noqa: BLE001
        st = {}
    manual = str(st.get('emulatorPath') or '').strip()
    if manual:
        out['emu_dir'] = manual
        out['source'] = '设置里手动指定'

    # 3) 兜底：从 adb 路径反推安装目录
    if not out['emu_dir'] and out['adb']:
        d = os.path.dirname(out['adb'])
        if os.path.isfile(os.path.join(d, 'adb.exe')) or d:
            out['emu_dir'] = d
    if out['emu_dir'] and not out['emu_dir'].endswith(('\\', '/')):
        out['emu_dir'] += os.sep
    if not out['emu_dir'] and not out['adb']:
        for d in _GUESS_DIRS:
            if os.path.isfile(os.path.join(d, 'adb.exe')):
                out['emu_dir'] = d + os.sep
                break

    # adb：配置里的路径不存在就退到模拟器目录里的 adb
    if not out['adb'] or not os.path.isfile(out['adb']):
        cand = os.path.join(out['emu_dir'], 'adb.exe') if out['emu_dir'] else ''
        out['adb'] = cand if cand and os.path.isfile(cand) else (out['adb'] or 'adb')

    # 4) 点名了地址 → 反查实例序号（放在最后：要用到上面定下来的 emu_dir）
    if address:
        idx, ser = index_of_address(out, address)
        if idx is not None:
            out['index'] = idx
            out['indexFrom'] = '地址'
            out['serial'] = ser
        else:
            out['serial'] = str(address)
    return out


def _devices(adb):
    """``adb devices`` → ``{地址: 状态}``；连 adb 都起不来时返回 None"""
    rc, out = _run([adb, 'devices'], timeout=20)
    if rc != 0:
        return None
    found = {}
    for line in out.splitlines():
        parts = line.split('\t')
        if len(parts) >= 2:
            addr, state = parts[0].strip(), parts[1].strip()
            if addr and not addr.lower().startswith('list of devices'):
                found[addr] = state
    return found


def adb_now(cfg=None):
    """现在的 adb 状态：``(ok, 地址, 说明)``

    地址对不上不要紧：MAA 里可能写 ``emulator-5554``，也可能写
    ``127.0.0.1:5555``，同一台机器两个名字，只要有一台 device 在线就算通
    （``stagenav.Touch.ready`` 也会这么自愈）。
    """
    cfg = cfg or read_config()
    devs = _devices(cfg['adb'])
    if devs is None:
        return False, '', 'adb 跑不起来（%s）' % cfg['adb']
    want = cfg.get('address') or ''
    if want and devs.get(want) == 'device':
        return True, want, 'adb 正常（%s）' % want
    live = [a for a, s in devs.items() if s == 'device']
    if live:
        return True, live[0], 'adb 正常（%s）' % live[0]
    if want and devs.get(want):
        return False, want, '设备 %s 状态是 %s' % (want, devs[want])
    return False, want, '没有在线设备'


# ---------------------------------------------------------------- 启动
def instance_up(cfg):
    """配置里那个实例在不在跑

    雷电能用 ``list2`` 精确到实例（多开时特别重要）；其余模拟器退回进程名，
    只判断「有没有开着的」。
    """
    spec = _KINDS.get(cfg.get('kind') or '') or {}
    if not _proc_alive(spec.get('procs') or []):
        return False
    if cfg.get('kind') == 'LDPlayer':
        up = _ld_instance_up(cfg.get('emu_dir'), cfg.get('index') or 0)
        if up is not None:
            return up
    return True


def launch(cfg):
    """拉起模拟器，返回 ``(ok, 说明)``；只负责点着火，不等它跑起来"""
    kind = cfg.get('kind') or ''
    edir = cfg.get('emu_dir') or ''
    spec = _KINDS.get(kind)
    if not spec:
        return False, ('不认识这个模拟器类型（%s），没法替你启动' % (kind or '未配置'))
    if not edir or not os.path.isdir(edir):
        return False, '模拟器目录不存在：%s' % (edir or '未配置')
    idx = str(cfg.get('index') or 0)
    tried = []
    for rel, args in spec['launchers']:
        exe = os.path.join(edir, rel)
        if not os.path.isfile(exe):
            continue
        argv = [exe] + [a.replace('{idx}', idx).replace('{dir}', edir)
                        for a in args]
        try:
            subprocess.Popen(argv, cwd=edir, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             creationflags=_NO_WINDOW)
            return True, '%s %s' % (os.path.basename(exe), ' '.join(argv[1:]))
        except Exception as e:                  # noqa: BLE001
            tried.append('%s（%s）' % (os.path.basename(exe), e))
    if tried:
        return False, '启动器都试过了，起不来：' + '；'.join(tried)
    return False, '目录里没找到启动器：%s（%s）' % (edir, spec['name'])


def _addr_hit(devs, wants):
    """``wants`` 里第一个在线的设备（都没在线返回空串）"""
    for w in wants or []:
        if w and (devs or {}).get(w) == 'device':
            return w
    return ''


def wait_ready(cfg, timeout=WAIT_DEFAULT, log=None, quiet_until=8.0,
               wants=None):
    """等 adb 就绪；``quiet_until`` 内不刷进度，免得开机快的时候一堆噪音

    ``wants`` 非空时**只等这几台**（``['emulator-5556', 'emulator-5554']``
    这种同机两个写法），不能被「另一台先起来了」骗过去——多开时那正是
    「等的是第二台，结果第一台一响就往下跑，最后连到错的那台」的来源。
    """
    log = log or (lambda *_: None)
    wants = [w for w in (wants or []) if w]
    t0 = time.time()
    last_note = 0.0
    while True:
        if wants:
            devs = _devices(cfg['adb']) or {}
            hit = _addr_hit(devs, wants)
            if hit:
                return True, hit, 'adb 正常（%s）' % hit
            live = [a for a, s in devs.items() if s == 'device']
            detail = '「%s」还没上线（现在在线：%s）' % (
                wants[0], '、'.join(live) or '无')
        else:
            ok, addr, detail = adb_now(cfg)
            if ok:
                return True, addr, detail
        spent = time.time() - t0
        if spent >= timeout:
            return False, (wants[0] if wants else cfg.get('address') or ''), \
                detail
        if spent - last_note >= 15 and spent >= quiet_until:
            last_note = spent
            log('还在等模拟器起来…（%d 秒，%s）' % (int(spent), detail))
        time.sleep(_TICK)


# ---------------------------------------------------------------- 对外
def _auto_start():
    """设置里的「没开就替用户启动」（读不到设置按开处理）"""
    try:
        return bool(settings.load().get('emulatorAutoStart', True))
    except Exception:                           # noqa: BLE001
        return True


def _fail_manual(name, detail):
    return ('%s没启动（%s）。设置里关了自动启动，请自己开一下。'
            % (name, detail))


def ensure(log=None, wait=None, maa_dir=None, profile=None, address=None):
    """开跑前确认环境，返回 ``(ok, 说明)``

    一切正常时**一行日志都不写**（用户要的是「别显示出来」，只在他没开
    模拟器的时候提示并代劳）。返回 False 时调用方应当直接停任务——连不上
    设备还往下跑，只会在几十秒后报一句更难懂的「找不到关卡」。

    ``maa_dir`` / ``profile`` 指定看哪份 MAA 配置（多开时每个账号一份）。

    ``address`` 是多开时点名的那台（账号绑的 / 用户当场说的）。这台不在线
    时**按它的实例序号去启动那一台**，绝不换成档案里那台——2026-11-23 用户
    报「只能帮我启动 5554，启动不了 5556」：以前这里只查不启，而启动分支
    用的是档案里的 ``InstanceIndex``（多开用户那儿常年是 0），于是永远拉起
    第一个实例，第二个永远是「没在线，请你自己开」。
    """
    log = log or (lambda *_: None)
    cfg = read_config(maa_dir, profile, address=address or '')
    if address:
        # 同一台的两个写法都要认：用户可能填 emulator-5556，adb 里却挂着
        # 127.0.0.1:5557（或反过来），只按字面比会误判「没在线」
        wants = [address]
        if cfg.get('serial') and cfg['serial'] != address:
            wants.append(cfg['serial'])
        devs = _devices(cfg['adb']) or {}
        hit = _addr_hit(devs, wants)
        if hit:
            return True, 'adb 正常（%s）' % hit
        live = [a for a, s in devs.items() if s == 'device']
        idx = cfg.get('index') if cfg.get('indexFrom') == '地址' else None
        if idx is None:
            # 反查不到是哪台实例（其他模拟器 / 目录不对）：说清楚，别乱启动
            return False, ('指定的模拟器「%s」没在线（现在在线：%s）。'
                           '先把它开起来再重跑'
                           % (address, '、'.join(live) or '无'))
        name = '%s 的实例 %d（%s）' % (cfg.get('name') or '模拟器', idx,
                                      address)
        if not _auto_start():
            return False, _fail_manual(name, '现在在线：%s'
                                       % ('、'.join(live) or '无'))
        log('%s没启动，正在帮你启动…' % name)
        started, how = launch(cfg)
        if not started:
            return False, ('%s没启动，我试着启动也没成功：%s。'
                           '手动打开模拟器后再重试。' % (name, how))
        log('启动命令已发出（%s），等它开机…' % how)
        ok, addr, detail = wait_ready(cfg, timeout=wait or WAIT_DEFAULT,
                                      log=log, wants=wants)
        if ok:
            log('%s已启动，adb 就绪（%s）' % (name, addr))
            return True, detail
        return False, ('%s拉起来了，但 %d 秒内 adb 没连上（%s）。'
                       '等它开完机，在 MAA 里点一次「连接」，或者直接重跑任务。'
                       % (name, wait or WAIT_DEFAULT, detail))
    if not cfg['adb'] and not cfg['emu_dir']:
        return True, '没读到 MAA 配置，跳过环境检查'
    if not _KINDS.get(cfg['kind']) and not cfg['emu_dir']:
        return True, 'MAA 里没配模拟器类型（%s），跳过启动检查' % (cfg['kind'] or '空')

    ok, addr, detail = adb_now(cfg)
    if ok:
        return True, detail

    name = cfg.get('name') or '模拟器'
    # 进程在、adb 没通：多半还在开机，给一小段时间自己缓过来
    if instance_up(cfg):
        log('%s已经在跑了，等 adb 就绪…' % name)
        ok, addr, detail = wait_ready(cfg, timeout=wait or 45, log=log)
        if ok:
            log('adb 已就绪（%s）' % addr)
            return True, detail
        return False, ('%s在运行，但 adb 一直连不上（%s）。'
                       '可以在 MAA 里点一次「连接」再重试。' % (name, detail))

    # 没在跑：替用户启动
    if not _auto_start():
        return False, _fail_manual(name, detail)

    log('%s没启动，正在帮你启动…' % name)
    started, how = launch(cfg)
    if not started:
        return False, ('%s没启动，我试着启动也没成功：%s。'
                       '手动打开模拟器后再重试。' % (name, how))
    log('启动命令已发出（%s），等它开机…' % how)
    ok, addr, detail = wait_ready(cfg, timeout=wait or WAIT_DEFAULT, log=log)
    if ok:
        log('%s已启动，adb 就绪（%s）' % (name, addr))
        return True, detail
    return False, ('%s拉起来了，但 %d 秒内 adb 没连上（%s）。'
                   '等它开完机，在 MAA 里点一次「连接」，或者直接重跑任务。'
                   % (name, wait or WAIT_DEFAULT, detail))


def describe():
    """给设置页/日志看的现状（不启动任何东西）"""
    cfg = read_config()
    ok, addr, detail = adb_now(cfg)
    return {
        'ok': ok, 'address': addr, 'detail': detail,
        'kind': cfg['kind'], 'name': cfg['name'],
        'emulatorPath': cfg['emu_dir'], 'index': cfg['index'],
        'source': cfg['source'], 'adb': cfg['adb'],
    }
