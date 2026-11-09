# -*- coding: utf-8 -*-
"""Windows DPAPI 加解密：token 只能在本机当前 Windows 用户下解开

用的是 Windows 自带的数据保护 API（crypt32.dll 的 CryptProtectData /
CryptUnprotectData，CurrentUser 作用域）：

- 密钥由 Windows 按当前用户 + 本机派生，程序自己不存密钥、也不碰密钥；
- 加密结果拷到别的电脑、或同一台电脑换用户登录，都解不开；
- 不引入新依赖（ctypes 直调），打包进 exe 也能用。

非 Windows 平台直接抛错——本程序本来就只跑在 Windows（pywebview + WebView2）；
DLL 懒加载到首次加解密时，非 Windows 上 import 本模块本身不会崩（冒烟的
A 组要在任意平台 import 全部 .py），平台检查也在那里真正生效。
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

_CRYPTPROTECT_UI_FORBIDDEN = 0x01

# use_last_error=True 才能拿到可靠的 GetLastError（裸 windll 会被 ctypes 内部调用覆盖）
_crypt32 = None
_kernel32 = None


def _libs():
    global _crypt32, _kernel32
    if _crypt32 is None:
        if sys.platform != 'win32':
            raise OSError('DPAPI 仅在 Windows 上可用')
        _crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
        _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    return _crypt32, _kernel32


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD),
                ('pbData', ctypes.POINTER(ctypes.c_char))]


def _dpapi(func_name, data: bytes) -> bytes:
    crypt32, kernel32 = _libs()
    func = getattr(crypt32, func_name)
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    ok = func(ctypes.byref(blob_in), None, None, None, None,
              _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        raise OSError('DPAPI 调用失败（错误码 %d）' % ctypes.get_last_error())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def protect(plain: bytes) -> bytes:
    """加密：密文只能在同一台机器的同一个 Windows 用户下解开"""
    return _dpapi('CryptProtectData', plain)


def unprotect(blob: bytes) -> bytes:
    """解密：换机器 / 换用户会失败（这正是设计目的）"""
    return _dpapi('CryptUnprotectData', blob)
