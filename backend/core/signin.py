# -*- coding: utf-8 -*-
"""森空岛每日签到（明日方舟 / 终末地）

链路（2026-11 在本机用真实 token 实测）：

    token ──skland.Skland──▶ cred + sign_token
      ├─ 明日方舟  GET  /api/v1/game/attendance?uid=&gameId=1   查今天签没签
      │            POST /api/v1/game/attendance {uid, gameId:1}
      └─ 终末地    GET  /api/v1/game/endfield/attendance        查 hasToday
                   POST /api/v1/game/endfield/attendance        角色身份在 header 里

几个踩过的点：
  - 终末地 **不吃 uid**：拿 uid 去打 /api/v1/game/attendance 一律 HTTP 403；
    必须走 /api/v1/game/endfield/attendance，身份放在
    `sk-game-role: 3_<roleId>_<serverId>` 头里（gameId_roleId_serverId）
  - 今天签过之后 POST 返回的是 **HTTP 403**，不是带 code 的业务错误：所以先查
    状态再签；真撞上 403 就按「今天已签到」记，不当失败（重试也只会更糟）
  - 签到是写操作，403 不重试（retries=1），多试几次只会更容易被风控
  - 一个 token 可能绑多个角色（官服 + B 服、方舟 + 终末地）：按账号文件逐个签，
    同一 (游戏, uid) 只签一次，免得重复请求把「已签到」报成失败

本模块只用标准库，可以直接打包进 exe。
"""
from __future__ import annotations

import datetime as dt
import threading
import time
import traceback
import urllib.error
import urllib.parse

from . import accounts, skland, tasks

ARK_PATH = '/api/v1/game/attendance'
EF_PATH = '/api/v1/game/endfield/attendance'
# binding_list 里直接给了 gameId：方舟 1 / 来自星尘 2 / 终末地 3
EF_GAME_ID = 3
EF_ORIGIN = 'https://game.skland.com/'
_TZ = dt.timezone(dt.timedelta(hours=8))

GAME_NAME = {'arknights': '明日方舟', 'endfield': '终末地'}


# ---------------------------------------------------------------- 时间
def _day(ts=None) -> str:
    """时间戳（秒）→ 上海时区的日期串；不传就是今天"""
    try:
        t = dt.datetime.fromtimestamp(float(ts), _TZ) if ts is not None else dt.datetime.now(_TZ)
    except Exception:
        t = dt.datetime.now(_TZ)
    return t.strftime('%Y-%m-%d')


def _is_today(ts) -> bool:
    """方舟的 records.ts 是秒级时间戳；按上海时区比日期（不是 24 小时窗口）"""
    try:
        return _day(int(float(ts))) == _day()
    except Exception:
        return False


# ---------------------------------------------------------------- 请求
def _ef_headers(role) -> dict:
    return {
        'sk-game-role': '%s_%s_%s' % (EF_GAME_ID, role['roleId'], role['serverId']),
        'Content-Type': 'application/json',
        'Referer': EF_ORIGIN,
        'Origin': EF_ORIGIN,
    }


def _ark_signed(sk, uid) -> bool:
    q = urllib.parse.urlencode({'uid': str(uid), 'gameId': 1})
    d = sk.api_get(ARK_PATH + '?' + q)
    if d.get('code') != 0:
        raise RuntimeError('查签到状态失败：%s' % (d.get('message') or d.get('code')))
    recs = (d.get('data') or {}).get('records') or []
    return any(_is_today(r.get('ts')) for r in recs)


def _ef_signed(sk, role) -> bool:
    d = sk.api_get(EF_PATH, headers=_ef_headers(role))
    if d.get('code') != 0:
        raise RuntimeError('查签到状态失败：%s' % (d.get('message') or d.get('code')))
    return bool((d.get('data') or {}).get('hasToday'))


def _ark_sign(sk, uid) -> list:
    d = sk.api_post(ARK_PATH, {'uid': str(uid), 'gameId': 1}, retries=1)
    if d.get('code') != 0:
        raise RuntimeError(d.get('message') or '签到失败 code=%s' % d.get('code'))
    out = []
    for a in ((d.get('data') or {}).get('awards') or []):
        name = (a.get('resource') or {}).get('name') or '奖励'
        out.append('%s×%s' % (name, a.get('count') or 1))
    return out


def _ef_sign(sk, role) -> list:
    d = sk.api_post(EF_PATH, None, headers=_ef_headers(role), retries=1)
    if d.get('code') != 0:
        raise RuntimeError(d.get('message') or '签到失败 code=%s' % d.get('code'))
    data = d.get('data') or {}
    info = data.get('resourceInfoMap') or {}
    out = []
    for a in data.get('awardIds') or []:
        r = info.get(a.get('id')) or {}
        out.append('%s×%s' % (r.get('name') or '奖励', r.get('count') or 1))
    return out


