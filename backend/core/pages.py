# -*- coding: utf-8 -*-
"""页面识别：现在停在哪一页，以及怎么稳稳回到主界面

为什么要单独一层
----------------
以前判断「在不在主界面」只有 :func:`stagenav.at_home` 一套**整屏统计特征**
（底部白占比 + 终端卡亮度）。它认得主页，但认不出主页之外的东西——不知道
自己在基建里、在设置页、还是在退出确认弹窗上。于是「回主界面」只能靠盲按
返回键，撞上基建的「是否确认离开罗德岛基建？」就永远退不出去（2026-11-22
用户实测：按 ESC 被这个弹窗卡住）。

这一层只做两件事：

1. **认主页**：左上角「设置齿轮」+「邮件」两个图标定点比对。它们是主页
   专属、位置钉死、不随皮肤变（实测同一帧 0.0 分，活动地图 27.6、设置页
   只有齿轮没有邮件）。比数白像素稳得多。
2. **认基建退出确认框**：底部那条横贯整屏的按钮条上，左「✕」右「✓」两个
   白色圆钮同时定点命中。认到就点右半「确认」的中心，而不是按返回键空转。

这里**刻意不做「认失败 / 认结算页」**：一场战斗是不是翻车，只认 MaaCore 的
回调（``TaskChainError`` / ``WaitUntilEndOfAction`` 子任务失败），不靠截图猜
（2026-11-22 用户明确要求：「失败识别应该以 MAA 的失败通知为准，而不是自己
识别有没有失败」）。翻车之后界面一定停在「行动结束」结算页，用户实测**点
屏幕任意位置都会立刻回到活动关卡界面**，所以那边只需要「点一下 → 确认现在是
不是活动关卡地图」，不需要先认出结算页。

返回键 / ESC 的行为
-------------------
用户观察（2026-11-22）：明日方舟**大部分情况一直按 ESC 都能回主页**，例外
有两个：

- 基建的「是否确认离开罗德岛基建？」（用户可能自己关掉了这个提示，那就不会
  出现）→ 点右边 ✓；
- **主界面上的「是否确认退出游戏？」**——一旦已经在主页还接着按 ESC 就会弹
  出来，再按一下游戏就关掉（2026-11-23 实测「S 关入口没认出来，一路按 ESC
  把游戏按没了」）→ 点左边 ✕ 取消，并直接当「已到家」收工。

所以 :func:`to_home` 的顺序是「先判主页 → 再判退出确认 → 再判基建确认 →
再按返回/ESC」，每一步都**先看一帧**，绝不在主页上盲按。
"""
from __future__ import annotations

import io
import os
import time

# 模板目录（和 eventnav 的 navtpl/events 并列）
_TPL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'navtpl', 'pages')

# 1920×1080 真机口径下两个图标的中心坐标。模板就是从这一帧原位裁的
# （56×56），所以中心 = 裁切点 + 28。
_HOME_ICONS = (
    ('home_gear.png', (48, 32)),
    ('home_mail.png', (188, 32)),
)

# 定点比对的阈值：真命中 0.0，活动地图上齿轮误命中 27.6。留一倍余量。
_HOME_MAX = 14.0

_tpl_cache = {}


def _tpl(name):
    """读内置模板灰度图；坏了返回 None（不让一张图缺失拖垮导航）"""
    if name in _tpl_cache:
        return _tpl_cache[name]
    try:
        from PIL import Image
        p = os.path.join(_TPL_DIR, name)
        im = Image.open(p).convert('L')
        _tpl_cache[name] = im
        return im
    except Exception:
        _tpl_cache[name] = None
        return None


def has_templates():
    """两张主页模板都在（打包漏文件时让调用方退回老特征，而不是永远判否）"""
    return all(_tpl(name) is not None for name, _ in _HOME_ICONS)


def _gray(data):
    try:
        from PIL import Image
        return Image.open(io.BytesIO(data)).convert('L')
    except Exception:
        return None


