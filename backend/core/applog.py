# -*- coding: utf-8 -*-
"""debug/app.log 的 core 侧写入端。

main.log() 一直能写这个文件，但 core 模块不能 import main（main → core
单向，见 appupdate 的退出注册），于是同步 / 签到 / 抽卡这些后台链路出
错时只有界面任务日志（内存态，重启即没），app.log 里什么都看不到——
排障经常拿不到现场。本模块给 core 一个独立的文件写入端：与 main.log
写同一个目标，时间戳前缀、2MB 轮转一致。写失败静默：日志不能拖垮业务。
"""
from __future__ import annotations

import os
import threading
import time

from . import settings

_lock = threading.Lock()
_MAX_LINE = 4000        # 单行上限：响应体再长也截，防止一条日志撑爆轮转


def write(line: str):
    """已带时间戳的原始行落盘（main.log 复用同一实现，避免双写端轮转竞争）。"""
    try:
        with _lock:
            path = settings.LOG_PATH
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if os.path.isfile(path) and os.path.getsize(path) > 2 * 1024 * 1024:
                os.replace(path, path + '.1')
            with open(path, 'a', encoding='utf-8') as f:
                f.write(str(line).rstrip('\n')[:_MAX_LINE] + '\n')
    except Exception:
        pass


def log(msg: str):
    """带时间戳写一行（core 模块用这个）。"""
    write('[%s] %s' % (time.strftime('%H:%M:%S'), msg))
