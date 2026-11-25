# -*- coding: utf-8 -*-
"""国内源：Mirror 酱加速（mirrorchyan.com）

和 appupdate（海外源：GitHub Releases）并列的一条下载链路：

    检查版本   → GET https://mirrorchyan.com/api/resources/<RID>/latest
    下载       → 响应里的 url（**带时效**，拿到就下，绝不落盘缓存）
    校验       → 响应里的 sha256 / filesize（**只能配这次这个包**）

三条来自官方文档、写错就出事的规矩：

1. **没有 `url` 不等于没有更新。** 不填 CDK 时照样返回 `version_name`，
   只是不给加速链接——所以「查版本」和「拿下载地址」是两件事，
   不能拿 `url` 是否存在去判断有没有新版本。
2. **哈希不能串源。** 从 Mirror 下的包只能用 Mirror 返回的 sha256 校验，
   从 GitHub 下的包只能用 GitHub 的 digest。两个包不是同一份字节
   （对面会重新打容器），混用等于随机拒装或放过坏包。
3. **`url` 有时效。** 落盘到 appupdate.json 隔天再点下载必然失败，
   所以带 url 的那份状态只在内存里活一次（见 appupdate._persistable）。

平台参数是**资源上传时的配置值**，不是文档里的通用写法：PR #137 的
workflow 写的是 `os=win` / `arch=x64`，按文档传 `windows` / `amd64`
会拿 8002 / 8003。改上传 workflow 时这里要一起看。

CDK 是用户在 Mirror 酱买的订阅凭证，等同密码：DPAPI 加密存
`config/mirror.key`，`settings.json` 里只有一个「有没有配」的位置都不留
（由本模块 _load_cdk 直接读文件判断）。日志 / 报错里**不许出现 CDK，
也不许出现带 cdk 参数的完整 URL**——本模块抛错时只拼 host + code。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from . import settings, vault

# 资源 ID / 平台参数：与 .github/workflows/mirrorchyan_release.yml 一致
RID = 'MAA-PRTS'
API = 'https://mirrorchyan.com/api/resources/%s/latest' % RID
OS = 'win'
ARCH = 'x64'
UA = 'maaprts_desktop'

# 国内 CDN，直连比走代理快（net.open 是给 GitHub 那条链路用的，不在这里用）
TIMEOUT = 8

KEY_PATH = os.path.join(settings.CONFIG_DIR, 'mirror.key')

# 错误码 → 给用户看的一句话。code 含义见官方 ErrorCode.md：
# 7001 过期 / 7002 错误 / 7003 次数用完 / 7004 类型不匹配 / 7005 封禁
HINTS = {
    -1: 'Mirror 酱服务端异常，稍后再试',
    1: 'Mirror 酱返回了一个未分类的错误',
    1001: '请求参数不对（这是程序的问题，请反馈）',
    7001: 'CDK 已过期，请续费或换一个',
    7002: 'CDK 不对，请检查后重新填写',
    7003: '这个 CDK 今天的下载次数用完了，明天再试',
    7004: '这个 CDK 不适用于 MAA-PRTS，请换一个',
    7005: 'CDK 已被封禁，请联系 Mirror 酱客服',
    8001: 'Mirror 酱上暂时没有这个版本的包（可能还没上传完）',
    8002: '系统参数不对（这是程序的问题，请反馈）',
    8003: '架构参数不对（这是程序的问题，请反馈）',
    8004: '更新通道参数不对（这是程序的问题，请反馈）',
    8009: '流程测试 CDK 不能真正下载，请换成正式 CDK',
}


class MirrorError(RuntimeError):
    """业务错误（code != 0）或网络错误。code 为 0 表示网络/解析层的问题

    str(e) 是可以直接贴给用户的中文，**保证不含 CDK 与完整 URL**。
    """

    def __init__(self, code: int, msg: str = '', hint: str = ''):
        self.code = int(code or 0)
        self.msg = str(msg or '')
        self.hint = hint or HINTS.get(self.code) or (self.msg or 'Mirror 酱请求失败')
        super().__init__(self.hint)

    @property
    def cdk_error(self) -> bool:
        """CDK 自身的问题（过期 / 填错 / 超次数 / 不匹配 / 封禁）

        这类要把提示说清楚，让用户去改 CDK，而不是含糊地说「下载失败」。
        """
        return 7001 <= self.code <= 7005


# 错误码 → 前端用的状态词。界面只认这几个词，不认数字（数字变了不用动界面）
CDK_STATES = {7001: 'expired', 7002: 'invalid', 7003: 'quota',
              7004: 'mismatch', 7005: 'blocked'}


def cdk_state(code) -> str:
    """业务码 → cdkState；非 CDK 错误统一 'error'（网络 / 参数 / 未知）"""
    return CDK_STATES.get(int(code or 0), 'error')


# ---------------------------------------------------------------- CDK
def _load_cdk() -> str:
    """读 DPAPI 加密的 CDK；没有 / 解不开（换机器了）返回空串"""
    try:
        with open(KEY_PATH, 'rb') as f:
            return vault.unprotect(f.read()).decode('utf-8').strip()
    except Exception:
        return ''


def cdk() -> str:
    return _load_cdk()


def has_cdk() -> bool:
    return bool(_load_cdk())


def mask(key: str = '') -> str:
    """给界面看的掩码：只露后 4 位，短值全遮

    CDK 本身就是凭证，界面上也只该显示「配过没有」+ 末尾几位，
    方便用户确认自己填的是哪一个。
    """
    k = key or _load_cdk()
    if not k:
        return ''
    if len(k) <= 4:
        return '·' * len(k)
    return '·' * min(8, len(k) - 4) + k[-4:]


def save_cdk(v) -> dict:
    """保存 / 清除 CDK（空串 = 清除）。返回 status()，绝不回传明文"""
    key = str(v or '').strip()
    if not key:
        try:
            os.remove(KEY_PATH)
        except OSError:
            pass
        return status()
    if len(key) > 128:
        raise ValueError('CDK 太长，检查一下是不是粘多了')
    if any(c.isspace() for c in key):
        raise ValueError('CDK 里不该有空格，检查一下是不是粘多了')
    os.makedirs(settings.CONFIG_DIR, exist_ok=True)
    blob = vault.protect(key.encode('utf-8'))
    tmp = KEY_PATH + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(blob)
    os.replace(tmp, KEY_PATH)
    return status()


def status() -> dict:
    """设置页要的：配没配、掩码。到期时间在 check() 之后由 appupdate 带回来"""
    key = _load_cdk()
    return {
        'hasCdk': bool(key),
        'masked': mask(key),
        'rid': RID,
    }


# ---------------------------------------------------------------- 检查
def _to_ts(v):
    """cdk_expired_time 可能是秒也可能是毫秒，统一成秒级时间戳（失败返回 0）"""
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return 0
    if n > 10 ** 12:      # 毫秒
        n //= 1000
    return n if n > 10 ** 9 else 0


def expires_text(v) -> str:
    """时间戳 → '2026-12-08'；拿不到返回空串"""
    ts = _to_ts(v)
    if not ts:
        return ''
    return time.strftime('%Y-%m-%d', time.localtime(ts))


def _fmt_current(v) -> str:
    """current_version 要和上传的 version_name 完全同格式（我们带 v）

    发版 CI 的 tag 是 `vX.Y.Z`（PR 标题「Release vX.Y.Z」），uploading-action
    又原样把 tag 当 version_name，所以这里补 v。一期只请求全量包（传空），
    这个函数留给二期开增量时用。
    """
    s = str(v or '').strip()
    if not s or s == 'dev':
        return ''
    return s if s.startswith('v') else 'v' + s


def _request(url: str, timeout: int) -> dict:
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        # 4xx/5xx 也可能带业务 JSON（7002 之类），先按 JSON 解；解不出来
        # 就只说 HTTP 状态——**别把 URL 拼进错误信息**（里面有 cdk）
        raw = b''
        try:
            raw = e.read() or b''
        except Exception:
            pass
        if raw[:1] in (b'{', b' '):
            return _parse(raw)
        raise MirrorError(0, 'HTTP %d' % e.code,
                          'Mirror 酱返回 HTTP %d，稍后再试' % e.code)
    except Exception as e:  # noqa: BLE001 - 超时 / DNS / 断网
        raise MirrorError(0, str(e), '连不上 Mirror 酱：%s' % e)
    return _parse(raw)


def _parse(raw: bytes) -> dict:
    try:
        d = json.loads(raw.decode('utf-8', 'replace'))
    except Exception:
        raise MirrorError(0, 'not json', 'Mirror 酱返回的内容看不懂（不是 JSON）')
    if not isinstance(d, dict):
        raise MirrorError(0, 'not dict', 'Mirror 酱返回的内容看不懂')
    try:
        code = int(d.get('code') or 0)
    except (TypeError, ValueError):
        code = 1
    if code != 0:
        raise MirrorError(code, str(d.get('msg') or ''))
    data = d.get('data')
    if not isinstance(data, dict):
        data = {}
    return data


def check(current_version: str = '', channel: str = 'stable',
          timeout: int = TIMEOUT, cdk_override=None) -> dict:
    """问一次最新版本。

    ``current_version`` 留空 = 请求**完整包**（一期就传空，不做增量；
    见 docs/update.md 的「一期为什么只走全量」）。传了才有机会拿到
    update_type=incremental 的增量包。

    ``cdk_override`` 只给设置页「验证」用：用还没保存的那串去试，
    不读也不写 config/mirror.key。

    失败抛 MirrorError（带中文提示）；成功返回规整后的 dict。
    """
    params = {'os': OS, 'arch': ARCH, 'user_agent': UA,
              'channel': channel or 'stable'}
    cur = _fmt_current(current_version)
    if cur:
        params['current_version'] = cur
    key = (cdk_override if cdk_override is not None else _load_cdk()).strip()
    if key:
        params['cdk'] = key

    url = API + '?' + urllib.parse.urlencode(params)
    data = _request(url, timeout)

    version = str(data.get('version_name') or '').strip()
    if not version:
        # 没有 version_name 就没法比版本，当成一次失败（不是「已是最新」）
        raise MirrorError(0, 'no version_name', 'Mirror 酱没返回版本号')

    return {
        'ok': True,
        'code': 0,
        'version': version,
        'url': str(data.get('url') or ''),
        'sha256': str(data.get('sha256') or '').strip().lower(),
        'size': int(data.get('filesize') or 0),
        'updateType': str(data.get('update_type') or 'full').strip().lower(),
        'notes': str(data.get('release_note') or '')[:20000],
        'expiredAt': expires_text(data.get('cdk_expired_time')),
        'channel': str(data.get('channel') or channel or 'stable'),
        'queryVersion': cur,
        'withCdk': bool(key),
    }


def probe(cdk_value=None) -> dict:
    """设置页「验证 CDK」：用给定的（或已存的）CDK 查一次

    不论成败都返回 dict，方便前端直接显示；**不回传 CDK 明文**。
    """
    key = (cdk_value if cdk_value is not None else _load_cdk())
    key = str(key or '').strip()
    try:
        r = check(cdk_override=key, timeout=TIMEOUT)
    except MirrorError as e:
        return {'ok': False, 'code': e.code, 'hint': str(e),
                'cdkError': e.cdk_error, 'hasCdk': bool(key),
                'cdkState': cdk_state(e.code)}
    return {
        'ok': True,
        'code': 0,
        'version': r['version'],
        'withCdk': r['withCdk'],
        'canDownload': bool(r['url']),
        'expiredAt': r['expiredAt'],
        'cdkState': '',
        'hint': ('CDK 有效，可以加速下载' if r['url']
                 else '没填 CDK：能查版本，但下载会走 GitHub'),
        'hasCdk': bool(key),
    }
