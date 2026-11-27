# -*- coding: utf-8 -*-
"""干员头像：用到哪张下哪张，安装包不带图

为什么不在包里：350 个干员头像压到 96px WebP 约 1.8MB，全打进安装包等于
给每个用户塞一份他只看得见几十张的图。这里按需下载、落到
``config/cache/avatar/``——`install.ps1` 的 ``/XD`` 本来就排除 ``config/``，
更新程序不会覆盖；清缓存也只是删这一层目录。

来源顺序（前一个失败才走下一个）：

  1. 本机缓存      ``config/cache/avatar/<id>.webp|png``，命中直接返回
  2. 数据分支      pack-data 的 ``resources/avatars/<id>.webp``（raw → gh-proxy → jsDelivr）
  3. PRTS 镜像     ``torappu.prts.wiki/assets/char_avatar/<id>.png``，现场转 WebP

第 2 条是主路（我们自己的包，可校验），第 3 条只兜「数据分支还没同步到这张」。
两条都挂就抛 ``KeyError``，前端回落到首字母圆角块（``.avatar``），不影响阅读。

校验刻意做得简单：不认 sha 表（那要额外拉一份 index），只认「真的是图片」——
文件头 + 大小区间，防的是 CDN 返回一段 HTML 错误页被当成图存下来。
"""
from __future__ import annotations

import io
import json
import os
import re
import threading
import time
import urllib.request

from . import net, packrepo, settings

_ID_RE = re.compile(r'^[A-Za-z0-9_]{1,64}$')
TIMEOUT_MIRROR = 4        # 自己那三个镜像，挂了就换
TIMEOUT_FALLBACK = 8      # PRTS 镜像，慢一点但一般能等到
MIN_BYTES = 512
MAX_BYTES = 400 * 1024
NEG_TTL = 300             # 下不到的 id 记 5 分钟，别让列表页每次刷新都重打一遍

_locks = {}
_guard = threading.Lock()
_neg = {}                 # cid -> 到期时间戳（只在内存里）
_index = {'at': 0.0, 'map': None}
INDEX_TTL = 3600


def cache_dir() -> str:
    return os.path.join(settings.CONFIG_DIR, 'cache', 'avatar')


def _safe(cid: str) -> str:
    cid = str(cid or '').strip()
    if not _ID_RE.match(cid):
        raise KeyError('头像 id 不合法')
    return cid


def _path_of(cid: str) -> tuple:
    """本地已有的缓存文件：(路径, 扩展名)；没有返回 ('', '')"""
    base = os.path.join(cache_dir(), cid)
    for ext in ('webp', 'png'):
        p = base + '.' + ext
        if os.path.isfile(p) and os.path.getsize(p) >= MIN_BYTES:
            return p, ext
    return '', ''


def _kind(data: bytes) -> str:
    """认文件头：webp / png / 其它（空字符串）"""
    if len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'webp'
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    return ''


def _ok(data: bytes) -> str:
    if not data or len(data) < MIN_BYTES or len(data) > MAX_BYTES:
        return ''
    return _kind(data)


def _get(url: str, timeout: float) -> bytes:
    from .version import app_version
    req = urllib.request.Request(url, headers={
        'User-Agent': 'MAA-PRTS/%s' % app_version(),
        'Accept': 'image/webp,image/png,*/*',
    })
    with net.open(req, timeout=timeout) as r:
        return r.read(MAX_BYTES + 1)


def _to_webp(raw: bytes) -> bytes:
    """PNG → 96×96 WebP；Pillow 缺 webp 支持时返回空字节（调用方存原图）"""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw)).convert('RGBA')
        im = im.resize((96, 96), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, 'WEBP', quality=80, method=4)
        data = out.getvalue()
        return data if _kind(data) == 'webp' else b''
    except Exception:
        return b''


def _save(cid: str, data: bytes, ext: str) -> None:
    d = cache_dir()
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, '%s.%s' % (cid, ext))
    tmp = dst + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, dst)