def _score_px(im, tpl, px, py, rng=6):
    """模板中心对准像素点 (px, py) 附近的**定点**比对，返回最小平均绝对差

    为什么不做整屏搜索：主页图标、确认框按钮的位置都是钉死的，整屏搜一个
    56×56 的小图标在 1920×1080 上要跑上千次比对（1~2 秒），而导航里每走
    一步都要判一次。定点 ±6 像素、步长 1 就够——实测同一帧 0.0 分，位移
    6 像素也还有余量。
    """
    from PIL import ImageChops, ImageStat
    s = im.width / 1920.0
    tw = max(8, int(round(tpl.width * s)))
    th = max(8, int(round(tpl.height * s)))
    try:
        t = tpl.resize((tw, th))
    except Exception:
        return 999.0
    bx = int(px) - tw // 2
    by = int(py) - th // 2
    best = 999.0
    for dy in range(-rng, rng + 1):
        for dx in range(-rng, rng + 1):
            x, y = bx + dx, by + dy
            if x < 0 or y < 0 or x + tw > im.width or y + th > im.height:
                continue
            d = ImageStat.Stat(ImageChops.difference(
                im.crop((x, y, x + tw, y + th)), t)).mean[0]
            if d < best:
                best = d
    return best


def _score_at(im, tpl, cx, cy, rng=6):
    """(cx, cy) 用 1920×1080 口径的坐标（主页图标是这么标定的）"""
    s = im.width / 1920.0
    return _score_px(im, tpl, int(round(cx * s)), int(round(cy * s)), rng)


def _score_ratio(im, tpl, fx, fy, rng=6):
    """(fx, fy) 用相对坐标（0~1）：窗口比例变了也跟得住"""
    return _score_px(im, tpl, int(round(im.width * fx)),
                     int(round(im.height * fy)), rng)


def is_home_bytes(data):
    """这一帧是不是主界面

    判据：**设置齿轮和邮件两个图标同时命中**。只要一个的话，设置页左上角
    也有齿轮（只是位置更靠右），会误判成主页；两个一起要求几乎排除了所有
    其它页面（基建 / 关卡列表 / 活动地图都不同时具备）。
    """
    im = _gray(data)
    if im is None:
        return False
    hits = []
    for name, (cx, cy) in _HOME_ICONS:
        tpl = _tpl(name)
        if tpl is None:
            return False
        hits.append(_score_at(im, tpl, cx, cy))
    return max(hits) <= _HOME_MAX


def is_home(touch):
    try:
        data = touch.shot()
    except Exception:
        return False
    return is_home_bytes(data)


# ---------------------------------------------------------------- 基建确认框
# 「是否确认离开罗德岛基建？」底部是一条横贯整屏的按钮条：左半黑（取消,
# 白色 ✕）、右半暗红（确认, 白色 ✓）。真机 1920×1080 实测这条横带
# y≈682..800（0.63~0.74 屏高），两个圆形按钮中心相对坐标 (0.346, 0.687)
# 与 (0.658, 0.688)。
#
# 为什么最后用**图标定点比对**、而不是按行数红像素：
# 那个暗红按钮带一层斜纹贴图，红像素是散点（同一行红占比只有 35%~60%、
# 最长连续红段仅 30px），于是「行内红像素够多」这个判据在关卡列表 / 主页
# 的红色角标上也能凑够数——2026-11-22 真机日志里就出现过一次假弹窗。
# 换成「✕ 和 ✓ 两个白色圆钮同时命中」后，主页 0.0 之外全是 40 分以上，
# 判据从「像不像红块」变成了「是不是这一页」，和主页双图标的思路一致。
_CONFIRM_ICONS = (
    ('confirm_cancel.png', (0.3464, 0.6870)),   # 左：取消
    ('confirm_ok.png', (0.6583, 0.6880)),       # 右：确认
)
_CONFIRM_MAX = 14.0                            # 真命中 0.0，其余页面 40+
_CONFIRM_TAP = (0.6583, 0.6880)                # 点右半「确认」的中心

# 打包漏了模板时的兜底：整条按钮条的色彩签名 = 左半很暗 + 右半偏红
_BAR_Y = (0.60, 0.80)          # 按钮条只可能在这一段高度
_BAR_DARK = 70                 # 左半（取消）亮度上限
_BAR_RED = 55                  # 右半 R 比 G/B 均值高多少才算「红按钮」


