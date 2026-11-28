# -*- coding: utf-8 -*-
"""卫戍协议图标：官方 CDN 按需下载，安装包不带图

森空岛的资源接口把图标地址直接发下来——盟约 `bond_icon/{id}.png`、策略
`band_icon/{id}.png`、装备 `char_skill/{id}.png` 全在 `web.hycdn.cn` 上。
所以这些图一张都不用进安装包，跟干员头像同一个思路：第一次显示时下载、
压成 WebP 存 ``config/cache/garrison/``，之后走本地。

``/api/garrison/img`` 是个对外的 GET 入口，url 由前端传，所以必须卡白名单：
只放行鹰角自己的两个 CDN，否则就成了任意 URL 代理（SSRF）。

压图失败就存原图——为了小几 KB 把图标弄丢不值。
"""
from __future__ import annotations

import hashlib
import io
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import net, settings

ALLOWED_HOSTS = ('web.hycdn.cn', 'bbs.hycdn.cn')
SIZES = (32, 48, 64, 96, 128)      # 只认这几档，避免 ?s=9999 被当成压图机
TIMEOUT = 12
MIN_BYTES = 64
MAX_BYTES = 2 * 1024 * 1024
NEG_TTL = 300

_locks = {}
_guard = threading.Lock()
_neg = {}                           # key -> 到期时间戳


def cache_dir() -> str:
    return os.path.join(settings.CONFIG_DIR, 'cache', 'garrison')


def normalize(url: str) -> str:
    """校验并归一图标地址：只留 http(s) + 白名单域名

    返回空串表示这个地址不能取（调用方按 404 处理，前端回落占位块）。
    """
    url = str(url or '').strip()
    if not url or len(url) > 1024:
        return ''
    try:
        p = urllib.parse.urlparse(url)
    except ValueError:
        return ''
    if p.scheme not in ('http', 'https'):
        return ''
    if (p.hostname or '').lower() not in ALLOWED_HOSTS:
        return ''
    return urllib.parse.urlunparse(('https', p.hostname, p.path, '', p.query, ''))


def _size(n) -> int:
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 64
    return n if n in SIZES else 64


def _key(url: str, size: int) -> str:
    return hashlib.sha1(('%s|%d' % (url, size)).encode('utf-8')).hexdigest()[:20]


def _path_of(key: str) -> tuple:
    base = os.path.join(cache_dir(), key)
    for ext in ('webp', 'png'):
        p = base + '.' + ext
        if os.path.isfile(p) and os.path.getsize(p) >= MIN_BYTES:
            return p, ext
    return '', ''


def _kind(data: bytes) -> str:
    if len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'webp'
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    if data[:3] == b'\xff\xd8\xff':
        return 'jpg'
    return ''


def _get(url: str) -> bytes:
    """取原图：网络层失败重试一次

    一屏练度表会同时发几百个 icon 请求，CDN 偶发断流/超时是常态（实测同一个
    地址前一次 TimeoutError、后一次就 200）。404 这类有应答的错误不重试——
    那是真没有；网络层错误重试一次能把「偶尔少一张图」压掉大半。
    """
    from .version import app_version
    req = urllib.request.Request(url, headers={
        'User-Agent': 'MAA-PRTS/%s' % app_version(),
        'Accept': 'image/webp,image/png,image/*;q=0.8',
        'Referer': 'https://www.skland.com/',
    })
    for attempt in (1, 2):
        try:
            with net.open(req, timeout=TIMEOUT) as r:
                return r.read(MAX_BYTES + 1)
        except urllib.error.HTTPError:
            raise                      # 有状态码：没有再试的意义
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            if attempt == 2:
                raise
            time.sleep(0.35)


def _to_webp(raw: bytes, size: int) -> bytes:
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw))
        if im.mode not in ('RGBA', 'RGB'):
            im = im.convert('RGBA')
        # 图标都是正方形容器，直接按长边缩到 size，别拉伸
        if im.width != size or im.height != size:
            im.thumbnail((size, size), Image.LANCZOS)
            canvas = Image.new('RGBA', (size, size), (0, 0, 0, 0))
            canvas.paste(im.convert('RGBA'),
                         ((size - im.width) // 2, (size - im.height) // 2))
            im = canvas
        out = io.BytesIO()
        im.save(out, 'WEBP', quality=82, method=4)
        data = out.getvalue()
        return data if _kind(data) == 'webp' else b''
    except Exception:
        return b''


def _save(key: str, data: bytes, ext: str) -> None:
    d = cache_dir()
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, '%s.%s' % (key, ext))
    tmp = dst + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, dst)


def fetch(url: str, size=64) -> tuple:
    """取一张图标：返回 (字节, 扩展名)；拿不到抛 KeyError

    同一张图的并发请求只下一次：一屏列表会同时发几十个 img 请求。
    """
    url = normalize(url)
    if not url:
        raise KeyError('图标地址不允许')
    size = _size(size)
    key = _key(url, size)

    p, ext = _path_of(key)
    if p:
        with open(p, 'rb') as f:
            return f.read(), ext

    with _guard:
        lock = _locks.setdefault(key, threading.Lock())

    got = lock.acquire(timeout=25)
    try:
        p, ext = _path_of(key)          # 等锁期间别人可能已经下好了
        if p:
            with open(p, 'rb') as f:
                return f.read(), ext
        until = _neg.get(key) or 0
        if until > time.time():
            raise KeyError('图标暂时拿不到')

        raw = _get(url)
        if len(raw) < MIN_BYTES or len(raw) > MAX_BYTES or not _kind(raw):
            _neg[key] = time.time() + NEG_TTL
            raise KeyError('图标不是图片')

        data = _to_webp(raw, size)
        out_ext = 'webp'
        if not data:
            data, out_ext = raw, _kind(raw)     # 转不了就存原图
        try:
            _save(key, data, out_ext)
        except OSError:
            pass                                 # 落盘失败也先把这张发出去
        _neg.pop(key, None)
        return data, out_ext
    finally:
        if got:
            lock.release()


def stats() -> dict:
    d = cache_dir()
    files, size = 0, 0
    try:
        for name in os.listdir(d):
            if name.endswith('.tmp') or name == 'guide.json':
                continue
            p = os.path.join(d, name)
            if os.path.isfile(p):
                files += 1
                size += os.path.getsize(p)
    except OSError:
        pass
    return {'dir': d, 'files': files, 'bytes': size}
