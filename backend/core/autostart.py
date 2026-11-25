# -*- coding: utf-8 -*-
"""开机自启（Windows 的 HKCU Run 项，默认关闭）

**纪律（用户 2026-11-25 原话）**：「我极度厌恶没有在我同意情况下默认开机
自启的软件」。所以这里只做三件事：

  - ``status()`` 只读，任何时候都能问现在是不是开着；
  - ``enable()`` / ``disable()`` 只在用户点了设置里的开关时调用；
  - ``sync(enabled)`` 启动时对一次账——**只有设置里是 True 才写**，是 False 就
    顺手删掉（用户手动取消勾选后，注册表里残留的那条也要清掉）。

不碰 HKLM，不写计划任务，不建启动文件夹快捷方式：只动当前用户这一条值，
删起来干净，用户自己在「任务管理器 → 启动」里也能看到它。

源码模式（``python tools/dev.py``）没有意义：自启一个 python.exe 不会带参数，
所以 ``supported`` 为 False，前端把开关置灰并说明「打包版才有」。
"""
from __future__ import annotations

import os
import sys

from . import settings

RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
VALUE_NAME = 'MAA-PRTS'


def _is_windows() -> bool:
    return os.name == 'nt'


def exe_path() -> str:
    """自启要拉起来的那个程序（打包版 = exe 自己；源码模式 = 空）"""
    if not getattr(sys, 'frozen', False):
        return ''
    p = os.path.abspath(sys.executable)
    return p if os.path.isfile(p) else ''


def supported() -> bool:
    return _is_windows() and bool(exe_path())


def _open_key(write=False):
    import winreg
    flags = winreg.KEY_READ | (winreg.KEY_WRITE if write else 0)
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, flags)


def _read() -> str:
    """注册表里那条命令（没有就返回空串）"""
    if not _is_windows():
        return ''
    import winreg
    try:
        with _open_key() as k:
            val, _typ = winreg.QueryValueEx(k, VALUE_NAME)
            return str(val or '')
    except FileNotFoundError:
        return ''
    except OSError:
        return ''


def status() -> dict:
    """给设置页看的现状：支不支持 / 注册表里有没有 / 指向哪个程序"""
    cmd = _read()
    return {
        'supported': supported(),
        'enabled': bool(cmd),
        'target': cmd,
        'exe': exe_path(),
        # 设置里用户勾的那一栏（正常应该与 enabled 一致，不一致说明被外部改过）
        'setting': bool(settings.load().get('launchAtLogin')),
    }


def enable() -> dict:
    """写自启项（**只应由用户勾选触发**）"""
    exe = exe_path()
    if not supported():
        raise RuntimeError('源码运行没有开机自启；请用打包版（MAA-PRTS.exe）')
    import winreg
    cmd = '"%s"' % exe
    with _open_key(write=True) as k:
        winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ, cmd)
    return {'ok': True, 'enabled': True, 'target': cmd}


def disable() -> dict:
    """删自启项（幂等：本来就没有也算成功）"""
    if not _is_windows():
        return {'ok': True, 'enabled': False}
    import winreg
    try:
        with _open_key(write=True) as k:
            winreg.DeleteValue(k, VALUE_NAME)
    except FileNotFoundError:
        pass
    except OSError as e:
        # 权限问题要如实报，不然用户以为关了其实还开着
        raise RuntimeError('删不掉自启项：%s' % e)
    return {'ok': True, 'enabled': False}


def apply(enabled: bool) -> dict:
    """按开关应用：True → 写；False → 删"""
    if enabled:
        return enable()
    return disable()


def sync(enabled: bool) -> dict:
    """启动时对账（不抛异常，避免开机自启这种小事把启动搞挂）

    - 设置里 False：把残留的值删掉（用户可能在外面手动加过，或旧版本写过）
    - 设置里 True：打包版就写/刷新一次（exe 换过路径也能自动修好）；
      源码模式忽略
    """
    try:
        # 源码模式一律不碰注册表：开发时跑 dev.py 不能把用户打包版的自启设置
        # 顺手删掉（那是真实生效的那一条）
        if not getattr(sys, 'frozen', False):
            return {'ok': True, 'enabled': bool(_read()), 'skipped': True,
                    'why': '源码运行不改开机自启'}
        if not enabled:
            if _read():
                return disable()
            return {'ok': True, 'enabled': False, 'skipped': True}
        if not supported():
            return {'ok': True, 'enabled': bool(_read()), 'skipped': True,
                    'why': '找不到 exe 路径'}
        return enable()
    except Exception as e:                      # noqa: BLE001 启动流程不许炸
        return {'ok': False, 'error': str(e)}
