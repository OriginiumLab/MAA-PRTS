# -*- coding: utf-8 -*-
"""森空岛 token → 账号配置 JSON（内置版，不依赖外部脚本）

要点：
  - 日志走 log 回调（界面实时显示），不写 stdout
  - 资源来自内置的 resources/arknights.json，不再需要游戏 excel 目录
  - 统计口径：只算干员练度 + 缺失干员，不统计悖论模拟
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import re

from . import skland
from .skland import Skland

SCHEMA_VERSION = 2

# 各星级精二满级
MAX_LEVEL = {6: 90, 5: 80, 4: 70, 3: 55, 2: 30, 1: 30}

PROF = {'PIONEER': '先锋', 'WARRIOR': '近卫', 'TANK': '重装', 'SNIPER': '狙击',
        'CASTER': '术师', 'MEDIC': '医疗', 'SUPPORT': '辅助', 'SPECIAL': '特种'}
ELITE_CN = {0: '精零', 1: '精一', 2: '精二'}
CN_RARITY = {6: '六星', 5: '五星', 4: '四星', 3: '三星', 2: '二星', 1: '一星'}


# ---------------------------------------------------------------- 基础工具
def load_json(path, default=None):
    try:
        with io.open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def dump_json(path, obj):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + '.tmp'
    with io.open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write('\n')
    os.replace(tmp, path)


def iso(ts):
    """秒级时间戳 → 本地时区 ISO8601；0/None 返回 None"""
    try:
        ts = int(ts)
    except Exception:
        return None
    if ts <= 0:
        return None
    return dt.datetime.fromtimestamp(ts).astimezone().isoformat(timespec='seconds')


def now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec='seconds')


def mask(token):
    return token if len(token) <= 12 else token[:4] + '…' + token[-4:]


def fmt_char(c):
    return '%s%d' % (ELITE_CN.get(int(c.get('elite') or 0), '精?'), int(c.get('level') or 0))


def fmt_missing(missing, limit=12):
    """按星级汇总缺失干员：六星 2（冻河、缇缇）；五星 1（…）"""
    groups = {}
    for c in missing:
        groups.setdefault(c['rarity'], []).append(c['name'])
    segs, shown = [], 0
    for rar in sorted(groups, reverse=True):
        names = groups[rar]
        take = names[:max(0, limit - shown)]
        shown += len(take)
        txt = '、'.join(take) + ('…' if len(take) < len(names) else '')
        segs.append('%s %d（%s）' % (CN_RARITY.get(rar, '%d星' % rar), len(names), txt))
        if shown >= limit:
            break
    return '；'.join(segs)


# ---------------------------------------------------------------- 资源表
class Tables:
    """内置资源包：补全名称/技能名/模组名，并算出「图鉴全集」

    数据来自 resources/arknights.json（resources.make_pack 生成），
    字段名沿用游戏 excel 表：rarity 是 0 基、isNotObtainable 标记不可获取。
    """

    def __init__(self, cache_path=None):
        from . import resources

        p = resources.pack()
        self.cache_path = cache_path
        self.chars = dict(p.get('chars') or {})
        self.skills = dict(p.get('skills') or {})
        self.equips = dict(p.get('equips') or {})
        # 资源包里的分支名种子 + 本地已学到的结果
        self.sub_names = dict(p.get('subProfessions') or {})
        self.sub_names.update(load_json(cache_path, {}) if cache_path else {})
        self._sub_dirty = False
        self.warnings = []
        self._obtainable = None

        if not self.chars:
            self.warnings.append('资源包为空：无法统计缺失干员（请检查 resources/arknights.json）')
        if not self.skills:
            self.warnings.append('资源包缺少技能名：技能名会回退到 id')
        if not self.equips:
            self.warnings.append('资源包缺少模组表：模组名会回退到 id')

    def learn_sub_name(self, char_id, api_meta):
        cn = api_meta.get('subProfessionName')
        sid = (self.chars.get(char_id) or {}).get('subProfessionId')
        if cn and sid and sid not in self.sub_names:
            self.sub_names[sid] = cn
            self._sub_dirty = True

    def save_sub_names(self):
        if self._sub_dirty and self.cache_path:
            dump_json(self.cache_path, dict(sorted(self.sub_names.items())))
            self._sub_dirty = False

    @property
    def obtainable(self):
        if self._obtainable is None:
            self._obtainable = [(cid, v) for cid, v in self.chars.items()
                                if not v.get('isNotObtainable')]
        return self._obtainable

    @property
    def char_total(self):
        return len(self.obtainable)

    def rarity_of(self, local):
        return int(local.get('rarity') or 0) + 1


# ---------------------------------------------------------------- 构建快照
def build_snapshot(sk, binding, info, tables, alias='', with_skins=False):
    st = info.get('status') or {}
    info_map = info.get('charInfoMap') or {}
    skin_map = info.get('skinInfoMap') or {}
    owned_skins = [s for s in (info.get('skins') or []) if s.get('id')]
    skin_count = {}
    for s in owned_skins:
        cid = (skin_map.get(s['id']) or {}).get('charId')
        if cid:
            skin_count[cid] = skin_count.get(cid, 0) + 1

    def skin_of(sid):
        if not sid:
            return None
        return {'id': sid, 'name': (skin_map.get(sid) or {}).get('name') or sid}

    chars = []
    for c in info.get('chars') or []:
        cid = c.get('charId')
        api_meta = info_map.get(cid) or {}
        loc = tables.chars.get(cid) or {}

        rarity_raw = api_meta.get('rarity')
        if rarity_raw is None:
            rarity_raw = loc.get('rarity') or 0
        prof_en = api_meta.get('profession') or loc.get('profession') or ''

        skills = []
        for s in c.get('skills') or []:
            sid = (s.get('id') or '').split('[')[0]
            if not sid:
                continue
            skills.append({
                'id': sid,
                'name': tables.skills.get(sid, sid),
                'mastery': int(s.get('specializeLevel') or 0),
            })

        modules = []
        for e in c.get('equip') or []:
            if e.get('locked'):
                continue
            eid = e.get('id')
            ed = tables.equips.get(eid) or {}
            modules.append({
                'id': eid,
                'name': ed.get('uniEquipName') or eid,
                'level': int(e.get('level') or 0),
                'kind': '证章' if ed.get('typeIcon') == 'original' else '模组',
            })

        ev = int(c.get('evolvePhase') or 0)
        lv = int(c.get('level') or 0)
        rarity = int(rarity_raw) + 1

        tables.learn_sub_name(cid, api_meta)
        sub_cn = (api_meta.get('subProfessionName')
                  or tables.sub_names.get(loc.get('subProfessionId'))
                  or loc.get('subProfessionId') or '')

        chars.append({
            'charId': cid,
            'name': api_meta.get('name') or loc.get('name') or cid,
            'rarity': rarity,
            'profession': PROF.get(prof_en, prof_en),
            'subProfession': sub_cn,
            'elite': ev,
            'level': lv,
            'potential': int(c.get('potentialRank') or 0),
            'skillLevel': int(c.get('mainSkillLvl') or 0),
            'skills': skills,
            'modules': modules,
            'favor': int(c.get('favorPercent') or 0),
            'skin': skin_of(c.get('skinId')),
            'skinCount': skin_count.get(cid, 0),
        })

    chars.sort(key=lambda r: (-r['rarity'], -r['elite'], -r['level'], r['name']))

    missing = build_missing(chars, tables)
    stats = build_stats(chars, missing, tables, len(owned_skins))

    account = {
        'uid': str(binding.get('uid') or st.get('uid') or ''),
        'nickName': binding.get('nickName') or st.get('name') or '',
        'alias': alias or '',
        'channelName': binding.get('channelName') or '',
        'channelMasterId': str(binding.get('channelMasterId') or ''),
        'isOfficial': bool(binding.get('isOfficial')),
        'isDefault': bool(binding.get('isDefault')),
        'level': st.get('level'),
        'serverName': st.get('serverName') or '',
        'registerAt': iso(st.get('registerTs')),
        'lastOnlineAt': iso(st.get('lastOnlineTs')),
        'avatar': (st.get('avatar') or {}).get('url'),
        'secretary': None,
    }
    sec = st.get('secretary') or {}
    if sec.get('charId'):
        account['secretary'] = {
            'charId': sec.get('charId'),
            'name': (info_map.get(sec.get('charId')) or {}).get('name') or sec.get('charId'),
            'skinId': sec.get('skinId'),
            'skinName': (skin_map.get(sec.get('skinId')) or {}).get('name'),
        }

    snap = {
        'schemaVersion': SCHEMA_VERSION,
        'generator': 'MAA-PRTS/2.0',
        'account': account,
        'maa': {'profile': '', 'note': ''},
        'tags': [],
        'sync': {
            'syncedAt': now_iso(),
            'gameTs': info.get('currentTs'),
            'gameTime': iso(info.get('currentTs')),
            'tokenHint': mask(sk.token),
            'source': 'zonai.skland.com/api/v1/game/player/info',
        },
        'stats': stats,
        'chars': chars,
        'missing': missing,
    }

    if with_skins:
        out = []
        for s in owned_skins:
            m = skin_map.get(s['id']) or {}
            out.append({'id': s['id'], 'name': m.get('name') or s['id'],
                        'charId': m.get('charId'), 'gotAt': iso(s.get('ts'))})
        snap['skins'] = out

    return snap


def build_missing(chars, tables):
    """图鉴全集 - 已拥有 = 缺失干员"""
    owned = {c['charId'] for c in chars}
    out = []
    for cid, loc in tables.obtainable:
        if cid in owned:
            continue
        prof_en = loc.get('profession') or ''
        out.append({
            'charId': cid,
            'name': loc.get('name') or cid,
            'rarity': tables.rarity_of(loc),
            'profession': PROF.get(prof_en, prof_en),
            'subProfession': tables.sub_names.get(loc.get('subProfessionId'), ''),
        })
    out.sort(key=lambda r: (-r['rarity'], r['name']))
    return out


def build_stats(chars, missing, tables, skin_total):
    by_rarity, by_prof = {}, {}
    for c in chars:
        by_rarity[str(c['rarity'])] = by_rarity.get(str(c['rarity']), 0) + 1
        if c['profession']:
            by_prof[c['profession']] = by_prof.get(c['profession'], 0) + 1

    mastery = {'1': 0, '2': 0, '3': 0}
    for c in chars:
        for s in c['skills']:
            lv = s['mastery']
            if lv >= 1:
                mastery[str(min(lv, 3))] = mastery.get(str(min(lv, 3)), 0) + 1

    def is_max(c):
        return c['elite'] >= 2 and c['level'] >= MAX_LEVEL.get(c['rarity'], 90)

    missing_by_rarity = {}
    for c in missing:
        missing_by_rarity[str(c['rarity'])] = missing_by_rarity.get(str(c['rarity']), 0) + 1

    return {
        'charCount': len(chars),
        'charTotal': tables.char_total or None,
        'missingCount': len(missing),
        'missingByRarity': missing_by_rarity,
        'sixStarCount': by_rarity.get('6', 0),
        'sixStarTotal': sum(1 for _, v in tables.obtainable
                            if int(v.get('rarity') or 0) == 5) or None,
        'byRarity': by_rarity,
        'byProfession': by_prof,
        'e2Count': sum(1 for c in chars if c['elite'] >= 2),
        'e2MaxCount': sum(1 for c in chars if is_max(c)),
        'e1Count': sum(1 for c in chars if c['elite'] == 1),
        'e0Count': sum(1 for c in chars if c['elite'] == 0),
        'mastery3Count': mastery.get('3', 0),
        'masteryByLevel': mastery,
        'moduleCount': sum(len(c['modules']) for c in chars),
        'module3Count': sum(1 for c in chars for m in c['modules'] if m['level'] >= 3),
        'potentialMaxCount': sum(1 for c in chars if c['potential'] >= 5),
        'favorMaxCount': sum(1 for c in chars if c['favor'] >= 200),
        'skinCount': skin_total,
    }


# ---------------------------------------------------------------- 增量对比
def diff_snapshots(old, new):
    if not old:
        return ['首次生成']
    out = []
    o_chars = {c['charId']: c for c in (old.get('chars') or [])}
    n_chars = {c['charId']: c for c in (new.get('chars') or [])}

    added = [n_chars[k]['name'] for k in n_chars if k not in o_chars]
    if added:
        out.append('新增干员 %d：%s' % (len(added), '、'.join(added[:12])
                                      + ('…' if len(added) > 12 else '')))
    removed = [o_chars[k]['name'] for k in o_chars if k not in n_chars]
    if removed:
        out.append('减少干员 %d：%s' % (len(removed), '、'.join(removed[:12])))

    ups, swaps = [], []
    for cid, n in n_chars.items():
        o = o_chars.get(cid)
        if not o:
            continue
        if (o['elite'], o['level']) != (n['elite'], n['level']):
            ups.append('%s %s→%s' % (n['name'], fmt_char(o), fmt_char(n)))
        if o.get('skillLevel') != n.get('skillLevel'):
            swaps.append('%s 技能%d→%d' % (n['name'], o.get('skillLevel', 0), n.get('skillLevel', 0)))
        om = {s['id']: s['mastery'] for s in (o.get('skills') or [])}
        nm = {s['id']: s['mastery'] for s in (n.get('skills') or [])}
        for sid, lv in nm.items():
            if lv > om.get(sid, 0):
                swaps.append('%s %s专%s→专%s' % (n['name'],
                                                 next((s['name'] for s in n['skills'] if s['id'] == sid), sid),
                                                 om.get(sid, 0), lv))
        oe = {e['id']: e['level'] for e in (o.get('modules') or [])}
        ne = {e['id']: e['level'] for e in (n.get('modules') or [])}
        for eid, lv in ne.items():
            if eid not in oe:
                swaps.append('%s 解锁%s' % (n['name'],
                                            next((e['name'] for e in n['modules'] if e['id'] == eid), eid)))
            elif lv > oe[eid]:
                swaps.append('%s %s Lv%d→Lv%d' % (n['name'],
                                                  next((e['name'] for e in n['modules'] if e['id'] == eid), eid),
                                                  oe[eid], lv))

    if ups:
        out.append('练度提升 %d：%s' % (len(ups), '；'.join(ups[:8]) + ('…' if len(ups) > 8 else '')))
    if swaps:
        out.append('技能/模组变化 %d：%s' % (len(swaps), '；'.join(swaps[:8]) + ('…' if len(swaps) > 8 else '')))
    if not out:
        out.append('无变化')
    return out


# ---------------------------------------------------------------- 文件命名
# R5 起账号文件与这些全局文件同层放在 config/，目录遍历必须显式跳过
# （account.uid 检查只是兜底，不能依赖它挡住解析失败的 json）
NON_ACCOUNT_JSON = {'settings.json', 'gamedata.json', 'tokens.json',
                    'subprof_cache.json'}


def safe_filename(name, fallback=''):
    """去掉 Windows 文件名非法字符，保留中文/#/空格"""
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', str(name or ''))
    return s.strip().strip('.').strip() or fallback