def _bar_point(im):
    """按钮条在不在 → 在就返回右半「确认」的像素坐标，不在返回 None

    两种弹窗（基建离开 / 退出游戏）共用这一条：左 ✕ 右 ✓ 两个白圆钮同时
    定点命中。模板缺失时返回 None，调用方自己退到色彩签名兜底。
    """
    if not all(_tpl(name) is not None for name, _ in _CONFIRM_ICONS):
        return None
    hits = [_score_ratio(im, _tpl(name), fx, fy, rng=8)
            for name, (fx, fy) in _CONFIRM_ICONS]
    if max(hits) > _CONFIRM_MAX:
        return None
    return (int(round(im.width * _CONFIRM_TAP[0])),
            int(round(im.height * _CONFIRM_TAP[1])))


def confirm_leave_bytes(data):
    """这一帧是不是基建退出确认框 → 是就返回「确认」按钮的像素坐标 ``(x, y)``

    返回坐标可以直接 ``touch.tap_px``。不是确认框返回 None。
    """
    im = _gray(data)
    if im is None:
        return None
    pt = _bar_point(im)
    if pt:
        return pt
    if all(_tpl(name) is not None for name, _ in _CONFIRM_ICONS):
        return None                 # 模板在手就只认定点比对，别用色彩签名猜
    return _bar_bytes(data)


# ---------------------------------------------------------------- 退出游戏确认
# 「是否确认退出游戏？」和上面那个基建确认框用的是**同一套按钮条**（左 ✕
# 右 ✓），光看按钮分不出来。但点错代价完全相反：在退出确认上点右边 ✓ 就是
# 替用户按了「退出游戏」，整个挂机当场结束。
#
# 2026-11-23 用户实测：S 关入口认不出来 → 一路按 ESC → 主界面又被按出这个框
# → 程序接着按 ESC，游戏直接关掉。所以这里必须**先认出它、点左边的 ✕**。
#
# 区分办法是**背后的页面**：退出确认只会在主界面弹，背后是模糊掉的主界面，
# 一整片都亮（真机截图按钮条上方 0.10~0.26 屏高平均亮度 125~155、下方
# 0.80~0.98 是 105~130）；基建确认背后是基地，是暗的。所以判据 =
# 「按钮条命中」+「上下两段里有一段够亮」。
#
# 阈值故意压得很低（100）：把基建框误认成退出框只会少退一层（下轮再试，
# 且 ✕ 是无害的取消）；反过来把退出框认成基建框就是直接关游戏。
_EXIT_LUM = 100
_EXIT_CANCEL = (0.3464, 0.6870)      # 左半「✕ 取消」的中心，和基建那个 ✕ 同位