def _sign_one(sk, acc) -> tuple:
    """给一个账号签到：返回 (state, text)，state = ok / already / skip"""
    game = acc.get('game') or 'arknights'
    if game == 'endfield':
        role = {'roleId': str(acc.get('roleId') or ''),
                'serverId': str(acc.get('serverId') or '')}
        if not role['roleId'] or not role['serverId']:
            return 'skip', '账号里没有 roleId / serverId（重新同步一次账号就能签）'
        if _ef_signed(sk, role):
            return 'already', '今天已签到'
        awards = _ef_sign(sk, role)
    else:
        uid = str(acc.get('uid') or '')
        if not uid:
            return 'skip', '账号没有 uid'
        if _ark_signed(sk, uid):
            return 'already', '今天已签到'
        awards = _ark_sign(sk, uid)
    return 'ok', ('签到成功：' + '、'.join(awards)) if awards else '签到成功'


# ---------------------------------------------------------------- 批量
def run(file: str = '', log=None) -> dict:
    """签到。file 为空 = 全部账号（方舟 + 终末地），否则只签这个账号"""
    log = log or (lambda s: None)
    rows = accounts.list_accounts()
    if file:
        pick = [r for r in rows if r['file'] == file]
        if not pick:
            raise RuntimeError('账号不存在：%s' % file)
        rows = pick
        log('单号签到：%s' % (rows[0].get('nickName') or file))
    else:
        ark = sum(1 for r in rows if (r.get('game') or 'arknights') != 'endfield')
        log('全局签到：%d 个账号（明日方舟 %d / 终末地 %d）' % (len(rows), ark, len(rows) - ark))

    counts = {'ok': 0, 'already': 0, 'fail': 0, 'skip': 0}
    items = []
    if not rows:
        log('没有账号，先在左侧添加一个')
        return {'counts': counts, 'items': items, 'day': _day()}

    sessions, broken, seen = {}, {}, set()

    def session(token):
        """同一个 token 只登录一次；登录失败过就别再拿它撞第二次"""
        if token in broken:
            raise RuntimeError(broken[token])
        sk = sessions.get(token)
        if sk is None:
            log('  · 登录森空岛…')
            sk = skland.Skland(token, log=lambda m: log('    ' + m.strip()))
            sessions[token] = sk
        return sk

    for i, acc in enumerate(rows, 1):
        name = acc.get('nickName') or acc['file']
        game = acc.get('game') or 'arknights'
        log('[%d/%d] %s（%s）' % (i, len(rows), name, GAME_NAME.get(game, game)))

        key = (game, str(acc.get('uid') or acc.get('roleId') or acc['file']))
        if key in seen:
            log('  · 跳过：同一个角色已经签过（另有账号文件指向它）')
            counts['skip'] += 1
            items.append({'file': acc['file'], 'nickName': name, 'game': game,
                          'state': 'skip', 'text': '同一角色重复'})
            continue
        seen.add(key)

        token = ''
        state, text = 'skip', ''
        if not acc.get('hasToken'):
            text = '没有森空岛 token（MAA 导入的账号签不了）'
        else:
            token = accounts.token_for(acc['file'])
            if not token:
                text = 'token 解不开（换过电脑？重新粘贴一次 token 再试）'
        if text:
            log('  · 跳过：%s' % text)
            counts['skip'] += 1
            items.append({'file': acc['file'], 'nickName': name, 'game': game,
                          'state': 'skip', 'text': text})
            continue

        try:
            state, text = _sign_one(session(token), acc)
        except urllib.error.HTTPError as e:
            if e.code == 403:
                state, text = 'already', 'HTTP 403：今天已经签过'
            else:
                state, text = 'fail', 'HTTP %s' % e.code
        except Exception as e:
            state, text = 'fail', str(e)
            if token not in sessions:
                broken[token] = text          # 登录就挂了，后面的号别再撞
        counts[state] = counts.get(state, 0) + 1
        mark = {'ok': '✓', 'already': '✓', 'fail': '✗'}.get(state, '·')
        log('  %s %s' % (mark, text))
        items.append({'file': acc['file'], 'nickName': name, 'game': game,
                      'state': state, 'text': text})
        if i < len(rows):
            time.sleep(0.4)                   # 连着打太快容易触发风控

    log('—— 完成：成功 %d · 已签到 %d · 失败 %d · 跳过 %d'
        % (counts['ok'], counts['already'], counts['fail'], counts['skip']))
    return {'counts': counts, 'items': items, 'day': _day()}


def start(file: str = '') -> str:
    """后台签到（前端拿 taskId 轮询日志），返回任务 id"""
    title = '森空岛签到 · 全部账号'
    if file:
        acc = next((r for r in accounts.list_accounts() if r['file'] == file), None)
        title = '森空岛签到 · %s' % ((acc or {}).get('nickName') or file)
    holder = {}

    def go():
        log = tasks.log_factory(holder['tid'])
        try:
            res = run(file, log=log)
            tasks.finish(holder['tid'], result=res)
        except Exception as e:
            log('✗ %s' % e)
            tasks.finish(holder['tid'], error='%s: %s' % (type(e).__name__, e),
                         result={'traceback': traceback.format_exc()})

    tid = tasks.create(title, None)
    holder['tid'] = tid
    threading.Thread(target=go, daemon=True, name='signin').start()
    return tid
