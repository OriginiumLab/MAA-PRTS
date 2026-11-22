# -*- coding: utf-8 -*-
"""多开分组执行：一组账号一个独立进程

**为什么必需**（2026-11-22 真机实测）：同一个进程里同时建两个 MaaCore
实例时，第二个一定连不上模拟器，asst.log 里是

    [ERR] CreateOverlappablePipe failed, err 231
    [WRN] Call `adb.exe devices` failed
    [INF] ConnectionInfo ... "why":"Cannot connect: address appears to be
          serial number but device not found"

err 231 = Windows「所有管道实例都忙」。MaaCore 调 adb 时用命名管道收输出，
两个实例撞同一个管道名，后到的直接失败。跟档案、地址、adb 路径都无关
（5556 单独跑完全正常），所以「多开」只能一进程一台模拟器。

用法：父进程把这一组的参数写成 payload JSON，起
``MAA-PRTS --skill-worker <payload>``（源码模式是 ``python backend/main.py
--skill-worker ...``），然后 tail 子进程的日志文件转发进任务日志，最后读回
结果 JSON。跑完删 payload / 结果文件，日志文件留着排障。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

from . import settings

# 正在跑的子进程：``(tid, idx) → {'proc', 'files', 'tag'}``。
# 「停止」要能只停某一路（用户 2026-11-23：「停止的话不能指定停止，会一起
# 停其他任务」）：多开时一台模拟器一个子进程，光 AsstStop 是够不着子进程里
# 那个 MaaCore 的，只能按账号把那一路的进程杀掉。
_RUNNING = {}
_STOPPED = set()
_RUN_LOCK = threading.Lock()


def _files_of(payload):
    """这一组负责哪些账号（group.files 优先，其次 rows[].file）"""
    g = payload.get('group') or {}
    files = [str(x) for x in (g.get('files') or []) if x]
    if not files:
        files = [str(r.get('file')) for r in (g.get('rows') or [])
                 if r.get('file')]
    return files


def stop_groups(owners=None):
    """停掉正在跑的子进程（``owners`` 空 = 全停）→ 停掉的组数

    只 kill 点名的那些组，别的组继续跑。被杀的那一路不会写回结果文件，
    调用方按「手动停止」记账（见 :func:`run_group` 的第三个返回值）。
    """
    with _RUN_LOCK:
        items = list(_RUNNING.items())
    n = 0
    from . import maacore
    for key, ent in items:
        with _RUN_LOCK:
            if key in _STOPPED:                  # 已经发过 kill，别重复计数
                continue
        if owners and not maacore.owner_match_any(ent.get('files') or [], owners):
            continue
        with _RUN_LOCK:
            _STOPPED.add(key)
        try:
            ent['proc'].kill()
            n += 1
        except Exception:                        # noqa: BLE001 已退出也算停掉
            pass
    return n


def group_tag(group):
    """这一组日志统一挂到哪个账号名下 → 昵称（一组只有一个号时）

    两组的日志会被父进程 tail 到**同一个**任务里，交错在一起。没有
    ``[账号]`` 前缀的行（MaaCore 的「模拟器已连接」「开始执行」等）前端
    只能当公共行，于是官服 tab 里也能看见 B 服那台的进度。这里在子进程
    写日志时统一补齐前缀，落盘即规范，父进程不用再猜。

    一组多号时不硬塞：``_run_group`` 本来就给每个号的行加了 ``[昵称]``，
    塞一个反而会把别的号的行记错人。
    """
    files = [str(x) for x in (group.get('files') or []) if x]
    if not files:
        files = [str(r.get('file')) for r in (group.get('rows') or [])
                 if r.get('file')]
    if len(files) != 1:
        return ''
    f = files[0]
    try:
        from . import accounts
        snap = accounts.get_account(f)
        a = snap.get('account') or {}
        return str(a.get('nickName') or a.get('alias')
                   or snap.get('alias') or f)
    except Exception:                            # noqa: BLE001 查不到就退回文件名
        return f


def worker_cmd(payload_path):
    """子进程启动命令（源码模式跑 backend/main.py，打包后跑 exe 自己）"""
    exe = sys.executable
    if getattr(sys, 'frozen', False):
        return [exe, '--skill-worker', payload_path]
    main_py = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'main.py')
    return [exe, main_py, '--skill-worker', payload_path]


def tail_lines(path, pos, log):
    """把日志文件里 pos 之后的新行转发进任务日志 → 新 pos"""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as fh:
            fh.seek(pos)
            for line in fh:
                text = line.rstrip('\n')
                if not text or text.startswith('# '):
                    continue
                log(text)
            return fh.tell()
    except FileNotFoundError:
        return pos
    except Exception:                            # noqa: BLE001 读日志失败别拖垮任务
        return pos


def run_group(kind, tid, idx, payload, log, who='默认模拟器'):
    """在独立进程里跑一组 → ``(results, report, stopped)``

    ``kind`` 是 worker 那边的分发键（``skill`` = skills._run_group，
    ``plan`` = maaplan._run_group）。子进程起不来 / 中途崩了返回
    ``({}, [], False)``，调用方按自己的口径记失败。

    ``stopped`` 为 True 表示这一组是**用户点名停掉的**（:func:`stop_groups`
    杀了子进程），此时结果为空但不算失败——不能给它记一条「子进程异常」。
    """
    base = os.path.join(settings.DEBUG_DIR, 'tasks', '%s-g%d' % (tid, idx))
    logp, resp, payp = base + '.log', base + '.json', base + '.payload'
    body = dict(payload)
    body.update({'kind': kind, 'log': logp, 'result': resp,
                 'tag': payload.get('tag') or group_tag(body.get('group') or {})})
    key = (tid, idx)
    for p in (logp, resp):
        try:
            os.remove(p)
        except OSError:
            pass
    try:
        with open(payp, 'w', encoding='utf-8') as fh:
            json.dump(body, fh, ensure_ascii=False)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        proc = subprocess.Popen(worker_cmd(payp), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                creationflags=flags)
    except Exception as e:                       # noqa: BLE001 起不来要留痕
        log('[组 %s] 起子进程失败：%s: %s（这一组没跑）'
            % (who, type(e).__name__, e))
        return {}, [], False

    with _RUN_LOCK:
        _RUNNING[key] = {'proc': proc, 'files': _files_of(body), 'tag': who}

    pos = 0
    try:
        while proc.poll() is None:
            pos = tail_lines(logp, pos, log)
            time.sleep(0.35)
        pos = tail_lines(logp, pos, log)
    finally:
        with _RUN_LOCK:
            _RUNNING.pop(key, None)
    with _RUN_LOCK:
        stopped = key in _STOPPED
        _STOPPED.discard(key)

    if stopped:
        log('[组 %s] 已按你的要求停下（这一组，别的组还在跑）' % who)
        try:
            os.remove(payp)
        except OSError:
            pass
        return {}, [], True

    got = {}
    try:
        with open(resp, 'r', encoding='utf-8') as fh:
            got = json.load(fh) or {}
    except Exception:
        got = {}
    if not got:
        log('[组 %s] 子进程没交回结果（退出码 %s），这一组按失败算'
            % (who, proc.returncode))
    try:
        os.remove(payp)
        os.remove(resp)
    except OSError:
        pass
    return got.get('results') or {}, got.get('report') or [], False
