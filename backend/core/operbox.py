# -*- coding: utf-8 -*-
"""MAA 干员识别数据（OperBoxData.json）→ 账号练度结构

文件在 <MAA 根目录>/data/OperBoxData.json，由 MAA「小工具 → 干员识别」生成，
每个 MAA 安装目录一份（不按账号命名）：

  - source=local：MAA Core 游戏内截图识别。条目只有 id/name/elite/level/
    own/potential/rarity 七个字段，name 来自 OCR（跨语言客户端可能不是中文），
    永远没有技能/专精/模组数据——这是数据源上限，不是 bug；
  - source=yituliu：一图流 OpenAPI 拉取，额外带 mainSkillLevel/skills/equips。

2025-05-29 之前的旧版 MAA 存的是裸 JArray，load() 两种形态都认。

映射原则：以 id 为主键查内置资源表补中文名/星级/职业（OCR 名只作回退，
否则名字匹配链路整体失配）；练度字段直映射；资源表查不到的条目（游戏出
新干员、资源包未更新）跳过并计入 warnings，不整单失败。技能/专精/模组
字段缺失就如实映射成 0/空，绝不编造数值凑放行。
"""
from __future__ import annotations

import json
import os

from . import accounts, atomicio, settings, updater


def operbox_path(maa_dir: str) -> str:
    """MAA 干员识别数据文件路径（相对 MAA 安装目录）"""
    return os.path.join(maa_dir, 'data', 'OperBoxData.json')


