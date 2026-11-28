# -*- coding: utf-8 -*-
"""干员练度：把账号快照里的 chars 段整理成「练度表」

数据完全来自 token 同步写进账号 json 的那份快照（updater.build_snapshot）：
等级 / 精英化 / 潜能 / 技能等级 / 专精 / 模组 / 信赖 都在里面，这个模块
只做「挑字段 + 拼图标地址 + 分档统计」，**不发任何网络请求**——所以它能
离线用，MAA 导入的号（练度来自 OperBoxData 识别）也能看。

图标一律走官方 CDN 现取（安装包零增长，同 garrisonstat 的思路）：

    干员头像  char/avatar/{charId}.png
    技能      char_skill/{skillId}.png      （skcom_atk_up[2] 里的方括号要转义）
    模组      uniequip/{moduleId}.png

**技能 id 得用资源包里的那份，不能直接用快照的**：森空岛 player/info 把公共技能
的等级后缀剥掉了（只给 skcom_heal_up），官方 CDN 上只有 skcom_heal_up[1]/[2]/[3].png，
拿裸 id 拼地址必 404，整格空着。资源包 chars[].skills 是 character_table 的原样 id，
用它补正（顺带把技能正式名补上——快照里这类技能的名字就是 id 本身）。

「证章」（uniequip_001_*，精英化后人人都有、恒 1 级）不进来：CDN 上没有
它的图，摆在模组列里既没信息也全是灰块——模组列只放真模组。
"""
from __future__ import annotations

import re
import urllib.parse

from . import accounts, resources

CDN = 'https://web.hycdn.cn/arknights/game/assets/'
# 职业筛选的固定顺序（游戏里从左到右），不按接口返回顺序
PROFESSIONS = ('先锋', '近卫', '重装', '狙击', '术师', '医疗', '辅助', '特种')
# 稀有度筛选：>=4 单独一档，3 星及以下并一档（三星以上才值得单独看）
RARITIES = ((6, '六星'), (5, '五星'), (4, '四星'), (0, '三星及以下'))

# 技能 id 的等级后缀：公共技能在官方资源里是 skcom_heal_up[1]/[2]/[3]
_SUFFIX = re.compile(r'\[[^\]]*\]$')



def _icon(path: str) -> str:
    return CDN + path


def _char_icon(char_id: str) -> str:
    return _icon('char/avatar/%s.png' % char_id) if char_id else ''


def _skill_icon(skill_id: str) -> str:
    # safe='' 才会把 [ ] 转成 %5B %5D；文件名里本来就是转义过的
    return _icon('char_skill/%s.png' % urllib.parse.quote(skill_id, safe=''))


def _module_icon(module_id: str) -> str:
    return _icon('uniequip/%s.png' % urllib.parse.quote(module_id, safe=''))


