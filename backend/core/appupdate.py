# -*- coding: utf-8 -*-
"""程序本体自动更新（海外源：GitHub Releases）

和资源包更新是两条独立链路，互不干扰：

    packsync   游戏资源包（0.6 MB，启动后静默换掉，不用重启）
    appupdate  程序本体（13 MB，下载完等用户点「重启并安装」）

检查用三级回退，尽量不消耗 GitHub API 配额：

    1. HEAD github.com/<repo>/releases/latest
       读 302 的 Location 拿到 v2.4.0 —— 不占 API 配额，绝大多数检查只走这一步
    2. 版本比当前新 → GET api.github.com/.../releases/latest
       拿 changelog 与 assets[].digest（sha256），失败就只提醒、不给安装按钮
    3. 全失败 → 静默，6 小时后重试（6 小时是成功才写的，失败不占用配额）

安装：Windows 上运行中的 exe 不能覆盖、_internal 里的 DLL 也不能删，
所以替换交给一个退出后才执行的 install.ps1：

    下载 zip → 校验 sha256/字节数 → 解压到 update/stage → 写 install.ps1
    用户点「重启并安装」→ 无窗口拉起 PowerShell → 主进程退出 → 脚本按 PID
    等旧进程消失后 robocopy

用 PowerShell 而不是 cmd 批处理：批处理里判断「进程还在不在」只能靠
`进程列表 | find`，那条管道在 Windows 上会死锁，安装会卡在第一步不动。

只替换程序本体（MAA-PRTS.exe、_internal/、version.json 等根级文件），
config/ debug/ resources/ update/ 一律不碰
（resources/ 里可能是用户自己换的资源包，config/ 里是账号和配置）。

version.json 必须跟 exe 一起换：版本号读的是它，漏了就会出现
「装完还是旧版本 → 每次启动都提示更新 → 再装还是旧版本」的死循环。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import zipfile

from . import net, settings

# 只认这一个仓库：写死，不读设置，防止被改配置指向别处
APP_REPO = 'OriginiumLab/MAA-PRTS'

RELEASES_HEAD = 'https://github.com/%s/releases/latest' % APP_REPO
RELEASES_API = 'https://api.github.com/repos/%s/releases/latest' % APP_REPO
RELEASES_PAGE = 'https://github.com/%s/releases/tag/%%s' % APP_REPO

# 下载地址依次回退：GitHub 直链 → 两个公共反代
DOWNLOAD_TEMPLATES = ('%s', 'https://gh-proxy.com/%s', 'https://ghfast.top/%s')

CHECK_INTERVAL = 6 * 3600    # 成功检查后 6 小时内不再自动查
RETRY_API_FAIL = 10 * 60     # API 拿不到 changelog 时，10 分钟后允许再试
TIMEOUT_HEAD = 6
TIMEOUT_API = 8
TIMEOUT_DOWNLOAD = 180

UA = 'MAA-PRTS/%s (+https://github.com/%s)'

_lock = threading.RLock()
_cache = [None]
_quit_hook = [None]

_VER_RE = re.compile(r'^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.\-]+))?$')


# ---------------------------------------------------------------- 路径
def update_dir() -> str:
    return os.path.join(settings.project_root(), 'update')


def state_path() -> str:
    return os.path.join(settings.CONFIG_DIR, 'appupdate.json')


def _iso(ts=None) -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(ts or time.time()))


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


def _state() -> dict:
    with _lock:
        if _cache[0] is None:
            _cache[0] = _read_json(state_path(), None) or {}
        return _cache[0]


def _save_state(st: dict):
    with _lock:
        _cache[0] = st
        try:
            _write_json(state_path(), st)
        except Exception:
            pass


# ---------------------------------------------------------------- 版本比较
def parse_version(v):
    """'v2.4.0' → (2,4,0,1,0)；'v2.4.0-beta.2' → (2,4,0,0,2)

    第四位：正式版 1、预发布 0，所以同号的预发布永远小于正式版。
    解析不了一律返回 None —— 脏 tag 绝不触发更新。
    """
    m = _VER_RE.match(str(v or '').strip())
    if not m:
        return None
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    pre = m.group(4)
    if not pre:
        return (major, minor, patch, 1, 0)
    num = 0
    for part in reversed(pre.split('.')):
        if part.isdigit():
            num = int(part)
            break
    else:
        tail = re.search(r'(\d+)$', pre)
        num = int(tail.group(1)) if tail else 0
    return (major, minor, patch, 0, num)


def is_newer(remote, local) -> bool:
    a, b = parse_version(remote), parse_version(local)
    if not a or not b:
        return False
    return a > b


def _current() -> str:
    # 版本号不在代码里维护：发版 CI 把 version.json 写进发布包根目录，
    # 本地源码运行 / 本地打包没有这个文件，读到的是 'dev'（此时不提示更新）。
    from .version import app_version
    return app_version()


def _arch() -> str:
    m = (platform.machine() or '').lower()
    if m in ('arm64', 'aarch64'):
        return 'arm64'
    if m in ('x86', 'i386', 'i686'):
        return 'x86'
    return 'x64'


def _is_prerelease_tag(tag) -> bool:
    return '-' in str(tag or '').lstrip('v')


# ---------------------------------------------------------------- HTTP
def _ua() -> str:
    return UA % (_current(), APP_REPO)


def _head_tag(timeout=TIMEOUT_HEAD) -> str:
    """不跟随跳转，从 302 的 Location 里取 tag：releases/latest → releases/tag/v2.4.0

    这条路不消耗 API 配额，但国内 github.com 经常直接超时，所以失败后退回
    api.github.com 拿 tag_name——多一条路总比「检查更新永远失败」好。
    """
    try:
        req = urllib.request.Request(RELEASES_HEAD, method='HEAD',
                                     headers={'User-Agent': _ua()})
        loc = ''
        try:
            # opener 每次现建（net 会读最新代理设置），检查频率低不心疼；
            # redirect=False 才能从 302 里拿 Location
            with net.open(req, timeout=timeout, redirect=False) as r:
                loc = r.headers.get('Location') or r.geturl()
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                loc = e.headers.get('Location') or ''
            else:
                raise
        tag = str(loc).rstrip('/').rsplit('/', 1)[-1]
        if not parse_version(tag):
            raise RuntimeError('GitHub 返回的 tag 无法解析：%r' % tag)
        return tag
    except Exception as head_err:  # noqa: BLE001 - 换条路再试
        try:
            tag = str(_api_latest(timeout).get('tag_name') or '')
        except Exception as api_err:  # noqa: BLE001
            raise RuntimeError('%s；api.github.com 也不行：%s' % (head_err, api_err))
        if not parse_version(tag):
            raise RuntimeError('api.github.com 返回的 tag 无法解析：%r' % tag)
        return tag


def _api_latest(timeout=TIMEOUT_API) -> dict:
    req = urllib.request.Request(RELEASES_API, headers={
        'User-Agent': _ua(),
        'Accept': 'application/vnd.github+json',
    })
    with net.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def _pick_asset(assets):
    """挑 Windows 的 zip：优先架构匹配，其次带 win 的，最后退第一个 zip"""
    zips = [a for a in (assets or [])
            if str(a.get('name') or '').lower().endswith('.zip')]
    if not zips:
        return None
    arch = _arch()
    for a in zips:
        if arch in a['name'].lower():
            return a
    for a in zips:
        if 'win' in a['name'].lower():
            return a
    return zips[0]


def _digest_sha256(asset) -> str:
    d = str(asset.get('digest') or '')
    if d.startswith('sha256:'):
        return d[len('sha256:'):].strip().lower()
    return ''


def _asset_urls(asset):
    base = str(asset.get('browser_download_url') or '').strip()
    if not base:
        return []
    return [tpl % base for tpl in DOWNLOAD_TEMPLATES]


def _fallback_asset(tag: str) -> dict:
    """API 拿不到时，按发版命名规则直接拼下载地址

    release.yml 产出的资产名固定是 MAA-PRTS-<tag>-win-<arch>.zip，所以哪怕
    api.github.com 完全不通（国内很常见），也能照常下载。代价是这次没有
    size/sha256，校验降级成「确实是 zip，里面确实有 exe」（见 _verify_zip）。
    """
    name = 'MAA-PRTS-%s-win-%s.zip' % (tag, _arch())
    base = 'https://github.com/%s/releases/download/%s/%s' % (APP_REPO, tag, name)
    return {
        'name': name,
        'size': 0,
        'sha256': '',
        'urls': [tpl % base for tpl in DOWNLOAD_TEMPLATES],
        'degraded': True,
    }


# ---------------------------------------------------------------- 检查
def _status_from_state(st: dict, **extra) -> dict:
    """从落盘状态拼返回值。hasUpdate 每次重算而不是缓存：
    缓存只该记住「远端是哪个版本」，本地版本变了（刚升级完）要立刻反映出来。"""
    tag = st.get('tag') or ''
    pre = bool(st.get('prerelease'))
    accept_pre = bool(settings.load().get('acceptPrerelease'))
    has = bool(tag) and is_newer(tag, _current()) and not (pre and not accept_pre)
    out = {
        'ok': True,
        'error': '',
        'current': _current(),
        'latest': st.get('latest') or '',
        'tag': tag,
        'hasUpdate': has,
        'prerelease': pre,
        'notes': st.get('notes') or '',
        'publishedAt': st.get('publishedAt') or '',
        'pageUrl': st.get('pageUrl') or '',
        'asset': st.get('asset') or {},
        'degraded': bool(st.get('degraded')),   # 没有官方 sha256，只按结构校验
        'checkedAt': st.get('checkedAt') or '',
        'cached': True,
        'throttled': True,
        'ready': _ready(),
        'frozen': bool(settings.FROZEN),
        'lastInstall': st.get('lastInstall') or None,
    }
    out.update(extra)
    return out


def _ready() -> bool:
    """安装包是否已经解压好、等着替换"""
    return os.path.isfile(_stage_exe())


def _stage_exe() -> str:
    d = os.path.join(update_dir(), 'stage')
    for rel in (('MAA-PRTS', 'MAA-PRTS.exe'), ('MAA-PRTS.exe',)):
        p = os.path.join(d, *rel)
        if os.path.isfile(p):
            return p
    return ''


def status(force=False) -> dict:
    """检查有没有新版本（只读，不下载）；force=True 跳过 6 小时节流"""
    st = _state()
    now = time.time()
    checked = float(st.get('checkedTs') or 0)
    if not force and checked and (now - checked) < CHECK_INTERVAL:
        return _status_from_state(st)

    out = _status_from_state(st, cached=False, throttled=False)

    try:
        tag = _head_tag()
    except Exception as e:  # noqa: BLE001 - 没网就静默，别打扰用户
        out['ok'] = False
        out['error'] = '连不上 GitHub：%s' % e
        return out

    latest = tag.lstrip('v')
    has = is_newer(tag, _current())
    pre = _is_prerelease_tag(tag)
    accept_pre = bool(settings.load().get('acceptPrerelease'))

    fresh = {
        'checkedTs': now,
        'checkedAt': _iso(now),
        'tag': tag,
        'latest': latest,
        'prerelease': pre,
        'notes': '',
        'publishedAt': '',
        'pageUrl': RELEASES_PAGE % tag,
        'asset': {},
        'lastInstall': st.get('lastInstall') or None,
    }

    # 预发布：没开开关就当没更新，但记下来，免得每次启动都白查
    if pre and not accept_pre:
        _save_state(fresh)
        out.update(fresh, hasUpdate=False, cached=False, throttled=False)
        return out

    if not has:
        _save_state(fresh)
        out.update(fresh, hasUpdate=False, cached=False, throttled=False)
        return out

    # 有新版：再花一次 API 配额拿 changelog 和校验值
    try:
        rel = _api_latest()
    except Exception as e:  # noqa: BLE001 - 拿不到 JSON 也不能让用户装不了
        # 降级：按命名规则拼地址，照样能下载安装，只是没有官方 sha256。
        # 这类失败多半是 api.github.com 被拦/超时（国内常见），不能拿它
        # 当 6 小时节流的依据，10 分钟后就允许重试一次。
        fresh['asset'] = _fallback_asset(tag)
        fresh['degraded'] = True
        fresh['detailError'] = str(e)
        fresh['checkedTs'] = now - CHECK_INTERVAL + RETRY_API_FAIL
        _save_state(fresh)
        out.update(fresh, hasUpdate=True, cached=False, throttled=False,
                   detailError=str(e),
                   error='')
        return out

    asset = _pick_asset(rel.get('assets')) or {}
    fresh['notes'] = str(rel.get('body') or '')[:20000]
    fresh['publishedAt'] = str(rel.get('published_at') or '')
    fresh['prerelease'] = bool(rel.get('prerelease'))
    fresh['asset'] = {
        'name': str(asset.get('name') or ''),
        'size': int(asset.get('size') or 0),
        'sha256': _digest_sha256(asset),
        'urls': _asset_urls(asset),
    } if asset else {}
    if fresh['asset'].get('name'):
        # 落盘前清掉上一版的残留（stage 里的旧包会让 ready 误判）
        old = st.get('tag') or ''
        if old and old != tag:
            shutil.rmtree(os.path.join(update_dir(), 'stage'), ignore_errors=True)
    _save_state(fresh)
    out.update(fresh, hasUpdate=True, cached=False, throttled=False)
    return out


# ---------------------------------------------------------------- 下载
def download(force=False) -> dict:
    """把安装包下到 update/ 并校验；不安装"""
    st = status(force=force)
    if not st['ok']:
        return {'ok': False, 'error': st['error']}
    if not st.get('hasUpdate'):
        return {'ok': False, 'error': '已经是最新版本'}

    meta = st.get('asset') or {}
    urls = meta.get('urls') or []
    if not urls:
        # 旧状态里可能只记了版本号（上次检查时 API 不通），用户既然点了
        # 下载就强制重查一次，别直接把人推到「前往下载页」
        st = status(force=True)
        if not st.get('hasUpdate'):
            return {'ok': False, 'error': st.get('error') or '已经是最新版本'}
        meta = st.get('asset') or {}
        urls = meta.get('urls') or []
    if not urls:
        return {'ok': False, 'error': '拿不到下载地址，请用「前往下载页」'}

    name = meta.get('name') or ('MAA-PRTS-%s.zip' % st.get('tag'))
    dest_dir = update_dir()
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, os.path.basename(name))

    if os.path.isfile(dest) and _match(dest, meta):
        return {'ok': True, 'path': dest, 'cached': True, 'version': st.get('latest'),
                'degraded': bool(meta.get('degraded'))}

    errors = []
    for url in urls:
        try:
            raw = _get(url, TIMEOUT_DOWNLOAD)
            _verify_zip(raw, meta)
        except Exception as e:  # noqa: BLE001
            errors.append('%s -> %s' % (url.split('/')[2], e))
            continue
        tmp = dest + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dest)
        return {'ok': True, 'path': dest, 'cached': False,
                'size': len(raw), 'version': st.get('latest'),
                'degraded': bool(meta.get('degraded'))}

    return {'ok': False, 'error': '下载失败：' + '；'.join(errors)}


def _get(url, timeout):
    req = urllib.request.Request(url, headers={'User-Agent': _ua()})
    with net.open(req, timeout=timeout) as r:
        return r.read()


def _match(path, meta) -> bool:
    try:
        if meta.get('size') and os.path.getsize(path) != int(meta['size']):
            return False
        want = (meta.get('sha256') or '').lower()
        if want:
            h = hashlib.sha256()
            with open(path, 'rb') as f:
                for chunk in iter(lambda: f.read(1 << 20), b''):
                    h.update(chunk)
            return h.hexdigest() == want
        # 降级路径没有 size/sha256：至少确认这文件是完整的 zip，别把下载
        # 中断留下的残包当成「已下载」而反复解压失败
        with zipfile.ZipFile(path) as zf:
            return any(n.lower().endswith('maa-prts.exe') for n in zf.namelist())
    except Exception:
        return False


def _verify_zip(raw: bytes, meta: dict):
    """下载完整性：字节数、sha256、能不能当作合法 zip 打开"""
    size = int(meta.get('size') or 0)
    if size and len(raw) != size:
        raise RuntimeError('大小不对：%d != %d' % (len(raw), size))
    want = (meta.get('sha256') or '').lower()
    if want:
        got = hashlib.sha256(raw).hexdigest()
        if got != want:
            raise RuntimeError('sha256 不匹配：%s != %s' % (got[:12], want[:12]))
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = zf.namelist()
    except Exception as e:
        raise RuntimeError('不是合法 zip：%s' % e)
    if not any(n.lower().endswith('maa-prts.exe') for n in names):
        raise RuntimeError('压缩包里没有 MAA-PRTS.exe')
    if not size and not want:
        # 两样都没有时不拦（降级路径），但至少保证不是空壳
        if len(raw) < 1024 * 1024:
            raise RuntimeError('压缩包太小，不像完整安装包')


# ---------------------------------------------------------------- 解压与安装
def _safe_members(zf):
    """挑出可安全解压的成员：绝对路径、`..` 一律拒绝（防 zip-slip）"""
    out = []
    for member in zf.infolist():
        rel = member.filename.replace('\\', '/')
        parts = [p for p in rel.split('/') if p not in ('', '.')]
        if rel.startswith('/') or '..' in parts:
            raise RuntimeError('压缩包里有可疑路径：%s' % member.filename)
        out.append(member)
    return out


def prepare() -> dict:
    """解压到 update/stage 并写好 install.ps1（此时还没替换任何东西）"""
    st = _state()
    tag = st.get('tag') or ''
    name = (st.get('asset') or {}).get('name') or ''
    if not tag:
        return {'ok': False, 'error': '还没有检查到新版本'}
    zip_path = os.path.join(update_dir(), os.path.basename(name)) if name else ''
    if not zip_path or not os.path.isfile(zip_path):
        return {'ok': False, 'error': '安装包还没下载'}

    stage = os.path.join(update_dir(), 'stage')
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage, exist_ok=True)

    try:
        with zipfile.ZipFile(zip_path) as zf:
            for member in _safe_members(zf):
                zf.extract(member, stage)
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(stage, ignore_errors=True)
        return {'ok': False, 'error': '解压失败：%s' % e}

    exe = _stage_exe()
    if not exe:
        shutil.rmtree(stage, ignore_errors=True)
        return {'ok': False, 'error': '解压后没找到 MAA-PRTS.exe，压缩包结构不对'}

    script = _write_installer(exe, st.get('latest') or tag.lstrip('v'))
    if not script:
        return {'ok': False, 'error': '写安装脚本失败（目录没有写权限？）'}

    # 记一笔，新版本启动时用它判断「到底替没替换成功」
    pending = {
        'from': _current(),
        'to': st.get('latest') or tag.lstrip('v'),
        'tag': tag,
        'at': _iso(),
    }
    _write_json(os.path.join(update_dir(), 'pending.json'), pending)
    return {'ok': True, 'exe': exe, 'script': script, 'pending': pending}


# NOTE: written as UTF-8 with BOM; PowerShell 5.1 needs the BOM to read it right.
# MAA-PRTS installer, generated by appupdate.py. Replaces program body only:
# MAA-PRTS.exe / _internal/ / version.json. config/ debug/ resources/ are user
# data and must never be touched. Waiting is done by PID, never by piping a
# process-list command into find: that pipeline deadlocks on Windows and hangs
# the whole install (a real user got stuck on exactly that on 2026-11-08).
INSTALL_PS1 = r'''param([int]$ParentPid = 0, [string]$Version = '')
$here = $PSScriptRoot
$root = Split-Path -Parent $here
$log  = Join-Path $here 'install.log'
$pkg  = Join-Path $here '@@PKG@@'
$exe  = Join-Path $root 'MAA-PRTS.exe'
$newExe = Join-Path $pkg 'MAA-PRTS.exe'
$bkdir = Join-Path $here 'backup'

function Log([string]$m) {
  $line = '[' + (Get-Date -Format 'HH:mm:ss') + '] ' + $m
  try { Add-Content -LiteralPath $log -Value $line -Encoding UTF8 } catch { }
}

function CleanUp {
  Remove-Item -LiteralPath (Join-Path $here 'stage') -Recurse -Force -ErrorAction SilentlyContinue
}

function Fail([string]$m) {
  Log ('失败：' + $m)
  try { Set-Content -LiteralPath (Join-Path $here 'install-error.txt') -Value $m -Encoding UTF8 } catch { }
  $bak = Join-Path $bkdir 'MAA-PRTS.exe'
  if (Test-Path -LiteralPath $bak) {
    try { Copy-Item -LiteralPath $bak -Destination $exe -Force } catch { Log '回滚 exe 失败' }
  }
  # 别把用户扔在「程序没了」的状态里：失败也要把旧版拉起来
  if (Test-Path -LiteralPath $exe) {
    try { Start-Process -FilePath $exe -WorkingDirectory $root } catch { Log '重新拉起失败' }
  }
  CleanUp
  exit 1
}

Log '=== 开始安装 ==='
Log ('目标版本 ' + $Version + '，运行目录 ' + $root)

# 1) 等本进程退出。按 PID 判断，不走管道（脚本内不得再出现进程列表管道）
if ($ParentPid -gt 0) {
  $deadline = (Get-Date).AddSeconds(60)
  $ms = 0
  while ($true) {
    $p = Get-Process -Id $ParentPid -ErrorAction SilentlyContinue
    if (-not $p) { break }
    if ($p.ProcessName -ne 'MAA-PRTS') { break }   # PID 被复用，别傻等
    if ((Get-Date) -gt $deadline) { Log '等待超时（60 秒），直接尝试替换'; break }
    Start-Sleep -Milliseconds 250
    $ms = $ms + 250
  }
  Log ('旧进程已退出，等待 ' + $ms + ' 毫秒')
} else {
  Start-Sleep -Milliseconds 800
}

# 2) 包结构先校验，别等替换到一半才发现解压不完整
if (-not (Test-Path -LiteralPath $newExe)) { Fail '解压目录里没有 MAA-PRTS.exe，压缩包不完整' }

# 3) 备份旧 exe，失败可回滚
New-Item -ItemType Directory -Force -Path $bkdir | Out-Null
if (Test-Path -LiteralPath $exe) {
  Copy-Item -LiteralPath $exe -Destination (Join-Path $bkdir 'MAA-PRTS.exe') -Force
}
Log '已备份旧 exe'

# 4) _internal 用 /MIR：新版本删掉的旧 DLL 也要清掉，否则新旧混装
& robocopy.exe (Join-Path $pkg '_internal') (Join-Path $root '_internal') /MIR /R:2 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
$rc = $LASTEXITCODE
Log ('_internal 镜像完成 rc=' + $rc)
# robocopy：0-7 都是成功（1=有复制、3=有额外文件），8 以上才是真失败
if ($rc -ge 8) { Fail ('替换 _internal 失败 rc=' + $rc) }

# 5) 根级文件（exe / version.json / README）覆盖复制：不带 /MIR，
#    目标侧多出来的用户数据不会被删；/XD 把用户数据目录整个排除
& robocopy.exe $pkg $root /E /XD _internal config debug resources update /R:2 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
$rc = $LASTEXITCODE
Log ('根级文件完成 rc=' + $rc)
if ($rc -ge 8) { Fail ('替换程序文件失败 rc=' + $rc) }

# 6) 校验：version.json 必须已经换新，否则装完版号还是旧的，会反复提示更新
if (-not (Test-Path -LiteralPath $exe)) { Fail '替换后找不到 MAA-PRTS.exe' }
$newVj = Join-Path $pkg 'version.json'
$curVj = Join-Path $root 'version.json'
if (Test-Path -LiteralPath $newVj) {
  if (-not (Test-Path -LiteralPath $curVj)) { Fail '替换后缺少 version.json' }
  $want = (Get-Content -LiteralPath $newVj -Raw) -replace '\s', ''
  $got = (Get-Content -LiteralPath $curVj -Raw) -replace '\s', ''
  if ($got -ne $want) { Fail ('version.json 没替换成功：' + $got + ' / ' + $want) }
  Log ('版本号已更新为 ' + $want)
}

# 7) 收尾
try { Set-Content -LiteralPath (Join-Path $here 'install-ok.txt') -Value $Version -Encoding ASCII } catch { }
Remove-Item -LiteralPath (Join-Path $here 'install-error.txt') -Force -ErrorAction SilentlyContinue
Log '替换完成，启动新版本'
try { Start-Process -FilePath $exe -WorkingDirectory $root } catch { Log ('启动失败：' + $_.Exception.Message) }
CleanUp
Remove-Item -Path (Join-Path $here '*.zip') -Force -ErrorAction SilentlyContinue
Log '=== 安装结束 ==='
Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
'''


def _write_installer(stage_exe: str, version: str = '') -> str:
    """生成等待进程退出后执行的安装脚本 install.ps1（纯 ASCII）

    为什么是 PowerShell 不是 .cmd：批处理里 `tasklist | find` 在 Windows 上会
    死锁——cmd 不关管道写端，find 等不到 EOF 就永远挂着，安装卡死在第一步，
    用户只看到一个黑窗口（2026-11-08 真机上就是这么挂的）。
    PowerShell 按 PID 等进程退出，没有管道，失败还能回滚并重新拉起旧版本。
    """
    stage_dir = os.path.join(update_dir(), 'stage')
    rel = os.path.relpath(os.path.dirname(stage_exe), stage_dir)
    if rel in ('.', ''):
        pkg = 'stage'
    else:
        pkg = 'stage\\%s' % rel.replace('/', '\\')

    text = INSTALL_PS1.replace('@@PKG@@', pkg)
    path = os.path.join(update_dir(), 'install.ps1')
    try:
        os.makedirs(update_dir(), exist_ok=True)
        # 必须带 BOM：PowerShell 5.1 读无 BOM 的 .ps1 会按 ANSI 解码，
        # 脚本里的中文提示会变成乱码（甚至语法错误）
        with open(path, 'w', encoding='utf-8-sig', newline='\n') as f:
            f.write(text)
        # 旧版生成的是 install.cmd，留着容易看串，顺手清掉
        stale = os.path.join(update_dir(), 'install.cmd')
        if os.path.isfile(stale):
            try:
                os.remove(stale)
            except Exception:
                pass
        return path
    except Exception:
        return ''


def _powershell() -> str:
    """定位 powershell.exe：先按系统目录拼绝对路径，再退到 PATH

    绝对路径优先是因为 DETACHED_PROCESS 拉起时不继承我们的 PATH 环境，
    有些精简过的系统 PATH 里没有 WindowsPowerShell。
    """
    cand = os.path.join(os.environ.get('SystemRoot') or r'C:\Windows',
                        r'System32\WindowsPowerShell\v1.0\powershell.exe')
    if os.path.isfile(cand):
        return cand
    return 'powershell.exe'


def launch() -> dict:
    """拉起安装脚本（它自己等本进程退出）；调用方紧接着要退出程序

    用 PowerShell 跑 install.ps1，不用 cmd 批处理：一是 `tasklist | find` 那条
    管道会死锁，二是批处理必然弹一个黑窗口。这里带 CREATE_NO_WINDOW 拉起，
    用户什么都看不见；Windows 不会因为父进程退出就杀掉子进程，脚本能跑完。
    """
    ps = os.path.join(update_dir(), 'install.ps1')
    if not os.path.isfile(ps):
        return {'ok': False, 'error': '安装脚本不存在，请重新下载'}
    # 只用 CREATE_NO_WINDOW：验证过 DETACHED_PROCESS 会让 PowerShell 一起
    # 就退出、连脚本第一行都不执行（PowerShell 需要一个控制台）。
    # CREATE_NEW_PROCESS_GROUP 是为了不跟着父进程吃 Ctrl+C。
    flags = 0
    for name in ('CREATE_NO_WINDOW', 'CREATE_NEW_PROCESS_GROUP'):
        flags |= getattr(subprocess, name, 0)
    exe = _powershell()
    if not exe:
        return {'ok': False, 'error': '找不到 PowerShell，无法自动替换（Windows 自带的，可能被删了）'}
    argv = [exe, '-NoProfile', '-NonInteractive',
            '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
            '-File', ps, '-ParentPid', str(os.getpid())]
    ver = str(_state().get('latest') or '')
    if ver:
        argv += ['-Version', ver]
    try:
        subprocess.Popen(argv, cwd=update_dir(), creationflags=flags,
                         close_fds=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {'ok': True}
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'error': '启动安装脚本失败：%s' % e}


def install() -> dict:
    """一键：解压 → 写脚本 → 拉起 → 让主进程退出（前端会看到窗口关闭）"""
    if not settings.FROZEN:
        return {'ok': False, 'error': '源码模式不支持自动替换，请手动覆盖（真机请用打包版）'}
    r = prepare()
    if not r['ok']:
        return r
    lr = launch()
    if not lr['ok']:
        return lr
    st = _state()
    st['lastInstall'] = {'from': _current(), 'to': r['pending']['to'],
                         'at': _iso(), 'state': 'launching'}
    _save_state(st)
    _request_quit('安装更新 %s' % r['pending']['to'])
    return {'ok': True, 'launched': True, 'to': r['pending']['to']}


# ---------------------------------------------------------------- 退出钩子
def set_quit_hook(fn):
    """main 启动时注册：install() 需要让整个程序退出，而 api 层不能 import main"""
    _quit_hook[0] = fn


def _request_quit(reason='安装更新'):
    fn = _quit_hook[0]
    if not fn:
        return

    def run():
        # 留一点时间把 HTTP 响应写回前端：否则前端收到的是「连接被重置」，
        # 明明装成功了却弹一句失败。install.cmd 那边本来就在等进程退出。
        time.sleep(1.2)
        fn(reason)

    threading.Thread(target=run, daemon=True, name='install-quit').start()


# ---------------------------------------------------------------- 启动自检
def on_start() -> dict:
    """启动时结算上一次安装：替换成功就把 pending 清掉，失败则留痕

    返回值进 /api/app/status 的 lastInstall 字段，前端只在失败时提示。
    """
    p = _read_json(os.path.join(update_dir(), 'pending.json'), None)
    if not p:
        return {}
    cur = _current()
    to = str(p.get('to') or '')
    ok = bool(to) and cur == to
    info = {
        'from': p.get('from') or '',
        'to': to,
        'at': p.get('at') or '',
        'ok': ok,
        'state': 'done' if ok else 'stale',
    }
    st = _state()
    st['lastInstall'] = info
    _save_state(st)
    if ok:
        try:
            os.remove(os.path.join(update_dir(), 'pending.json'))
        except Exception:
            pass
        # 备份留着没意义了，顺手清掉
        shutil.rmtree(os.path.join(update_dir(), 'backup'), ignore_errors=True)
        try:
            os.remove(os.path.join(update_dir(), 'install-ok.txt'))
        except Exception:
            pass
    return info


def open_page(url='') -> dict:
    """用系统浏览器打开 Release 页（前端拿不到跨域页面，交给后端）"""
    st = _state()
    target = url or st.get('pageUrl') or ('https://github.com/%s/releases' % APP_REPO)
    try:
        webbrowser.open(target)
        return {'ok': True, 'url': target}
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'error': str(e), 'url': target}
