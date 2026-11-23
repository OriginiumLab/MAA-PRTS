# -*- coding: utf-8 -*-
"""多开子进程入口：一台模拟器一个进程，跑完一组就退出

为什么要子进程而不是线程，见 :mod:`backend.core.multiproc` 的模块注释
（同进程两个 MaaCore 会抢命名管道，err 231）。

payload 两种 kind：

  - ``skill``：``skills.run_batch`` 的一组账号（steps 是 skill 列表）
  - ``plan``：``maaplan.run_plan`` 的一组账号（spec 是一整套任务参数）
"""
from __future__ import annotations

import json
import os
import sys
import threading
import traceback


def main(payload_path):
    """子进程入口：跑一组 → 写结果文件。返回进程退出码。"""
    # 打包后的 exe 是 windowed 模式，stdout 可能是无效句柄；日志一律走文件，
    # 这里只保证 print 不会因为编码炸掉
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

    from . import multiproc, tasks

    with open(payload_path, 'r', encoding='utf-8') as fh:
        payload = json.load(fh)
    kind = payload.get('kind') or 'skill'
    g = payload.get('group') or {}
    name = '多开子进程（%s）' % (g.get('address') or '默认模拟器')
    # 这一组只有一个号时，给它所有日志统一挂上 [昵称]：两组日志会被父进程
    # tail 到同一个任务里，没前缀的行前端只能当公共行，官服 tab 就会混进
    # B 服那台的进度。挂在 tasks 的任务级 prefix 上，MAA / maaplan 直接调
    # log_factory 写的那些行（「MaaCore 已加载」等）也能一并带上。
    tag = payload.get('tag') or multiproc.group_tag(g)
    wtid = tasks.create(name, log_path=payload.get('log') or None,
                        prefix=('[%s]' % tag) if tag else '')
    log = tasks.log_factory(wtid)
    results, report = {}, []
    lock = threading.Lock()
    code = 0
    try:
        if kind == 'plan':
            from . import maaplan
            report = maaplan._run_group(g, payload.get('spec') or {},
                                        wtid, log) or []
        elif kind == 'one':
            # 单个 skill（run_skill）：本进程已经有一路在跑 MaaCore 时，
            # 那一路被搬到这里跑（同一进程两个实例会互相打断）
            from . import skills
            got = skills._skill_body(payload.get('skill') or '',
                                     payload.get('params') or {}, log, wtid)
            files = [f for f in (g.get('files') or []) if f]
            if files:
                results[files[0]] = (got if isinstance(got, dict)
                                     else {'ok': True})
        else:
            from . import skills
            steps = [(s[0], s[1] if len(s) > 1 else {})
                     for s in (payload.get('steps') or []) if s]
            skills._run_group(g, steps, payload.get('names') or {},
                              log, results, lock, tid=wtid)
    except Exception as e:                       # noqa: BLE001 子进程不能静默死
        log('这一组出错：%s: %s' % (type(e).__name__, e))
        log(traceback.format_exc())
        files = (g.get('files') or [r.get('file') for r in (g.get('rows') or [])])
        for f in files:
            if f:
                results.setdefault(f, {'ok': False, 'name': f,
                                       'error': '%s: %s' % (type(e).__name__, e)})
        code = 1
    finally:
        try:
            res_path = payload.get('result')
            if res_path:
                tmp = res_path + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as fh:
                    json.dump({'results': results, 'report': report}, fh,
                              ensure_ascii=False)
                os.replace(tmp, res_path)
        except Exception:
            code = 1
        tasks.finish(wtid)
        try:
            fh = (tasks._tasks.get(wtid) or {}).get('_fh')
            if fh:
                fh.flush()
        except Exception:
            pass
    return code
