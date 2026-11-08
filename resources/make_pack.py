# -*- coding: utf-8 -*-
"""把 ArknightsGameResource 的 excel 表压成一个内置资源包

用途：MAA-PRTS 需要干员名、星级、职业、技能名、模组名、关卡表来本地完成
     「缺卡计算」和「关卡代号 → stage_id」的换算。原始 excel 表合计 50 MB，
     直接塞进仓库太重，这里只保留用得到的字段，压到 1 MB 以内。

用法：
    python resources/make_pack.py --excel "D:\\ArknightsGameResource\\gamedata\\excel"
    python resources/make_pack.py --excel ... --out resources/arknights.json
    python resources/make_pack.py --excel ... --subprof config/subprof_cache.json

只在游戏大版本更新后需要重跑一次；平时程序只读 arknights.json，不碰原始表。
CI 自动同步走 tools/sync_pack.py，它直接 import 本文件的 build()。

幂等约定（自动同步的前提，改动前先看这里）：
    version      来自上游仓库根的 version 文件（游戏版本日期），不是"生成日期"，
                 同一份上游数据任何时候生成都得到同一个 version
    fingerprint  只由内容决定（干员/技能/模组/关卡/区域/活动），日期和分支名缓存
                 都不参与，所以在任意机器上重跑结果一致
    generatedAt  调用方给了就照抄（CI 传上游提交时间），没给才用当前时间
    数据没变 → 产物除 generatedAt 外逐字节相同，git diff 能直接判出"无需提交"
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def load(path, default=None):
    try:
        with io.open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def read_game_version(excel_dir):
    """上游仓库根目录的 version 文件（gamedata/excel 上两级）

    老版本是纯文本 `26-10-29-15-36-11-33649006`，新版本可能是
    `{"versionId": "..."}` 的 json，两种都认；读不到返回空串。
    """
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(excel_dir))), 'version')
    try:
        with io.open(p, encoding='utf-8') as f:
            raw = f.read().strip()
    except Exception:
        return ''
    try:
        v = (json.loads(raw) or {}).get('versionId')
        if v:
            return str(v)
    except Exception:
        pass
    return raw[:64]


def norm_version(game_version, fallback=''):
    """游戏版本串 → 界面展示的 2026.10.29

    '26-10-29-15-36-11-33649006' 是上游的「客户端版本-时间戳」，取前三位；
    也兼容 2026-10-29 / 20261029 这类写法。认不出就用 fallback。
    """
    s = str(game_version or '')
    m = re.search(r'(?<!\d)(\d{2})-(\d{2})-(\d{2})(?!\d)', s)
    if m:
        return '20%s.%s.%s' % m.groups()
    m = re.search(r'(?<!\d)(\d{4})[-.]?(\d{2})[-.]?(\d{2})(?!\d)', s)
    if m:
        return '%s.%s.%s' % m.groups()
    return fallback


def content_fingerprint(chars, skills, equips, stages, zones, activities):
    """内容指纹：只由数据决定

    日期、分支名缓存（subProfessions 来自本地学习，各机器不一样）都不参与，
    否则「同一天重复生成」或「换台机器生成」都会让用户白弹一次更新提示。
    """
    payload = {'chars': chars, 'skills': skills, 'equips': equips,
               'stages': stages, 'zones': zones, 'activities': activities}
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:8]


def build(excel_dir, subprof_path=None, base_path=None, game_version='',
          source_commit='', generated_at=''):
    chars_src = load(os.path.join(excel_dir, 'character_table.json')) or {}
    patch = (load(os.path.join(excel_dir, 'char_patch_table.json')) or {}).get('patchChars') or {}
    skills_src = load(os.path.join(excel_dir, 'skill_table.json')) or {}
    equips_src = (load(os.path.join(excel_dir, 'uniequip_table.json')) or {}).get('equipDict') or {}
    stages_src = (load(os.path.join(excel_dir, 'stage_table.json')) or {}).get('stages') or {}
    zones_src = (load(os.path.join(excel_dir, 'zone_table.json')) or {}).get('zones') or {}
    acts_src = (load(os.path.join(excel_dir, 'activity_table.json')) or {}).get('basicInfo') or {}

    if not chars_src:
        raise SystemExit('character_table.json 读不到，检查 --excel 路径：%s' % excel_dir)

    chars = {}
    for cid, v in list(chars_src.items()) + list(patch.items()):
        if not cid.startswith('char_'):
            continue
        chars[cid] = {
            'name': v.get('name') or cid,
            'rarity': int(v.get('rarity') or 0),          # 0 基，6 星 = 5
            'profession': v.get('profession') or '',
            'subProfessionId': v.get('subProfessionId') or '',
            'isNotObtainable': bool(v.get('isNotObtainable')),
        }

    skills = {}
    for sid, s in skills_src.items():
        levels = s.get('levels') or []
        name = (levels[0].get('name') if levels else '') or s.get('skillName') or ''
        if name:
            skills[sid] = name

    equips = {}
    for eid, e in equips_src.items():
        equips[eid] = {
            'uniEquipName': e.get('uniEquipName') or eid,
            'typeIcon': e.get('typeIcon') or '',
        }

    stages = []
    for sid, st in stages_src.items():
        code = st.get('code') or ''
        if not code:
            continue
        stages.append({
            'id': sid,
            'code': code,
            'name': st.get('name') or '',
            'zone': st.get('zoneId') or '',
            # 剧情关（NL-ST-1 这类）：进不了战斗，作业站永远不会有作业。
            # 带上官方标记，程序侧据此把它们从搜索/活动展开里剔掉，
            # 免得每个活动都顶着几行「共 0 份 / 打不了」。
            'story': bool(st.get('isStoryOnly')),
        })
    stages.sort(key=lambda r: r['id'])

    zones = {}
    for zid, z in zones_src.items():
        zones[zid] = {
            'name': z.get('zoneNameSecond') or z.get('zoneNameFirst') or '',
            'type': z.get('type') or '',
        }

    # 活动正式名（activity_table.basicInfo.name）
    # zone_table 里存的是地图分区名：act54side 的 zone 叫「通学路」，
    # 但活动本身叫「月行水上」；act55side 的 zone 叫「残照终尽」，活动叫「时间啊，向前！」。
    # 界面上一律用这个当活动名，zone 名只留作回退。
    activities = {}
    for aid, a in acts_src.items():
        if not str(aid).startswith('act'):
            continue
        name = (a.get('name') or '').strip()
        if not name:
            continue
        activities[aid] = {
            'name': name,
            'start': int(a.get('startTime') or 0),
            'end': int(a.get('endTime') or 0),
        }

    # 分支中文名没有官方静态表，只能从森空岛接口学；把已学到的带进包里当种子。
    # 两处来源叠加：先继承旧包里已有的（程序跑久了攒下来的），再用运行时缓存覆盖。
    # 运行时缓存不存在时（新装、清过配置）就靠旧包兜底，不会把 73 条种子清零。
    subprof = {}
    if base_path and os.path.isfile(base_path):
        subprof.update((load(base_path) or {}).get('subProfessions') or {})
    if subprof_path and os.path.isfile(subprof_path):
        subprof.update(load(subprof_path) or {})
    # 排序后落盘：本地有 subprof_cache、CI 只有旧包，两边来源不同但结果必须逐字节一致
    subprof = dict(sorted(subprof.items()))

    counts = {
        'chars': len(chars),
        'skills': len(skills),
        'equips': len(equips),
        'stages': len(stages),
        'zones': len(zones),
        'activities': len(activities),
        'subProfessions': len(subprof),
    }
    now = dt.datetime.now().astimezone()
    version = norm_version(game_version) or now.strftime('%Y.%m.%d')
    # 内容指纹：同数据必然同指纹（日期、分支名缓存都不参与，见文件头「幂等约定」）
    fingerprint = content_fingerprint(chars, skills, equips, stages, zones, activities)

    return {
        'schema': 1,
        'version': version,
        'fingerprint': fingerprint,
        'generatedAt': generated_at or now.isoformat(timespec='seconds'),
        'gameVersion': game_version or '',
        'sourceCommit': source_commit or '',
        'source': 'ArknightsGameResource/gamedata/excel',
        'counts': counts,
        'chars': chars,
        'skills': skills,
        'equips': equips,
        'stages': stages,
        'zones': zones,
        'activities': activities,
        'subProfessions': subprof,
    }


def _default_subprof(root):
    """找运行时学到的分支名缓存：新路径在前，旧版目录兜底"""
    for rel in (('config', 'subprof_cache.json'),
                ('config', 'skland', 'subprof_cache.json'),
                ('skland', 'subprof_cache.json')):
        p = os.path.join(root, *rel)
        if os.path.isfile(p):
            return p
    return os.path.join(root, 'config', 'subprof_cache.json')


def main(argv=None):
    root = os.path.dirname(HERE)
    ap = argparse.ArgumentParser(description='生成内置游戏资源包')
    ap.add_argument('--excel', required=True, help='ArknightsGameResource/gamedata/excel 目录')
    ap.add_argument('--out', default=os.path.join(HERE, 'arknights.json'))
    ap.add_argument('--subprof', default=None,
                    help='分支名缓存（默认自动找 config/subprof_cache.json）')
    ap.add_argument('--game-version', default=None,
                    help='上游游戏版本串（默认自动读 <excel>/../../version）')
    ap.add_argument('--source-commit', default='',
                    help='上游仓库提交 sha（写进包，自动同步用）')
    ap.add_argument('--commit-time', default='',
                    help='上游提交时间（写进 generatedAt，保证重跑结果一致）')
    args = ap.parse_args(argv)

    subprof_path = args.subprof or _default_subprof(root)
    game_version = args.game_version
    if game_version is None:
        game_version = read_game_version(args.excel)
    old = load(args.out) or {}
    old_counts = old.get('counts') or {}
    pack = build(args.excel, subprof_path, base_path=args.out,
                 game_version=game_version or '',
                 source_commit=args.source_commit or '',
                 generated_at=args.commit_time or '')
    os.makedirs(os.path.dirname(args.out) or '.', exist_ok=True)
    tmp = args.out + '.tmp'
    with io.open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(pack, f, ensure_ascii=False, separators=(',', ':'))
        f.write('\n')
    os.replace(tmp, args.out)

    size = os.path.getsize(args.out) / 1024 / 1024
    print('已生成：%s（%.2f MB）' % (args.out, size))
    print('  游戏版本：%s → 资源版本 %s｜指纹 %s' % (
        pack.get('gameVersion') or '（没读到 version 文件，退回生成日期）',
        pack['version'], pack['fingerprint']))
    if pack.get('sourceCommit'):
        print('  上游提交：%s' % pack['sourceCommit'][:12])
    if os.path.isfile(subprof_path):
        print('  分支名缓存：%s' % subprof_path)
    else:
        print('  分支名缓存：无（沿用旧包里的 %d 条）' % len(pack['subProfessions']))

    # 逐项和旧包对比：条数只该增不减，变少多半是资源库没更新
    lost = []
    for k, v in pack['counts'].items():
        old_v = old_counts.get(k)
        if old_v is None:
            mark = '新增'
        elif v > old_v:
            mark = '+%d' % (v - old_v)
        elif v < old_v:
            mark = '%+d  <- 变少' % (v - old_v)
            lost.append(k)
        else:
            mark = '持平'
        print('  %-16s %-6d %s' % (k, v, mark))

    if lost:
        print('')
        print('[警告] 这些字段比旧包少：%s' % '、'.join(lost))
        print('       资源库很可能是旧的，先 cd 到 ArknightsGameResource 执行 git pull 再跑一次。')
        return 2
    if old_counts:
        print('')
        print('校验通过：各项条数没有变少')
    return 0


if __name__ == '__main__':
    sys.exit(main())
