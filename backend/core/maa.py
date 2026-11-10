# -*- coding: utf-8 -*-
"""MAA 集成：进程控制、配置读写、作业写入"""
from __future__ import annotations

import copy
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time

from . import copilot, settings, tasks

CREATE_NO_WINDOW = 0x08000000
SAFE = re.compile(r'[\\/:*?"<>|]')

# 每次写入作业都会备份一份 gui.new.json，只保留最近几份，避免占满用户磁盘
KEEP_BACKUPS = 2
BACKUP_PREFIX = '.bak-app-'
# 毫秒段：同秒内两次 apply（双击触发）第二次的 copyfile 不再覆盖第一次的备份；
# 毫秒段可缺省——旧版写出的不带毫秒的备份名也要继续认（清理逻辑用）
BACKUP_STAMP = re.compile(r'^\d{8}-\d{6}(?:-\d{3})?$')


def _dir(maa_dir=None):
    """解析本次操作用的 MAA 根目录：账号绑定的目录，没有就回退全局默认"""
    return maa_dir or settings.load()['maaDir']


def _exe(maa_dir=None):
    return os.path.join(_dir(maa_dir), 'MAA.exe')


def _config_path(maa_dir=None):
    return os.path.join(_dir(maa_dir), 'config', 'gui.new.json')


def _copilot_dir(maa_dir=None):
    return os.path.join(_dir(maa_dir), 'config', 'copilot')


def _prune_backups(keep=KEEP_BACKUPS, maa_dir=None):
    """清理本程序生成的 .bak-app-<时间戳>，只留最近 keep 份

    只认「gui.new.json.bak-app-YYYYMMDD-HHMMSS」这种自己写出来的名字，
    用户或 MAA 自己的 .bak / .bak-auto / .bak-before-xxx 一律不碰。
    返回被删掉的文件名列表。
    """
    cfg = _config_path(maa_dir)
    folder = os.path.dirname(cfg)
    prefix = os.path.basename(cfg) + BACKUP_PREFIX
    try:
        names = os.listdir(folder)
    except OSError:
        return []

    cand = [n for n in names
            if n.startswith(prefix) and BACKUP_STAMP.match(n[len(prefix):])]
    if len(cand) <= keep:
        return []

    cand.sort(reverse=True)          # 时间戳定长，字典序即时间序
    removed = []
    for name in cand[keep:]:
        try:
            os.remove(os.path.join(folder, name))
            removed.append(name)
        except OSError:
            pass
    return removed


def _load_cfg(maa_dir=None):
    p = _config_path(maa_dir)
    if not os.path.isfile(p):
        raise FileNotFoundError('找不到 MAA 配置：%s（设置里检查 MAA 根目录）' % p)
    with io.open(p, encoding='utf-8') as f:
        return json.load(f)


def _save_cfg(data, maa_dir=None):
    p = _config_path(maa_dir)
    tmp = p + '.tmp'
    with io.open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    os.replace(tmp, p)


# ---------------------------------------------------------------- 状态
def is_running() -> bool:
    try:
        out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq MAA.exe', '/NH'],
                             capture_output=True, timeout=8,
                             creationflags=CREATE_NO_WINDOW)
        return b'maa.exe' in out.stdout.lower()
    except Exception:
        return False


def list_running():
    """列出正在运行的 MAA.exe，从进程反查安装目录（首次引导用）。

    tasklist 只告诉有没有、不给路径，起 PowerShell 又慢，直接
    Toolhelp32 快照 + QueryFullProcessImageNameW 拿完整路径。
    同目录多开只留一条，pid 合并展示。
    """
    import ctypes
    import ctypes.wintypes as wintypes

    class PE32W(ctypes.Structure):
        _fields_ = [
            ('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
            ('th32ProcessID', wintypes.DWORD),
            ('th32DefaultHeapID', ctypes.c_size_t),
            ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
            ('th32ParentProcessID', wintypes.DWORD),
            ('pcPriClassBase', wintypes.LONG), ('dwFlags', wintypes.DWORD),
            ('szExeFile', ctypes.c_wchar * 260),
        ]

    TH32CS_SNAPPROCESS = 0x2
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, ctypes.c_wchar_p, ctypes.c_void_p]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    k32.CloseHandle.restype = wintypes.BOOL

    def image_path(pid):
        """exe 完整路径；权限不够（别的用户的进程）拿不到就返回 None"""
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return None
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = wintypes.DWORD(len(buf))
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                return buf.value
            return None
        finally:
            k32.CloseHandle(h)

    found = {}
    try:
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == ctypes.c_void_p(-1).value:
            return []
        try:
            ent = PE32W()
            ent.dwSize = ctypes.sizeof(PE32W)
            ok = k32.Process32FirstW(snap, ctypes.byref(ent))
            while ok:
                if ent.szExeFile.lower() == 'maa.exe':
                    p = image_path(ent.th32ProcessID)
                    if p:
                        row = found.setdefault(p.lower(), {'path': p, 'dir': os.path.dirname(p), 'pids': []})
                        row['pids'].append(ent.th32ProcessID)
                ok = k32.Process32NextW(snap, ctypes.byref(ent))
        finally:
            k32.CloseHandle(snap)
    except Exception:
        return []
    out = sorted(found.values(), key=lambda r: r['dir'])
    return out


