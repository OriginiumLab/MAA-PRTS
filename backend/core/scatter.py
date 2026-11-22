# -*- coding: utf-8 -*-
"""散点式关卡（S 关这类）：MAA 认不出，我们自己点进去、用单作业模式打

为什么单独一条路
----------------
当期活动（雪山降临1101 / OS）的 S 关**不在关卡列表里**：点开活动地图右下角的
「喀兰之心」，进去是一张满屏雪山地图，五个 OS-S-x 按钮浮在上面（左上竖条写着
「积雪清理系统」）。MaaCore 的 Copilot 只在**关卡列表**里整屏 OCR 找关卡名
（``Copilot@ClickStageName``），这种界面它一个都认不出来——用户 2026-11-23 的
原话是「MAA 无法识别 S 关卡」。

所以这条路完全不开 Copilot 的导航：

1. 定点模板认出五个格子（:func:`cells_of`）。没解锁的格子游戏不画，认不到就
   跳过——**不要拿坐标硬点**，点在空地上虽然多半无害，但会把雪山的装饰当成
   关卡点开。
2. 点格子 → 等右侧详情面板 → 点「开始行动」（普通）或
   「突袭模式 → 开始突袭」（突袭）。
3. 界面进到干员编队页后，让 MaaCore 用**单作业模式**开打：Copilot 参数只给
   ``filename``、不给 ``copilot_list``（MaaCore 里那句话是
   ``CopilotTask set_params failed, stage_name or filename not found``，说明
   filename 单独成立）。单作业模式**不做 ClickStageName 导航**，界面在哪就从
   哪开始，正好配合我们手点到编队页。
4. 单作业模式打完只发 ``TaskChainCompleted``，**不判三星、也不点结算页**
   （asst.log 实证：``BattleProcessTask`` 完成 → 直接 ``TaskChainCompleted``，
   中间没有 StageDrops / EndOfAction）。所以「点一下回关卡界面」也归我们：
   循环点屏幕，直到重新认出格子。

模板都在 ``navtpl/scatter/``，1920 口径；``find_card_bytes`` 会按截图宽度缩放
（1920 宽时 1.0 倍，正好是原图）。
"""
import os
import re
import time

from . import stagenav

#: 相似度阈值，和 ``stagenav._CARD_MAX_SCORE`` 同口径（越小越像）
_MAX = 12.0

#: 一个活动一条。``cells`` 的键就是关卡编号（OS-S-3 → 3）
LAYOUTS = {
    'OS': {
        'label': '积雪清理系统',
        # 活动地图上点哪个分区节点进来（和 eventnav 的 zones 键一致）
        'zone': 's',
        # 每个格子带一个 ROI（1920 口径 x,y,w,h）：模板里带了不少雪山地貌，
        # 单独拿去整屏搜会认到别的格子上（实测 S2 的模板在 S5 上只有 6.9 分，
        # 比阈值还低）。ROI 把命中位置也锁死，两重保险。
        'cells': {
            1: {'tpl': 'OSs_1.png', 'roi': [220, 295, 420, 180]},
            2: {'tpl': 'OSs_2.png', 'roi': [1325, 295, 420, 180]},
            3: {'tpl': 'OSs_3.png', 'roi': [220, 576, 420, 180]},
            4: {'tpl': 'OSs_4.png', 'roi': [1325, 576, 420, 180]},
            5: {'tpl': 'OSs_5.png', 'roi': [765, 771, 420, 180]},
        },
        # 详情页
        'start': 'OSs_start.png',              # 「开始行动」（普通难度）
        'raid': 'OSs_raid.png',                # 「突袭模式」开关
        'start_raid': 'OSs_start_raid.png',    # 「开始突袭」
        # 编队页右上角那个「快捷编队」：认到它就说明已经进了干员列表，可以
        # 交给 MaaCore 的单作业模式了。
        # **别用右下角那条竖的「开始行动」当判据**：它入场时有滑入动画、亮度
        # 也跟着结算主题变，同一个位置重截一帧能从 0 分掉到 999（实测两次
        # 编队页截图，竖条模板一次 0 分一次认不出）。「快捷编队」是静止的。
        'formation': 'OSs_quickform.png',
    },
}