def char_path(accounts_dir, nick, channel, uid):
    """账号文件命名：<昵称> <服务器>.json；同名不同 uid 自动加 (2)(3)"""
    nick = safe_filename(nick)
    if not nick:
        return os.path.join(accounts_dir, '账号_%s.json' % uid)
    base = ('%s %s' % (nick, safe_filename(channel))).strip()
    cand, n = base, 2
    while True:
        p = os.path.join(accounts_dir, '%s.json' % cand)
        if not os.path.exists(p):
            return p
        d = load_json(p) or {}
        if str((d.get('account') or {}).get('uid')) == str(uid):
            return p
        cand = '%s (%d)' % (base, n)
        n += 1


def scan_accounts(accounts_dir):
    """扫描账号目录（config/），返回 {uid: 文件路径}"""
    found = {}
    if not os.path.isdir(accounts_dir):
        return found
    for name in os.listdir(accounts_dir):
        if not name.endswith('.json') or name.startswith('_'):
            continue
        if name in NON_ACCOUNT_JSON:
            continue
        p = os.path.join(accounts_dir, name)
        d = load_json(p) or {}
        uid = str(((d.get('account') or {}).get('uid')) or '')
        if uid:
            found[uid] = p
    return found


def write_index(accounts_dir):
    rows = []
    if os.path.isdir(accounts_dir):
        for name in sorted(os.listdir(accounts_dir)):
            if not name.endswith('.json') or name.startswith('_'):
                continue
            if name in NON_ACCOUNT_JSON:
                continue
            d = load_json(os.path.join(accounts_dir, name)) or {}
            a, s, sy = d.get('account') or {}, d.get('stats') or {}, d.get('sync') or {}
            if not a.get('uid'):
                continue
            rows.append({
                'uid': str(a.get('uid')), 'nickName': a.get('nickName'),
                'alias': a.get('alias') or '', 'channelName': a.get('channelName'),
                'level': a.get('level'),
                'maaProfile': ((d.get('maa') or {}).get('profile') or ''),
                'file': name,
                'charCount': s.get('charCount'), 'charTotal': s.get('charTotal'),
                'missingCount': s.get('missingCount'),
                'sixStarCount': s.get('sixStarCount'), 'sixStarTotal': s.get('sixStarTotal'),
                'e2Count': s.get('e2Count'), 'mastery3Count': s.get('mastery3Count'),
                'moduleCount': s.get('moduleCount'),
                'syncedAt': sy.get('syncedAt'), 'gameTime': sy.get('gameTime'),
            })
    rows.sort(key=lambda r: (-(r.get('level') or 0), r.get('nickName') or ''))
    idx = {'schemaVersion': SCHEMA_VERSION, 'updatedAt': now_iso(),
           'count': len(rows), 'accounts': rows}
    dump_json(os.path.join(accounts_dir, '_index.json'), idx)
    return idx