def _num(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _skills(c: dict) -> list:
    """技能：``id`` 来自快照，图标/名字按资源包里的**带后缀 id** 补正

    森空岛 player/info 把公共技能的等级后缀剥掉了（只给 ``skcom_heal_up``），
    而官方 CDN 上只有 ``skcom_heal_up[1]/[2]/[3].png`` —— 直接拿裸 id 拼地址
    必 404，整列空一格（2026-11-29 用户报的「部分图标丢失」）。资源包里
    ``chars[].skills`` 是 character_table 的原样 id（带后缀），拿它对一遍即可；
    对不上（旧包 / 新干员）就退回裸 id，最差是占位块，不会崩。
    """
    pack_ids = [s for s in (_pack_char(c.get('charId')) or {}).get('skills') or []
                if isinstance(s, str) and s]
    by_base = {}
    for pid in pack_ids:
        by_base.setdefault(_SUFFIX.sub('', pid), pid)

    out = []
    for s in c.get('skills') or []:
        if not isinstance(s, dict):
            continue
        sid = (s.get('id') or '').strip()
        if not sid:
            continue
        real = sid if sid in pack_ids else by_base.get(sid) or sid
        name = (s.get('name') or '').strip()
        if not name or name == sid:
            # 快照里公共技能没有名字（updater 查表查不到就写了 id），
            # 资源包技能表里有「治疗强化·γ型」这种正式名
            name = (resources.skill_table().get(real) or '').strip() or real
        out.append({
            'id': real,
            'name': name,
            # specializeLevel：0 = 没专精，1/2/3 = 专一/专二/专三
            'mastery': max(0, min(3, _num(s.get('mastery')))),
            'icon': _skill_icon(real),
        })
    return out


def _pack_char(char_id: str) -> dict:
    if not char_id:
        return {}
    try:
        return (resources.char_table().get(char_id) or {})
    except Exception:          # 资源包坏了/没有也不该让整页挂掉
        return {}



def _modules(c: dict) -> list:
    out = []
    for m in c.get('modules') or []:
        if not isinstance(m, dict):
            continue
        mid = (m.get('id') or '').strip()
        if not mid or m.get('kind') == '证章':
            continue
        out.append({
            'id': mid,
            'name': m.get('name') or mid,
            'level': max(0, min(3, _num(m.get('level')))),
            'icon': _module_icon(mid),
        })
    out.sort(key=lambda r: -r['level'])
    return out


def _row(c: dict) -> dict:
    cid = c.get('charId') or ''
    return {
        'charId': cid,
        'name': c.get('name') or cid,
        'rarity': _num(c.get('rarity')),
        'profession': c.get('profession') or '',
        'subProfession': c.get('subProfession') or '',
        'elite': max(0, min(2, _num(c.get('elite')))),
        'level': _num(c.get('level')),
        'potential': max(0, min(6, _num(c.get('potential')))),
        'skillLevel': _num(c.get('skillLevel')),
        'favor': _num(c.get('favor')),
        'avatar': _char_icon(cid),
        'skills': _skills(c),
        'modules': _modules(c),
    }


def _stats(rows: list) -> dict:
    by_rarity, by_prof = {}, {}
    for r in rows:
        by_rarity[str(r['rarity'])] = by_rarity.get(str(r['rarity']), 0) + 1
        if r['profession']:
            by_prof[r['profession']] = by_prof.get(r['profession'], 0) + 1
    return {
        'total': len(rows),
        'sixStar': by_rarity.get('6', 0),
        'e2': sum(1 for r in rows if r['elite'] >= 2),
        'mastery3': sum(1 for r in rows for s in r['skills'] if s['mastery'] >= 3),
        'module3': sum(1 for r in rows for m in r['modules'] if m['level'] >= 3),
        'moduleCount': sum(len(r['modules']) for r in rows),
        'favorMax': sum(1 for r in rows if r['favor'] >= 200),
        'byRarity': by_rarity,
        'byProfession': by_prof,
    }


def view(file: str) -> dict:
    """一个账号的完整练度表

    返回 ``{file, account:{nickName,uid,level,channelName,syncedAt},
    chars:[...], stats:{...}, filters:{professions,rarities}}``。
    ``file`` 走 accounts.get_account，账号不存在/读坏了照常抛给上层。
    """
    snap = accounts.get_account(file)
    a = snap.get('account') or {}
    rows = [_row(c) for c in (snap.get('chars') or []) if isinstance(c, dict)]
    # 默认序：星级 → 精英化 → 等级 → 名字（跟快照里一致，但快照可能是
    # 旧版写的，这里不依赖它排过序）
    rows.sort(key=lambda r: (-r['rarity'], -r['elite'], -r['level'], r['name']))
    return {
        'file': file,
        'account': {
            'nickName': a.get('nickName') or '',
            'uid': str(a.get('uid') or ''),
            'level': a.get('level'),
            'channelName': a.get('channelName') or '',
            'avatar': a.get('avatar') or '',
            'secretary': a.get('secretary') or {},
        },
        'hasToken': bool((snap.get('skd') or {}).get('data')),
        'syncedAt': (snap.get('sync') or {}).get('syncedAt') or '',
        'chars': rows,
        'stats': _stats(rows),
        'filters': {'professions': list(PROFESSIONS),
                    'rarities': [{'value': v, 'name': n} for v, n in RARITIES]},
    }
