# -*- coding: utf-8 -*-
"""MAA 运行日志桥：读 ``<MAA>/debug/asst.log``，把 MAA 自己说的话翻成中文进度

为什么要多这一层
----------------
MaaCore 的回调（``maacore._translate``）只覆盖「任务链 / 子任务 / 少数 extra」，
战斗过程里「往哪部署、开没开技能、停在哪一步、为什么没打下去」这些细节，
一半靠 ``SubTaskExtraInfo``（已在 ``maaplan._on_core_event`` 补上），另一半
**只写在 asst.log 的纯文本行里**，根本不走回调：

    [ERR][Px19672][Tx20991] asst::MultiCopilotTaskPlugin::confirm_stage_name
    confirm stage name failed after retrying 3 times, stage name: MN-EX-6

用户 2026-11-18 报的「自动战斗没有日志」，就是这两段都没人读——面板上只有
「开始：自动战斗」和最后的「完成」。

分工（别重复）
------------
- 回调侧的进度（换作业 / 编队 / 部署 / 理智 / 掉落 / 培养结果）走
  ``maaplan._on_core_event``，实时、带结构化数据；
- 本模块**只读回调之外的纯文本**：``[ERR]`` 行（认不出关卡名、干员不存在、
  任务参数不合法…）。回调 JSON 行（``append_callback``）一律跳过，避免刷两遍。

原则：宽松解析。认不出的行一律跳过；MAA 升级改格式，最坏结果只是日志变少，
绝不抛异常、绝不拖慢跑机线程。
"""
from __future__ import annotations

import re
import time

from . import asstlog

# 这些是已知噪音：读进来只会把右栏刷满，一句有用信息都没有
_NOISE = (
    'roi is empty',
    'image is empty',
    'Unknown key or value',
    'FightSeries',
    'set_specific_support_unit',
    'update deployment failed',
    'Task not found',          # 老资源缺 pipeline，MAA 自己会兜底
    'Unknown task:',
)

_LEVEL = re.compile(r'\[(ERR|WRN)\]\[[^\]]*\]\[[^\]]*\]\s*(.*)$')

# 常见报错 → 人话。命中就整句换成这里，没命中走「MAA 报错：原文」
_HINTS = (
    (re.compile(r'confirm stage name failed.*?stage name:\s*(.+)$'),
     'MAA 认不出关卡名「%s」（连试 3 次），这一关多半点空了'),
    (re.compile(r'unknown oper name:\s*([^,]+)'),
     'MAA 不认识干员「%s」，检查名字有没有写错'),
    (re.compile(r'(?:Cannot set stage|stage name is not in invalid).*?([A-Za-z0-9\u4e00-\u9fa5-]+)\s*$'),
     'MAA 不接受关卡「%s」（资源里没有，或这关不在当前地图）'),
    (re.compile(r'missing plans'),
     '培养计划是空的（plans 没传进去）'),
    (re.compile(r'oper plan does not support drone:\s*(.+)$'),
     '「%s」是装置/召唤物，没有技能可专精'),
    # 这条 ERR 其实无害：作业里把装置/召唤物写成了「干员组」，MAA 找不到就跳过。
    # 原文一行 `asst::BattleProcessTask::to_group Group not found in combat
    # data:  雪崩触发装置` 挂在「开始任务」下面，用户只会以为程序坏了
    # （2026-11-22 用户对着这条报错问「战斗日志有问题」）。
    (re.compile(r'to_group .*?combat data:\s*(.+)$'),
     '作业里的「%s」是装置/召唤物占位，MAA 认不出会跳过，不影响打'),
)


def _humanize(body: str) -> str:
    """一行 MAA 日志正文 → 一句中文；认不出返回原文（截断）"""
    s = body.strip()
    for pat, fmt in _HINTS:
        m = pat.search(s)
        if m:
            return fmt % m.group(1).strip()
    return 'MAA 报错：' + (s[:160] + ('…' if len(s) > 160 else ''))


class Tap:
    """盯住 MAA 日志的增量游标

    用法::

        tap = Tap(maa_dir)
        while running:
            for level, text in tap.poll():
                log(text)

    ``poll()`` 只返回**本次开跑之后**新增的行：构造时直接定位到文件末尾，
    不会把上一轮那几十 MB 历史重放一遍（用户一开跑就刷屏）。
    """

    def __init__(self, maa_dir=None, min_gap=20.0, initial_tail=2 * 1024 * 1024):
        paths = asstlog.log_paths(maa_dir)
        self.path = paths.get('asst') or ''
        self.tail = asstlog.Tail(self.path, initial_tail=initial_tail) if self.path else None
        # 同一句报错 20 秒内只说一次：MAA 的 retry 循环会把同一句写十几遍
        self.min_gap = min_gap
        self._last = {}
        if self.tail:
            try:
                self.tail.seek_end()
            except Exception:
                self.tail = None

    def poll(self):
        """返回 [(level, text)]；level 取 err / warn"""
        if not self.tail:
            return []
        out = []
        try:
            lines = self.tail.read_new()
        except Exception:
            return []
        for line in lines:
            item = self.translate(line)
            if item:
                out.append(item)
        return out

    def translate(self, line):
        m = _LEVEL.search(line or '')
        if not m:
            return None
        level, body = m.group(1).lower(), m.group(2)
        low = body.lower()
        if any(n.lower() in low for n in _NOISE):
            return None
        # 警告只留「重试到上限 / 超时」这类能解释卡住的，其余（识别框为空等）跳过
        if level == 'wrn' and not any(k in low for k in ('retry', 'timeout', 'fail')):
            return None
        text = _humanize(body)
        key = re.sub(r'\d+', '#', text)[:90]
        now = time.time()
        if now - self._last.get(key, 0) < self.min_gap:
            return None
        self._last[key] = now
        if len(self._last) > 200:                 # 别让它无限长
            self._last = dict(list(self._last.items())[-100:])
        return level, text