def merge_preserved(snap, old):
    """保留用户手写字段（maa/note/tags/skd），刷新不冲掉"""
    if not old:
        return snap
    for k in ('maa', 'note'):
        if k in old:
            snap[k] = old[k]
    if old.get('tags'):
        snap['tags'] = old['tags']
    # 新快照不带 skd（重加密失败/兜底路径）而旧文件有 → 原样保留，token 不丢
    if not snap.get('skd') and old.get('skd'):
        snap['skd'] = old['skd']
    if not ((snap.get('account') or {}).get('alias')) and (old.get('account') or {}).get('alias'):
        snap['account']['alias'] = old['account']['alias']
    return snap


# ---------------------------------------------------------------- 主流程
def sync_one(token, alias, accounts_dir, log, with_skins=False, cache_path=None,
             skd_meta=None):
    """同步单个 token 绑定的所有角色，返回 {ok, fail, files}

    skd_meta = {alias, addedAt, data}：调用方组装好的 token 密文段，
    同步成功的账号文件会带上它（R4，token 与账号数据同文件存放）；
    不传则留给 merge_preserved 保留旧文件里已有的 skd。
    """
    tables = Tables(cache_path)
    for w in tables.warnings:
        log('[资源包] ' + w)

    os.makedirs(accounts_dir, exist_ok=True)
    existing = scan_accounts(accounts_dir)

    label = alias or mask(token)
    log('账号：%s  (%s)' % (label, mask(token)))

    sk = Skland(token)
    bds = [b for b in sk.bindings() if not b.get('isDelete')]
    if not bds:
        raise RuntimeError('该 token 未绑定明日方舟角色')

    ok, fail, files = 0, 0, []
    for b in bds:
        uid = str(b.get('uid'))
        info = sk.player_info(uid)
        snap = build_snapshot(sk, b, info, tables, alias=alias or '', with_skins=with_skins)

        path = char_path(accounts_dir, snap['account']['nickName'],
                         snap['account']['channelName'], uid)
        old_path = existing.get(uid)
        old = load_json(old_path) if old_path else None
        snap = merge_preserved(snap, old)
        if skd_meta:
            snap['skd'] = dict(skd_meta)
        changes = diff_snapshots(old, snap)

        a, s = snap['account'], snap['stats']
        log('  ✓ %s（%s） uid=%s Lv%s' % (a['nickName'], a['channelName'], uid, a['level']))
        log('    干员 %d%s｜精二 %d（满级 %d）｜六星 %d%s｜专三 %d｜模组 %d（满级 %d）' % (
            s['charCount'], '/%d' % s['charTotal'] if s['charTotal'] else '',
            s['e2Count'], s['e2MaxCount'], s['sixStarCount'],
            '/%d' % s['sixStarTotal'] if s['sixStarTotal'] else '',
            s['mastery3Count'], s['moduleCount'], s['module3Count']))
        log('    缺卡 %d%s' % (s['missingCount'],
                               '：' + fmt_missing(snap['missing']) if snap['missing'] else '（图鉴已满）'))
        for line in changes:
            log('    · %s' % line)

        if old_path and os.path.abspath(old_path) != os.path.abspath(path):
            os.replace(old_path, path)
            log('    ~ 重命名 %s → %s' % (os.path.basename(old_path), os.path.basename(path)))
        dump_json(path, snap)
        log('    → %s' % os.path.basename(path))
        existing[uid] = path
        files.append({'file': os.path.basename(path), 'uid': uid, 'nickName': a['nickName']})
        ok += 1

    tables.save_sub_names()
    return {'ok': ok, 'fail': fail, 'files': files}