def avatar_index() -> dict:
    """pack-data 上的头像索引：{charId: {sha256, bytes, name}}

    拿它只为判断「这张到底有没有成品」：有才去试三个镜像，没有就直接走
    PRTS 兜底，省掉三次必然 404 / 超时的往返（实测能省 5 秒）。拉不到就
    返回空表（1 小时后再试），此时全部走兜底，功能照常。
    """
    if _index['map'] is not None and time.time() - _index['at'] < INDEX_TTL:
        return _index['map']
    got = {}
    for url in packrepo.mirror_urls('resources/avatars/avatar-index.json'):
        try:
            d = json.loads(_get(url, TIMEOUT_MIRROR).decode('utf-8'))
            got = d.get('chars') or {}
            break
        except Exception:
            continue
    _index['map'] = got
    _index['at'] = time.time()
    return got


def _from_mirrors(cid: str):
    """第 2 条：数据分支上的成品 WebP"""
    rel = 'resources/avatars/%s.webp' % cid
    for url in packrepo.mirror_urls(rel):
        try:
            data = _get(url, TIMEOUT_MIRROR)
            if _ok(data) == 'webp':
                return data, 'webp'
        except Exception:
            continue
    return None


def _from_prts(cid: str):
    """第 3 条：PRTS 镜像上的原图，现场压成 96px WebP"""
    url = 'https://torappu.prts.wiki/assets/char_avatar/%s.png' % cid
    try:
        raw = _get(url, TIMEOUT_FALLBACK)
    except Exception:
        return None
    if _ok(raw) != 'png':
        return None
    webp = _to_webp(raw)
    if webp:
        return webp, 'webp'
    return raw, 'png'          # 转不了就存原图，别为了小 10 倍丢功能


def fetch(cid: str) -> tuple:
    """取一张头像：返回 (字节, 扩展名)。哪都拿不到就抛 KeyError

    同一个 id 的并发请求只下一次：列表页一屏会同时发十几个 img 请求。
    """
    cid = _safe(cid)
    p, ext = _path_of(cid)
    if p:
        return _read(p), ext

    with _guard:
        lock = _locks.setdefault(cid, threading.Lock())

    acquired = lock.acquire(timeout=20)
    try:
        p, ext = _path_of(cid)      # 等锁期间别人可能已经下好了
        if p:
            return _read(p), ext
        until = _neg.get(cid) or 0
        if until > time.time():
            raise KeyError('头像暂时拿不到')
        got = None
        if cid in avatar_index():
            got = _from_mirrors(cid)
        got = got or _from_prts(cid)
        if not got:
            _neg[cid] = time.time() + NEG_TTL
            raise KeyError('头像拿不到')
        data, ext = got
        try:
            _save(cid, data, ext)
        except OSError:
            pass                     # 落盘失败也先把这张发出去
        _neg.pop(cid, None)
        return data, ext
    finally:
        if acquired:
            lock.release()


def _read(path: str) -> bytes:
    with open(path, 'rb') as f:
        return f.read()


def stats() -> dict:
    """缓存占用（设置页 / 以后做「清理头像缓存」用）"""
    d = cache_dir()
    files, size = 0, 0
    try:
        for name in os.listdir(d):
            if name.endswith('.tmp'):
                continue
            p = os.path.join(d, name)
            if os.path.isfile(p):
                files += 1
                size += os.path.getsize(p)
    except OSError:
        pass
    return {'dir': d, 'files': files, 'bytes': size}


def clear() -> dict:
    """清空缓存；返回删掉的数量（下次用到会重新下）"""
    d = cache_dir()
    n = 0
    try:
        for name in os.listdir(d):
            p = os.path.join(d, name)
            try:
                if os.path.isfile(p):
                    os.remove(p)
                    n += 1
            except OSError:
                pass
    except OSError:
        pass
    with _guard:
        _neg.clear()
    return {'removed': n}
