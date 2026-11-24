# -*- coding: utf-8 -*-
"""主线（主题曲 EP 00~17）导航

用户 2026-11-24 逐屏标注的那条路，和老活动导航共用前四步：

    任意界面 --底部第1个tab「终端」--> 主界面
    主界面   --「终端」大卡---------> 带底部 tab 的那一页
    底部 tab --第 2 个（曲谱）-------> 曲谱：左边 Story Line 时间轴
    曲谱     --右上「乐章收录」------> 网格视图
    网格     --顶部「默认进度」------> 卡片上不带进度徽章
    网格     --滚到能看见目标行------> 点 EPxx 卡片
    章节详情 --右下「前往章节」------> 该章关卡列表

到关卡列表就不用管了：Copilot 的 ``ClickStageName`` 会 OCR 认出 ``15-3``
点进去开打。

**和活动卡（``stagenav.goto_story_activity``）的三个区别**：

1. 卡片位置**能算出来**：主线 18 张卡在网格最顶上「主题曲 / 为了明日」区，
   固定 5 列、行距 315（1920 口径），滚到顶后第 0 行卡片的左上角就是
   ``(100, 261)``。所以不用像活动卡那样整页翻着模板找，按章号算格子就行。
2. 但**仍然要认模板**：格子位置对，不代表那格子里就是我们想要的那一章——
   万一官方调整了章节顺序、或者页面还在加载（黑占位），点下去就进错章。
   所以每一格都用整卡模板定点比对一次（实测同一张卡 0.0 分，最像的别的卡
   22.6 分，阈值 12 足够拉开）。
3. 点开卡片是**章节详情页**（剧情简介 + 「前往章节」），不像活动那样直接进
   地图，得再点一下右下角。

模板在 ``backend/core/navtpl/mainstory/EP00.png`` ~ ``EP17.png``，都是
1920×1080 截图里按卡片白边框裁的整卡（265×265，含右下角 EP 编号），用户
配置目录 ``config/navtpl/mainstory/`` 同名文件优先，升级不覆盖。

**为什么不复用根目录那套活动卡模板**：根目录的 ``EP.png`` 是「出苍白海」
**活动**的卡（EP 前缀活动），和主题曲第 16 章完全两回事，别弄混。
"""
from __future__ import annotations

import io
import os
import re
import time

from PIL import Image

from . import stagenav

#: 章号 → 章节名（只用来写日志，用户看着顺眼）
EPISODES = {
    0: '黑暗时代·上', 1: '黑暗时代·下', 2: '异卵同生', 3: '二次呼吸',
    4: '急性衰竭', 5: '靶向药物', 6: '局部坏死', 7: '苦难摇篮',
    8: '怒号光明', 9: '风暴瞭望', 10: '破碎日冕', 11: '淬火尘霾',
    12: '惊霆无声', 13: '恶兆湍流', 14: '慈悲灯塔', 15: '离解复合',
    16: '反常光谱', 17: '相变临界',
}

#: 网格几何（**1920 口径**，2026-11-24 在真机 1920×1080 上量）
#: ``_TOP`` 是滚到顶后第 0 行卡片的上边框 y，``_COLS`` 是 5 列卡片的左边界 x，
#: 卡片边长 ``_CARD``，行距 ``_STEP_Y``（= 576-261 = 891-576）。
_TOP = 261
_COLS = (100, 415, 730, 1045, 1360)
_CARD = 265
_STEP_Y = 315

#: 卡片模板阈值：实测真卡 0.0~2.3 分，最像的别的卡 22.6 分（EP00 vs EP01，
#: 两张都是「黑暗时代」所以最像），12 是稳稳的中间线。
_MAX_SCORE = 12.0

#: 「行间暗带」的亮度上限（灰度 0~255）。网格卡片外面那条空隙实测 15~30，
#: 卡片行平均 60 以上，45 是安全的分界。
_DARK = 45

#: 主线关卡代号：``15-3`` / ``0-11`` / 突袭 ``H15-1``
_STAGE_RE = re.compile(r'^(?:H)?(\d{1,2})-\d')


def episode_of(stage_code):
    """关卡代号 → 章号（0~17）；不是主线关返回 ``None``

    ``15-3`` → 15，``0-11`` → 0，突袭 ``H15-1`` → 15（突袭入口在章节关卡
    列表里切，导航只需要把界面带到那一章的列表）。
    """
    m = _STAGE_RE.match(str(stage_code or '').strip().upper())
    if not m:
        return None
    n = int(m.group(1))
    return n if n in EPISODES else None


def _tpl_dirs():
    """模板目录（用户配置优先，其次内置）"""
    out = []
    for d in stagenav._navtpl_dirs():
        p = os.path.join(d, 'mainstory')
        if os.path.isdir(p):
            out.append(p)
    return out


