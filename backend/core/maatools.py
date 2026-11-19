# -*- coding: utf-8 -*-
"""MAA「小工具 → 牛杂」里那几项的触发方式

2026-11-19 用户要求把其中两项接进助手：**战时新闻委员会**（活动小游戏）和
**活动商店**。两项在 MAA 里都是 ``Custom`` 任务，实测（v6.22.0 的 MaaCore）：

    战时新闻委员会   {"task_names": ["MiniGame@WartimePressCommittee@Begin"]}
    活动商店         {"task_names": ["SS@Store@Begin"],
                      "params": {"event_shop": {"blacklist": ["数据增补条", ...]}}}

黑名单的三个坑（都是对着 asst.log 试出来的，别改）
--------------------------------------------------
1. 位置：必须挂在 ``params.event_shop`` 下面。挂到顶层 / 塞进 params 字符串
   都会被**静默忽略**，asst.log 里只有 ``CustomTask::set_params | leave``，
   跑起来就把无限池一起买光。
2. 非空：``blacklist`` 给空数组时 MaaCore **不注册** EventShop 插件（不打
   ``Parsed and registered EventShop task``），同样会买无限池。所以这里强制
   至少一条。
3. 类型：必须是字符串数组。写成数字会直接失败：
   ``set_params failed, event shop blacklist item is not string``。
   给对了 asst.log 会出现
   ``Parsed and registered EventShop task: SS@Store@Begin``。

导航
----
这两项都在**当期活动地图**上（和「残照终尽」那些分区节点同一屏），所以流程
是「活动地图 → 点节点 → 再交给 MaaCore」，导航部分见 :func:`eventnav.run_tool`。

教程
----
小游戏第一次进要过一段教程，MAA 的任务不会替你点教程里的对话/选项——它会
一直停在同一个节点上重试。用户 2026-11-19 的要求：碰到这种情况要明确告诉
用户「手动把教程过一遍，再挂一次」，不要闷着转。
"""
from __future__ import annotations

#: 用户 2026-11-19 在牛杂面板里勾的四个（数据增补条 / 数据增补 / 龙门币 /
#: 家具零件）。活动商店默认就按这份黑名单买，用户传 blacklist 可覆盖。
DEFAULT_BLACKLIST = ['数据增补条', '数据增补', '龙门币', '家具零件']

TOOLS = {
    'wartime_press': {
        'name': '战时新闻委员会',
        'alias': ('战时新闻委员会', '新闻委员会', '战时新闻', '小游戏',
                  'wartime_press', 'press', 'wartime'),
        'nav': 'minigame',                  # eventnav ENTRIES[...]['tools'] 的键
        'task': {'task_names': ['MiniGame@WartimePressCommittee@Begin']},
        'timeout': 1800,
        'tutorial': ('这个功能第一次进要过一段教程，MAA 不会替你点教程里的'
                     '对话和选项，会一直停在同一个节点上重试。如果日志卡住'
                     '不动，先手动把教程过一遍，再让我挂一次。'),
    },
    'event_shop': {
        'name': '活动商店',
        'alias': ('活动商店', '活动兑换', '商店', 'event_shop', 'shop', 'store'),
        'nav': 'shop',
        'task': {'task_names': ['SS@Store@Begin']},
        'timeout': 1800,
        'note': '到商店页面再交给 MAA；黑名单里的商品不会买，无限池也不会碰。',
    },
}

#: 需要先导航到活动地图的键（nav 字段），eventnav 那边要有同名入口
NAV_KEYS = ('minigame', 'shop')


def _norm(s):
    return ''.join(str(s or '').split()).lower()


def resolve(name):
    """用户说的名字 → ``(键, 配置)``；认不出抛 ValueError（把可选值列出来）"""
    key = _norm(name)
    if key:
        if key in TOOLS:
            return key, TOOLS[key]
        for k, ent in TOOLS.items():
            if key in {_norm(a) for a in ent.get('alias') or ()}:
                return k, ent
            if key == _norm(ent.get('name')):
                return k, ent
    raise ValueError('没有这个小工具：%s（可选：%s）'
                     % (name, '、'.join('%s=%s' % (k, v['name'])
                                       for k, v in TOOLS.items())))


def build_params(key, blacklist=None):
    """拼 ``Custom`` 任务的 params（活动商店一定带非空黑名单）"""
    ent = TOOLS[key]
    task = dict(ent.get('task') or {})
    task['task_names'] = list(task.get('task_names') or [])
    if key != 'event_shop':
        return task

    bl = [str(x) for x in (blacklist if blacklist is not None
                           else DEFAULT_BLACKLIST) if str(x).strip()]
    if not bl:
        # 空数组 MaaCore 不注册插件，等于「连无限池一起买」——宁可报错
        raise ValueError('活动商店的黑名单不能为空（空着 MAA 会把无限池也买掉）')
    task['params'] = {'event_shop': {'blacklist': bl}}
    return task


def describe():
    """给助手看的清单"""
    rows = []
    for k, ent in TOOLS.items():
        rows.append({'key': k, 'name': ent.get('name'),
                     'alias': list(ent.get('alias') or ()),
                     'tutorial': bool(ent.get('tutorial')),
                     'blacklist': (list(DEFAULT_BLACKLIST)
                                   if k == 'event_shop' else [])})
    return rows