def _mean_lum(im, y0, y1, step=4):
    """这一段高度的平均亮度（灰度图抽样，别整屏遍历）"""
    w, h = im.size
    px = im.load()
    xs = range(0, w, max(4, w // 80))
    tot = n = 0
    for y in range(int(h * y0), max(int(h * y0) + 1, int(h * y1)), step):
        for x in xs:
            tot += px[x, y]
            n += 1
    return (tot / float(n)) if n else 0.0


def exit_confirm_bytes(data):
    """这一帧是不是「是否确认退出游戏？」→ 是就返回「✕ 取消」的像素坐标

    **认到必须点 ✕**：这个框只在主界面弹，取消之后一定还在主界面，所以调用方
    点完就可以认定「已经到家」，别再按返回/ESC（再按一次又弹出来，按歪了就
    把游戏关了）。

    不是这个框返回 None——基建确认框会走 :func:`confirm_leave_bytes` 点 ✓。
    """
    im = _gray(data)
    if im is None:
        return None
    pt = _bar_point(im)
    if not pt and not all(_tpl(name) is not None
                          for name, _ in _CONFIRM_ICONS):
        pt = _bar_bytes(data)       # 模板缺失时的兜底，和 confirm_leave 一致
    if not pt:
        return None
    # 取「按钮条上方」和「按钮条下方」两段里更亮的那段：模糊后的主界面上下
    # 都是亮的（真机截图上方 125~155、下方 105~130），只要有一段亮就够了；
    # 基建那边两段都是暗的。
    lum = max(_mean_lum(im, 0.10, 0.26), _mean_lum(im, 0.80, 0.98))
    if lum < _EXIT_LUM:
        return None                 # 背后是暗的 → 基建那个框，交给 confirm_leave
    return (int(round(im.width * _EXIT_CANCEL[0])),
            int(round(im.height * _EXIT_CANCEL[1])))


def _bar_bytes(data):
    """兜底：按按钮条的色彩签名找红半边的中心（模板缺失时才用）"""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert('RGB')
    except Exception:
        return None
    w, h = im.size
    px = im.load()
    y0, y1 = int(h * _BAR_Y[0]), int(h * _BAR_Y[1])
    step_y = max(2, h // 180)
    left_xs = range(int(w * 0.05), int(w * 0.40), max(2, w // 240))
    right_xs = range(int(w * 0.55), w, max(2, w // 240))
    rows = []
    for y in range(y0, y1, step_y):
        lv = [sum(px[x, y]) / 3.0 for x in left_xs]
        rd = [px[x, y][0] - (px[x, y][1] + px[x, y][2]) / 2.0 for x in right_xs]
        if sum(lv) / len(lv) < _BAR_DARK and sum(rd) / len(rd) > _BAR_RED:
            rows.append(y)
    if len(rows) < 3:
        return None
    # 只认最长的连续段，且有一段像样的高度（真机 118px / 1080）
    band, cur = [], [rows[0]]
    for y in rows[1:]:
        if y - cur[-1] <= step_y * 2:
            cur.append(y)
        else:
            band.append(cur)
            cur = [y]
    band.append(cur)
    seg = max(band, key=len)
    if seg[-1] - seg[0] < h * 0.05:
        return None
    return int(w * 0.66), (seg[0] + seg[-1]) // 2


# ---------------------------------------------------------------- 回主界面
def to_home(touch, log=None, rounds=8, data=None):
    """从任意一页退回主界面

    顺序（每轮先看一帧，确认到了就停）：
      1. 认主页 → 成功
      2. 认「退出游戏」确认框 → 点左边 ✕，直接当到家（再按就关游戏了）
      3. 认基建确认框 → 点红条（不按返回，按了也没用）
      4. 剧情 / CG 回放页 → 跳幕
      5. 否则按返回键（前 3 轮）→ ESC（再 3 轮）→ 点左上角「<」
    """
    log = log or (lambda *_: None)
    for i in range(rounds):
        if data is None:
            data = touch.shot()
        if data and is_home_bytes(data):
            return True
        # 退出确认**必须排在基建确认前面**：两者按钮条一模一样，但这里点 ✓
        # 等于替用户退出游戏（2026-11-23 实测：ESC 按到这一步，程序又按了一下，
        # 游戏就没了）。认到就点左边的 ✕，取消完一定在主界面。
        ex = exit_confirm_bytes(data) if data else None
        if ex:
            log('弹出「是否确认退出游戏？」——点左边的 ✕ 取消'
                '（再按返回/ESC 会把游戏关掉）')
            touch.tap_px(*ex)
            time.sleep(1.2)
            if is_home(touch):
                log('已取消退出确认，回到主界面')
            else:
                # 这个框只在主界面弹，取消之后本来就该在家；认不出图标也按
                # 「到家」处理，绝不再补一发 ESC 去把它弹回来。
                log('已取消退出确认（图标没认出来，按主界面继续）')
            return True
        pt = confirm_leave_bytes(data) if data else None
        if pt:
            log('基建「离开确认」弹窗，点右边的勾')
            touch.tap_px(*pt)
            data = None
            time.sleep(1.3)
            continue
        try:
            from . import stagenav
            if stagenav.in_story_dialog(touch):
                log('剧情 / CG 回放页，先跳幕')
                stagenav._skip_story_replay(touch, log)
                data = None
                continue
        except Exception:
            pass
        if i < 3:
            log('不在主界面，按返回键')
            touch.back()
        elif i < 6:
            log('返回键没反应，按 ESC')
            touch.esc()
        else:
            log('点左上角「<」')
            try:
                from . import stagenav
                touch.tap(*stagenav.points()['backCorner'])
            except Exception:
                pass
        data = None
        time.sleep(1.5)
    return is_home(touch)