def tpl_of(ep):
    """章号 → 整卡模板路径；没有返回 ''"""
    name = 'EP%02d.png' % int(ep)
    for d in _tpl_dirs():
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return ''


def _rows_of(im):
    """在一张灰度网格截图里找每行卡片的起点 y（1920 口径）

    **判据是「行与行之间的暗带」，不是卡片白边框**（2026-11-24 实测）：
    EP15~EP17 那几张卡的顶部边框根本不是白的（离解复合是血红色、反常光谱是
    白底黑手印、相变临界是墨绿），按「上边框亮度」找，这三行一个都认不出来；
    而卡片外面那条 50px 左右的行间空隙永远是纯暗的，找它反而稳。

    做法：把 x=100~1624（整排卡片）压成一列，逐行算平均亮度，连续 15~90 行
    低于 ``_DARK`` 就是一条暗带，暗带的下一个像素就是卡片行起点。

    长度上限 250 是为了排掉页尾大片空白（第 17 章下面接着「Story Line · RL」
    分组，中间有一百多像素的纯暗区）。上限给宽一点没坏处：候选多了只是多比
    对几格，漏一行（滚到顶时第一行上方是标题区，暗带 189px，实测 90 就会漏）
    才是真出事——那正好是滚到顶后唯一确定的一行。
    """
    w, h = im.size
    k = w / 1920.0
    x0 = max(0, int(round(100 * k)))
    x1 = min(w, int(round(1624 * k)))
    if x1 - x0 < 40:
        return []
    col = im.crop((x0, 0, x1, h)).resize((1, h), Image.BOX)
    vals = list(col.getdata())
    rows = []
    start = None
    for y, v in enumerate(vals):
        if v < _DARK:
            if start is None:
                start = y
            continue
        if start is not None:
            if 15 <= y - start <= 250:
                rows.append(int(round(y / k)))
            start = None
    return rows


def _cells(im):
    """当前这一屏所有可能的卡片左上角（1920 口径）"""
    rows = _rows_of(im) or [_TOP]
    return [(c, y) for y in rows for c in _COLS]


def _best_in(data, tpl, rng=18):
    """在这屏所有卡片格子里找最像 ``tpl`` 的位置 → ``(分数, 中心x, 中心y)``

    只在格子附近 ``rng`` 像素内做全分辨率比对，不做整屏粗扫：整屏粗扫是
    ``stagenav.find_card_bytes`` 那套（1/4 尺度 + 三个缩放档），对这种
    265×265 的大模板会锁错位置——2026-11-24 实测同一帧里 EP02 的卡自匹配
    报了 26 分（EP00/EP01 都是 0.0），因为 1/4 尺度下选中的前三个候选里
    没有它的真位置。限定在已知格子上找，既准又快。
    """
    from PIL import ImageChops, ImageStat
    screen = Image.open(io.BytesIO(data)).convert('L')
    W, H = screen.size
    k = W / 1920.0
    tw = max(4, int(round(tpl.width * k)))
    th = max(4, int(round(tpl.height * k)))
    t = tpl if (tw, th) == tpl.size else tpl.resize((tw, th), Image.BILINEAR)
    def diff(x, y):
        return ImageStat.Stat(ImageChops.difference(
            screen.crop((x, y, x + tw, y + th)), t)).mean[0]

    # 先只看每格的正中：卡确实在这格的话，就算偏十个像素，分数也远低于
    # 「这格是空的 / 是别的卡」（空格子 60~120）。挑最低的 6 格再细搜，
    # 30 个格子全细搜要多花三倍时间。
    coarse = []
    for gx, gy in _cells(screen):
        x0 = int(round(gx * k))
        y0 = int(round(gy * k))
        if x0 < 0 or y0 < 0 or x0 + tw > W or y0 + th > H:
            continue
        coarse.append((diff(x0, y0), x0, y0))
    coarse.sort()
    best = (999.0, 0, 0)
    for _, x0, y0 in coarse[:6]:
        for dy in range(-rng, rng + 1, 2):
            for dx in range(-rng, rng + 1, 2):
                x, y = x0 + dx, y0 + dy
                if x < 0 or y < 0 or x + tw > W or y + th > H:
                    continue
                s = diff(x, y)
                if s < best[0]:
                    best = (s, x, y)
    s, x, y = best
    return s, x + tw // 2, y + th // 2


