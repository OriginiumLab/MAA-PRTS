# -*- coding: utf-8 -*-
"""账号 json 的原子落盘：唯一临时名 + 进程内写锁（#47）

服务端是多线程 HTTP，后台同步线程又会写同一批账号文件。所有写路径
共用这里：

- tmp 文件名带线程号：两个写者永远不会打开同一个 .tmp（后者 'w' 截断
  前者半截内容、交错写、把残缺文件 os.replace 发布成正式 json）；
- 全局 RLock 串行发布：读-改-写（save_pref / paradox.mark / MAA 导入
  合并）拿同一把锁（lock()）把「读→改→写」整段圈起来，丢更新也随之消除。

锁是进程内的——同一份 config/ 被两个程序实例同时开不在防护范围
（settings.py 的 _lock 同理），这种用法本来也不支持。
"""
from __future__ import annotations

import io
import json
import os
import threading

_lock = threading.RLock()


def lock():
    """账号文件读-改-写段的互斥锁（RLock，可重入）"""
    return _lock


def dump_json(path, obj):
    """json 原子写：tmp + os.replace。与 updater.load_json 配对。"""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = '%s.%d.tmp' % (path, threading.get_ident())
    with _lock:
        with io.open(tmp, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.write('\n')
        os.replace(tmp, path)
