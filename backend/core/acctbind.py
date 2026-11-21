# -*- coding: utf-8 -*-
r"""账号 ↔ MAA 切号名（AccountName）的解析与绑定

为什么单独一个模块
------------------
「这个账号跑 StartUp 时写哪个切号名」有三个调用方：助手建方案（agent._roster）、
单个 skill 唤醒（skills.skills_wakeup）、多账号编排（multiacct.candidates）。
2026-11-22 之前规则是「唯一来源 = 账号自己存的 maa.accountName，绝不读用户的
MAA 配置」——理由是 agent 跑的是临时配置，不碰用户的 gui.new.json。

用户随后提了更实际的要求：**设置里选了配置文件，就按那份档案的账号名来**
（他已经在 MAA 里配过一次，不该再手抄一遍）。所以现在的规则是：

1. ``maa.profile`` 选了档案、且没被手动改过 → 读该档案 StartUp 的 AccountName
2. 否则用账号里存的 ``maa.accountName``（手填的 / 上次对话里报的）
3. 都没有 → 空串，调用方去问用户（官服问后 4 位）

**只读，绝不写回用户的 MAA 配置**：档案是用户的，程序只在 ``maa.profile``
这一格引用它的名字；切换 / 改字段都走别的入口（写作业那套）。

``accountNameFrom`` 是这里唯一的新状态：``manual`` = 用户手填过（听他的），
``profile`` = 从配置档案带出来的（跟着档案走）。老账号没这个字段，按
profile 处理——反正没选档案时也不会去读配置。
"""
from __future__ import annotations

from . import accounts, acctname, maa, settings


def _maa_of(snap) -> dict:
    m = (snap or {}).get('maa')
    return m if isinstance(m, dict) else {}


def _snap(file, snap=None):
    if snap is not None:
        return snap
    try:
        return accounts.get_account(file) or {}
    except Exception:
        return {}


def dir_of(file='', snap=None) -> str:
    """这个账号用哪个 MAA 目录：账号自己绑的 → 全局默认

    2026-11-22 用户口径：每个账号都可以绑自己的 MAA 路径（设置 → MAA 路径
    按账号填），不同 MAA 往往连的是不同模拟器。所以「跑这个号」的时候
    必须按账号取目录，不能一律用全局那个——否则多开时全挤到同一台模拟器上，
    表现就是「说好挂五个号，结果只有一个号在动」。
    """
    d = str(_maa_of(_snap(file, snap)).get('dir') or '').strip()
    if d:
        return d
    try:
        return str(settings.load().get('maaDir') or '').strip()
    except Exception:
        return ''


def profile_of(file='', snap=None) -> str:
    """这个账号绑的 MAA 配置档案名（没绑给空串）"""
    return str(_maa_of(_snap(file, snap)).get('profile') or '').strip()


def channel_of(snap) -> str:
    a = (snap or {}).get('account')
    return str((a.get('channelName') if isinstance(a, dict) else '') or '')


def profile_name(profile, maa_dir=None) -> str:
    """档案里的 AccountName（读不到就给空串，绝不抛）"""
    try:
        return maa.profile_account_name(profile, maa_dir or None)
    except Exception:
        return ''


def resolve(file, snap=None):
    """账号跑 StartUp 时用的切号名 → ``(名字, 来源)``

    来源取值：``profile``（读自配置档案）/ ``saved``（账号里存的）/
    ``''``（还没有，要问用户）。名字已经过 ``acctname.check`` 归一化，
    直接可以写进 StartUp 任务参数。
    """
    if snap is None:
        try:
            snap = accounts.get_account(file)
        except Exception:
            snap = {}
    m = _maa_of(snap)
    ch = channel_of(snap)
    stored, _ = acctname.check(m.get('accountName') or '', ch)
    stored = stored or ''
    prof = str(m.get('profile') or '').strip()
    src = str(m.get('accountNameFrom') or '').strip()
    if prof and src != 'manual':
        live, _ = acctname.check(profile_name(prof, m.get('dir') or None), ch)
        if live:
            return live, 'profile'
    return stored, ('saved' if stored else '')


def bind(file, raw, channel=None):
    """把用户报的切号名存进账号（``maa.accountName``，标记为手填）

    返回 ``(归一化后的名字, 原因)``；名字不可用时不落盘，原因交给调用方
    原样告诉用户（官服填昵称这类）。
    """
    snap = {}
    try:
        snap = accounts.get_account(file)
    except Exception:
        pass
    ch = channel if channel is not None else channel_of(snap)
    nm, why = acctname.check(raw, ch)
    if not nm:
        return '', why
    accounts.save_maa_meta(file, {'accountName': nm, 'accountNameFrom': 'manual'})
    return nm, ''
