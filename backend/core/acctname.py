# -*- coding: utf-8 -*-
r"""切号名（MAA 的 AccountName）的归一化与校验

为什么单独一个模块
------------------
同一件事有三个调用方：agent 的 build_run、multiacct 的多账号编排、maaplan
写档案。之前 agent 那边自己写了一份 ``^\d{3}\*{4}\d{4}$`` 的严格校验，
而 MAA 实际是**子串匹配**：官方说明里明写「可输入 123****4567、4567 或
23****45」三种。用户照 MAA 的说明只给了后四位，我们判成非法 → 助手只能
反复追问 → 任务出不来。规则只放在这里，谁都别再自己写一份。

MAA 到底怎么比
--------------
StartUp 任务的切号是拿 ``AccountName`` 去和登录界面 OCR 出来的那行做
**包含**比较，所以下面三种都认（官方说明原话）：

- 纯尾号   ``5505``         —— **首选**。问用户只要后四位，基本不会撞号
- 部分掩码 ``23****45``
- 完整掩码 ``181****5505``

用户手里往往只有一个片段。**能补全的补成完整掩码，补不全的原样保留**，
别拿"格式不对"去卡他——格式错是我们的活，不是他的。

对话里一律问「手机号后 4 位」，别让用户去拼 ``****``：拼错的是他，报错的是我们。
"""
from __future__ import annotations

import re
import unicodedata

# 各种「星号」：全角、数学、装饰符都当打码段。用户从记事本、微信里复制
# 出来的经常是这些变体，NFKC 只转得了全角那一个。
_STARS = '*＊✱✳﹡∗×✕✖╳•·●○■□'
_ZERO_WIDTH = '\u200b\u200c\u200d\u2060\ufeff'
# 数字之间的分隔符：空格、横线、点、括号、斜杠……用户爱怎么打怎么打
_SEPS = ' \t\r\n\u3000-－—_.,，、/\\|:：;；()（）[]【】<>《》"\'`'

_DIGITS = re.compile(r'\D')


def is_official_channel(channel) -> bool:
    """是不是官服。渠道缺失时按官服算（默认值，最保守）

    空渠道当官服：官服的账号名是打码手机号，校验严一点最多是"多问一句"；
    反过来把官服当 B 服放过去，就是切号死循环 4 分钟——两头代价不对等。
    """
    c = str(channel or '')
    if not c:
        return True
    low = c.lower()
    if '官服' in c or 'official' in low:
        return True
    # MAA 支持的四类里，其余三类都不是手机号（B 服/繁中/韩服是用户名或邮箱）
    for k in ('bilibili', 'b服', '繁中', '韩', '国际', '日服', '台服', '美服'):
        if k in low:
            return False
    return True


def _mask(digits: str, head: int, tail: int) -> str:
    return '%s****%s' % (digits[:head], digits[-tail:])


def normalize(v) -> str:
    """把用户给的切号名整理成 MAA 认的写法

    能吃下的写法（结果都是 ``181****5505``）：

    - ``1815505``（前 3 + 后 4，最常见）
    - ``18112345505``（完整手机号）
    - ``181****5505`` / ``181＊5505`` / ``181*5505`` / ``181***5505``
    - ``181 5505`` / ``181-5505`` / ``181_5505`` / ``（181）5505``
    - ``18123****45505`` 这种整号打码的（前后段都超长，只留前 3 后 4）

    B 服用户名、昵称原样返回（只做去空白、统一星号）。
    """
    s = str(v or '')
    for ch in _ZERO_WIDTH:
        s = s.replace(ch, '')
    s = unicodedata.normalize('NFKC', s).strip().strip('"\'`“”‘’ ')
    if not s:
        return ''
    # 星号先统一，否则下面按分隔符切的时候会被当成普通字符
    # 只换字符、不合并连续星号：181****5505 的 4 个星是有意义的
    # （re.sub 带 + 会压成 1 个，后面的位数判断就全乱了）
    s = ''.join('*' if ch in _STARS else ch for ch in s)
    body = s
    for ch in _SEPS:
        body = body.replace(ch, '')
    if not body:
        return ''

    star = '*' in body
    digits = _DIGITS.sub('', body)

    if star:
        # 打码段：只认「纯数字 + 星号」的组合，其余（昵称里带 *）不动
        if digits and re.fullmatch(r'[\d*]+', body):
            m = re.fullmatch(r'(\d+)\*+(\d+)', body)
            if m:
                head, tail = m.group(1), m.group(2)
                # 前后段加起来就是手机号长度：取前 3 后 4，中间一律 4 个星
                if len(head) + len(tail) >= 7:
                    if len(head) >= 3 and len(tail) >= 4:
                        return _mask(head + tail, 3, 4)
                    # 23****45 这种只有 4 位有效数字的，原样保留前后段
                    return '%s****%s' % (head, tail)
                return body
        return body

    if not digits:
        return body
    if not re.fullmatch(r'[\d*]+', body):
        # 数字和字母混着 → B 服用户名（灰bao#7986），不碰
        return body
    if len(digits) == 11 and digits.startswith('1'):
        return _mask(digits, 3, 4)          # 完整手机号
    if len(digits) == 7:
        return _mask(digits, 3, 4)          # 前 3 后 4
    # 3~10 位的片段原样给 MAA 做子串匹配（本机档案里就有 '136' / '3635'）
    return digits if 3 <= len(digits) <= 10 else body


def looks_official(nm) -> bool:
    """长得像不像官服的切号名：只有数字和星号，且至少 3 位数字

    这样 ``5505`` / ``136`` / ``23****45`` / ``181****5505`` 都算，昵称
    「小卡拉帕鲁灰」不算——官服拿昵称去匹配就是死循环几十次然后切号失败。

    3 位也放行的依据：本机 MAA 里用户自己那几份档案写的就是 ``136`` /
    ``165``（纯数字片段，AccountSwitchEnabled=true），一直能切。
    """
    s = str(nm or '')
    if not s or not re.fullmatch(r'[\d*]+', s):
        return False
    return len(_DIGITS.sub('', s)) >= 3


def check(nm, channel=None):
    """校验并归一化；给不出可用的值就返回 ``(None, 原因)``

    这层只拦真正跑不起来的东西（空、官服填了昵称）。格式一律先归一再判，
    绝不把「星号数量不对」这类问题丢回给用户。
    """
    raw = str(nm or '').strip()
    s = normalize(raw)
    if not s:
        return None, '没填'
    if is_official_channel(channel) and not looks_official(s):
        return None, ('官服切号名要用手机号（给后 4 位就行，如 5505）；'
                      '「%s」不是手机号，像昵称，对不上官服账号列表' % raw)
    return s, ''
