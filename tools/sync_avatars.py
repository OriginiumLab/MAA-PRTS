# -*- coding: utf-8 -*-
"""把干员头像从 PRTS Wiki 抓下来，压成 96px WebP，推到 pack-data 分发

为什么走 PRTS Wiki 而不是官方 CDN：
  - 官方资源没有稳定的公开索引，名字和 bundle 路径一一对应还要自己解包；
  - PRTS Wiki 的 ``头像_<干员名>.png`` 是社区维护的现成索引，带 MediaWiki
    API（``list=allimages&aiprefix=头像``），能分页拿全，不用爬 HTML；
  - 产物只有几 KB 一张，350 个干员全量约 2.2MB——放 pack-data 的 resources/avatars/，
    客户端用到哪张下哪张，安装包一个字节都不加。

用法：
    python tools/sync_avatars.py                 # 全量，写 build/avatars/
    python tools/sync_avatars.py --limit 30      # 先跑一小批看看
    python tools/sync_avatars.py --out D:\\tmp\\av
    python tools/sync_avatars.py --check-only    # 只报能不能连上、命中多少

产物：
    <out>/<charId>.webp
    <out>/avatar-index.json     {count, chars: {id: {sha256, bytes, name}}}

退出码：0 成功；2 有干员没匹配到头像（仍写出已拿到的）；1 网络/环境错误。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACK = os.path.join(ROOT, 'resources', 'arknights.json')

API = 'https://prts.wiki/api.php'
UA = 'MAA-PRTS-avatar-sync/1.0 (+https://github.com/OriginiumLab/MAA-PRTS)'
SIZE = 96
QUALITY = 80
THREADS = 8


def _get(url: str, timeout: float = 25) -> bytes:
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def list_avatars() -> dict:
    """PRTS Wiki 上所有「头像_*.png」：{文件名: 原图地址}"""
    out = {}
    cont = {}
    while True:
        q = {
            'action': 'query', 'list': 'allimages', 'aiprefix': '头像',
            'ailimit': '500', 'format': 'json',
        }
        q.update(cont)
        url = API + '?' + urllib.parse.urlencode(q)
        d = json.loads(_get(url).decode('utf-8'))
        for it in (d.get('query') or {}).get('allimages') or []:
            out[it.get('name') or ''] = it.get('url') or ''
        cont = d.get('continue') or {}
        if not cont:
            break
        time.sleep(0.3)
    return out


def pick(files: dict, name: str) -> str:
    """按干员名找头像地址：先精确，再带消歧后缀（头像_X(卫戍协议).png）"""
    exact = files.get('头像_%s.png' % name)
    if exact:
        return exact
    prefix = '头像_%s(' % name
    for fn, url in files.items():
        if fn.startswith(prefix) and fn.endswith(').png'):
            return url
    return ''


def to_webp(raw: bytes) -> bytes:
    from PIL import Image
    im = Image.open(io.BytesIO(raw)).convert('RGBA').resize((SIZE, SIZE), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, 'WEBP', quality=QUALITY, method=4)
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=os.path.join(ROOT, 'build', 'avatars'))
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--only', default='', help='逗号分隔的 charId，只处理这些')
    ap.add_argument('--force', action='store_true', help='已存在的也重下')
    ap.add_argument('--check-only', action='store_true')
    args = ap.parse_args()

    try:
        pack = json.load(open(PACK, encoding='utf-8'))
    except Exception as e:
        print('读不到 %s：%s' % (PACK, e))
        return 1
    chars = pack.get('chars') or {}
    ids = list(chars.keys())
    if args.only:
        want = {s.strip() for s in args.only.split(',') if s.strip()}
        ids = [i for i in ids if i in want]
    if args.limit:
        ids = ids[:args.limit]

    try:
        files = list_avatars()
    except Exception as e:
        print('拉 PRTS Wiki 列表失败：%s' % e)
        return 1
    print('PRTS Wiki 上 %d 个头像文件；本地干员 %d 个' % (len(files), len(ids)))

    os.makedirs(args.out, exist_ok=True)
    jobs = []
    missing = []
    for cid in ids:
        name = (chars.get(cid) or {}).get('name') or ''
        url = pick(files, name)
        if not url:
            missing.append('%s %s' % (cid, name))
            continue
        dst = os.path.join(args.out, cid + '.webp')
        if os.path.isfile(dst) and not args.force:
            continue
        jobs.append((cid, name, url))

    print('要处理 %d 张，没匹配到 %d 个' % (len(jobs), len(missing)))
    if args.check_only:
        for m in missing[:20]:
            print('  MISS', m)
        return 0 if jobs else 2

    index = {}
    old = os.path.join(args.out, 'avatar-index.json')
    if os.path.isfile(old):
        try:
            index = (json.load(open(old, encoding='utf-8')) or {}).get('chars') or {}
        except Exception:
            index = {}

    failed = []

    def work(job):
        cid, name, url = job
        try:
            raw = _get(url)
            webp = to_webp(raw)
            with open(os.path.join(args.out, cid + '.webp'), 'wb') as f:
                f.write(webp)
            return cid, name, webp
        except Exception as e:      # noqa: BLE001
            failed.append('%s %s: %s' % (cid, name, e))
            return None

    done = 0
    with ThreadPoolExecutor(max_workers=THREADS) as ex:
        for fut in as_completed([ex.submit(work, j) for j in jobs]):
            r = fut.result()
            if not r:
                continue
            cid, name, webp = r
            index[cid] = {
                'name': name,
                'bytes': len(webp),
                'sha256': hashlib.sha256(webp).hexdigest(),
            }
            done += 1
            if done % 25 == 0:
                print('  ... %d/%d' % (done, len(jobs)))

    total = sum(v.get('bytes') or 0 for v in index.values())
    meta = {
        'schema': 1,
        'generatedAt': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'size': SIZE,
        'count': len(index),
        'bytes': total,
        'chars': index,
    }
    with open(old, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
        f.write('\n')

    print('完成 %d 张，索引 %d 条，合计 %.1f MB'
          % (done, len(index), total / 1048576.0))
    if failed:
        print('失败 %d 个，前 10 个：' % len(failed))
        for f_ in failed[:10]:
            print('  ', f_)
    if missing:
        print('没匹配到头像 %d 个，前 10 个：' % len(missing))
        for m in missing[:10]:
            print('  ', m)
    return 0 if not failed and not missing else 2


if __name__ == '__main__':
    sys.exit(main())