# ---------------------------------------------------------------- 模板与识别
def _tpl(name):
    """模板路径（用户配置目录优先，其次内置目录）"""
    if not name:
        return ''
    for d in stagenav._navtpl_dirs():
        p = os.path.join(d, 'scatter', name)
        if os.path.isfile(p):
            return p
    return ''


def _hit(data, name, roi=None):
    """认一个模板 → ``(像素x, 像素y, 分数)``；没认到返回 ``(0, 0, 分数)``

    ``roi`` 是 1920 口径的 ``[x, y, 宽, 高]``：命中点落在这个框外就当没认到。
    模板里带着背景，整屏搜会认到隔壁格子上（实测 S2 的模板在 S5 上 6.9 分，
    比阈值还低），所以位置也要一起验。
    """
    p = _tpl(name)
    if not p or not data:
        return 0, 0, 999.0
    x, y, sc = stagenav.find_card_bytes(data, p)
    if not x or sc > _MAX:
        return 0, 0, sc
    if roi and len(roi) == 4:
        try:
            from PIL import Image
            import io as _io
            w, _h = Image.open(_io.BytesIO(data)).size
            k = w / 1920.0
            x1, y1 = roi[0] * k, roi[1] * k
            x2, y2 = (roi[0] + roi[2]) * k, (roi[1] + roi[3]) * k
            if not (x1 <= x <= x2 and y1 <= y <= y2):
                return 0, 0, sc+100
        except Exception:
            pass
    return x, y, sc


def layout_of(prefix):
    return LAYOUTS.get(str(prefix or '').upper())


def has(stage_code):
    """这个代号有没有散点布局（``OS-S-3`` 也算——它自己取前缀）

    以前只按前缀查，调用方传完整代号时会一律返回 False，分派永远不成立。
    注意 ``OS-EX-1`` 也会返回 True（前缀一样），所以调用方还要再用
    :func:`cell_index` 卡一下形状。
    """
    code = str(stage_code or '').strip().upper()
    pre = code.split('-', 1)[0] if '-' in code else code
    return layout_of(pre) is not None


_CELL_RE = re.compile(r'^[A-Z][A-Z0-9]*-S-(\d+)$')


def cell_index(stage_code):
    """``OS-S-3`` → ``3``；不是 S 关的写法一律返回 0

    必须卡死 ``<前缀>-S-<数字>`` 这个形状：``rsplit('-', 1)`` 那种写法会把
    ``OS-EX-1`` 也解析成 1，于是挂 EX 关会被点成 S-1（同一张地图上的第一个
    格子）。宁可返回 0 让调用方跳过，也不能打错关。
    """
    m = _CELL_RE.match(str(stage_code or '').strip().upper())
    return int(m.group(1)) if m else 0


def cells_of(data, prefix):
    """截图上认得出的格子 → ``{编号: (像素x, 像素y)}``

    只返回**真的认到**的：未解锁的关卡游戏不画按钮，这里自然就没有它。
    """
    lay = layout_of(prefix)
    if not lay or not data:
        return {}
    out = {}
    for idx, spec in (lay.get('cells') or {}).items():
        if isinstance(spec, dict):
            _x, _y, _sc = _hit(data, spec.get('tpl'), spec.get('roi'))
        else:
            _x, _y, _sc = _hit(data, spec)
        if _x:
            out[int(idx)] = (_x, _y)
    return out


def is_here(data, prefix):
    """这一帧是不是散点关卡界面（认出任意一个格子就算）"""
    return bool(cells_of(data, prefix))


def in_formation(data, prefix):
    """这一帧是不是干员编队页（右上是返回/快捷编队，右下是竖条「开始行动」）"""
    lay = layout_of(prefix)
    if not lay:
        return False
    return bool(_hit(data, lay.get('formation'))[0])


def has_detail(data, prefix):
    """右侧关卡详情面板是不是展开着（「开始行动」/「开始突袭」在就是）

    ``back_to_cells`` 用它当「还没回到干净界面」的判据：详情面板展开时格子
    照样认得出，光看 ``is_here`` 会误判成「已经回来了」，于是下一关点在面板上。
    """
    lay = layout_of(prefix)
    if not lay:
        return False
    return bool(_hit(data, lay.get('start'))[0]
                or _hit(data, lay.get('start_raid'))[0])