def _dir_of(row):
    return os.path.normcase(os.path.normpath(row['dir']))


def running_in(maa_dir):
    """指定目录的 MAA 进程行（list_running 的行），没在跑返回 None。
    多开时只认自己目录的，别的 MAA 不关不报。"""
    want = os.path.normcase(os.path.normpath(maa_dir or _dir()))
    for r in list_running():
        if _dir_of(r) == want:
            return r
    return None


def status(maa_dir=None):
    d = _dir(maa_dir)
    cfg_ok = os.path.isfile(_config_path(d))
    profiles, current = [], None
    if cfg_ok:
        try:
            cfg = _load_cfg(d)
            profiles = list((cfg.get('Configurations') or {}).keys())
            current = cfg.get('Current')
        except Exception:
            cfg_ok = False
    mine = running_in(d)
    return {
        'maaDir': d,
        'exeExists': os.path.isfile(_exe(d)),
        'configExists': cfg_ok,
        'running': bool(mine),
        'runningPids': (mine or {}).get('pids') or [],
        'profiles': profiles,
        'current': current,
    }


def close(maa_dir=None):
    """关闭 MAA。给了目录就只关那个目录的进程（多开互不影响）；
    没给就按映像名全关（老语义，兜底用）。"""
    if maa_dir:
        mine = running_in(maa_dir)
        if not mine:
            return True
        for pid in mine['pids']:
            subprocess.run(['taskkill', '/PID', str(pid), '/F'],
                           capture_output=True, timeout=15, creationflags=CREATE_NO_WINDOW)
        for _ in range(20):
            if not running_in(maa_dir):
                return True
            time.sleep(0.3)
        return False
    if is_running():
        subprocess.run(['taskkill', '/IM', 'MAA.exe', '/F'],
                       capture_output=True, timeout=15, creationflags=CREATE_NO_WINDOW)
        for _ in range(20):
            if not is_running():
                return True
            time.sleep(0.3)
    return not is_running()


def _launch_env():
    """启动 MAA 用的环境：把 PyInstaller 注入 PATH 的包内目录剔掉。

    打包运行时 bootloader 会把 exe 目录与 _internal 前插进 PATH；这个
    环境会被 MAA.exe 原样继承。MAA 目录自己没带 VCRUNTIME140.dll 时，
    它会沿 PATH 捞到我们 _internal 里的那份——此后 MAA.exe 反向锁死
    发布包，build and run.bat 覆盖合并必失败（taskkill 又绝不能碰 MAA）。
    启动前清干净，MAA 就只会用自己的/系统的 CRT。
    """
    env = os.environ.copy()
    me = os.path.dirname(os.path.abspath(sys.executable))
    drop = {os.path.normcase(me), os.path.normcase(os.path.join(me, '_internal'))}
    parts = [p for p in env.get('PATH', '').split(os.pathsep)
             if p and os.path.normcase(p) not in drop]
    env['PATH'] = os.pathsep.join(parts)
    return env


def launch(maa_dir=None):
    exe = _exe(maa_dir)
    if not os.path.isfile(exe):
        raise FileNotFoundError('找不到 MAA.exe：%s' % exe)
    subprocess.Popen([exe], cwd=_dir(maa_dir), env=_launch_env(), close_fds=True)
    # 冷启动/机械盘上进程创建可能超过 1 秒：5 秒内每 0.5s 探测一次，
    # 不再单次探测就误报「启动后未检测到进程」（#61）
    d = _dir(maa_dir)
    for _ in range(10):
        if running_in(d):
            return True
        time.sleep(0.5)
    return False


# ---------------------------------------------------------------- 写作业
def _safe(name):
    return SAFE.sub('_', name).strip() or 'stage'


