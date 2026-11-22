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
3. **认「行动结束」结算页**：左上角那两颗亮星（OS-6 打 2 星那次的真机截图
   原位裁的）。翻车重挂前必须先把结算页点掉——用户 2026-11-22 明确说
   「在失败界面重新挂作业是不会成功的，需要在关卡菜单界面重新挂」。
   光靠 ``stagenav.probe`` 判不出来：结算页底部也是暗的，probe 会把它当成
   「活动地图」直接放行，作业排上去就白跑一轮。

返回键 / ESC 的行为
-------------------
用户观察（2026-11-22）：明日方舟**大部分情况一直按 ESC 都能回主页**，唯一
例外就是基建这个确认框（用户可能自己关掉了这个提示，那就不会出现）。所以
:func:`to_home` 的顺序是「先判主页 → 再判确认框 → 再按返回/ESC」，每一步
都**先看一帧**，绝不在主页上盲按（主界面按返回会弹「退出游戏」）。
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


def confirm_leave_bytes(data):
    """这一帧是不是基建退出确认框 → 是就返回「确认」按钮的像素坐标 ``(x, y)``

    返回坐标可以直接 ``touch.tap_px``。不是确认框返回 None。
    """
    im = _gray(data)
    if im is None:
        return None
    if all(_tpl(name) is not None for name, _ in _CONFIRM_ICONS):
        hits = [_score_ratio(im, _tpl(name), fx, fy, rng=8)
                for name, (fx, fy) in _CONFIRM_ICONS]
        if max(hits) > _CONFIRM_MAX:
            return None
        return (int(round(im.width * _CONFIRM_TAP[0])),
                int(round(im.height * _CONFIRM_TAP[1])))
    return _bar_bytes(data)


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


# ---------------------------------------------------------------- 结算页
# 「行动结束 / MISSION RESULTS」左上角一排三颗星（三星全亮、两星亮两颗）。
# 模板是 2026-11-22 那次真机翻车（OS-6 打 2 星）的 MaaCore 截图原位裁的：
# 1280×720 口径下两颗亮星中心 (99,321) / (173,321)，裁 56×56 后放大到 84×84
# （1920 口径）。比对时按 ``im.width/1920`` 缩回去，正好对上原图。
#
# 为什么需要它：翻车后游戏停在失败结算页，而 ``stagenav.probe`` 只看「底部
# 白占比」——结算页底部是暗的，会被判成 ``map``（活动地图），于是重挂直接
# 放行、MaaCore 在结算页上 OCR 不到关卡名，白跑一轮。用户 2026-11-22 的原话：
# 「在失败界面重新挂作业是不会成功的，需要在关卡菜单界面重新挂」。
_RESULT_ICONS = (
    ('result_star1.png', (0.07734, 0.44583)),
    ('result_star2.png', (0.13516, 0.44583)),
)
_RESULT_MAX = 16.0          # 真命中 0.0；关卡列表/地图没有这排星，分数很高


def is_result_bytes(data):
    """这一帧是不是「行动结束」结算页（三星 / 两星都算）

    判据：左上角**两颗亮星同时命中**。只认一颗的话，活动地图角落的装饰
    图形容易凑上；两颗一起要求基本只有结算页满足。模板缺失时返回 False
    （宁可多走一次导航，也不要在结算页上排作业）。
    """
    im = _gray(data)
    if im is None:
        return False
    for name, _ in _RESULT_ICONS:
        if _tpl(name) is None:
            return False
    hits = [_score_ratio(im, _tpl(name), fx, fy, rng=8)
            for name, (fx, fy) in _RESULT_ICONS]
    return max(hits) <= _RESULT_MAX


def is_result(touch):
    try:
        data = touch.shot()
    except Exception:
        return False
    return is_result_bytes(data)


# ---------------------------------------------------------------- 回主界面
def to_home(touch, log=None, rounds=8, data=None):
    """从任意一页退回主界面

    顺序（每轮先看一帧，确认到了就停）：
      1. 认主页 → 成功
      2. 认基建确认框 → 点红条（不按返回，按了也没用）
      3. 剧情 / CG 回放页 → 跳幕
      4. 否则按返回键（前 3 轮）→ ESC（再 3 轮）→ 点左上角「<」
    """
    log = log or (lambda *_: None)
    for i in range(rounds):
        if data is None:
            data = touch.shot()
        if data and is_home_bytes(data):
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