# ---------------------------------------------------------------- 界面动作
def _wait(touch, cond, log, timeout=10.0, step=1.0, what=''):
    """反复截图直到 ``cond(data)`` 成立；返回 ``(成立, 最后一帧)``"""
    t0 = time.time()
    data = b''
    while time.time() - t0 <= timeout:
        data = touch.shot()
        if data and cond(data):
            return True, data
        time.sleep(step)
    if what:
        log('等了 %.0f 秒，%s还没出现' % (timeout, what))
    return False, data


def open_stage(touch, prefix, idx, raid, log=None, pos=None, timeout=12.0):
    """点开某一关到**编队界面**，返回 ``(ok, 说明)``

    流程：点格子 → 等详情面板 → （突袭：点「突袭模式」等「开始突袭」出现）
    → 点开始 → 等编队页。

    ``pos`` 是进来时认到的坐标。**多关连续跑一定要传**：详情面板一展开整屏
    会蒙一层暗色，模板分数从 0 涨到 11 分上下，这时重新识别会把「有」看成
    「没有」（实测 S4 就是这样被判成没解锁）。

    没解锁 / 点空了的表现是**详情面板不出现**，这里如实返回 False，调用方跳过
    这一关继续下一关——不在这里重试，因为再点还是一样的结果。
    """
    log = log or (lambda *_: None)
    lay = layout_of(prefix)
    if not lay:
        return False, '「%s」没有散点关卡的标定' % (prefix or '?')
    if pos:
        x, y = pos
    else:
        data = touch.shot()
        cells = cells_of(data, prefix)
        if idx not in cells:
            return False, '这一关的按钮没认到（没解锁，或者活动 UI 改版了）'
        x, y = cells[idx]
    log('点第 %d 关（识别坐标 %d,%d）' % (idx, x, y))
    touch.tap_px(x, y)

    def _detail(d):
        return bool(_hit(d, lay['start'])[0] or _hit(d, lay['start_raid'])[0])

    ok, data = _wait(touch, _detail, log, timeout=timeout, what='关卡详情面板')
    if not ok:
        return False, '点了没反应，关卡详情没出来（多半是没解锁）'

    if raid:
        if _hit(data, lay['start_raid'])[0]:
            log('已经是突袭难度')
        else:
            rx, ry, rsc = _hit(data, lay['raid'])
            if not rx:
                return False, '没认出「突袭模式」按钮（%.0f 分）' % rsc
            log('切突袭难度（%.1f 分）' % rsc)
            touch.tap_px(rx, ry)
            ok, data = _wait(touch, lambda d: bool(_hit(d, lay['start_raid'])[0]),
                             log, timeout=timeout, what='「开始突袭」按钮')
            if not ok:
                return False, '点了「突袭模式」但「开始突袭」没出来'
        btn = lay['start_raid']
    else:
        btn = lay['start']

    bx, by, bsc = _hit(data, btn)
    if not bx:
        return False, '没认出开始按钮（%.0f 分）' % bsc
    log('点「%s」（%.1f 分）' % ('开始突袭' if raid else '开始行动', bsc))
    touch.tap_px(bx, by)
    ok, _data = _wait(touch, lambda d: in_formation(d, prefix), log,
                      timeout=timeout + 8, what='干员编队界面')
    if not ok:
        return False, '点开始后没进到编队界面（理智不够 / 关卡未开放？）'
    log('已进到编队界面，交给 MAA 单作业模式')
    return True, ''


def back_to_cells(touch, prefix, log=None, rounds=6, step=2.0):
    """打完点一下回散点关卡界面，返回是否回到

    单作业模式不点结算页（见模块注释），所以这里替它点：结算页 → 「行动结束」
    → 关卡界面要连点两下。每点一下都截图确认，认到格子就停手，不多点。
    """
    log = log or (lambda *_: None)
    for i in range(rounds):
        data = touch.shot()
        if data and is_here(data, prefix) and not has_detail(data, prefix):
            if i:
                log('已回到 S 关界面')
            return True
        touch.tap(*_SAFE_TAP)
        time.sleep(step)
    data = touch.shot()
    if data and is_here(data, prefix) and not has_detail(data, prefix):
        log('已回到 S 关界面')
        return True
    log('点了 %d 下还没回到 S 关界面' % rounds)
    return False


#: 结算页「点一下继续」的位置（1280 口径）。放在屏幕中间偏下：结算页任何位置
#: 都能推进（用户 2026-11-22 实测），偏下还能避开顶部的活动入口。
_SAFE_TAP = (640, 400)