def locate(touch, ep, log=None, rounds=5):
    """在「乐章收录」网格里找到第 ``ep`` 章的卡片 → ``(ok, 像素x, 像素y, 分数)``

    调用前界面必须在网格上（:func:`stagenav.to_score_grid`），而且已经滚到顶
    （:func:`stagenav._scroll_top`）。找不到就一屏一屏往下滑，最多 ``rounds``
    屏——最深的是第 17 章，从顶往下要滑两屏。
    """
    log = log or (lambda *_: None)
    p = tpl_of(ep)
    if not p:
        return False, 0, 0, 999.0
    from PIL import Image
    tpl = Image.open(p).convert('L')
    best = (999.0, 0, 0)
    for i in range(rounds):
        data = stagenav._settled_shot(touch, wait=2.0)
        if data:
            sc, x, y = _best_in(data, tpl)
            if sc <= _MAX_SCORE:
                return True, x, y, sc
            best = (sc, x, y)
            log('第 %d 屏没有第 %02d 章（最像 %.0f 分），往下滑一屏'
                % (i + 1, ep, sc))
        else:
            log('第 %d 屏截图失败' % (i + 1))
        # 步长 240（1280 口径）= 360 实际像素，和活动卡扫描一致：小于一格
        # 行高（315），同一行在相邻两屏里都完整，滚过头也能补救。
        touch.swipe(20, 640, 20, 400, 700)
        time.sleep(0.6)
    return False, 0, 0, best[0]


def _enter_chapter(touch, log):
    """章节详情页 → 点右下「前往章节」→ ``(ok, 截图)``

    按钮位置固定（1920 口径中心约 (1590,934)），**不认字**：那块白色渐变条
    大小稳定，但底下的章节立绘每章都不一样，做模板反而要 18 份。
    点了画面没变就往左挪 40 再点一次——按钮很宽（约 300px），挪一下一定还在
    按钮上，不会点到旁边。
    """
    pt = stagenav.points().get('mainChapterEnter') or [1060, 623]
    before = touch.shot()
    touch.tap(int(pt[0]), int(pt[1]))
    time.sleep(stagenav.MAP_DELAY)
    after = touch.shot()
    if before and after and stagenav._moved(before, after):
        return True, after
    log('点「前往章节」画面没变，往左挪 40 再点一次')
    touch.tap(int(pt[0]) - 40, int(pt[1]))
    time.sleep(stagenav.MAP_DELAY)
    after = touch.shot()
    if before and after and stagenav._moved(before, after):
        return True, after
    return False, after


def run(touch, stage_code, log=None):
    """把界面带到某条主线章节的关卡列表 → ``(ok, 说明)``"""
    log = log or (lambda *_: None)
    ep = episode_of(stage_code)
    if ep is None:
        return False, '「%s」不是主线关卡' % stage_code
    if not tpl_of(ep):
        return False, ('没有第 %02d 章的卡片模板（需要一张 1920×1080 截图里该'
                       '卡片的裁图，存成 navtpl/mainstory/EP%02d.png）'
                       % (ep, ep))

    if not touch.game_alive():
        log('游戏进程不在运行，先启动游戏')
        if not touch.restart_app(log):
            return False, '游戏没能启动（检查模拟器是否开着）'

    log('走主线导航：第 %02d 章「%s」' % (ep, EPISODES.get(ep, '')))

    # 1) 回主界面 →「终端」大卡 → 曲谱 → 乐章收录 →「默认进度」
    ok, why = stagenav.reach_home(touch, log)
    if not ok:
        return False, why
    ok, why = stagenav.to_score_grid(touch, log)
    if not ok:
        return False, why

    # 2) 滚到顶（这页会记住上次浏览的位置），再按章号找卡片
    stagenav._scroll_top(touch)
    time.sleep(1.0)
    ok, x, y, sc = locate(touch, ep, log)
    if not ok:
        return False, ('在「乐章收录」网格里没找到第 %02d 章「%s」的卡片（最像 '
                       '%.0f 分，模板可能过期了）'
                       % (ep, EPISODES.get(ep, ''), sc))
    log('找到第 %02d 章的卡片（%.1f 分，(%d,%d)），点开' % (ep, sc, x, y))
    shot_grid = touch.shot()
    touch.tap_px(x, y)
    time.sleep(stagenav.MAP_DELAY)
    shot_detail = touch.shot()
    if shot_grid and shot_detail and not stagenav._moved(shot_grid, shot_detail):
        return False, '点了第 %02d 章的卡片但详情页没开（模板可能对错了卡片）' % ep

    # 3) 章节详情页 → 右下「前往章节」→ 关卡列表
    ok, shot_list = _enter_chapter(touch, log)
    if not ok:
        return False, '章节详情页开了，但点「前往章节」没进去（界面结构可能变了）'
    log('进第 %02d 章的关卡列表了' % ep)
    return True, '已到第 %02d 章「%s」的关卡列表' % (ep, EPISODES.get(ep, ''))