def apply_plan(items, profile=None, mode='replace', auto_start=True, tid=None, maa_dir=None):
    """items: [{copilotId, stageCode, title}] → 写入 MAA 多作业列表"""
    log = tasks.log_factory(tid) if tid else (lambda *_: None)
    d = _dir(maa_dir)

    log('MAA 目录：%s' % d)
    # 先校验配置可读、档案存在，再关进程：目录配错 / gui.new.json 损坏时
    # 任务反正会失败，不能先把用户的 MAA 白杀了（#53）
    cfg = _load_cfg(d)
    configs = cfg.get('Configurations') or {}
    # 没指定档案就用 MAA 当前启用的那个；一个都没有才报错
    profile = profile or cfg.get('Current') or ''
    if not profile or profile not in configs:
        raise ValueError('配置「%s」不存在，可选：%s' % (profile, ', '.join(configs)))

    # 取齐所有作业再关 MAA（#53 的原则推进一步）：加入作业集时大多已预下载好
    # （copilot.prepare），还在途的等它做完，没预到的现场下载。放在关进程之前，
    # 下载卡住或失败时用户的 MAA 不白关；MAA 关闭到重启之间只剩写文件写配置。
    fetched = {}   # copilotId → (content, flags, stage_name)，普通/突袭两行共用一份不重复取
    for idx, it in enumerate(items, 1):
        cid = int(it['copilotId'])
        if cid in fetched:
            continue
        content, flags, sname, notes, pre = copilot.fetch_prepared(cid)
        log('[%d/%d] %s #%s %s' % (idx, len(items),
            '取预下载的作业' if pre else '下载作业', cid, it.get('title') or ''))
        for n in notes:
            log('    修正：%s' % n)
        fetched[cid] = (content, flags, sname)
    if fetched:
        log('作业已就绪 %d 份' % len(fetched))

    want = os.path.normcase(os.path.normpath(d))
    rows = list_running()
    mine = next((r for r in rows if _dir_of(r) == want), None)
    others = [r for r in rows if _dir_of(r) != want]
    if mine:
        log('检测到该目录的 MAA 正在运行（PID %s），先关闭…' % ' / '.join(map(str, mine['pids'])))
        if not close(d):
            # 关不掉还硬写，运行中的 MAA 会在退出时把内存态回写、
            # 本次 TaskList 直接被覆盖丢失
            raise RuntimeError('MAA 关闭失败（PID %s），请手动退出 MAA 后重试'
                               % ' / '.join(map(str, mine['pids'])))
        log('MAA 已关闭')
    else:
        log('MAA 未运行')
    if others:
        log('其他目录的 MAA 在跑（%d 个），不动它们' % len(others))

    time.sleep(0.4)
    bak = _config_path(d) + BACKUP_PREFIX + time.strftime('%Y%m%d-%H%M%S') \
        + '-%03d' % int((time.time() % 1) * 1000)
    shutil.copyfile(_config_path(d), bak)
    log('配置已备份 → %s' % os.path.basename(bak))
    dropped = _prune_backups(maa_dir=d)
    if dropped:
        log('清理旧备份 %d 个（只保留最近 %d 份）' % (len(dropped), KEEP_BACKUPS))

    # 杀完进程重新读一份：MAA 退出前可能按退出流程再回写一次配置，
    # 不能拿关进程前读到的旧内容去覆盖
    cfg = _load_cfg(d)
    configs = cfg.get('Configurations') or {}
    if profile not in configs:
        raise ValueError('配置「%s」在 MAA 退出后不存在了，可选：%s'
                         % (profile, ', '.join(configs)))

    tpl = copy.deepcopy((configs.get('Default') or {}).get('Copilot') or {})
    target = configs[profile]
    cop = target.get('Copilot') or {}
    for k, v in tpl.items():
        cop.setdefault(k, copy.deepcopy(v))

    old = list(cop.get('TaskList') or []) if mode == 'append' else []
    new_entries, files = [], []
    # copilotId → (文件名, flags, stage_name)：同一份作业被普通/突袭两行各加一次时只落一次盘
    fname_cache = {}

    for idx, it in enumerate(items, 1):
        cid = int(it['copilotId'])
        stage_code = it.get('stageCode') or str(cid)
        if cid in fname_cache:
            fname, flags, sname = fname_cache[cid]
            log('[%d/%d] 复用已就绪的作业 #%s %s' % (idx, len(items), cid, it.get('title') or ''))
        else:
            content, flags, sname = fetched[cid]
            fname = '%s_%s.json' % (_safe(stage_code), cid)
            os.makedirs(_copilot_dir(d), exist_ok=True)
            fpath = os.path.join(_copilot_dir(d), fname)
            with io.open(fpath, 'w', encoding='utf-8') as f:
                json.dump(content, f, ensure_ascii=False, indent=2)
            fname_cache[cid] = (fname, flags, sname)
        if fname not in files:
            files.append(fname)

        # 作业集里带 variant 的条目是从「普通 / 突袭 / 磨难 / 险地」那一行加的，
        # 只写对应那一条；不带 variant 的（卡片、详情弹窗）按作业自己的难度展开。
        # 险地/磨难/简单以作业 stage_name 为准（entry_variants 里说明）。
        raids = copilot.entry_variants(flags, it.get('variant'), sname)
        for is_raid in raids:
            new_entries.append({
                # Name 只是 MAA 列表里的标签，用可读的名字（悖论关卡就是「悖论·仇白」；
                # 难度变体行是「12-7·磨难」），没给名字的仍然退回关卡代号
                'Name': it.get('name') or stage_code,
                'IsNavNameOverride': False,
                'FilePath': 'config\\copilot\\' + fname,
                'CopilotId': cid,
                'IsRaid': is_raid,
                'IsChecked': True,
                'Index': 0,
            })
        log('    难度：%s → 写入 %s' % (
            copilot.difficulty_text_for(sname, flags),
            ' + '.join('突袭/险地' if r else '普通' for r in raids) or '无'))

    task_list = old + new_entries
    for i, t in enumerate(task_list):
        t['Index'] = i
    cop['TaskList'] = task_list
    target['Copilot'] = cop
    cfg.setdefault('Configurations', {})[profile] = target
    if 'Current' in cfg:
        cfg['Current'] = profile
    _save_cfg(cfg, maa_dir=d)
    log('已写入配置「%s」，作业列表 %d 条（本次新增 %d）' % (profile, len(task_list), len(new_entries)))

    started = False
    if auto_start:
        log('启动 MAA…')
        try:
            started = launch(d)
            log('MAA 已启动' if started else 'MAA 启动后未检测到进程，请手动打开')
        except Exception as e:
            log('启动失败：%s' % e)

    return {'profile': profile, 'taskCount': len(task_list),
            'added': len(new_entries), 'files': files, 'started': started}


