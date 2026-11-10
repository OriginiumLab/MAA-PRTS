# -*- coding: utf-8 -*-
"""抽卡记录（明日方舟 / 明日方舟：终末地）

链路（2026-11 在本机用真实 token 实测，全程直连，不需要代理、不需要 cred 签名）：

    森空岛 token ──grant(be36d44aa36bfb5b, type=1)──▶ grant token
      ├─ binding_list?appCode=arknights / endfield ──▶ 角色 uid
      └─ u8_token_by_uid {uid, token: grant}       ──▶ 角色 token（u8）

    明日方舟：role/login 换 ak-user-center cookie
              → gacha/cate（分类是动态的，只能查）
              → gacha/history?category=&size=&pos=&gachaTs=  游标分页
    终末地：  ef-webview /api/record/char?token=&server_id=1&pool_type=

几个容易踩的点：
  - `gachaTs` 是 **13 位毫秒**字符串，按秒解析会炸；分页要用上一页最后一条的
    `gachaTs` + `pos` 一起做游标
  - `size` 只认 10~100，传 500 服务端不报错但直接返回空列表（静默截断）
  - 明日方舟 `rarity` 是 0 基（3 = 四星）；终末地没实测到数据，按「<6 视为 0 基」
    归一，兼容两种口径
  - `u8_token_by_uid` 对终末地必须传绑定的 **uid**，传 roleId 会报 109「角色不存在」

本模块只用标准库，可以直接打包进 exe。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import atomicio, settings, tasks

# 通行证 appCode：明日方舟与终末地共用同一个（写 'endfield' 会 400：
# appCode 校验要求十六进制）
APP_CODE = 'be36d44aa36bfb5b'
GRANT_URL = 'https://as.hypergryph.com/user/oauth2/v2/grant'
BINDING_URL = 'https://binding-api-account-prod.hypergryph.com/account/binding/v1/binding_list'
U8_URL = 'https://binding-api-account-prod.hypergryph.com/account/binding/v1/u8_token_by_uid'
ARK_LOGIN_URL = 'https://ak.hypergryph.com/user/api/role/login'
ARK_CATE_URL = 'https://ak.hypergryph.com/user/api/inquiry/gacha/cate'
ARK_HIST_URL = 'https://ak.hypergryph.com/user/api/inquiry/gacha/history'
EF_RECORD_URL = 'https://ef-webview.hypergryph.com/api/record/char'
EF_WEAPON_URL = 'https://ef-webview.hypergryph.com/api/record/weapon'

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

ARK_REFERER = {'Referer': 'https://ak.hypergryph.com/'}

# 终末地角色池类型（枚举值由服务端校验，写错会 40000 "Invalid pool_type"；
# 实测只有这 5 个是合法值，庆典池等分类的抽卡都归在其中一类里）
EF_CHAR_POOLS = (
    ('E_CharacterGachaPoolType_Standard', '常驻寻访'),
    ('E_CharacterGachaPoolType_Special', '限定寻访'),
    ('E_CharacterGachaPoolType_Beginner', '新手寻访'),
    ('E_CharacterGachaPoolType_Joint', '联合寻访'),
    ('E_CharacterGachaPoolType_Rerun', '复刻寻访'),
)
EF_WEAPON_CATEGORY = 'weapon'

PAGE_SIZE = 100     # 明日方舟上限；>100 服务端静默返回空列表

CACHE_VERSION = 2   # v1 的终末地缓存只取了每池第一页、星级还错位，读到直接丢弃重建

CN_RARITY = {6: '六星', 5: '五星', 4: '四星', 3: '三星', 2: '二星', 1: '一星'}


# ---------------------------------------------------------------- HTTP
def _http(method, url, data=None, headers=None, timeout=25, retries=3):
    """带退避重试的 JSON 请求。只重试网络层错误，业务错误原样抛。"""
    last = None
    body = None
    if data is not None:
        body = json.dumps(data, separators=(',', ':')).encode('utf-8')
    for attempt in range(1, max(1, retries) + 1):
        h = {'User-Agent': UA, 'Accept': 'application/json'}
        h.update(headers or {})
        if body is not None:
            h['Content-Type'] = 'application/json'
        try:
            req = urllib.request.Request(url, data=body, headers=h,
                                         method=method or ('POST' if body else 'GET'))
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode('utf-8')), r.headers
        except urllib.error.HTTPError:
            raise                      # 4xx/5xx 是明确的业务/接口错误，重试无意义
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = e
            if attempt >= retries:
                raise
            time.sleep(1.2 * attempt)
    raise last


def _get_json(url, headers=None, timeout=25):
    return _http('GET', url, headers=headers, timeout=timeout)[0]


# ---------------------------------------------------------------- 通行证
def grant_token(token: str) -> str:
    """森空岛 token → 通行证 grant（其余接口都靠它）"""
    d, _ = _http('POST', GRANT_URL, {'appCode': APP_CODE, 'token': token, 'type': 1})
    if d.get('status') != 0:
        raise RuntimeError('森空岛 token 换 grant 失败：%s' % (d.get('msg') or d))
    t = (d.get('data') or {}).get('token')
    if not t:
        raise RuntimeError('森空岛 token 换 grant 失败：返回里没有 token')
    return t


def _bindings(grant: str, app_code: str) -> list:
    q = urllib.parse.urlencode({'token': grant, 'appCode': app_code})
    d = _get_json(BINDING_URL + '?' + q)
    if d.get('status') != 0:
        raise RuntimeError('取角色绑定失败：%s' % (d.get('msg') or d))
    out = []
    for app in (d.get('data') or {}).get('list') or []:
        out.extend(app.get('bindingList') or [])
    return out


def endfield_roles(token: str) -> list:
    """该 token 绑定的终末地角色（一个账号可能挂多个区服角色）

    返回 [{uid, roleId, nickName, level, serverId, serverName, channelMasterId,
           channelName, isDefault}]；没有绑定返回 []。
    """
    grant = grant_token(token)
    rows = []
    for b in _bindings(grant, 'endfield'):
        if b.get('isDeleted'):
            continue
        for r in b.get('roles') or []:
            if r.get('isBanned'):
                continue
            rows.append({
                # 抽卡接口要的是账号 uid（不是 roleId，roleId 会报 109）
                'uid': str(b.get('uid') or ''),
                'roleId': str(r.get('roleId') or ''),
                'nickName': r.get('nickName') or '',
                'level': r.get('level'),
                'serverId': str(r.get('serverId') or ''),
                'serverName': r.get('serverName') or '',
                'channelMasterId': str(b.get('channelMasterId') or ''),
                'channelName': b.get('channelName') or '',
                'isDefault': bool(r.get('isDefault')),
            })
    return rows


def role_token(grant: str, uid: str) -> str:
    d, _ = _http('POST', U8_URL, {'uid': str(uid), 'token': grant})
    if d.get('status') != 0:
        raise RuntimeError('取角色凭证失败：%s' % (d.get('msg') or d))
    t = (d.get('data') or {}).get('token')
    if not t:
        raise RuntimeError('取角色凭证失败：返回里没有 token')
    return t


# ---------------------------------------------------------------- 明日方舟
def _ark_login(u8: str) -> str:
    """换 ak-user-center cookie（只有 X-Role-Token 会 401 UN_LOGIN）"""
    d, hdrs = _http('POST', ARK_LOGIN_URL, {'token': u8})
    if d.get('code') not in (0, None):
        raise RuntimeError('角色登录失败：%s' % (d.get('msg') or d))
    cookie = ''
    try:
        for v in hdrs.get_all('Set-Cookie') or []:
            if 'ak-user-center=' in v:
                cookie = v.split(';')[0].strip()
                break
    except Exception:
        cookie = ''
    if not cookie:
        raise RuntimeError('角色登录失败：没拿到 ak-user-center cookie')
    return cookie


def ark_categories(u8: str, cookie: str, uid: str) -> list:
    """寻访分类是服务端动态给的（限定 / 标准 / 中坚 …），不能写死"""
    d = _get_json(ARK_CATE_URL + '?' + urllib.parse.urlencode({'uid': uid}),
                  headers=dict(ARK_REFERER, **{'X-Role-Token': u8, 'Cookie': cookie}))
    if d.get('code') != 0:
        raise RuntimeError('取寻访分类失败：%s' % (d.get('msg') or d))
    out = []
    for c in d.get('data') or []:
        if c.get('id'):
            out.append({'id': c['id'], 'name': ' '.join((c.get('name') or '').split())})
    return out


def ark_history(u8: str, cookie: str, uid: str, category: str, log=None) -> list:
    """按游标翻完一个分类的全部记录（新 → 旧）"""
    headers = dict(ARK_REFERER, **{'X-Role-Token': u8, 'Cookie': cookie})
    out, pos, gacha_ts, page = [], None, None, 0
    while True:
        q = {'uid': uid, 'category': category, 'size': PAGE_SIZE}
        if pos is not None:
            q['pos'], q['gachaTs'] = pos, gacha_ts
        d = _get_json(ARK_HIST_URL + '?' + urllib.parse.urlencode(q), headers=headers)
        if d.get('code') != 0:
            raise RuntimeError('取寻访记录失败（%s）：%s' % (category, d.get('msg') or d))
        data = d.get('data') or {}
        rows = data.get('list') or []
        for r in rows:
            out.append(_ark_norm(r, category))
        page += 1
        if not rows or not data.get('hasMore') or page > 60:
            break
        pos, gacha_ts = rows[-1].get('pos'), rows[-1].get('gachaTs')
    if log and page > 1:
        log('    %s：%d 条（%d 页）' % (category, len(out), page))
    return out


def _ark_norm(r: dict, category: str) -> dict:
    return {
        'category': category,
        'poolId': r.get('poolId') or '',
        'poolName': ' '.join((r.get('poolName') or '').split()) or '未知寻访',
        'charId': r.get('charId') or '',
        'charName': r.get('charName') or '?',
        'rarity': _norm_rarity(r.get('rarity')),
        'isNew': bool(r.get('isNew')),
        'gachaTs': str(r.get('gachaTs') or ''),
        'pos': int(r.get('pos') or 0),
    }


# ---------------------------------------------------------------- 终末地
def _ef_drain(url: str, params: dict, category: str, label: str, log=None,
              stop_below: int = 0) -> list:
    """终末地记录翻页：服务端每页固定 5 条，游标是上一页最后一条的 seqId

    2026-11 实测：不传 `seq_id` 时永远只返回最新 5 条（hasMore=true 也没用），
    `size` / `limit` / `pageSize` / `page` 全部被忽略；`seq_id` 表示「取 seqId
    小于它的最近 5 条」。所以只能一页页往前翻，翻到 hasMore=false。

    `stop_below` 是本地缓存里这一部分已有的最大 seqId：翻到它就停，增量同步
    只发「新增那几页」的请求（不传则翻完整窗口）。
    """
    out, cursor, pages, t0 = [], None, 0, time.time()
    while True:
        q = dict(params)
        if cursor is not None:
            q['seq_id'] = cursor
        try:
            d = _get_json(url + '?' + urllib.parse.urlencode(q))
        except urllib.error.HTTPError:
            if log:
                log('    %s：接口拒绝（HTTP）' % label)
            break
        if d.get('code') != 0:
            if log:
                log('    %s：%s' % (label, d.get('msg') or d.get('code')))
            break
        data = d.get('data') or {}
        rows = data.get('list') or []
        for r in rows:
            n = _ef_norm(r, category)
            if n:
                out.append(n)
        pages += 1
        cursor = rows[-1].get('seqId') if rows else None
        if log and pages % 20 == 0:
            log('    %s：已翻 %d 页…' % (label, pages))
        if not rows or not data.get('hasMore') or not cursor or pages > 400:
            break
        if stop_below and str(cursor).isdigit() and int(cursor) <= stop_below:
            break                      # 已接上本地缓存，更老的不必再拉
    if log:
        log('    %s：%d 条（%d 页，%.1f 秒）' % (label, len(out), pages, time.time() - t0))
    return out


def ef_history(u8: str, log=None, known: dict = None) -> list:
    """终末地抽卡记录：角色寻访（5 个池类型）+ 武器申领

    接口只保留最近一段窗口（实测角色约 400+ 抽、武器约 160 抽），更早的
    拿不到——不是本地截断，是服务端本身就这么返回。本地缓存只增不减，
    所以窗口滑走的旧记录不会丢。
    """
    known = known or {}
    char_stop = int(known.get('char') or 0)
    wp_stop = int(known.get('weapon') or 0)
    out = []
    for pool_type, label in EF_CHAR_POOLS:
        out.extend(_ef_drain(EF_RECORD_URL,
                             {'token': u8, 'lang': 'zh-cn', 'server_id': '1',
                              'pool_type': pool_type},
                             pool_type, label, log=log, stop_below=char_stop))
    out.extend(_ef_drain(EF_WEAPON_URL,
                         {'token': u8, 'lang': 'zh-cn', 'server_id': '1'},
                         EF_WEAPON_CATEGORY, '武器申领', log=log, stop_below=wp_stop))
    return out


def _ef_norm(r: dict, category: str) -> dict:
    """终末地记录 → 统一结构

    实测字段（2026-11，角色）：
        kind / poolId / poolName / nameText / charId / charName / rarity /
        isFree / isNew / gachaTs / seqId
    武器多两个 weaponId / weaponName / weaponType，poolId 长这样：
    `weaponbox_1_6_1`（官方还有写成 `weponbox` 的池，拼写不一致，展示按池名合并）
    """
    kind = (r.get('kind') or 'draw').lower()
    if kind != 'draw':
        return {}                      # 赠礼（gift_intel_book）之类的非抽卡记录
    seq = str(r.get('seqId') or '')
    return {
        'category': category,
        'kind': 'weapon' if category == EF_WEAPON_CATEGORY else 'char',
        'poolId': str(r.get('poolId') or ''),
        'poolName': ' '.join((r.get('poolName') or '未知寻访').split()),
        'charId': str(r.get('charId') or r.get('weaponId') or ''),
        'charName': (r.get('charName') or r.get('weaponName')
                     or r.get('nameText') or '?'),
        'rarity': _norm_ef_rarity(r.get('rarity')),
        'isNew': bool(r.get('isNew')),
        'isFree': bool(r.get('isFree')),
        'gachaTs': str(r.get('gachaTs') or ''),
        # 终末地没有 pos 字段，拿 seqId 当「同一秒内的次序」兼去重键
        'pos': int(seq) if seq.isdigit() else 0,
    }


def _norm_ef_rarity(v) -> int:
    """终末地星级是 1 基（3=三星 … 6=六星），实测分布 4/5/6 —— 原样返回，
    不能套明日方舟那套 +1（会把六星顶成七星、四星变五星）"""
    try:
        n = int(v or 0)
    except Exception:
        return 0
    return n if 1 <= n <= 6 else 0


def _norm_rarity(v) -> int:
    """明日方舟星级是 0 基（5 = 六星），统一 +1 到 1~6"""
    try:
        n = int(v or 0)
    except Exception:
        return 0
    if n <= 0:
        return 0
    return n + 1 if n < 6 else n


# ---------------------------------------------------------------- 缓存
def cache_path(file: str) -> str:
    """config/gacha/<账号文件名>.json（config 根下只扫 *.json，子目录不干扰账号列表）"""
    stem = os.path.splitext(os.path.basename(file or 'account'))[0]
    return os.path.join(settings.CONFIG_DIR, 'gacha', stem + '.json')


def load_cache(file: str) -> dict:
    try:
        with open(cache_path(file), encoding='utf-8') as f:
            d = json.load(f)
    except Exception:
        return {}
    if not isinstance(d, dict):
        return {}
    # v1 的终末地缓存只取了每池第一页（5 条）、星级还错了一位，留着会污染统计：
    # 读到就当没有，下次同步会整段重拉（明日方舟的 v1 缓存结构没问题，照用）
    if str(d.get('game')) == 'endfield' and int(d.get('version') or 1) < CACHE_VERSION:
        return {}
    return d


def _rec_key(r: dict) -> str:
    return '%s|%s|%s|%s|%s' % (r.get('category') or '', r.get('poolId') or '',
                               r.get('charId') or '', r.get('gachaTs') or '',
                               r.get('pos') or 0)


def save_cache(file: str, meta: dict, records: list) -> None:
    path = cache_path(file)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    atomicio.dump_json(path, {
        'version': CACHE_VERSION,
        'updatedAt': _now(),
        'game': meta.get('game') or 'arknights',
        'uid': str(meta.get('uid') or ''),
        'nickName': meta.get('nickName') or '',
        'records': records,
    })


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec='seconds')


def _ms(ms) -> int:
    """13 位毫秒 → int（空/坏值返回 0），排序用"""
    try:
        ts = int(str(ms or '').strip()[:13])
        return ts if ts > 0 else 0
    except Exception:
        return 0


def _at(ms) -> str:
    """13 位毫秒 → 本地 'YYYY-MM-DD HH:MM'（空/坏值返回 ''）"""
    ts = _ms(ms)
    if not ts:
        return ''
    return dt.datetime.fromtimestamp(ts / 1000.0).strftime('%Y-%m-%d %H:%M')


# ---------------------------------------------------------------- 同步
_locks = {}
_locks_guard = threading.Lock()


def _lock_for(file: str):
    with _locks_guard:
        return _locks.setdefault(os.path.basename(file), threading.Lock())


def fetch(token: str, game: str, uid: str, log=print, known: dict = None) -> list:
    """拉记录（不做缓存合并）。`known` 是本地已有记录的最大 seqId，用于增量"""
    grant = grant_token(token)
    u8 = role_token(grant, uid)
    if (game or 'arknights') == 'endfield':
        return ef_history(u8, log=log, known=known)
    cookie = _ark_login(u8)
    cats = ark_categories(u8, cookie, uid)
    log('    寻访分类 %d 个：%s' % (len(cats), '、'.join(c['name'] for c in cats)))
    out = []
    for c in cats:
        out.extend(ark_history(u8, cookie, uid, c['id'], log=log))
    return out


def sync(file: str, token: str, meta: dict, log=print) -> dict:
    """拉取 + 并入缓存（按 分类|池|干员|时间戳|pos 去重，旧记录不丢）"""
    with _lock_for(file):
        old = load_cache(file)
        merged = {}
        for r in old.get('records') or []:
            merged[_rec_key(r)] = r
        before = len(merged)
        # 终末地翻页成本高（每页固定 5 条）：拿本地已有的最大 seqId 做断点，
        # 只有新抽卡才会真的翻页，重复同步基本是一两个请求就回来
        known = {}
        if (meta.get('game') or '') == 'endfield':
            for r in merged.values():
                k = 'weapon' if (r.get('kind') or 'char') == 'weapon' else 'char'
                known[k] = max(known.get(k, 0), int(r.get('pos') or 0))
        fresh = fetch(token, meta.get('game') or 'arknights', str(meta.get('uid') or ''),
                      log=log, known=known)
        for r in fresh:
            merged[_rec_key(r)] = r
        records = sorted(merged.values(),
                         key=lambda r: (int(r.get('gachaTs') or 0), r.get('pos') or 0),
                         reverse=True)
        save_cache(file, meta, records)
        log('    本次拉到 %d 条，新增 %d 条，累计 %d 条'
            % (len(fresh), len(records) - before, len(records)))
        return summarize(records)


def start_sync(file: str, token: str, meta: dict) -> str:
    """后台同步（前端拿 taskId 轮询进度），返回任务 id"""
    title = '同步抽卡记录 · %s' % (meta.get('nickName') or file)
    holder = {}

    def go():
        log = tasks.log_factory(holder['tid'])
        try:
            log('账号：%s（%s）' % (meta.get('nickName') or file,
                                   '终末地' if meta.get('game') == 'endfield' else '明日方舟'))
            res = sync(file, token, meta, log=log)
            tasks.finish(holder['tid'], result=res)
        except Exception as e:
            import traceback
            log('✗ %s' % e)
            tasks.finish(holder['tid'], error='%s: %s' % (type(e).__name__, e),
                         result={'traceback': traceback.format_exc()})

    tid = tasks.create(title, None)
    holder['tid'] = tid
    threading.Thread(target=go, daemon=True, name='gacha').start()
    return tid


# ---------------------------------------------------------------- 统计
def _tally(rs: list) -> dict:
    """单一口径的汇总：星级分布、六星记录、保底、距上次六星

    保底按 **池子（poolId）连续计数**：同池每抽 +1，出六星清零。抽卡记录本身
    只有最近一段窗口，窗口之前的历史拿不到，所以最早那几次六星的「垫抽数」
    是从窗口内开始算的下限，不是真实值——界面上有脚注说明。
    """
    by_rarity = {str(i): 0 for i in range(1, 7)}
    counters, six = {}, []
    for r in rs:
        rar = int(r.get('rarity') or 0)
        if 1 <= rar <= 6:
            by_rarity[str(rar)] += 1
        pid = r.get('poolId') or r.get('category') or ''
        counters[pid] = counters.get(pid, 0) + 1
        if rar >= 6:
            six.append({
                'charName': r.get('charName') or '?',
                'charId': r.get('charId') or '',
                'kind': r.get('kind') or 'char',
                'poolName': r.get('poolName') or '未知寻访',
                'poolId': pid,
                'at': _at(r.get('gachaTs')),
                'pity': counters[pid],
                'isNew': bool(r.get('isNew')),
            })
            counters[pid] = 0

    total = len(rs)
    gap = 0
    for r in reversed(rs):
        if int(r.get('rarity') or 0) >= 6:
            break
        gap += 1
    six_count = by_rarity['6']
    return {
        'total': total,
        'byRarity': by_rarity,
        'sixCount': six_count,
        'rate6': round(six_count / total, 4) if total else 0,
        'avgPity': round(sum(s['pity'] for s in six) / len(six), 1) if six else 0,
        'lastGap': gap,
        'six': six,
    }


def _pools_of(rs: list) -> list:
    """按卡池分组（升序 rs 进，最新在前的六星挂在池子下，池子本身也最新的在前）

    合并口径：
    - 展示按 **池名** 合并——明日方舟的「标准寻访」会拆成好几期，终末地武器
      池的 poolId 还有 `weponbox` / `weaponbox` 两种拼法，按 id 分等于同名刷屏
    - 保底计数仍按 **poolId**——同一期池子跨期保底是连续的，按池名合并后整体
      +1 才对得上游戏里显示的垫抽数

    排序口径：按**最近一次抽卡时间倒序**，正在抽/刚抽完的池子永远在最上面
    （角色池、武器池同一套）。同一分钟再按抽数多的在前，没有时间的池子垫底。
    """
    pools, counters = {}, {}
    for r in rs:
        pid = r.get('poolId') or r.get('category') or ''
        pname = r.get('poolName') or '未知寻访'
        counters[pid] = counters.get(pid, 0) + 1
        p = pools.setdefault(pname, {
            'poolId': pid, 'poolName': pname,
            'count': 0, 'six': 0, 'firstTs': '', 'lastTs': '', 'lastMs': 0,
            'kind': r.get('kind') or 'char', 'sixItems': [],
        })
        p['count'] += 1
        at = _at(r.get('gachaTs'))
        if at:
            p['firstTs'] = p['firstTs'] or at
            p['lastTs'] = at
            p['lastMs'] = _ms(r.get('gachaTs'))
        if int(r.get('rarity') or 0) >= 6:
            p['six'] += 1
            p['sixItems'].append({
                'charName': r.get('charName') or '?',
                'at': at,
                'pity': counters[pid],
                'isNew': bool(r.get('isNew')),
            })
            counters[pid] = 0
    for p in pools.values():
        p['rate6'] = round(p['six'] / p['count'], 4) if p['count'] else 0
        p['sixItems'] = list(reversed(p['sixItems']))      # 新的在前
    out = sorted(pools.values(), key=lambda p: (p['lastMs'], p['count']), reverse=True)
    for p in out:
        p.pop('lastMs', None)                              # 内部排序用，不给前端
    return out


def _slice_summary(rs: list) -> dict:
    """一个口径的完整汇总：卡片数字 + 星级分布 + 六星时间线 + 按卡池 + 最近记录"""
    t = _tally(rs)
    recent = []
    for r in reversed(rs[-200:]):
        recent.append({
            'charName': r.get('charName') or '?',
            'rarity': int(r.get('rarity') or 0),
            'kind': r.get('kind') or 'char',
            'poolName': r.get('poolName') or '未知寻访',
            'at': _at(r.get('gachaTs')),
            'isNew': bool(r.get('isNew')),
        })
    return {
        'total': t['total'],
        'byRarity': t['byRarity'],
        'sixCount': t['sixCount'],
        'fiveCount': t['byRarity']['5'],
        'rate6': t['rate6'],
        'avgPity': t['avgPity'],
        'lastSixGap': t['lastGap'],
        'firstAt': _at(rs[0].get('gachaTs')) if rs else '',
        'lastAt': _at(rs[-1].get('gachaTs')) if rs else '',
        'sixStars': list(reversed(t['six'])),
        'pools': _pools_of(rs),
        'records': recent,
    }


def summarize(records: list) -> dict:
    """把记录汇总成界面要的数字

    终末地的「角色寻访」和「武器申领」是两套保底与概率（实测角色 6 星约
    1.9%、武器约 5%），界面按两个 tab 分开看，所以各给一份**完整**汇总
    （`char` / `weapon`：卡片数字、星级分布、六星时间线、按卡池、最近记录
    都在里面）。顶层字段仍是全部记录的口径，给账号卡等只需要一个总数的地方用。
    """
    rs = sorted(records or [],
                key=lambda r: (int(r.get('gachaTs') or 0), r.get('pos') or 0))
    chars = [r for r in rs if (r.get('kind') or 'char') != 'weapon']
    weapons = [r for r in rs if (r.get('kind') or 'char') == 'weapon']
    full = _slice_summary(rs)
    full['char'] = _slice_summary(chars)      # 明日方舟：char 就是全部记录
    full['weapon'] = _slice_summary(weapons) if weapons else None
    full['hasSix'] = bool(full['sixCount'])
    return full


def view(file: str, token: str, meta: dict) -> dict:
    """只读视图：读缓存算统计，不发网络请求"""
    cache = load_cache(file)
    records = cache.get('records') or []
    out = {
        'file': file,
        'game': meta.get('game') or 'arknights',
        'uid': str(meta.get('uid') or ''),
        'nickName': meta.get('nickName') or '',
        'hasToken': bool(token),
        'updatedAt': cache.get('updatedAt') or '',
        'count': len(records),
    }
    out['summary'] = summarize(records) if records else None
    return out
