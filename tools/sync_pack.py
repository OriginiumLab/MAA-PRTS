# -*- coding: utf-8 -*-
"""把上游游戏资源库同步成本项目的资源包（CI 与本地共用同一份逻辑）

做的事：
  1. 问 GitHub 要上游 main 的提交 sha 和提交时间（1 次 API 调用）
  2. 和本地包里的 sourceCommit 比，一样就直接退出（零下载、零提交）
  3. 按那个 sha 定点下载 gamedata/excel 下 9 个 json（不 clone，上游仓库 7 GB）
  4. 调 resources/make_pack.py 和 tools/build_paradox_data.py 生成两个包
  5. 条数只增不减才放行（arknights 各字段 + 悖论条数），写出 resources/pack-manifest.json 给客户端用

用法：
    python tools/sync_pack.py                       # 本地手动同步
    python tools/sync_pack.py --force               # 忽略 sha 相同，强制重算
    python tools/sync_pack.py --sha 6e5c0541...     # 指定上游提交
    python tools/sync_pack.py --check-only          # 只报告有没有更新

退出码：
    0  成功（有新内容 / 上游没变）
    2  校验失败（条数变少等），此时不会改动任何文件
    1  网络或环境错误
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

UPSTREAM = 'yuanyan3060/ArknightsGameResource'
# 客户端按这个仓库地址拼下载 URL；改名后要一起改（和 releases 页面一致）
PACK_REPO = 'OriginiumLab/MAA-PRTS'
# 资源数据单独走一条分支，不进 main：main 的规则集强制走 PR + 审批，
# bot 直推会被拒。数据分支没有保护，客户端按这个分支名拼 raw/jsDelivr 地址。
PACK_BRANCH = os.environ.get('MAA_PRTS_PACK_BRANCH') or 'pack-data'

FILES = [
    'version',
    'gamedata/excel/character_table.json',
    'gamedata/excel/char_patch_table.json',
    'gamedata/excel/skill_table.json',
    'gamedata/excel/uniequip_table.json',
    'gamedata/excel/stage_table.json',
    'gamedata/excel/zone_table.json',
    'gamedata/excel/activity_table.json',
    'gamedata/excel/handbook_info_table.json',
]

MIRRORS = [
    'https://raw.githubusercontent.com/%s/%s/%s',
    'https://gh-proxy.com/https://raw.githubusercontent.com/%s/%s/%s',
    'https://ghfast.top/https://raw.githubusercontent.com/%s/%s/%s',
]

UA = 'MAA-PRTS-packsync/1.0 (+https://github.com/%s)' % PACK_REPO


def log(msg=''):
    print(msg, flush=True)


def http_get(url, timeout=30, retries=2):
    """带 UA / 重试的 GET，返回 bytes"""
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001 - 网络错误名目多，统一重试
            last = e
            if i < retries:
                time.sleep(1 + i * 2)
    raise RuntimeError('%s -> %s' % (url, last))


def upstream_head(repo):
    """上游 main 的 (sha, 提交时间, 提交说明)"""
    url = 'https://api.github.com/repos/%s/commits/main' % repo
    token = os.environ.get('GITHUB_TOKEN') or ''
    headers = {'User-Agent': UA, 'Accept': 'application/vnd.github+json'}
    if token:
        headers['Authorization'] = 'Bearer %s' % token
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode('utf-8'))
    except Exception as e:
        raise RuntimeError('取上游最新提交失败：%s' % e)
    commit = d.get('commit') or {}
    when = ((commit.get('committer') or {}).get('date')
            or (commit.get('author') or {}).get('date') or '')
    msg = (commit.get('message') or '').splitlines()[0][:100]
    sha = d.get('sha') or ''
    if not sha:
        raise RuntimeError('上游返回里没有 sha')
    return sha, when, msg


def download_one(rel, sha, work, repo):
    """下载单个文件（逐个镜像回退），返回字节数"""
    dest = os.path.join(work, *rel.split('/'))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    errors = []
    for tpl in MIRRORS:
        url = tpl % (repo, sha, rel)
        try:
            raw = http_get(url)
        except Exception as e:  # 换下一个镜像
            errors.append(str(e))
            continue
        if len(raw) < (8 if rel == 'version' else 1024):
            errors.append('%s 内容过短（%d 字节）' % (url, len(raw)))
            continue
        if rel.endswith('.json'):
            try:
                json.loads(raw.decode('utf-8'))
            except Exception as e:
                errors.append('%s 不是合法 json：%s' % (url, e))
                continue
        with open(dest, 'wb') as f:
            f.write(raw)
        return rel, len(raw)
    raise RuntimeError('下载失败：%s\n%s' % (rel, '\n'.join(errors)))


def download_all(sha, work, repo):
    """按 sha 并行下载全部文件到 work/，返回总字节数"""
    t0 = time.time()
    total = 0
    # 4 路并发：单文件 60 MB 的串行下载要两分钟，并发后一般 30 秒内
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(download_one, rel, sha, work, repo): rel for rel in FILES}
        for fut in as_completed(futs):
            rel, size = fut.result()
            total += size
            log('  ✓ %-42s %7.1f KB' % (rel, size / 1024.0))
    log('  下载完成：%.1f MB，%.1f 秒' % (total / 1048576.0, time.time() - t0))
    return total


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read_json(path):
    try:
        with io.open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def write_json(path, obj, separators=(',', ':')):
    tmp = path + '.tmp'
    with io.open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(obj, f, ensure_ascii=False, separators=separators)
        f.write('\n')
    os.replace(tmp, path)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def check_counts(old, new):
    """条数只增不减；返回变少的字段列表"""
    lost = []
    old_counts = (old or {}).get('counts') or {}
    for k, v in (new or {}).items():
        prev = old_counts.get(k)
        if prev is None:
            log('  %-16s %-6d 新增' % (k, v))
        elif v > prev:
            log('  %-16s %-6d +%d' % (k, v, v - prev))
        elif v < prev:
            log('  %-16s %-6d %+d  <- 变少' % (k, v, v - prev))
            lost.append(k)
        else:
            log('  %-16s %-6d 持平' % (k, v))
    return lost


def build_manifest(root, sha, commit_time, game_version):
    packs = {}
    for key, rel, local in (
        ('arknights', 'resources/arknights.json', 'resources/arknights.json'),
        ('paradox', 'resources/paradox.json', 'resources/paradox.json'),
    ):
        path = os.path.join(root, local)
        if not os.path.isfile(path):
            continue
        d = read_json(path)
        fp = d.get('fingerprint') or ''
        urls = []
        for base in ('https://cdn.jsdelivr.net/gh/%s@%s/',
                     'https://raw.githubusercontent.com/%s/%s/',
                     'https://gh-proxy.com/https://raw.githubusercontent.com/%s/%s/'):
            urls.append((base % (PACK_REPO, PACK_BRANCH)) + rel + '?v=' + (fp or sha[:8]))
        packs[key] = {
            'file': os.path.basename(local),
            'version': d.get('version') or d.get('gameVersion') or '',
            'fingerprint': fp,
            'size': os.path.getsize(path),
            'sha256': sha256_of(path),
            'counts': d.get('counts') or ({'count': d.get('count')} if d.get('count') else {}),
            'urls': urls,
        }
    return {
        'schema': 1,
        'generatedAt': commit_time or time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'source': {'repo': UPSTREAM, 'commit': sha, 'commitTime': commit_time,
                   'gameVersion': game_version},
        'packs': packs,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description='同步上游游戏资源，生成本项目资源包')
    ap.add_argument('--repo', default=UPSTREAM, help='上游仓库 owner/name')
    ap.add_argument('--sha', default='', help='指定上游提交；默认取 main 最新')
    ap.add_argument('--root', default=ROOT, help='本项目根目录')
    ap.add_argument('--work', default='', help='下载临时目录（默认系统临时目录）')
    ap.add_argument('--force', action='store_true', help='sha 相同也重新生成')
    ap.add_argument('--check-only', action='store_true', help='只报告有没有更新')
    ap.add_argument('--commit-time', default='', help='覆盖上游提交时间（离线/补录用）')
    ap.add_argument('--keep-work', action='store_true', help='保留临时目录便于排查')
    ap.add_argument('--from-dir', default='',
                    help='用本地上游目录代替下载（离线调试用，不会删除该目录）')
    args = ap.parse_args(argv)

    root = os.path.abspath(args.root)
    pack_path = os.path.join(root, 'resources', 'arknights.json')
    paradox_path = os.path.join(root, 'resources', 'paradox.json')
    manifest_path = os.path.join(root, 'resources', 'pack-manifest.json')

    src_dir = os.path.abspath(args.from_dir) if args.from_dir else ''
    if src_dir and not os.path.isdir(os.path.join(src_dir, 'gamedata', 'excel')):
        log('[错误] --from-dir 里没有 gamedata/excel：%s' % src_dir)
        return 1

    if src_dir and not args.sha:
        # 离线模式：直接读本地仓库的 HEAD
        try:
            import subprocess
            sha = subprocess.check_output(
                ['git', '-C', src_dir, 'rev-parse', 'HEAD'], text=True).strip()
            commit_time = subprocess.check_output(
                ['git', '-C', src_dir, 'show', '-s', '--format=%cI', 'HEAD'],
                text=True).strip()
            msg = '(本地目录)'
        except Exception as e:
            log('[错误] 读本地仓库 HEAD 失败，请用 --sha 指定：%s' % e)
            return 1
        log('上游提交：%s（--from-dir）' % sha[:12])
    elif args.sha:
        sha, commit_time, msg = args.sha, args.commit_time or '', ''
        log('上游提交：%s（手动指定）' % sha[:12])
    else:
        sha, commit_time, msg = upstream_head(args.repo)
        log('上游提交：%s  %s' % (sha[:12], msg))
        if commit_time:
            log('提交时间：%s' % commit_time)
    if args.commit_time:
        commit_time = args.commit_time

    old = read_json(pack_path)
    old_sha = old.get('sourceCommit') or ''
    if old_sha == sha and not args.force:
        log('上游没有新提交（本地已是 %s），什么都不用做' % sha[:12])
        return 0
    log('本地资源：%s / %s → 上游：%s' % (
        old.get('version') or '?', (old_sha or '未知')[:12], sha[:12]))
    if args.check_only:
        log('有新内容（--check-only，未下载）')
        return 0

    if src_dir:
        work, cleanup = src_dir, False
    else:
        work = args.work or tempfile.mkdtemp(prefix='maaprts-pack-')
        cleanup = not args.keep_work
        if not cleanup:
            os.makedirs(work, exist_ok=True)

    outdir = ''
    try:
        if src_dir:
            log('')
            log('使用本地上游目录：%s' % work)
        else:
            log('')
            log('下载上游 excel 表 → %s' % work)
            download_all(sha, work, args.repo)
        excel = os.path.join(work, 'gamedata', 'excel')

        # 中间产物单独放临时目录：--from-dir 时 work 是用户的克隆，绝不能往里写
        outdir = tempfile.mkdtemp(prefix='maaprts-out-')

        make_pack = _load_module('make_pack', os.path.join(root, 'resources', 'make_pack.py'))
        paradox_build = _load_module('build_paradox_data',
                                     os.path.join(root, 'tools', 'build_paradox_data.py'))
        game_version = make_pack.read_game_version(excel)
        log('')
        log('游戏版本：%s' % (game_version or '（上游没有 version 文件）'))

        log('')
        log('生成 arknights.json')
        tmp_pack = os.path.join(outdir, 'arknights.json')
        base = tmp_pack if os.path.isfile(pack_path) else None
        if base:
            shutil.copyfile(pack_path, tmp_pack)   # 只为继承旧包里的分支中文名
        data = make_pack.build(
            excel, subprof_path=None, base_path=base,
            game_version=game_version, source_commit=sha,
            generated_at=commit_time or '')
        lost = check_counts(old, data.get('counts') or {})
        if lost:
            log('')
            log('[失败] 这些字段比现有包少：%s' % '、'.join(lost))
            log('       上游可能只推了部分表，等下一次提交再同步')
            return 2
        for key, label in (('chars', '干员'), ('stages', '关卡')):
            if not (data.get('counts') or {}).get(key):
                log('[失败] %s 为空，资源表没读对' % label)
                return 2
        write_json(tmp_pack, data)

        log('')
        log('生成 paradox.json')
        pdata, _diag = paradox_build.build(work, generated_at=commit_time or '',
                                           source_commit=sha)
        if not pdata.get('count'):
            log('[失败] 悖论清单为空')
            return 2
        # 悖论条数与旧包只增不减：客户端 packsync._verify 对条数变少是拒绝
        # 落盘的，服务端不拦的话每次启动都「资源已更新→校验失败」循环（#49）
        prev_pc = (read_json(paradox_path) or {}).get('count') or 0
        new_pc = pdata.get('count') or 0
        if prev_pc and new_pc < prev_pc:
            log('[失败] 悖论条数变少：%d → %d（上游可能只推了部分表，等补全再同步）'
                % (prev_pc, new_pc))
            return 2
        log('悖论条数：%d%s' % (new_pc, '（+%d）' % (new_pc - prev_pc) if new_pc > prev_pc else ''))

        log('')
        log('落盘')
        shutil.copyfile(tmp_pack, pack_path)
        # 与 build_paradox_data.py 的写法保持一致：indent=0 + sort_keys（保证逐字节可比）
        with io.open(paradox_path, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(pdata, f, ensure_ascii=False, indent=0, sort_keys=True)

        manifest = build_manifest(root, sha, commit_time, game_version)
        with io.open(manifest_path, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write('\n')

        log('  %-18s %s  %s  %.1f KB' % (
            'arknights.json', data['version'], data['fingerprint'],
            os.path.getsize(pack_path) / 1024.0))
        log('  %-18s %s  %s  %.1f KB' % (
            'paradox.json', pdata.get('gameVersion') or '', pdata['fingerprint'],
            os.path.getsize(paradox_path) / 1024.0))
        log('  %-18s %d 个资源' % ('pack-manifest.json', len(manifest['packs'])))
        log('')
        log('完成：上游 %s 已同步（%s）' % (sha[:12], data['version']))
        return 0
    except RuntimeError as e:
        log('')
        log('[错误] %s' % e)
        return 1
    finally:
        if outdir:
            shutil.rmtree(outdir, ignore_errors=True)
        if cleanup:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