def start_apply(items, profile=None, mode='replace', auto_start=True, maa_dir=None):
    title = '写入 MAA（%d 份作业）' % len(items or [])
    holder = {}

    def go():
        try:
            res = apply_plan(items, profile, mode, auto_start, holder['tid'], maa_dir=maa_dir)
            tasks.finish(holder['tid'], result=res)
        except Exception as e:
            import traceback
            tasks.finish(holder['tid'], error='%s: %s' % (type(e).__name__, e),
                         result={'traceback': traceback.format_exc()})

    import threading
    tid = tasks.create(title, None)
    holder['tid'] = tid
    threading.Thread(target=go, daemon=True).start()
    return tid


def clear_tasklist(profile=None, maa_dir=None):
    cfg = _load_cfg(maa_dir)
    configs = cfg.get('Configurations') or {}
    profile = profile or cfg.get('Current') or ''
    if not profile or profile not in configs:
        raise ValueError('配置不存在：%s' % (profile or '（没有可用配置）'))
    # 与 apply_plan 同一套防护：MAA 运行中改 TaskList 会在它退出时被
    # 内存态全量回写「复活」，必须先关掉且确认关掉了（#53）
    d = _dir(maa_dir)
    mine = running_in(d)
    if mine:
        if not close(d):
            raise RuntimeError('MAA 关闭失败（PID %s），请手动退出 MAA 后重试'
                               % ' / '.join(map(str, mine['pids'])))
        cfg = _load_cfg(maa_dir)   # 退出回写后再读，别拿旧内容覆盖
        configs = cfg.get('Configurations') or {}
        if profile not in configs:
            raise ValueError('配置「%s」在 MAA 退出后不存在了' % profile)
    configs[profile].setdefault('Copilot', {})['TaskList'] = []
    _save_cfg(cfg, maa_dir=maa_dir)
    return True


def tasklist(profile=None, maa_dir=None):
    cfg = _load_cfg(maa_dir)
    profile = profile or cfg.get('Current') or ''
    cop = ((cfg.get('Configurations') or {}).get(profile) or {}).get('Copilot') or {}
    return {'profile': profile, 'items': cop.get('TaskList') or []}