def _read(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def load(path: str) -> dict:
    """读 OperBoxData.json，新旧形态统一成 {done, own_opers, source, syncTime}

    旧版（2025-05-29 前）是裸干员数组，没有 source/syncTime，按 local 对待。
    非 JSON、own_opers 缺失或为空 → ValueError（文案直接显示到界面）。
    """
    try:
        data = _read(path)
    except Exception as e:
        raise ValueError('OperBoxData.json 解析失败（非 JSON 或 own_opers 为空）') from e
    if isinstance(data, list):
        data = {'done': True, 'own_opers': data, 'source': 'local', 'syncTime': None}
    if not isinstance(data, dict) or not data.get('own_opers'):
        raise ValueError('OperBoxData.json 解析失败（非 JSON 或 own_opers 为空）')
    return data


def probe(maa_dir: str) -> dict:
    """给 /api/maa/probe 用的轻量探测：文件在不在、own_opers 条数、识别时间

    JSON 读失败时 count/syncTime 为 None（存在性不受影响）。
    """
    p = operbox_path(maa_dir)
    if not os.path.isfile(p):
        return {'exists': False, 'count': None, 'syncTime': None}
    try:
        data = _read(p)
        if isinstance(data, list):
            return {'exists': True, 'count': len(data), 'syncTime': None}
        return {'exists': True,
                'count': len(data.get('own_opers') or []),
                'syncTime': (data or {}).get('syncTime')}
    except Exception:
        return {'exists': True, 'count': None, 'syncTime': None}


def build_chars(data: dict, tables) -> dict:
    """own_opers → 与 build_snapshot 条目同构的 chars 列表

    返回 {'chars': [...], 'skipped': 跳过条数, 'warnings': [...]}：
    只收 own=true 的条目；id 不在资源表（新干员 + 旧资源包）跳过并计数。
    mainSkillLevel/skills/equips 仅 source=yituliu 才有，local 数据整个缺省，
    如实映射成 skillLevel=0 / skills=[] / modules=[]。
    """
    chars, skipped, warnings = [], 0, []
    for c in data.get('own_opers') or []:
        if not c.get('own'):
            skipped += 1
            continue
        cid = c.get('id')
        loc = (tables.chars or {}).get(cid)
        if not loc:
            skipped += 1
            warnings.append('资源表查不到 %s（%s），已跳过——游戏出新干员后'
                            '重新生成资源包即可' % (cid, c.get('name') or '?'))
            continue

        skills = []
        for s in c.get('skills') or []:          # 仅 source=yituliu 存在
            sid = s.get('id') or ''
            if not sid:
                continue
            skills.append({'id': sid, 'name': tables.skills.get(sid, sid),
                           'mastery': int(s.get('level') or 0)})

        modules = []
        for e in c.get('equips') or []:          # 仅 source=yituliu 存在
            eid = e.get('id')
            if not eid:
                continue
            ed = tables.equips.get(eid) or {}
            modules.append({'id': eid,
                            'name': ed.get('uniEquipName') or eid,
                            'level': int(e.get('level') or 0),
                            'kind': '证章' if ed.get('typeIcon') == 'original' else '模组'})

        rar = loc.get('rarity')
        chars.append({
            'charId': cid,
            'name': loc.get('name') or c.get('name') or cid,   # OCR 名只作回退
            'rarity': int(rar) + 1 if rar is not None else int(c.get('rarity') or 0),
            'profession': updater.PROF.get(loc.get('profession'), loc.get('profession') or ''),
            'subProfession': tables.sub_names.get(loc.get('subProfessionId'),
                                                  loc.get('subProfessionId') or ''),
            'elite': int(c.get('elite') or 0),
            'level': int(c.get('level') or 0),
            'potential': int(c.get('potential') or 0),
            'skillLevel': int(c.get('mainSkillLevel') or 0),
            'skills': skills,
            'modules': modules,
            'favor': 0,
            'skin': None,
            'skinCount': 0,
        })

    if (data.get('source') or 'local') != 'yituliu':
        warnings.append('source=%s：MAA 本地识别不含技能/专精/模组数据，'
                        '作业匹配只比精英化与等级（技能模组按已拥有处理）'
                        % (data.get('source') or 'local'))
    chars.sort(key=lambda r: (-r['rarity'], -r['elite'], -r['level'], r['name']))
    return {'chars': chars, 'skipped': skipped, 'warnings': warnings}


def _read_operbox(maa_dir):
    """读并校验 MAA 干员识别结果，返回 (path, data, tables, built)。

    done=false（识别中断的残缺名单）整单拒绝，own_opers 全空报解析失败。"""
    path = operbox_path(maa_dir)
    if not os.path.isfile(path):
        raise FileNotFoundError(
            'MAA 目录下没有 data/OperBoxData.json（先在 MAA 小工具里跑一次干员识别）')
    data = load(path)
    if data.get('done') is False:
        # 识别中断的残缺名单整单拒绝，不能拿它覆盖现有练度
        raise ValueError('MAA 干员识别未完成（done=false），'
                         '请在 MAA 小工具里重跑一次识别再导入')
    tables = updater.Tables(cache_path=settings.subprof_cache())
    built = build_chars(data, tables)
    if not built['chars']:
        raise ValueError('OperBoxData.json 解析失败（own_opers 为空或全部条目无法识别）')
    return path, data, tables, built


def _apply_chars(snap, chars, tables, path, data):
    """把识别结果写进快照：皮肤/信赖按 charId 沿用旧值，重建 stats/missing/sync。

    MAA 数据不含皮肤/信赖：不沿用的话同一份文件里 per-char 全置零、
    stats.skinCount 却还计着保留的 skins 列表，自相矛盾。"""
    old_by_id = {c.get('charId'): c for c in (snap.get('chars') or []) if c.get('charId')}
    for c in chars:
        o = old_by_id.get(c['charId'])
        if o:
            c['favor'] = int(o.get('favor') or 0)
            c['skin'] = o.get('skin')
            c['skinCount'] = int(o.get('skinCount') or 0)
    missing = updater.build_missing(chars, tables)
    snap['chars'] = chars
    snap['stats'] = updater.build_stats(chars, missing, tables,
                                        len(snap.get('skins') or []))
    snap['missing'] = missing
    snap['sync'] = {
        'syncedAt': updater.now_iso(),
        'source': 'maa-operbox',
        'operbox': {
            'path': path,
            'source': data.get('source'),
            'syncTime': data.get('syncTime'),
            'done': data.get('done'),
        },
    }


def _summary(fname, path, data, built, snap):
    """导入/建号共用的返回摘要"""
    return {
        'file': fname,
        'path': path,
        'source': data.get('source'),
        'syncTime': data.get('syncTime'),
        'done': data.get('done'),
        'imported': len(built['chars']),
        'skipped': built['skipped'],
        'warnings': built['warnings'],
        'stats': snap['stats'],
        'sync': snap['sync'],
    }


def import_into(file: str, maa_dir: str) -> dict:
    """把 MAA 干员识别结果写进账号文件，返回导入摘要。

    done=false（识别未完成）整单拒绝。chars/stats/missing/sync 被识别结果
    覆盖（皮肤/信赖按 charId 沿用旧值）；account/maa/tags/note/skd/skins
    原样保留（合并语义同 updater.merge_preserved），原子替换落盘。
    """
    path, data, tables, built = _read_operbox(maa_dir)
    # 读旧快照 → 合并 → 写回整段圈进写锁：与后台同步/其他请求并发时
    # 不丢更新（#47）。锁内只有内存合并，没有 IO，拿一会儿没关系。
    with atomicio.lock():
        snap = accounts.get_account(file)
        _apply_chars(snap, built['chars'], tables, path, data)
        accounts.save_account(file, snap)
    return _summary(os.path.basename(str(file)), path, data, built, snap)


def _safe_stem(s: str) -> str:
    """文件名可用段：去掉 Windows 非法字符与控制符，限长"""
    kept = ''.join(c for c in s if c not in '<>:"/\\|?*' and ord(c) >= 32)
    return (kept.strip().rstrip('.') or '本地账号')[:40]


def create_from_dir(maa_dir: str, alias: str = '') -> dict:
    """不用森空岛 token：直接用 MAA 目录的干员识别结果建一个本地账号。

    解析/统计与 import_into 同一套；区别在账号文件是新建的：
      - account.uid 用合成值（list_accounts 以有无 uid 判定账号有效性），
        nickName 取备注或 MAA 目录名；
      - 不写 skd 段——没有 token，也就不参与森空岛同步；
      - 文件名 <备注或目录名>.json，重名顺延 (2)(3)。
    """
    path, data, tables, built = _read_operbox(maa_dir)
    chars = built['chars']

    d = settings._norm_dir(maa_dir) or maa_dir
    folder = os.path.basename(d.rstrip('\\/')) or 'MAA'
    alias = (alias or '').strip()
    base = _safe_stem(alias or folder)
    name = base + '.json'
    n = 2
    accounts_dir = settings.accounts_dir()
    while (os.path.exists(os.path.join(accounts_dir, name))
           or name in updater.NON_ACCOUNT_JSON):
        name = '%s(%d).json' % (base, n)
        n += 1

    snap = {
        'account': {
            'uid': 'local-' + _safe_stem(folder).replace(' ', ''),
            'nickName': alias or folder,
            'alias': alias,
            'channelName': '本地导入',
        },
        'maa': {'dir': d},
    }
    _apply_chars(snap, chars, tables, path, data)   # 新快照无旧值，皮肤/信赖自然为零
    accounts.save_account(name, snap)
    return _summary(name, path, data, built, snap)
