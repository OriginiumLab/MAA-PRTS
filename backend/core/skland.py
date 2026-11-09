# -*- coding: utf-8 -*-
"""森空岛（Skland）数据接口

token → grant code → cred + sign_token → 签名请求 → 干员数据

签名算法（社区逆向，非官方）：
    s = path + (GET: query / POST: json body) + timestamp + json({platform,timestamp,dId,vName})
    sign = md5( hmac_sha256(key=sign_token, msg=s) )
    注意四参数 JSON 顺序必须是 platform → timestamp → dId → vName

本模块只用标准库，可以直接打包进 exe。
"""
from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

APP_CODE = '4ca99fa6b56cc2ba'
GRANT_URL = 'https://as.hypergryph.com/user/oauth2/v2/grant'
CRED_URL = 'https://zonai.skland.com/api/v1/user/auth/generate_cred_by_code'
API = 'https://zonai.skland.com'
SKLAND_UA = ('Skland/1.0.1 (com.hypergryph.skland; build:100001014; '
             'Android 31; ) Okhttp/4.11.0')
DID = 'de9759a5afaa634f'

_RETRY_HOOK = None      # 由调用方注入 log 函数，用于提示重试


def set_logger(fn):
    global _RETRY_HOOK
    _RETRY_HOOK = fn


def _note(msg):
    if _RETRY_HOOK:
        try:
            _RETRY_HOOK(msg)
        except Exception:
            pass


def _http(method, url, headers, body=None, timeout=30, retries=3, note=None):
    """带退避重试的请求。

    headers 可以传 dict，也可以传无参函数（每次重试重新签名，避免时间戳过期返 401）。
    只重试网络层超时/断连/坏响应体，以及**可重签名请求**的 401/403（签名过期）；
    登录链（grant/cred）的 401/403 是坏 token 本身，重试纯属浪费。其他 4xx 直接抛出。
    note 是本实例的日志回调（重试提示写进各自任务的日志，#61）。
    """
    data = None
    if body is not None:
        data = json.dumps(body, separators=(',', ':')).encode('utf-8')

    last = None
    for attempt in range(1, max(1, retries) + 1):
        h = headers() if callable(headers) else headers
        if body is not None:
            h = dict(h, **{'Content-Type': 'application/json'})
        try:
            req = urllib.request.Request(url, data=data, headers=h, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get('Content-Encoding') == 'gzip':
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode('utf-8'))
        except urllib.error.HTTPError as e:
            last = e
            # 401/403 只有「headers 是函数」的可重签名请求才值得重试（时间戳
            # 过期）；登录链的死 token 重试三次只是浪费
            if e.code not in (401, 403) or not callable(headers) or attempt >= retries:
                raise
            (note or _note)('    [签名重试 %d/%d] HTTP %s' % (attempt, retries - 1, e.code))
            time.sleep(1.0 * attempt)
        except (urllib.error.URLError, socket.timeout, TimeoutError,
                ConnectionError, OSError) as e:
            last = e
            if attempt >= retries:
                raise
            (note or _note)('    [网络重试 %d/%d] %s' % (attempt, retries - 1, e))
            time.sleep(1.5 * attempt)
        except ValueError:
            # 网关/代理返回 HTML 错误页时 json.loads 抛 JSONDecodeError（ValueError
            # 子类）：按网络类重试换下一轮，而不是冒泡成同步任务失败
            last = ValueError('响应不是 JSON（网关错误页？）')
            if attempt >= retries:
                raise last
            (note or _note)('    [网络重试 %d/%d] 响应不是 JSON' % (attempt, retries - 1))
            time.sleep(1.5 * attempt)
    if last:
        raise last


class Skland:
    def __init__(self, token: str, did: str = DID, log=None):
        self.token = token          # 森空岛用户 token（鹰角通行证）
        self.did = did
        self._note = log            # 重试提示随实例走：两个同步任务并行时不再
                                    # 互相写进对方的任务日志（#61）
        self.cred = None            # 32 位凭证
        self.sign_token = None      # 签名密钥
        self.login()

    # ---------------- 认证链 ----------------
    def login(self):
        g = _http('POST', GRANT_URL,
                  {'User-Agent': SKLAND_UA, 'Accept-Encoding': 'identity',
                   'Connection': 'close', 'dId': self.did},
                  {'appCode': APP_CODE, 'token': self.token, 'type': 0},
                  note=self._note)
        if g.get('status') != 0:
            raise RuntimeError('换取 grant code 失败: %s' % (g.get('msg') or g))
        grant = g['data']['code']

        c = _http('POST', CRED_URL,
                  {'User-Agent': SKLAND_UA, 'Accept-Encoding': 'identity',
                   'Connection': 'close', 'dId': self.did},
                  {'code': grant, 'kind': 1},
                  note=self._note)
        if c.get('code') != 0:
            raise RuntimeError('换取 cred 失败: %s' % (c.get('msg') or c))
        self.cred = c['data']['cred']
        self.sign_token = c['data']['token']
        return self

    # ---------------- 签名 ----------------
    def _sign(self, path: str, payload: str):
        ts = str(int(time.time()) - 2)      # 官方对时间精度敏感，退 2 秒
        ca = {'platform': '3', 'timestamp': ts, 'dId': self.did, 'vName': '1.0.0'}
        ca_str = json.dumps(ca, separators=(',', ':'))
        s = path + payload + ts + ca_str
        h = hmac.new(self.sign_token.encode('utf-8'), s.encode('utf-8'),
                     hashlib.sha256).hexdigest()
        sign = hashlib.md5(h.encode('utf-8')).hexdigest()
        return {
            'User-Agent': SKLAND_UA,
            'Accept-Encoding': 'identity',
            'Connection': 'close',
            'cred': self.cred,
            'sign': sign,
            'platform': ca['platform'],
            'timestamp': ca['timestamp'],
            'dId': self.did,
            'vName': ca['vName'],
            'X-Requested-With': 'com.hypergryph.skland',
        }

    def api_get(self, path_query: str, timeout=40):
        p = urllib.parse.urlparse(API + path_query)
        # 传函数而不是现成 headers：每次重试都重新签名，时间戳才是新的
        return _http('GET', API + path_query,
                     lambda: self._sign(p.path, p.query or ''),
                     timeout=timeout, note=self._note)

    def _get(self, path_query: str, timeout=40):
        """401/403 视作 cred 过期：重新走一次鉴权链再请求"""
        try:
            return self.api_get(path_query, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code not in (401, 403):
                raise
            self._note('    [凭证过期] 重新登录一次')
            self.login()
            return self.api_get(path_query, timeout=timeout)

    # ---------------- 数据 ----------------
    def bindings(self):
        d = self._get('/api/v1/game/player/binding')
        if d.get('code') != 0:
            raise RuntimeError('取绑定失败: %s' % (d.get('msg') or d))
        out = []
        for app in d['data']['list']:
            if app.get('appCode') != 'arknights':
                continue
            out.extend(app.get('bindingList') or [])
        return out

    def player_info(self, uid: str):
        d = self._get('/api/v1/game/player/info?uid=%s' % uid)
        if d.get('code') != 0:
            raise RuntimeError('取玩家数据失败: %s' % (d.get('msg') or d))
        return d['data']
