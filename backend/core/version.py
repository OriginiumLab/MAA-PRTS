# -*- coding: utf-8 -*-
"""版本管理：程序版本 + 内置游戏资源包版本

资源包 resources/arknights.json 由 make_pack.py 从游戏数据生成，每次生成会写入：

    version      生成日期版本号，形如 2026.11.05
    fingerprint  内容指纹（8 位），同一天多次生成也能区分
    counts       干员 / 关卡 / 模组等条数

version.check() 拿它和 config/gamedata.json 里记下的「上次见到的版本」比对：

    没有记录（首次运行） → 静默记下当前版本，不打扰用户
    完全一致            → 什么都不做
    不一致              → 记一条 pending，前端在页面顶部提示「资源已更新」，
                          并列出新增的干员 / 关卡数和新的活动；
                          用户点「知道了」才清掉（不点就每次启动都提醒）

这样我们发新资源包时，用户换上新的 arknights.json（或安装新的程序版本），
下次打开就能看到「新活动已经置顶到快捷栏」的提示。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import time

from . import settings

STATE_PATH = os.path.join(settings.CONFIG_DIR, 'gamedata.json')

_lock = threading.RLock()
_cache = [None]


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def _read_json(path, default=None):
    try:
        with io.open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, data):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp = path + '.tmp'
    with io.open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')
    os.replace(tmp, path)


# ---------------------------------------------------------------- 程序版本
# 版本号不在代码里维护：发版 CI 按 PR 标题的版号把 version.json 写进
# 发布包根目录（与 exe 同级），本地源码运行和本地打包都没有这个文件，
# 统一是 dev 版。
_app_version_cache = [None]


def app_version() -> str:
    if _app_version_cache[0] is None:
        data = _read_json(os.path.join(settings.ROOT, 'version.json'), None)
        _app_version_cache[0] = str((data or {}).get('version') or '').strip() or 'dev'
    return _app_version_cache[0]


# ---------------------------------------------------------------- 资源包版本
def pack_meta() -> dict:
    """读取内置资源包的版本信息（缺字段时用生成日期 / 内容指纹兜底）"""
    from . import resources

    p = resources.pack() or {}
    counts = p.get('counts') or {}
    ver = str(p.get('version') or '').strip()
    if not ver:
        # 老资源包没有 version 字段：退化成「生成日期」
        gen = str(p.get('generatedAt') or '')
        ver = gen[:10].replace('-', '.') if len(gen) >= 10 else '0.0.0'
    fp = str(p.get('fingerprint') or '').strip()
    if not fp:
        raw = json.dumps([ver, counts, p.get('generatedAt')], sort_keys=True)
        fp = hashlib.sha1(raw.encode('utf-8')).hexdigest()[:8]
    return {
        'version': ver,
        'fingerprint': fp,
        'generatedAt': p.get('generatedAt'),
        'counts': counts,
        'path': settings.pack_path(),
    }


# ---------------------------------------------------------------- 本地记录
def _state() -> dict:
    with _lock:
        if _cache[0] is None:
            _cache[0] = _read_json(STATE_PATH, None) or {}
        return _cache[0]


def _save_state(st: dict):
    with _lock:
        _cache[0] = st
        try:
            _write_json(STATE_PATH, st)
        except Exception:
            pass


def check(activities=None) -> dict:
    """比对资源包版本，返回给前端的版本信息

    activities 传 resources.activity_list() 的结果，用来算出「哪些活动是新的」。
    内部会取一份全量活动列表做比对：调用方通常只传快捷栏那一小截（18 条），
    直接拿去比会把排序变化后的老活动误判成新活动。
    """
    meta = pack_meta()
    acts = []
    try:
        from . import resources
        acts = [a for a in resources.activity_list(400) if a.get('id')]
    except Exception:
        acts = []
    if not acts:
        acts = [a for a in (activities or []) if a.get('id')]
    st = _state()

    # 首次运行：只记录，不提示
    if not st:
        _save_state({
            'version': meta['version'],
            'fingerprint': meta['fingerprint'],
            'counts': meta['counts'],
            'acts': [a['id'] for a in acts],
            'seenAt': _now(),
            'pending': None,
        })
        return {
            'app': app_version(),
            'version': meta['version'],
            'fingerprint': meta['fingerprint'],
            'generatedAt': meta['generatedAt'],
            'counts': meta['counts'],
            'fresh': True,
            'updated': False,
            'pending': None,
        }

    changed = (st.get('fingerprint') != meta['fingerprint']
               or st.get('version') != meta['version'])

    if changed:
        old_counts = st.get('counts') or {}
        known = set(st.get('acts') or [])
        # 只有「最近一个半月开的活动」才标 NEW：老活动没在旧记录里出现，
        # 往往只是上次只存了快捷栏那一小截，不是真的新增
        recent = time.time() - 45 * 86400
        new_acts = [{
            'id': a['id'],
            'name': a.get('name') or a['id'],
            'prefix': a.get('prefix') or '',
            'count': a.get('count') or 0,
        } for a in acts
            if a['id'] not in known
            and (not a.get('start') or a['start'] >= recent)]
        delta = {}
        for k, v in (meta['counts'] or {}).items():
            d = int(v or 0) - int(old_counts.get(k) or 0)
            if d:
                delta[k] = d
        pending = {
            'from': st.get('version') or '',
            'to': meta['version'],
            'delta': delta,
            'newActs': new_acts,
            'at': _now(),
            'generatedAt': meta.get('generatedAt'),
        }
        _save_state({
            'version': meta['version'],
            'fingerprint': meta['fingerprint'],
            'counts': meta['counts'],
            'acts': [a['id'] for a in acts],
            'seenAt': _now(),
            'pending': pending,
        })
        return {
            'app': app_version(),
            'version': meta['version'],
            'fingerprint': meta['fingerprint'],
            'generatedAt': meta['generatedAt'],
            'counts': meta['counts'],
            'fresh': False,
            'updated': True,
            'pending': pending,
        }

    pending = st.get('pending')
    return {
        'app': app_version(),
        'version': meta['version'],
        'fingerprint': meta['fingerprint'],
        'generatedAt': meta['generatedAt'],
        'counts': meta['counts'],
        'fresh': False,
        'updated': bool(pending),
        'pending': pending,
    }


def ack(activities=None) -> dict:
    """用户点「知道了」：清掉待提示的更新"""
    st = _state()
    if st:
        st['pending'] = None
        _save_state(st)
    return check(activities)
