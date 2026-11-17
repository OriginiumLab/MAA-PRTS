# -*- coding: utf-8 -*-
"""森空岛 player/info → 关卡进度

同一个接口里本来就带着推图进度、活动区域进度、剿灭最高击杀、日常周常，
以前只取了等级和干员列表，其余全丢——所以「读不到关卡进度」这个结论是错的，
只是没人解析。

**口径差异（重要）**：森空岛的 totalStage 把剧情关和突袭关都算在内，我们的
关卡表不含剧情关，两边数字对不齐。所以这里只做 zone 级「还有剩」判断，
绝不拿 clearedStage 去切我们的关卡列表——那样会错位（act11d7 森空岛 14 关、
我们表里 8 关，差的就是这些）。
"""
from __future__ import annotations

from . import resources

# 剿灭满击杀：maxKills 小于它就是没打满
CAMPAIGN_FULL = 400


def parse(info) -> dict:
    """player_info 的 data → 进度模型

    字段缺失一律给 None / []，不抛异常：森空岛的返回是游戏内部结构，
    多个字段总比一次同步失败好。
    """
    info = info or {}
    st = info.get('status') or {}
    return {
        'level': st.get('level'),
        'ap': _ap(st.get('ap')),
        'main': _main(st.get('mainStageProgress')),
        'campaign': _campaign(info.get('campaign')),
        'activities': _activities(info.get('activity')),
        'routine': _routine(info.get('routine')),
    }


def summary(prog) -> dict:
    """给界面用的一句话汇总：主线还差几关、活动还差几个区域、剿灭几张没满"""
    prog = prog or {}
    main = prog.get('main') or {}
    # 只算能对上我们关卡表的活动：森空岛列表里的老复刻活动没有 zone 数据，
    # 算进去会让「还有 40 关没打」这种数字变得毫无意义
    acts = [a for a in (prog.get('activities') or [])
            if a.get('matched') and a.get('left')]
    camps = [c for c in (prog.get('campaign') or []) if not c.get('full')]
    return {
        # mainKnown=false 表示森空岛这次没给 mainStageProgress（字段为 null），
        # mainRemaining 的 0 是「没有数据」不是「打完了」。助手/界面别拿它当已通关。
        'mainKnown': bool(main),
        'mainRemaining': main.get('remaining') or 0,
        'mainNext': main.get('next'),
        'activityLeft': len(acts),
        'activityStages': sum(a.get('left') or 0 for a in acts),
        'campaignLeft': len(camps),
    }


def _brief(s):
    return {'code': s.get('code') or '',
            'stageId': s.get('stageId') or '',
            'name': s.get('name') or '',
            'difficultyCN': s.get('difficultyCN') or ''}


def _main(sid):
    """主线进度点 → 当前关 + 后面还剩哪些战斗关

    mainStageProgress 给的是 stageId（st_09-01 这种），而且经常正好落在剧情关
    上——第 9 章起每章开场就是一段剧情，占掉了 9-1 这个代号，第一场战斗叫 9-2。
    所以「推到哪」和「下一关是哪」要分开说，不能拿代号硬推。
    """
    if not sid:
        return None
    row = resources.stage_detail(sid) or {}
    after = resources.stages_after(sid)
    return {
        'stageId': sid,
        'code': row.get('code') or '',
        'name': row.get('name') or '',
        'story': resources.story_of(sid),
        'remaining': len(after),
        'next': _brief(after[0]) if after else None,
        'stages': [_brief(s) for s in after[:30]],
    }


def _campaign(camp):
    """剿灭：maxKills 是唯一没有歧义的进度指标（< 400 就是没打满）"""
    out = []
    for r in ((camp or {}).get('records') or []):
        cid = str(r.get('campaignId') or '')
        if not cid:
            continue
        kills = r.get('maxKills')
        row = resources.stage_detail(cid) or {}
        out.append({
            'id': cid,
            'name': row.get('name') or cid,     # 切尔诺伯格
            'area': row.get('code') or '',      # 乌萨斯
            'maxKills': kills,
            'full': (kills or 0) >= CAMPAIGN_FULL,
        })
    # 没打满的排前面，同档按击杀升序（越少越缺）
    out.sort(key=lambda r: (r['full'], r['maxKills'] or 0, r['id']))
    return out


def _activities(acts):
    """活动：zone 级已打 / 总数

    left 是 zone 的余量，不是「我们表里还剩几关能抄」——包含剧情关和突袭关
    的计数差没法精确映射，具体的关号要用户自己勾（见 docs/plan-v2.6.md M1）。
    """
    za = resources.zone_activity()
    out = []
    for a in (acts or []):
        zones = []
        for z in (a.get('zones') or []):
            zid = str(z.get('zoneReplicaId') or z.get('zoneId') or '')
            if not zid:
                continue
            cleared = int(z.get('clearedStage') or 0)
            total = int(z.get('totalStage') or 0)
            zones.append({
                'zoneId': zid,
                'name': za.get(zid) or '',
                'cleared': cleared,
                'total': total,
                'matched': zid in za,
                'left': max(0, total - cleared),
            })
        if not zones:
            continue
        out.append({
            'actId': str(a.get('actId') or ''),
            'replica': str(a.get('actReplicaId') or ''),
            'name': (next((z['name'] for z in zones if z['name']), '')
                     or str(a.get('actReplicaId') or a.get('actId') or '')),
            'zones': zones,
            'left': sum(z['left'] for z in zones),
            'matched': any(z['matched'] for z in zones),
        })
    # 能对上我们关卡表的排前面——森空岛的 activity 列表里混着大量很早的
    # 复刻活动，它们在我们表里查不到 zone，也不该占据「还有剩」的位置
    out.sort(key=lambda r: (not r['matched'], -r['left'], r['name'], r['actId']))
    return out


def _ap(ap):
    if not ap:
        return None
    return {'current': ap.get('current'), 'max': ap.get('max')}


def _routine(r):
    r = r or {}
    return {'daily': r.get('daily'), 'weekly': r.get('weekly')}
