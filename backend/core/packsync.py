# -*- coding: utf-8 -*-
"""资源包自动更新（客户端侧）

服务器（GitHub Actions）把 arknights.json / paradox.json 和 pack-manifest.json
一起推到数据分支 pack-data，程序启动后读这份 manifest，和自己这份比 fingerprint，
不一样就下载。

三条底线：
  - 只认 manifest 给的 sha256 和字节数，下歪了绝不落盘
  - 条数不能比本地少（半截包/旧包都会把数据清零）
  - 换下来的旧包留 .bak，出事能手动回滚

换包之后两种资源的生效方式不同：
  arknights.json  resources.clear_cache() 之后立即生效
  paradox.json    本来按 mtime 缓存，下次调用自动重读
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import time
import urllib.request

from . import net, packrepo, resources, settings

# 仓库/分支/镜像顺序的契约单点在 packrepo（与 tools/sync_pack.py 共用，#63）
PACK_REPO = packrepo.PACK_REPO
PACK_BRANCH = packrepo.PACK_BRANCH
MANIFEST_URLS = packrepo.mirror_urls('resources/pack-manifest.json')

UA = 'MAA-PRTS/%s'
TIMEOUT_CHECK = 6      # 启动时的检查不能拖住界面
TIMEOUT_DOWNLOAD = 45


def _iso():
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def _get(url, timeout):
    from .version import app_version
    req = urllib.request.Request(url, headers={'User-Agent': UA % app_version()})
    with net.open(req, timeout=timeout) as r:
        return r.read()


def _local_meta(kind):
    """本地这份包的 version / fingerprint / counts"""
    if kind == 'paradox':
        from . import paradox
        d = paradox.table()
        return {
            'version': d.get('gameVersion') or '',
            'fingerprint': d.get('fingerprint') or '',
            'counts': {'count': d.get('count') or len(d.get('chars') or {})},
        }
    p = resources.pack() or {}
    return {
        'version': p.get('version') or '',
        'fingerprint': p.get('fingerprint') or '',
        'counts': p.get('counts') or {},
    }


def fetch_manifest(timeout=TIMEOUT_CHECK):
    """读远端 manifest；三个源依次试，全失败抛 RuntimeError"""
    errors = []
    stamp = int(time.time())
    for base in MANIFEST_URLS:
        url = '%s?t=%d' % (base, stamp)   # 破 CDN 缓存，保证看到刚提交的版本
        try:
            raw = _get(url, timeout)
            d = json.loads(raw.decode('utf-8'))
            if d.get('packs'):
                return d
            errors.append('%s 里没有 packs' % base)
        except Exception as e:  # noqa: BLE001
            errors.append('%s -> %s' % (base.split('/')[2], e))
    raise RuntimeError('读不到资源清单：' + '；'.join(errors))


def status(timeout=TIMEOUT_CHECK):
    """检查有没有可更新的资源（只读，不下载）"""
    out = {
        'ok': False,
        'error': '',
        'checkedAt': _iso(),
        'auto': bool(settings.load().get('autoUpdatePack', True)),
        'source': PACK_BRANCH,
        'current': {},
        'remote': {},
        'available': [],
        'notes': [],
    }
    try:
        man = fetch_manifest(timeout)
    except Exception as e:  # noqa: BLE001 - 没网就静默用旧包
        out['error'] = str(e)
        return out

    out['ok'] = True
    out['generatedAt'] = man.get('generatedAt') or ''
    out['gameVersion'] = (man.get('source') or {}).get('gameVersion') or ''
    out['upstreamCommit'] = (man.get('source') or {}).get('commit') or ''
    for kind in ('arknights', 'paradox'):
        remote = (man.get('packs') or {}).get(kind) or {}
        if not remote:
            continue
        local = _local_meta(kind)
        out['current'][kind] = local
        out['remote'][kind] = {
            'version': remote.get('version') or '',
            'fingerprint': remote.get('fingerprint') or '',
            'size': remote.get('size') or 0,
            'counts': remote.get('counts') or {},
        }
        if remote.get('fingerprint') and remote['fingerprint'] != local['fingerprint']:
            out['available'].append(kind)
    return out


def _verify(kind, raw, meta):
    """下载完整性 + 数据合理性校验，任何一条不过就拒绝落盘"""
    size = meta.get('size') or 0
    if size and len(raw) != size:
        raise RuntimeError('%s 大小不对：%d != %d' % (kind, len(raw), size))
    digest = meta.get('sha256') or ''
    if digest:
        got = hashlib.sha256(raw).hexdigest()
        if got != digest:
            raise RuntimeError('%s 校验失败：%s != %s' % (kind, got[:12], digest[:12]))
    try:
        data = json.loads(raw.decode('utf-8'))
    except Exception as e:
        raise RuntimeError('%s 不是合法 json：%s' % (kind, e))

    local = _local_meta(kind)['counts'] or {}
    if kind == 'paradox':
        got = data.get('count') or len(data.get('chars') or {})
        if not got:
            raise RuntimeError('悖论清单为空')
        if local.get('count') and got < local['count']:
            raise RuntimeError('悖论条数变少：%s → %s' % (local['count'], got))
        return data

    counts = data.get('counts') or {}
    for key in ('chars', 'stages'):
        if not counts.get(key):
            raise RuntimeError('资源包缺 %s' % key)
    lost = [k for k, v in counts.items() if k in local and v < (local[k] or 0)]
    if lost:
        raise RuntimeError('这些字段比本地少，拒绝覆盖：%s' % '、'.join(lost))
    return data


def _target_path(kind):
    return settings.paradox_path() if kind == 'paradox' else settings.pack_path()


def _install(kind, raw, meta):
    """原子替换 + 留 .bak；返回落盘路径"""
    name = 'paradox.json' if kind == 'paradox' else 'arknights.json'
    dest_dir = os.path.join(settings.project_root(), 'resources')
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, name)

    old = _target_path(kind)
    if os.path.isfile(old):
        try:
            with open(old, 'rb') as f:
                keep = f.read()
            with open(dest + '.bak', 'wb') as f:
                f.write(keep)
        except Exception:
            pass

    tmp = dest + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, dest)
    return dest


def can_apply():
    """源码模式默认不动仓库文件（改一行就 dirty 了），要测请设环境变量"""
    if settings.FROZEN:
        return True, ''
    if os.environ.get('MAA_PRTS_PACK_FORCE'):
        return True, ''
    return False, '源码模式不自动覆盖 resources/（设 MAA_PRTS_PACK_FORCE=1 可强制）'


def apply(timeout=TIMEOUT_DOWNLOAD, only=None):
    """下载并安装有更新的资源包

    返回 {ok, applied:[...], skipped:[...], error, remote:{...}}
    """
    ok, why = can_apply()
    if not ok:
        return {'ok': False, 'applied': [], 'skipped': [], 'error': why}

    st = status()
    if not st['ok']:
        return {'ok': False, 'applied': [], 'skipped': [], 'error': st['error']}
    keys = [k for k in st['available'] if not only or k in only]
    if not keys:
        return {'ok': True, 'applied': [], 'skipped': [], 'error': '',
                'remote': st.get('remote') or {}}

    man = fetch_manifest(timeout)
    applied, skipped = [], []
    for kind in keys:
        meta = (man.get('packs') or {}).get(kind) or {}
        raw, errors = None, []
        for url in (meta.get('urls') or []):
            try:
                raw = _get(url, timeout)
                break
            except Exception as e:  # noqa: BLE001
                errors.append('%s -> %s' % (url.split('/')[2], e))
        if raw is None:
            skipped.append({'pack': kind, 'reason': '下载失败：' + '；'.join(errors)})
            continue
        try:
            _verify(kind, raw, meta)
            _install(kind, raw, meta)
            applied.append({'pack': kind, 'version': meta.get('version') or '',
                            'fingerprint': meta.get('fingerprint') or ''})
        except Exception as e:  # noqa: BLE001
            skipped.append({'pack': kind, 'reason': str(e)})

    if any(a['pack'] == 'arknights' for a in applied):
        resources.clear_cache()
    return {
        'ok': bool(applied),
        'applied': applied,
        'skipped': skipped,
        'error': '' if applied else '没有更新成功',
        'remote': st.get('remote') or {},
        'at': _iso(),
    }
