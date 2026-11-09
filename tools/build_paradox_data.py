# -*- coding: utf-8 -*-
"""从游戏资源目录生成 resources/paradox.json（悖论模拟清单）

数据来源（ArknightsGameResource 仓库，纯 json，不需要任何图片）：
  gamedata/excel/handbook_info_table.json  → handbookStageData
      每个有悖论模拟的干员一条：
      {charId, stageId: "mem_xxx_1", name: 关卡名, unlockParam: [{unlockType:1, unlockParam1:精英化, unlockParam2:等级}]}
  gamedata/excel/character_table.json      → 干员中文名

产物只有「干员 + 关卡代号 + 关卡名 + 解锁条件」，316 条约 30 KB，直接打进 exe。

为什么不需要截图/图片哈希：
  解锁条件只有「精英化阶段 + 等级」，而账号快照（森空岛同步）里每个干员的
  elite / level 都有，所以「哪些悖论能打」可以完全推算出来。

用法：
  python tools/build_paradox_data.py
  python tools/build_paradox_data.py --src "E:/.github-ARK/ArknightsGameResource"
  python tools/build_paradox_data.py --src <目录> --out resources/paradox.json
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_SRC = r'E:\.github-ARK\ArknightsGameResource'
DEFAULT_OUT = os.path.join(ROOT, 'resources', 'paradox.json')


def load_json(path):
    with io.open(path, encoding='utf-8') as f:
        return json.load(f)


def _load_make_pack():
    """版本读取用 make_pack 的单点实现（此前两份拷贝截断长度 32/64 已分叉，#63）"""
    import importlib.util
    path = os.path.join(ROOT, 'resources', 'make_pack.py')
    spec = importlib.util.spec_from_file_location('make_pack', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def game_version(src):
    """资源库里的 version 文件（委托 make_pack.read_game_version，截断口径一致）"""
    return _load_make_pack().read_game_version(os.path.join(src, 'gamedata', 'excel'))


def build(src, generated_at='', source_commit=''):
    """生成悖论清单数据

    generated_at 传了就用它（CI 传上游提交时间），没传才用当前时间——
    只是显示用；客户端判断「要不要更新」看的是 fingerprint（纯内容哈希）。
    """
    hb = os.path.join(src, 'gamedata', 'excel', 'handbook_info_table.json')
    ct = os.path.join(src, 'gamedata', 'excel', 'character_table.json')
    for p in (hb, ct):
        if not os.path.isfile(p):
            sys.exit('找不到：%s（用 --src 指定游戏资源目录）' % p)

    stages = load_json(hb).get('handbookStageData') or {}
    chars = load_json(ct)
    print('悖论模拟关卡 %d 条，干员表 %d 条' % (len(stages), len(chars)))

    out, no_name, no_stage, dup = {}, [], [], {}
    for cid, row in sorted(stages.items()):
        stage = row.get('stageId') or ''
        if not stage:
            no_stage.append(cid)
            continue
        if stage in dup:
            print('！关卡代号重复：%s 同时属于 %s 和 %s' % (stage, dup[stage], cid))
        dup[stage] = cid

        info = chars.get(cid) or {}
        name = info.get('name') or ''
        if not name:
            no_name.append(cid)

        up = (row.get('unlockParam') or [{}])[0]
        try:
            elite = int(up.get('unlockParam1') or 0)
            level = int(up.get('unlockParam2') or 0)
        except (TypeError, ValueError):
            elite, level = 2, 1

        out[cid] = {
            'name': name,
            'pname': row.get('name') or '',
            'stage': stage,
            'elite': elite,
            'level': level,
        }

    data = {
        'schema': 2,
        'generatedAt': generated_at or time.strftime('%Y-%m-%dT%H:%M:%S'),
        'gameVersion': game_version(src),
        'sourceCommit': source_commit or '',
        'source': os.path.basename(src.rstrip('\\/')) or src,
        'count': len(out),
        # 内容指纹：客户端拿它和本地那份比，决定要不要下载（与日期无关）
        'fingerprint': hashlib.sha1(
            json.dumps(out, sort_keys=True, ensure_ascii=False,
                       separators=(',', ':')).encode('utf-8')
        ).hexdigest()[:8],
        'chars': out,
    }
    return data, {'no_name': no_name, 'no_stage': no_stage}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', default=DEFAULT_SRC, help='ArknightsGameResource 目录')
    ap.add_argument('--out', default=DEFAULT_OUT, help='输出 json 路径')
    ap.add_argument('--source-commit', default='', help='上游提交 sha（自动同步用）')
    ap.add_argument('--commit-time', default='', help='上游提交时间（写进 generatedAt）')
    args = ap.parse_args()

    data, diag = build(args.src, generated_at=args.commit_time,
                       source_commit=args.source_commit)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with io.open(args.out, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(data, f, ensure_ascii=False, indent=0, sort_keys=True)

    size = os.path.getsize(args.out)
    print('已写出 %s（%d 条，%.1f KB）｜指纹 %s' % (
        args.out, data['count'], size / 1024.0, data['fingerprint']))
    print('解锁条件分布 精英化:', dict(Counter(v['elite'] for v in data['chars'].values())))
    print('解锁条件分布 等级  :', dict(Counter(v['level'] for v in data['chars'].values())))
    if diag['no_name']:
        print('！以下干员在 character_table 里查不到名字：%s' % ', '.join(diag['no_name'][:10]))
    if diag['no_stage']:
        print('！以下干员没有 stageId：%s' % ', '.join(diag['no_stage'][:10]))
    if not (diag['no_name'] or diag['no_stage']):
        print('校验通过：干员名、关卡代号均完整')


if __name__ == '__main__':
    main()
