# -*- coding: utf-8 -*-
"""卫戍协议核心包下载引擎：records → 回退直链，断点续传 + 逐文件校验 + 原子落盘

核心包（约 61MB 的 .hbext = zip，解压 ~114MB）不随本程序分发，首次使用
卫戍协议模块时从这里下载（前端面板自动触发，全程进度可见）：

  1. 首选：本机小黑盒扩展记录
     %APPDATA%\\HeyboxDesktop\\desktop\\extension-artifacts\\v1\\records\\*.catalog.json
     里 package_id == ai-desktop.arknights-medal-farm 的记录（多条取版本最大），
     带 target.cdn_url（官方 CDN 直链，匿名可下）与 integrity.files 逐文件清单。
  2. 回退：硬编码的已验证直链（1.1.11 / 1.1.7 / 1.1.5 / 1.1.4，整包 sha256
     已锚定；无逐文件清单时退化为 zip 可解压 + core/service.mjs 存在 + 体量下限）。

落盘到 ``<运行根>/garrison/medal-farm-v<版本>/``（garrison.core_dir 的约定
位置之一，装好即被发现）。下载在后台线程跑，进度/阶段记在模块级 _DL，
``/api/garrison/status`` 顺路带给前端轮询；``.part`` 断点续传，失败可重试。

版本锚定：落盘前检查包内 README 仍含原许可声明（引导下载的依据）；新版
若缺失则停在需人工确认，不自动安装，回退用已验证的 v1.1.4。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
import zipfile

from . import applog, settings

PKG_ID = 'ai-desktop.arknights-medal-farm'
PKG_DIR_RE = re.compile(r'^medal-farm-v(\d+(?:\.\d+)*)$')

# 已验证直链（匿名可下载，整包 sha256 锚定）：按版本降序作回退链——
# 记录里的新版不在适配表时，装「表内最新」的直链版本而不是一味落到最老。
FALLBACKS = [
    {
        'version': '1.1.11',
        'url': ('https://static.max-c.com/heybox/desktop/extension/'
                'ai-desktop.arknights-medal-farm/version/1.1.11/'
                'f87f56c2110138158f44fce014dff979b36d26e206baf99b004e3e0953988fe1-'
                '20261124T111647922Z/ai-desktop.arknights-medal-farm-1.1.11.hbext'),
        'sha256': 'f87f56c2110138158f44fce014dff979b36d26e206baf99b004e3e0953988fe1',
        'bytes': 61131567,
        'files': None,
        'source': '官方直链（回退）',
    },
    {
        'version': '1.1.7',
        'url': ('https://static.max-c.com/heybox/desktop/extension/'
                'ai-desktop.arknights-medal-farm/version/1.1.7/'
                '6bc385116129d13b2e66679d100551950c6b03c793a92cfe1bc9f53f6db3166b-'
                '20261120T150609837Z/ai-desktop.arknights-medal-farm-1.1.7.hbext'),
        'sha256': '6bc385116129d13b2e66679d100551950c6b03c793a92cfe1bc9f53f6db3166b',
        'bytes': 61089122,
        'files': None,
        'source': '官方直链（回退）',
    },

    {
        'version': '1.1.5',
        'url': ('https://static.max-c.com/heybox/desktop/extension/'
                'ai-desktop.arknights-medal-farm/version/1.1.5/'
                '596196307afbdc4c102d66b85b7210ebb292612fd6f1e0d2d2c51ab83445c8b0-'
                '20261119T163854129Z/ai-desktop.arknights-medal-farm-1.1.5.hbext'),
        'sha256': '596196307afbdc4c102d66b85b7210ebb292612fd6f1e0d2d2c51ab83445c8b0',
        'bytes': 61083080,
        'files': None,
        'source': '官方直链（回退）',
    },
    {
        'version': '1.1.4',
        'url': ('https://static.max-c.com/heybox/desktop/extension/'
                'ai-desktop.arknights-medal-farm/version/1.1.4/'
                '970f8e97e13fd427abf63289c53c8a68765d4ac870f745c5f3c58250a25829ff-'
                '20261118T140851382Z/ai-desktop.arknights-medal-farm-1.1.4.hbext'),
        'sha256': '970f8e97e13fd427abf63289c53c8a68765d4ac870f745c5f3c58250a25829ff',
        'bytes': 61071704,
        'files': None,
        'source': '官方直链（回退）',
    },
]
FALLBACK = FALLBACKS[-1]   # 最老一档，兼容旧引用


MIN_TOTAL_BYTES = 50 * 1024 * 1024
MIN_FILE_COUNT = 100

_LOCK = threading.Lock()
_RUNNING = [False]
_DL = {
    'stage': 'idle',        # idle / connecting / downloading / verifying / done / error
    'done': 0, 'total': 0,  # 下载字节
    'files': 0, 'filesTotal': 0,
    'source': '', 'version': '', 'error': '',
    'startedAt': None, 'endedAt': None,
}


class NeedConfirmError(Exception):
    """包内容校验未过（原许可声明缺失）：停在需人工确认，不自动落盘"""


# ---------------------------------------------------------------- 路径与版本
def garrison_dir() -> str:
    return os.path.join(settings.project_root(), 'garrison')


def download_dir() -> str:
    return os.path.join(garrison_dir(), '.medal-farm-download')


def ver_key(v):
    parts = []
    for x in re.findall(r'\d+', str(v or '')):
        try:
            parts.append(int(x))
        except ValueError:
            break
    return tuple(parts or (0,))


def _installed_pkg_dir() -> str:
    """garrison/ 下已装的最高版本目录（空串 = 没装）"""
    best = None
    try:
        names = os.listdir(garrison_dir())
    except OSError:
        return ''
    for name in names:
        m = PKG_DIR_RE.match(name)
        if not m or not os.path.isfile(os.path.join(garrison_dir(), name, 'core', 'service.mjs')):
            continue
        if best is None or ver_key(m.group(1)) > ver_key(best[0]):
            best = (m.group(1), name)
    return best[1] if best else ''


# ---------------------------------------------------------------- 下载源
def record_candidates():
    base = os.environ.get('APPDATA') or ''
    rdir = os.path.join(base, 'HeyboxDesktop', 'desktop',
                        'extension-artifacts', 'v1', 'records')
    out, seen = [], set()
    try:
        names = os.listdir(rdir)
    except OSError:
        return out
    for name in names:
        if not name.endswith('.catalog.json'):
            continue
        try:
            with open(os.path.join(rdir, name), encoding='utf-8') as f:
                d = json.load(f)
        except Exception:
            continue
        t = d.get('target') or {}
        if t.get('package_id') != PKG_ID:
            continue
        ver = str(t.get('version') or '')
        url = str(t.get('cdn_url') or '')
        if not ver or not url or ver in seen:
            continue
        try:
            bytes_ = int(t.get('bytes') or 0) or None
        except (TypeError, ValueError):
            bytes_ = None
        seen.add(ver)
        out.append({'version': ver, 'url': url,
                    'sha256': str(t.get('artifact_sha256') or '').strip().lower() or None,
                    'bytes': bytes_,
                    'files': ((d.get('integrity') or {}).get('files')) or None,
                    'source': '小黑盒扩展记录'})
    out.sort(key=lambda r: ver_key(r['version']), reverse=True)
    return out


def candidates():
    # 全部下载源：本机记录 ∪ 已验证直链，按版本降序（同版本记录优先——
    # 带官方逐文件 sha256 清单，校验更严）
    recs = record_candidates()
    seen = {r['version'] for r in recs}
    out = recs + [dict(f) for f in FALLBACKS if f['version'] not in seen]
    out.sort(key=lambda r: ver_key(r['version']), reverse=True)
    return out

def _pick_candidate(installed: str):
    # 该装哪个：候选里第一个「适配表内且比已装新」的；没有返回 None。
    # 记录里只有表外新版（如 1.1.6）时，顺着回退链选表内最新的直链版本
    # （如 1.1.5），而不是一味落到最老的 1.1.4。
    for rec in candidates():
        if not _is_adapted(rec['version']):
            continue
        if installed and ver_key(rec['version']) <= ver_key(installed):
            continue
        return rec
    return None


# ---------------------------------------------------------------- 引擎
def _set(**kw):
    with _LOCK:
        _DL.update(kw)


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _download(url, dest, expected_bytes=None, expected_sha256=None,
              timeout=30, attempts=3):
    """流式下载（Range 断点续传 + 重试）；校验失败删 .part 重抛。"""
    part = dest + '.part'
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            have = os.path.getsize(part) if os.path.isfile(part) else 0
            headers = {'User-Agent': 'MAA-PRTS-garrison/1.0'}
            if have:
                headers['Range'] = 'bytes=%d-' % have
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                code = getattr(r, 'status', 200) or 200
                resume = code == 206
                if not resume and have:
                    have = 0
                total = expected_bytes
                if not total:
                    cl = r.headers.get('Content-Length')
                    total = (have + int(cl)) if (resume and cl and cl.isdigit()) \
                        else (int(cl) if cl and cl.isdigit() else None)
                done = have
                with open(part, 'ab' if resume else 'wb') as f:
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        _set(done=done, total=total or done)
            size = os.path.getsize(part)
            if expected_bytes and size != expected_bytes:
                os.remove(part)
                raise RuntimeError('下载大小不符：期望 %d，实得 %d' % (expected_bytes, size))
            if expected_sha256 and _sha256_file(part) != expected_sha256.lower():
                os.remove(part)
                raise RuntimeError('整包校验失败，下载可能不完整，请重试')
            os.replace(part, dest)
            return dest
        except RuntimeError:
            raise
        except Exception as e:                       # noqa: BLE001 网络/中断：保留 .part 续传
            last_err = e
            if attempt >= attempts:
                break
            time.sleep(1.5 * attempt)
    raise RuntimeError('下载失败（重试 %d 次）：%s' % (attempts, last_err))


def _license_ok(extract_dir) -> bool:
    """落盘前置校验：包内 README 仍含原许可声明才继续（引导下载的依据）"""
    for rel in ('README.md', 'core/README.md', 'extension.manifest.json'):
        p = os.path.join(extract_dir, *rel.split('/'))
        try:
            with open(p, encoding='utf-8', errors='ignore') as f:
                if 'GPL-3.0' in f.read().upper():
                    return True
        except OSError:
            continue
    return False


def _extract_verify(hbext, out_dir, manifest=None):
    """解压 + 逐文件校验（大小+sha256，清单全覆盖）；无清单退化校验体量。"""
    want = {}
    for f in manifest or []:
        rel = str(f.get('relativePath') or '').replace('\\', '/')
        if rel:
            want[rel] = (int(f.get('bytes') or 0), str(f.get('sha256') or '').lower())
    root_real = os.path.realpath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    got, total_bytes = {}, 0
    with zipfile.ZipFile(hbext) as zf:
        names = [n.replace('\\', '/') for n in zf.namelist() if not n.endswith('/')]
        _set(files=0, filesTotal=len(names))
        for i, name in enumerate(names):
            dest = os.path.realpath(os.path.join(out_dir, *name.split('/')))
            try:
                inside = os.path.commonpath([root_real, dest]) == root_real
            except ValueError:
                inside = False
            if not inside or os.path.basename(name) in ('', '.', '..'):
                raise RuntimeError('包内路径异常，拒收：%s' % name)
            info = zf.getinfo(name)
            os.makedirs(os.path.dirname(dest) or dest, exist_ok=True)
            h = hashlib.sha256()
            with open(dest, 'wb') as f_out, zf.open(info) as src:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    f_out.write(chunk)
                    h.update(chunk)
                    total_bytes += len(chunk)
            got[name] = (info.file_size, h.hexdigest())
            _set(files=i + 1)
    if 'core/service.mjs' not in got:
        raise RuntimeError('包里没有 core/service.mjs，不是速刷核心包')
    if want:
        missing = [k for k in want if k not in got]
        if missing:
            raise RuntimeError('清单里的文件包里没有（%d 个），如 %s' % (len(missing), missing[0]))
        bad = [rel for rel, (size, dig) in want.items()
               if got[rel][0] != size or (dig and got[rel][1] != dig)]
        if bad:
            raise RuntimeError('%d 个文件校验失败，如 %s' % (len(bad), bad[0]))
        # 清单外的额外文件允许（签名信封等本就不进清单），完整性由整包校验兜住
    elif len(got) < MIN_FILE_COUNT or total_bytes < MIN_TOTAL_BYTES:
        raise RuntimeError('包体量异常（%d 个文件 / %d 字节），拒收' % (len(got), total_bytes))
    return len(got)


def _adapted_versions():
    """patches.json 里硬编码的「已适配核心版号」表；读不到按无限制处理

    更新器只升到表内版本（表外新版等补丁适配后再放行），比在暂存区上
    数补丁命中更直白，也省一次 61MB 的无效下载。
    """
    p = os.path.join(settings.RES, 'resources', 'garrison', 'patches.json')
    try:
        with open(p, encoding='utf-8') as f:
            v = (json.load(f) or {}).get('adaptedVersions')
        return [str(x) for x in v] if isinstance(v, list) else None
    except (OSError, ValueError):
        return None


def _is_adapted(version: str) -> bool:
    allowed = _adapted_versions()
    return allowed is None or str(version) in allowed


def _install(hbext, version, manifest=None):
    """解压校验 → 前置校验 → 补丁兼容门禁 → 原子落位；失败不伤旧版。

    门禁：上游换版本常把混淆产物整个重排（v1.1.4 → v1.1.5 连文案都换了），
    我们的补丁一条都命不中＝这个版本没适配过，升上去等于把已修的 OCR
    容错/弹窗识别全丢掉。宁可停在旧版等适配——全新安装同理会顺着回退
    链落到已适配的版本上。
    """
    pkg = os.path.join(garrison_dir(), 'medal-farm-v%s' % version)
    stage = os.path.join(download_dir(), 'stage')
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage, exist_ok=True)
    try:
        _extract_verify(hbext, stage, manifest)
        if not _license_ok(stage):
            raise NeedConfirmError(
                '新版本 v%s 内容校验未通过，已停止自动安装。可点重试改用已验证的 v%s。'
                % (version, FALLBACK['version']))
        had_old = os.path.exists(pkg)
        bak = pkg + '.replacing'
        if had_old:
            os.replace(pkg, bak)
        try:
            os.replace(stage, pkg)
        except OSError:
            if had_old:
                os.replace(bak, pkg)
            raise
        if had_old:
            shutil.rmtree(bak, ignore_errors=True)
        # 新版已就位：清掉其它旧版本（删不掉的留着不影响）
        for name in os.listdir(garrison_dir()):
            m = PKG_DIR_RE.match(name)
            if m and m.group(1) != version:
                shutil.rmtree(os.path.join(garrison_dir(), name), ignore_errors=True)
        return pkg
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _cleanup(keep_part=True):
    d = download_dir()
    if not os.path.isdir(d):
        return
    shutil.rmtree(os.path.join(d, 'stage'), ignore_errors=True)
    for name in os.listdir(d):
        if name == 'lock' or (keep_part and name.endswith('.part')):
            continue
        if name.endswith('.hbext') or name.endswith('.part'):
            try:
                os.remove(os.path.join(d, name))
            except OSError:
                pass


# ---------------------------------------------------------------- 对外
def state() -> dict:
    with _LOCK:
        return dict(_DL)


def start() -> dict:
    """启动（或继续）下载/升级；已在跑/已是最新时原样返回。返回 state 快照。

    没装过 → 走下载链装「表内最新」（记录 ∪ 已验证直链）；装着旧版且
    候选里有「适配表内且更新」的版本（_pick_candidate）→ 升级；升级
    失败不影响已装版本继续使用。
    """
    with _LOCK:
        running = _RUNNING[0]
    if running:
        return state()
    cur = _installed_pkg_dir()
    if cur and _pick_candidate(cur) is None:
        _set(stage='done', error='', endedAt=time.time())
        return state()
    threading.Thread(target=_run, daemon=True, name='garrison-dl').start()
    return state()


def check_update() -> None:
    """看一眼有没有可升的目标（适配表内且比已装新），有就后台升级。

    触发时机由调用方决定：程序启动时（main）、速刷任务全部停止后
    （garrison.stop）。没有可升目标就是一次本地扫描，零成本；记录里的
    新版不在适配表时记一行日志、不动已装版本。
    """
    with _LOCK:
        running = _RUNNING[0]
    if running:
        return
    cur = _installed_pkg_dir()
    if not cur:
        return
    target = _pick_candidate(cur)
    if not target:
        recs = record_candidates()
        if recs and ver_key(recs[0]['version']) > ver_key(cur) \
                and not _is_adapted(recs[0]['version']):
            applog.log('[卫戍协议] 核心新版本 v%s 尚无对应补丁（已适配：%s），暂不更新'
                       % (recs[0]['version'], '/'.join(_adapted_versions() or ['?'])))
        return
    applog.log('[卫戍协议] 检测到核心新版本（本机 v%s → v%s），后台升级'
               % (cur, target['version']))
    threading.Thread(target=_run, daemon=True, name='garrison-upgrade').start()


def _run():
    with _LOCK:
        if _RUNNING[0]:
            return
        _RUNNING[0] = True
    _set(stage='connecting', error='', done=0, total=0, files=0, filesTotal=0,
         startedAt=time.time(), endedAt=None)
    lock_dir = ''
    tried = []
    try:
        os.makedirs(download_dir(), exist_ok=True)
        lock_dir = os.path.join(download_dir(), 'lock')
        if os.path.isdir(lock_dir):
            try:
                stale = time.time() - os.path.getmtime(lock_dir) > 15 * 60
            except OSError:
                stale = True
            if not stale:
                raise RuntimeError('另一个实例正在下载核心包，稍候再试')
            shutil.rmtree(lock_dir, ignore_errors=True)
        os.mkdir(lock_dir)

        installed = _installed_pkg_dir()
        for rec in candidates():
            if not _is_adapted(rec['version']):
                continue   # 版号不在适配表：等补丁，不下载
            if installed and ver_key(rec['version']) <= ver_key(installed):
                continue   # 防降级：已装新版时旧记录/回退直链一律不装
            label = 'v%s（%s）' % (rec['version'], rec['source'])
            try:
                _set(stage='connecting', source=rec['source'], version=rec['version'],
                     done=0, total=rec.get('bytes') or 0)
                hbext = os.path.join(download_dir(), 'medal-farm-%s.hbext' % rec['version'])
                _download(rec['url'], hbext,
                          expected_bytes=rec.get('bytes'),
                          expected_sha256=rec.get('sha256'))
                _set(stage='verifying', files=0, filesTotal=0)
                _install(hbext, rec['version'], manifest=rec.get('files'))
                try:
                    os.remove(hbext)
                except OSError:
                    pass
                _set(stage='done', error='', endedAt=time.time())
                applog.log('[卫戍协议] 核心包 v%s 下载安装完成（%s）' % (rec['version'], label))
                return
            except NeedConfirmError as e:
                # 校验门禁：不落盘；换下一个源（回退直链是已验证版本）
                tried.append('%s：%s' % (label, e))
            except Exception as e:                    # noqa: BLE001
                tried.append('%s：%s' % (label, e))
        raise RuntimeError('所有下载源都失败了：\n' + '\n'.join('· ' + t for t in tried))
    except Exception as e:                            # noqa: BLE001
        _set(stage='error', error=str(e), endedAt=time.time())
        applog.log('[卫戍协议] 核心包下载失败：%s' % e)
    finally:
        if lock_dir:
            shutil.rmtree(lock_dir, ignore_errors=True)
        _cleanup()
        with _LOCK:
            _RUNNING[0] = False
