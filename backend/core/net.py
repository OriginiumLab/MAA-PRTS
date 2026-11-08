# -*- coding: utf-8 -*-
"""更新链路共用的 HTTP 入口：设置里配了代理（settings.proxy）就走代理

为什么不用 urllib 自带的 ProxyHandler：它对 https:// 目标只会把请求
原样发给代理端口，不会先发 CONNECT 建隧道（CPython 一直没实现），
而更新源（github.com / raw.githubusercontent.com / jsdelivr）全是
https，等于必挂。所以 https 走这里自己写的隧道连接：

    TCP 连代理 → CONNECT 目标:443 → 在隧道里对目标做 TLS

open() 带直连回退：配了代理但连不上（没开 / 超时 / 不可达）时自动换
直连重试，代理挂了不把更新检查一起弄死。设置页保存时的「检测」用
check() 摸真可用性。

只给 appupdate / packsync 用：森空岛、作业站都是国内直连服务，跟着
走代理反而可能失败，不走这里。
"""
from __future__ import annotations

import http.client
import socket
import time
import urllib.error
import urllib.request

from . import settings


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """appupdate 要读 302 的 Location（releases/latest → tag），不能跟"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _proxy_addr(p: str):
    """'http://127.0.0.1:7890' → ('127.0.0.1', 7890)；格式不对返回 None

    settings._norm_proxy 已把存进来的值都归一成带端口的 http://host:port，
    这里只做防御性解析：坏值一律按「没有代理」处理——检查更新宁可直连
    重试，也别拿一个连不上的代理把所有路都堵死。
    """
    try:
        _scheme, _, rest = str(p or '').partition('://')
        host, _, port = rest.rpartition(':')
        if not host or not port.isdigit():
            return None
        return (host.strip('[]'), int(port))
    except Exception:
        return None


class _TunnelHTTPSConnection(http.client.HTTPSConnection):
    """经 HTTP 代理访问 https 目标：CONNECT 隧道（标准库缺的那块）"""

    def __init__(self, host, port=None, proxy_addr=None, **kw):
        super().__init__(host, port, **kw)
        self._proxy_addr = proxy_addr
        # do_open 传进来的 host/port 是真实目标；连 TCP 到代理、做 TLS 对目标
        self.set_tunnel(host, port if port is not None else self.default_port)

    def connect(self):
        # 与 HTTPSConnection.connect 同序，只是 TCP 目标换成代理：
        # 连代理 → CONNECT 目标 → 隧道里对目标握手 TLS（SNI 也是目标）
        self.sock = self._create_connection(self._proxy_addr, self.timeout,
                                            self.source_address)
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self._tunnel()
        self.sock = self._context.wrap_socket(self.sock,
                                              server_hostname=self._tunnel_host)


class _ProxyHTTPSHandler(urllib.request.HTTPSHandler):
    """把 https 请求接进 _TunnelHTTPSConnection"""

    def __init__(self, proxy_addr):
        self._proxy_addr = proxy_addr
        super().__init__()

    def https_open(self, req):
        addr = self._proxy_addr

        def factory(host, **kw):
            return _TunnelHTTPSConnection(host, proxy_addr=addr, **kw)

        # check_hostname 是 3.12 才有的参数，老版本连接类不收
        kw = {'context': self._context}
        if hasattr(self, '_check_hostname'):
            kw['check_hostname'] = self._check_hostname
        return self.do_open(factory, req, **kw)


def _build_opener(redirect=True, proxy=None, addr=None):
    """按需组装 opener；代理为空时与 build_opener 完全一致（含环境变量）"""
    extra = () if redirect else (_NoRedirect(),)
    if not addr:
        return urllib.request.build_opener(*extra)
    return urllib.request.build_opener(
        *extra,
        urllib.request.ProxyHandler({'http': proxy}),   # http:// 目标：绝对 URI 形式
        _ProxyHTTPSHandler(addr),                       # https:// 目标：CONNECT 隧道
    )


def open(req, timeout=None, redirect=True):
    """urllib.request.urlopen 的带代理版（redirect=False 不跟随跳转）

    配了代理但连不上（拒绝 / 超时 / 不可达 / 隧道被拒）时自动回退直连
    重试一次。拿到 HTTP 应答（哪怕 4xx/5xx）不算「连不上」，照常抛给
    上层按业务处理——这里的请求全是 GET/HEAD，重试无副作用。
    """
    p = str(settings.load().get('proxy') or '').strip()
    addr = _proxy_addr(p) if p else None
    direct = _build_opener(redirect)
    if not addr:
        return direct.open(req, timeout=timeout)
    try:
        return _build_opener(redirect, p, addr).open(req, timeout=timeout)
    except urllib.error.URLError as e:
        # HTTPError（有状态码的应答）的 reason 是字符串，只有网络层错误
        # 才回退——别把「代理明确拒绝了这个请求」也偷偷绕过去
        if isinstance(getattr(e, 'reason', None), OSError):
            return direct.open(req, timeout=timeout)
        raise


def check(p, timeout_tcp=2, timeout_http=8):
    """设置页「检测」用：代理端口开着吗 + 经它真能摸到 GitHub 吗

    只探测调用方拼好的这串值，不读 settings（还没保存呢）。
    """
    addr = _proxy_addr(p)
    if not addr:
        return {'ok': False, 'error': '地址格式不对，要像 http://127.0.0.1:7890'}
    t0 = time.time()
    try:
        with socket.create_connection(addr, timeout=timeout_tcp):
            pass
    except OSError as e:
        return {'ok': False, 'error': '连不上 %s:%d（代理没开？）：%s' % (addr[0], addr[1], e)}
    ms = int((time.time() - t0) * 1000)
    req = urllib.request.Request('https://github.com', method='HEAD',
                                 headers={'User-Agent': 'MAA-PRTS/probe'})
    try:
        with _build_opener(True, p, addr).open(req, timeout=timeout_http) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code   # 拿到 HTTP 状态 = 隧道和出网都通了
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'tcpMs': ms,
                'error': '端口开着，但经它访问 GitHub 失败：%s' % e}
    return {'ok': True, 'tcpMs': ms, 'status': code}