def parse_tokens_file(path):
    """旧版明文 tokens.txt → [(alias, token, outname, lineno)]

    现在只在迁移时用（accounts._migrate_txt_if_any），运行期的 token
    密文随各账号 json 的 skd 段存放（旧版 config/tokens.json 兜底）。
    """
    items = []
    with io.open(path, encoding='utf-8-sig') as f:
        for lineno, raw in enumerate(f, 1):
            s = raw.strip()
            if not s or s.startswith('#'):
                continue
            s = re.split(r'\s+#', s, maxsplit=1)[0].strip()
            if not s:
                continue
            parts = s.split()
            if len(parts) >= 2 and ':' not in s:
                items.append((' '.join(parts[:-1]), parts[-1], None, lineno))
                continue
            seg = s.split(':')
            if len(seg) == 1:
                items.append(('', seg[0].strip(), None, lineno))
            elif len(seg) == 2:
                items.append((seg[0].strip(), seg[1].strip(), None, lineno))
            else:
                items.append((seg[0].strip(), ':'.join(seg[1:-1]).strip(), seg[-1].strip(), lineno))

    seen, uniq = {}, []
    for alias, token, outname, lineno in items:
        if token in seen:
            continue
        seen[token] = lineno
        uniq.append((alias, token, outname, lineno))
    return uniq


