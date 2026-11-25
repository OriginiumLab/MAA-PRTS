# -*- coding: utf-8 -*-
"""界面主题：内置黑 / 白，第三方主题从 pack-data 分支按需下载

为什么第三方主题要下载（用户 2026-11-25 口径）：「除了默认的黑色主题和白色
主题，其他主题资源放在 pack-data 分支，用户需要在设置里点击下载，才能从我们
仓库拉包下载」——exe 体积不能跟着主题一起涨。所以：

    frontend/themes.css        内置 dark / light（随 exe 打包，几百行 CSS）
    config/themes/<id>/        第三方主题解压到这里（用户点「下载」才会出现）
    pack-data:themes/manifest.json   远端目录（id / 名称 / 体积 / sha256）
    pack-data:themes/<id>.zip        主题包（theme.json + theme.css + assets）

主题包内的契约（theme.json）：

    {
      "id": "closure", "name": "卫戍协议 · 可露希尔", "version": "1.0.0",
      "desc": "...", "css": "theme.css", "preview": "preview.jpg",
      "license": "配色/样式取自 ...（MIT）；字体 Ark Pixel（OFL 1.1）",
      "extra": [ {"file": "pet/idle.webm", "url": "https://..."} ]
    }

``preview`` 是**主题包内**的相对路径（装了才读得到）；manifest 里同名字段的
含义不一样，指的是数据分支 ``themes/`` 下的独立文件（没装也能预览，见
``_remote_preview``）。

``extra`` 是**不在我们仓库里再分发**的素材（角色动画这类），安装时从原作者的
仓库直连下载到本机；下不到就降级成纯配色主题（不报错），因为版权上我们只能
指路，不能替对方分发。

安全：zip 里不许有绝对路径 / ``..``；解压先到 ``<id>.tmp`` 再原子改名，下歪了
绝不覆盖已经能用的那份。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import time
import urllib.request
import zipfile

from . import net, packrepo, settings

THEMES_DIR = os.path.join(settings.CONFIG_DIR, 'themes')
MANIFEST_URLS = packrepo.mirror_urls('themes/manifest.json')

#: 内置主题（永远可用，删不掉）
BUILTIN = [
    {'id': 'dark', 'name': '深色', 'desc': '默认主题，低亮度护眼', 'builtin': True},
    {'id': 'light', 'name': '白色', 'desc': '浅色界面，白天看得清', 'builtin': True},
]

UA = 'MAA-PRTS/%s'
TIMEOUT_MANIFEST = 8
TIMEOUT_DOWNLOAD = 90

MAX_ZIP = 60 * 1024 * 1024          # 单个主题包上限（防止 manifest 被写坏时拉爆磁盘）
MAX_EXTRA = 80 * 1024 * 1024


def _iso():
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def _get(url, timeout=TIMEOUT_DOWNLOAD):
    from .version import app_version
    req = urllib.request.Request(url, headers={'User-Agent': UA % app_version()})
    with net.open(req, timeout=timeout) as r:
        return r.read()


def _dir_of(tid):
    return os.path.join(THEMES_DIR, tid)


def _safe_id(tid):
    tid = str(tid or '').strip().lower()
    if not tid or len(tid) > 40:
        raise ValueError('主题 id 不合法')
    for ch in tid:
        if not (ch.isalnum() or ch in '-_'):
            raise ValueError('主题 id 只能用字母数字和 - _：%s' % tid)
    if tid in ('dark', 'light'):
        raise ValueError('内置主题不能安装 / 删除')
    return tid


def _read_meta(tid):
    """读本机已装主题的 theme.json（没有/坏了返回 None）"""
    p = os.path.join(_dir_of(tid), 'theme.json')
    if not os.path.isfile(p):
        return None
    try:
        with io.open(p, encoding='utf-8') as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return None
    except Exception:                       # noqa: BLE001 坏文件当没装
        return None
    d.setdefault('id', tid)
    return d


def installed():
    """本机已装的主题（['closure', ...]）"""
    out = []
    if not os.path.isdir(THEMES_DIR):
        return out
    for name in sorted(os.listdir(THEMES_DIR)):
        if name in ('dark', 'light'):
            continue
        if os.path.isfile(os.path.join(THEMES_DIR, name, 'theme.json')):
            out.append(name)
    return out


def fetch_manifest(timeout=TIMEOUT_MANIFEST):
    """读远端主题目录；三个镜像依次试，全失败抛 RuntimeError"""
    errs = []
    stamp = int(time.time())
    for base in MANIFEST_URLS:
        url = '%s?t=%d' % (base, stamp)     # 破 CDN 缓存
        try:
            d = json.loads(_get(url, timeout).decode('utf-8'))
            if isinstance(d, dict) and isinstance(d.get('themes'), list):
                return d
            errs.append('%s 里没有 themes' % base.split('/')[2])
        except Exception as e:              # noqa: BLE001
            errs.append('%s -> %s' % (base.split('/')[2], e))
    raise RuntimeError('读不到主题目录：' + '；'.join(errs))


def catalog(timeout=TIMEOUT_MANIFEST, local_only=False):
    """设置页要的完整清单：内置 + 已装 + 远端可下载

    远端读不到**不算错误**——离线也得能切黑/白，只在 ``remoteError`` 里说一句。

    ``local_only=True`` 只读本机（不联网，毫秒级）：设置页先用它把已装主题
    画出来，再拉全量补「可下载」那几张。否则远端慢的时候（8 秒超时）页面
    上只剩深色/白色两张，「卫戍协议」像丢了——2026-11-25 用户反馈的
    「白色和卫戍协议混淆」就是这么来的。
    """
    have = set(installed())
    items = [dict(x, installed=True) for x in BUILTIN]
    for tid in sorted(have):
        meta = _read_meta(tid) or {}
        css = meta.get('css') or 'theme.css'
        items.append({
            'id': tid,
            'name': meta.get('name') or tid,
            'desc': meta.get('desc') or '',
            'version': meta.get('version') or '',
            'license': meta.get('license') or '',
            'css': '/themes/%s/%s' % (tid, css),
            # 桌宠（可选）：主题包自己声明的动画与尺寸，前端照着插 <video>
            'pet': meta.get('pet') or {},
            'preview': ('/themes/%s/%s' % (tid, meta['preview'])
                        if meta.get('preview') else ''),
            'installed': True,
            'builtin': False,
        })
    out = {'themes': items, 'installed': sorted(have), 'remote': [],
           'remoteError': '', 'checkedAt': _iso(), 'partial': bool(local_only)}
    if local_only:
        return out
    try:
        man = fetch_manifest(timeout)
    except Exception as e:                  # noqa: BLE001
        out['remoteError'] = str(e)
        return out
    remote = []
    for t in man.get('themes') or []:
        if not isinstance(t, dict):
            continue
        tid = str(t.get('id') or '').strip().lower()
        if not tid or tid in ('dark', 'light'):
            continue
        remote.append({
            'id': tid,
            'name': t.get('name') or tid,
            'desc': t.get('desc') or '',
            'version': t.get('version') or '',
            'bytes': int(t.get('bytes') or 0),
            'license': t.get('license') or '',
            'preview': _remote_preview(t),
            'installed': tid in have,
            'builtin': False,
        })
    out['remote'] = remote
    return out


def _remote_preview(t):
    """还没下载的主题，缩略图也得看得见：指向数据分支里的同名文件

    manifest 里的 ``preview`` 是**数据分支 themes/ 下的相对路径**（如
    ``closure-preview.jpg``），不是主题包里的路径——包还没下下来，本地没有
    那个文件可服务。

    镜像选择跟下载包不一样：缩略图是 <img> 直接拉的，raw 在国内基本连不上，
    jsDelivr 对分支路径会 301，所以优先 gh-proxy；一个都拿不到就返回空，
    前端用主题色块兜底（不会裂图，不会白框）。
    """
    p = str(t.get('preview') or '').strip()
    if not p:
        return ''
    if p.startswith(('http://', 'https://')):
        return p
    urls = packrepo.mirror_urls('themes/' + p.lstrip('/'))
    for u in urls:
        if 'gh-proxy' in u:
            return u
    return urls[0]


def _safe_extract(zf, dest):
    """解压（拒绝绝对路径 / ..，只允许文件与目录）"""
    root = os.path.abspath(dest)
    for info in zf.infolist():
        name = info.filename.replace('\\', '/')
        if name.startswith('/') or '..' in name.split('/'):
            raise RuntimeError('主题包里有非法路径：%s' % info.filename)
        target = os.path.abspath(os.path.join(root, name))
        if os.path.commonpath([root, target]) != root:
            raise RuntimeError('主题包里有非法路径：%s' % info.filename)
    zf.extractall(root)


def _download_extra(meta, log=None):
    """下载主题包里声明「别处托管」的素材（角色动画这类）

    失败**不抛**：降级成纯配色主题，把失败的文件名列进返回的 failed 里。
    版权上我们不替原作者分发这些素材，只指路（见模块开头）。
    """
    got, failed = [], []
    base = _dir_of(meta['id'])
    for item in meta.get('extra') or []:
        if not isinstance(item, dict):
            continue
        rel = str(item.get('file') or '').strip()
        urls = [str(u).strip() for u in (item.get('urls') or []) if str(u).strip()]
        if item.get('url'):
            urls.append(str(item['url']).strip())
        if not rel or not urls:
            continue
        if rel.startswith('/') or '..' in rel.split('/'):
            continue
        dst = os.path.join(base, rel.replace('/', os.sep))
        if os.path.isfile(dst) and os.path.getsize(dst) > 0:
            got.append(rel)
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        errs = []
        for url in urls:
            try:
                raw = _get(url)
                if not raw or len(raw) > MAX_EXTRA:
                    raise RuntimeError('大小异常（%d 字节）' % len(raw or b''))
                tmp = dst + '.part'
                with open(tmp, 'wb') as f:
                    f.write(raw)
                os.replace(tmp, dst)
                got.append(rel)
                if log:
                    log('素材 %s（%.1f MB）' % (rel, len(raw) / 1048576.0))
                break
            except Exception as e:          # noqa: BLE001
                errs.append('%s -> %s' % (url.split('/')[2], e))
        else:
            failed.append('%s: %s' % (rel, '；'.join(errs)))
            if log:
                log('素材没下到 %s（%s）' % (rel, '；'.join(errs)))
    return got, failed


def install(tid, log=None):
    """下载并安装一个第三方主题（幂等：已装就重装一次）"""
    tid = _safe_id(tid)
    man = fetch_manifest()
    entry = next((t for t in (man.get('themes') or [])
                  if str(t.get('id') or '').lower() == tid), None)
    if not entry:
        raise RuntimeError('远端没有这个主题：%s' % tid)
    rel = str(entry.get('file') or ('themes/%s.zip' % tid)).lstrip('/')
    want_sha = str(entry.get('sha256') or '').lower()
    want_bytes = int(entry.get('bytes') or 0)

    if log:
        log('下载主题包 %s（%.1f MB）…' % (rel, want_bytes / 1048576.0))
    raw, errors = None, []
    for url in packrepo.mirror_urls(rel):
        try:
            got = _get(url)
            if want_bytes and len(got) != want_bytes:
                raise RuntimeError('字节数不对（%d ≠ %d）' % (len(got), want_bytes))
            if want_sha:
                h = hashlib.sha256(got).hexdigest()
                if h != want_sha:
                    raise RuntimeError('sha256 不对（%s ≠ %s）' % (h[:12], want_sha[:12]))
            raw = got
            break
        except Exception as e:              # noqa: BLE001
            errors.append('%s -> %s' % (url.split('/')[2], e))
    if raw is None:
        raise RuntimeError('主题包下载失败：' + '；'.join(errors))
    if len(raw) > MAX_ZIP:
        raise RuntimeError('主题包太大（%d 字节），拒绝解压' % len(raw))

    os.makedirs(THEMES_DIR, exist_ok=True)
    final = _dir_of(tid)
    tmp = final + '.tmp'
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            _safe_extract(zf, tmp)
        # 允许 zip 里套一层同名目录
        meta_p = os.path.join(tmp, 'theme.json')
        if not os.path.isfile(meta_p):
            sub = [d for d in os.listdir(tmp)
                   if os.path.isfile(os.path.join(tmp, d, 'theme.json'))]
            if len(sub) == 1:
                inner = os.path.join(tmp, sub[0])
                for name in os.listdir(inner):
                    shutil.move(os.path.join(inner, name), os.path.join(tmp, name))
                os.rmdir(inner)
                meta_p = os.path.join(tmp, 'theme.json')
        if not os.path.isfile(meta_p):
            raise RuntimeError('主题包里没有 theme.json')
        with io.open(meta_p, encoding='utf-8') as f:
            meta = json.load(f)
        if not isinstance(meta, dict):
            raise RuntimeError('theme.json 不是对象')
        meta['id'] = tid
        if not meta.get('css'):
            meta['css'] = 'theme.css'
        if not os.path.isfile(os.path.join(tmp, meta['css'])):
            raise RuntimeError('主题包里没有 %s' % meta['css'])
        with io.open(meta_p, 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        shutil.rmtree(final, ignore_errors=True)
        os.replace(tmp, final)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    got, failed = _download_extra(meta, log)
    return {'ok': True, 'id': tid, 'name': meta.get('name') or tid,
            'version': meta.get('version') or '',
            'css': '/themes/%s/%s' % (tid, meta.get('css')),
            'pet': meta.get('pet') or {},
            'extra': got, 'extraFailed': failed}


def remove(tid):
    tid = _safe_id(tid)
    shutil.rmtree(_dir_of(tid), ignore_errors=True)
    return {'ok': True, 'id': tid, 'removed': True}


def file_of(tid, rel):
    """第三方主题里的一个文件（给 /themes/<id>/<file> 用，防目录穿越）"""
    tid = _safe_id(tid)
    rel = str(rel or '').replace('\\', '/').lstrip('/')
    if not rel or '..' in rel.split('/'):
        return None
    base = os.path.abspath(_dir_of(tid))
    full = os.path.abspath(os.path.join(base, rel))
    try:
        if os.path.commonpath([base, full]) != base:
            return None
    except ValueError:
        return None
    return full if os.path.isfile(full) else None


def usable(tid):
    """这个主题现在能不能用（内置，或第三方已装且有 css）"""
    tid = str(tid or '').strip().lower()
    if tid in ('dark', 'light'):
        return True
    meta = _read_meta(tid)
    if not meta:
        return False
    return bool(file_of(tid, meta.get('css') or 'theme.css'))