def sync_many(items, accounts_dir, log, with_skins=False, cache_path=None):
    """批量同步 token 列表 [{"alias","token","data"?}]，单个失败不影响其他

    条目自带的密文（data/addedAt，来自账号文件 skd 段或旧 tokens.json）
    直接透传给 sync_one，避免每个角色再加密一次。
    """
    seen, uniq = set(), []
    for e in items or []:
        token = (e.get('token') or '').strip()
        if token and token not in seen:
            seen.add(token)
            uniq.append(e)
    if not uniq:
        raise RuntimeError('token 列表为空，先在界面里添加账号')
    log('token 共 %d 个' % len(uniq))
    log('-' * 60)

    ok, fail = 0, 0
    for e in uniq:
        alias, token = (e.get('alias') or '').strip(), e['token']
        skd_meta = None
        if e.get('data'):
            skd_meta = {'alias': alias, 'addedAt': e.get('addedAt') or '',
                        'data': e.get('data')}
        log('')
        try:
            r = sync_one(token, alias, accounts_dir, log,
                         with_skins=with_skins, cache_path=cache_path,
                         skd_meta=skd_meta)
            ok += r['ok']
        except Exception as e:
            log('  ✗ 失败：%s' % e)
            fail += 1

    write_index(accounts_dir)
    log('')
    log('完成：成功 %d 个角色，失败 %d 个 token' % (ok, fail))
    return {'ok': ok, 'fail': fail}
