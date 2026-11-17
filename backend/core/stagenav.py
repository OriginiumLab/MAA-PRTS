# -*- coding: utf-8 -*-
"""把界面带到「某个关卡的关卡列表」

为什么需要这一段（2026-11-17 实测，别再重复踩）：

MAA 自己的活动关导航写在 ``resource/tasks/Stages/<前缀>.json`` 里
（``TL-5`` → ``TL-OpenOpt`` → ``TL-Open`` 认活动入口 → ``TLChapterToTL``
点进活动章节 → ``SwipeToStage`` 左滑找关），但它依赖一批模板图
（``StageActivity.png`` / ``StageSideStory.png`` / ``TLChapterToTL.png``），
在当前活动 UI 上**三种起点全都匹配失败**（主界面 / 章节地图 / 关卡主题页，
各连试 20 次以上）。

而 Copilot 走的又是另一条更弱的链：``StageNavigationBegin`` →
``ClickStageName``（OCR 找代号）→ ``FullStageNavigation``（**靠横向滑动切
章节**）。这版游戏的章节切换改成了底部 EPISODE 分页点击，滑动完全无效，
所以从主线章节永远滑不到活动章节。

于是这一段由我们自己做。**整条路是 2026-11-17 在真机上一步步点出来的**，
每次点击后都截图核对过：

    任意界面 --底部第1个tab「终端」--> 主界面          （幂等，反复点没事）
    主界面   --左侧当期活动立绘--------> 活动（主题页 或 活动地图）
    主题页   --区域名（当期=世纪之春）-> 活动地图       （= 关卡列表，右下角
                                                        叠着区域列表）
    活动地图 --区域列表第 N 行--------> 第 N 个区域的关卡列表

只要界面停在关卡列表，后面就不用我们管了：Copilot 的 ``ClickStageName``
会 OCR 认出 ``TL-S-1`` 并点进去开打（实测能打）。

两个实测出来的坑：

1. **主界面点活动大图是「记忆位置」的**——上次停在主题页就进主题页，上次停在
   活动地图就直接进活动地图。所以流程必须看状态收敛，不能死按顺序点。
2. **MaaCore 的 ``AsstAsyncClick`` 一次连接里只能点中第一下**：单独一点有效，
   紧接着再点第二、第三下，界面纹丝不动。导航天然要连点，于是点击和截图都走
   adb（``input tap`` / ``screencap``），MaaCore 只负责跑任务。

坐标全部是 **1280×720 口径**，``Touch`` 按实际分辨率换算。改坐标不用改代码：
往 ``config/nav.json`` 写同名键即可（活动换版、UI 挪位置时用）。

    {"activityCard": [187, 387], "themeZone": [1055, 399]}

``themeZone`` 是**当期最新区域**的入口。活动换版后主题页的区域名会重排，那一格
最可能需要重新量——量法：进主题页截一张图，取目标区域名中心的像素坐标，
x 除以 1.5、y 除以 1.5 就是 1280 口径（1920×1080 时）。
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import time

# 1280×720 坐标系
DEFAULT_POINTS = {
    # 底部第 1 个 tab：终端 → 主界面。幂等：在主界面点也没事
    'tabTerminal': [67, 675],
    # 主界面左侧当期活动立绘（「时间啊，向前！」那块）
    'activityCard': [187, 387],
    # 活动主题页上的「当期最新区域」入口（TL 活动 = 世纪之春，TL-S 就在这里）
    'themeZone': [1055, 399],
    # 活动地图右下角的区域列表，第 1/2/3 行（残阳终尽 / 长冬初青 / 世纪之春）
    'zoneRows': [[1125, 471], [1125, 565], [1125, 659]],
    # 左上角返回键（活动页里的 ←）。只用来从误入的商店/弹窗里退出来
    'backCorner': [47, 43],
    # 剧情对话页右上角的「跳过▶」（1920 口径 (1775,79)，2026-11-17 去咧嘴谷
    # 幕间实测：白字 bbox x1538~1830 / y61~97，「跳过」居 (1720~1830)）和
    # 「是否跳过剧情」确认弹窗上的 ✓（1920 口径 (1210,700)，弹窗横排居中：
    # ✕ 居 (750,700)、✓ 居 (1210,700)，2026-11-17 网格实测）。
    # 老活动卡点进去有时会自动播剧情回放，不跳出去导航就卡死在这。
    'storySkip': [1183, 53],
    'storyConfirm': [807, 467],

    # ---------------- 老故事集（曲谱）路径 ----------------
    # 主界面右上那张「终端」大卡（理智 41/210 那块）。
    # 注意：**按 16:9 固定位置点，别去认「终端」两个字**——很多人换了皮肤，
    # 这两个字会被盖掉，但这块按钮区域很大且必然存在（2026-11-17 用户在带
    # 皮肤的主界面上确认）。中心点换算：1920 口径 (1370,260) → /1.5。
    'terminalCard': [913, 173],
    # 底部第 2 个 tab：曲谱（左起第 1 个是终端）
    'tabStory': [280, 673],
    # 曲谱页右上「乐章收录」：切成一格一格的网格视图
    'scoreBook': [1102, 89],
    # 网格视图顶部的第一个标签「默认进度」（另外两个是「奖励进度」「完成进度」）。
    # **进网格后必须点一下**：这个页面记住上次选的标签，停在「完成进度」时每张
    # 卡片左上角都会盖一层进度徽章（38/39 这种蓝色菱形），卡片图被改得面目全非，
    # 模板匹配直接从 5 分掉到 20 分以上（2026-11-18 用户报 RI 导航失败，现场
    # 截图停在「完成进度」就是这个原因）。实测点击位置 1920 口径 (1111,67)。
    'tabProgress': [741, 45],
    # 活动详情页右下「进入活动」
    'enterActivity': [1140, 616],
    # 活动地图右下角那个圆形的「EX」按钮：点一下从普通关切到 EX 关（再点
    # 一下切回去，按钮上的字会变成「STORY」）。
    # 2026-11-17 在 MN 上实测：1920 口径 (1799,930) → /1.5。
    # 老故事集的 EX 关都靠这一个按钮，位置一样；万一某个活动挪了位置，
    # 用 config/nav.json 覆盖，或者按活动前缀写 exSwitchByEvent。
    'exSwitch': [1199, 620],
    'exSwitchByEvent': {},
}

# Windows 上控制台子进程默认会**弹一个黑色 cmd 窗口**：打包成窗口程序后没有
# 控制台可供继承，每次 subprocess 都新开一个。导航一屏要两次 adb（截图 + 滑动），
# 扫十几屏就是几十个黑框在屏幕上闪（2026-11-17 用户报「一直跳出黑色 cmd 框」）。
# ``maa.py`` 里的 tasklist/taskkill 早就带了，这里当初漏了。
# 非 Windows 上没这个常量，getattr 兜 0；Popen 的 creationflags 传 0 是合法值。
_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

STEP_DELAY = 1.8                 # 每次点击后的等待（切页动画）
MAP_DELAY = 2.6                  # 进活动页/地图后要多等一会儿（大图 + 网络图）

# 底部条带（y 86%~94%, x 1%~35%）的白像素占比：
#   活动地图 ~0.01（底下是场景图）/ 主界面 ~0.47（半透明 tab 栏 + 白图标）
#   / 活动主题页 ~0.87（雪景或大幅插画）
_MAP_WHITE_MAX = 0.15


def points():
    """实际用的点：默认值 + config/nav.json 覆盖"""
    p = {k: (list(v) if isinstance(v, list) else v)
         for k, v in DEFAULT_POINTS.items()}
    try:
        from .settings import CONFIG_DIR
        f = os.path.join(CONFIG_DIR, 'nav.json')
        if os.path.exists(f):
            with open(f, encoding='utf-8') as fh:
                got = json.load(fh) or {}
            for k, v in got.items():
                if k in p and v:
                    p[k] = v
    except Exception:
        pass
    return p


_STORY_SKIP_MAX = 0.55     # 「跳过▶」字形掩码 XOR 占比：剧情页 0.00~0.14，非剧情 ≥1.0
_STORY_SKIP_TPL = None
_WAKEUP_MAX = 25.0         # 「开始唤醒」按钮模板阈值：登录页 0.0，其它页 ≥36.5
_WAKEUP_TPL = None
# 两帧差值低于这个数按「画面没动」算（判断网格「滚到底了」用）。网格上的
# 卡片有呼吸/闪光动画，同一位置连拍两帧也有 1.76 的差（2026-11-18 实测），
# 所以不能定得比它小。惯性衰减实测：+0.5s 22.0 / +1.4s 3.8 / +2.2s 1.6。
_SETTLE_THR = 2.5


def _nav_tpl(name):
    """读内置模板灰度图（navtpl/ 下以下划线开头的辅助模板），坏了返回 None"""
    try:
        from PIL import Image
        f = os.path.join(os.path.dirname(__file__), 'navtpl', name)
        return Image.open(f).convert('L')
    except Exception:
        return None


def _story_skip_tpl():
    global _STORY_SKIP_TPL
    if _STORY_SKIP_TPL is None:
        _STORY_SKIP_TPL = _nav_tpl('_storyskip.png') or False
    return _STORY_SKIP_TPL or None


def _wakeup_tpl():
    global _WAKEUP_TPL
    if _WAKEUP_TPL is None:
        _WAKEUP_TPL = _nav_tpl('_wakeup.png') or False
    return _WAKEUP_TPL or None


def _fixed_match(touch, tpl, ox, oy, rng=12, max_score=None):
    """固定位置 UI 的模板匹配：在 (ox, oy)（1920 口径**左上角**）±rng 找最小分

    分数 = 平均绝对差，越小越像。返回 (score, 点击中心像素 x, y)。
    """
    from PIL import Image, ImageChops, ImageStat
    data = touch.shot()
    if not data:
        return 999.0, 0, 0
    im = Image.open(io.BytesIO(data)).convert('L')
    w, h = im.size
    k = w / 1920.0
    tw = max(4, int(tpl.width * k))
    th = max(4, int(tpl.height * k))
    t = tpl.resize((tw, th), Image.BILINEAR)
    best = 999.0
    bx, by = int(ox * k), int(oy * k)
    for dy in range(-rng, rng + 1, 2):
        for dx in range(-rng, rng + 1, 2):
            x, y = bx + dx, by + dy
            if x < 0 or y < 0 or x + tw > w or y + th > h:
                continue
            s = ImageStat.Stat(ImageChops.difference(
                im.crop((x, y, x + tw, y + th)), t)).mean[0]
            if s < best:
                best = s
    return best, (bx + tw / 2) / k, (by + th / 2) / k


def on_login_page(touch):
    """当前是不是「开始唤醒」登录页

    登录页是固定 UI，「开始唤醒」深色按钮 + 白字钉在 (820,726)~(1100,816)。
    **不能用整屏均值**：登录页底部有一条深色横带，均值只有 ~150，过不了
    >165 的旧判据——restart_app 轮询因此在登录页上误报「已回主界面」
    （2026-11-17 重启实测）。也别用底部白像素：浅灰底 + 白 logo 照样骗过
    at_home。实测分离度：登录页 0.0，主界面 36.5 / 剧情 51.8 / START 69.9
    / 桌面 59.2，阈值 25。**还要前置一道整屏均值 >130**：暗底页面（活动详情
    页均值 67.7）上偶尔有内容局部撞上按钮模板（详情页实测 21.3 分，低于 25），
    亮度门槛把它挡掉——登录页均值 182，其它已知页面全部 ≤122，两边都有余量。
    """
    try:
        from PIL import Image, ImageStat
        data = touch.shot()
        if not data:
            return False
        im = Image.open(io.BytesIO(data)).convert('L')
        if ImageStat.Stat(im).mean[0] <= 130:
            return False
        tpl = _wakeup_tpl()
        if tpl is None:
            return False

        class _Shot:
            def __init__(self, d):
                self.d = d

            def shot(self):
                return self.d

        s, _, _ = _fixed_match(_Shot(data), tpl, 820, 726, rng=12)
        return s < _WAKEUP_MAX
    except Exception:
        return False


def game_package(client=None):
    """官服 / B 服的安卓包名：B 服包名带 .bilibili 后缀

    ``pidof`` / ``am start`` 都要包名；B 服用户给官服包名会「进程永远不在」，
    启动检测全部落空。
    """
    return ('com.hypergryph.arknights.bilibili'
            if str(client or '').lower() in ('bilibili', 'b', 'b服')
            else 'com.hypergryph.arknights')


# ---------------------------------------------------------------- 触屏通道
class Touch:
    """adb 点击 + 截图（1280×720 坐标，内部换算到实际分辨率）

    不用 MaaCore 的 ``AsstAsyncClick``：同一次连接里连点第二下就不生效
    （2026-11-17 实测，五连点界面完全没动），而导航要连点好几下。
    adb 每一下都是独立进程，稳。
    """

    def __init__(self, adb_path, address, log=None, package=None):
        self.adb = adb_path or 'adb'
        self.address = address or '127.0.0.1:5555'
        self.log = log or (lambda *_: None)
        self.package = package or 'com.hypergryph.arknights'
        self._size = None

    def game_alive(self):
        """游戏进程在不在运行

        2026-11-17 中午实测的坑：StartUp 假成功 + 导航并发，导航对着**安卓
        桌面**按了半天返回键/ESC，nav-fail 现场图就是一张桌面截图。进程不在
        就别再瞎点了，先拉起来。
        """
        out = self._run(['shell', 'pidof', self.package])
        return bool((out or b'').strip())

    def _run(self, args, timeout=20):
        cmd = [self.adb, '-s', self.address] + list(args)
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                               stdin=subprocess.DEVNULL,
                               creationflags=_NO_WINDOW)
        except Exception as e:
            self.log('adb 调用失败（%s）：%s' % (' '.join(args[:2]), e))
            return None
        if r.returncode != 0:
            self.log('adb 返回 %d：%s' % (r.returncode,
                                        (r.stderr or b'')[:160].decode('utf-8', 'ignore')))
            return None
        return r.stdout

    def size(self):
        """实际分辨率 (w, h)，拿不到就当 1280×720"""
        if self._size:
            return self._size
        out = self._run(['shell', 'wm', 'size'])
        text = (out or b'').decode('utf-8', 'ignore')
        m = re.search(r'(\d+)\s*x\s*(\d+)', text)
        self._size = (int(m.group(1)), int(m.group(2))) if m else (1280, 720)
        return self._size

    def tap(self, x, y):
        w, h = self.size()
        px = int(round(x * w / 1280.0))
        py = int(round(y * h / 720.0))
        ok = self._run(['shell', 'input', 'tap', str(px), str(py)]) is not None
        self._release()
        return ok

    def tap_px(self, px, py):
        """按真实像素点（模板匹配出来的坐标就是像素，不用换算）"""
        ok = self._run(['shell', 'input', 'tap',
                        str(int(px)), str(int(py))]) is not None
        self._release()
        return ok

    def swipe(self, x1, y1, x2, y2, ms=400):
        """1280×720 口径的滑动

        **不发 ``_release()``**：滑动自带抬起动作，不会像 ``input tap`` 那样
        把左键留在按下状态；而每次补发要两条 adb 命令（雷电要
        ``sendevent /dev/input/event6``），一条 0.25s，翻十几屏就是十几秒白烧
        （2026-11-18 实测：一次滑动 0.77s，其中 0.5s 是补的两条 sendevent）。
        """
        w, h = self.size()
        sx = lambda v: str(int(round(v * w / 1280.0)))
        sy = lambda v: str(int(round(v * h / 720.0)))
        return self._run(['shell', 'input', 'swipe',
                          sx(x1), sy(y1), sx(x2), sy(y2),
                          str(int(ms))]) is not None

    def _release(self):
        """每次点击/滑动后补一发鼠标抬键（雷电踩过的坑，见 ``heal()``）

        ``input tap`` 偶发把左键留在「按下」状态，之后所有 tap 全被吃掉——
        2026-11-17 真机连着踩了两次（第一次导致 25 个活动全 FAIL）。事后补
        一发 BTN_MOUSE=0 对正常状态是无害 no-op，对卡死状态就是解药。
        """
        try:
            self._run(['shell', 'sendevent', '/dev/input/event6', '1', '272', '0'])
            self._run(['shell', 'sendevent', '/dev/input/event6', '0', '0', '0'])
        except Exception:
            pass

    def back(self):
        """系统返回键

        比点左上角那个「<」可靠：实测某些详情页上点 (47,43) 毫无反应，
        keyevent 4 一下就退回去了。**别在主界面上用**，会弹退出游戏确认框。
        """
        return self._run(['shell', 'input', 'keyevent', '4']) is not None

    def esc(self):
        """ESC 键（keyevent 111）——退一层

        比 ``back()`` 好用的地方：有些详情页对系统返回键完全没反应（实测 MN
        的奖励详情页），ESC 一下就有反应。**同样别在主界面按**。
        """
        return self._run(['shell', 'input', 'keyevent', '111']) is not None

    def shot(self):
        """当前画面 PNG 字节"""
        return self._run(['exec-out', 'screencap', '-p']) or b''

    def heal(self):
        """把卡死的鼠标按键强制抬起（雷电模拟器踩过的坑，见 ``_release()``）"""
        self._release()

    def restart_app(self, log=None):
        """强杀并重启明日方舟，一路点到主界面

        2026-11-17 真机踩过的最后一课：误点老故事集（去咧嘴谷）的卡片后，游戏
        会自动播「幕间」剧情回放，而且**整段回放循环重播**——右上「跳过」只
        能跳当前幕，跳完全部又从第一幕重来，靠点击永远出不去。唯一的脱困方法
        就是重启 App：force-stop → am start → 启动页 START → 登录页「开始唤醒」
        → 主界面（账号是记住的，不用重新输密码）。全程约 1 分钟。
        """
        log = log or (lambda *_: None)
        log('强杀明日方舟并重启')
        self._run(['shell', 'am', 'force-stop', self.package])
        time.sleep(4)
        # am start 偶发哑火（2026-11-17 中午实测：强杀后模拟器一直停在桌面，
        # 游戏压根没起来），所以拉起后要确认进程真的在，不在就再拉，最多 3 次
        for attempt in range(3):
            self._run(['shell', 'am', 'start', '-n',
                       self.package + '/com.u8.sdk.U8UnityContext'])
            t0 = time.time()
            while time.time() - t0 < 30:
                time.sleep(3)
                if self.game_alive():
                    break
            if self.game_alive():
                break
            log('am start 没把游戏拉起来（第 %d 次），重试' % (attempt + 1))
        if not self.game_alive():
            log('游戏进程始终没起来（模拟器还开着吗？）')
            return False
        t0 = time.time()
        seen_gate = False
        while time.time() - t0 < 600:
            time.sleep(5)
            # 游戏半路死掉要重新拉起：实测强杀过一次后重启，游戏可能走到
            # START 页又崩（2026-11-17 中午：12:32 还在 START 页，12:36 只剩
            # 桌面），轮询在桌面上空转会把 300 秒白烧完
            if not self.game_alive():
                log('游戏进程中途没了，重新拉起')
                self._run(['shell', 'am', 'start', '-n',
                           self.package + '/com.u8.sdk.U8UnityContext'])
                time.sleep(6)
                seen_gate = False
                continue
            # **先认拦截页再判主界面**：登录页浅灰底部会把 at_home 骗过，
            # 先判主界面会在登录页上误报「已回主界面」（2026-11-17 实测）
            step = self._boot_step()
            if step:
                kind, px, py = step
                log('启动页，点 START' if kind == 'start' else '登录页，点「开始唤醒」')
                self.tap_px(px, py)
                seen_gate = True
                continue
            # **必须先点过拦截页才接受主界面确认**：冷启动必然经过 START /
            # 登录页，而资源加载页也是浅色底，at_home 会在加载页误报
            # （2026-11-17 傍晚实测：轮询在加载页就 return True，调用方以为
            # 在主界面，实际游戏随后才走到登录页停住，后面全盘皆输）
            if (seen_gate and at_home(self) and not in_story_page(self)
                    and not on_login_page(self)):
                log('已回到主界面')
                return True
        return False

    def _boot_step(self):
        """启动过程中的两种拦截页：START 启动页 / 登录页「开始唤醒」

        返回 ``(页面类型, 点击像素 x, 点击像素 y)``，都不是就在加载，返回
        None 干等。

        判据（2026-11-17 真机截图量的，1920×1080）：
        - 登录页：「开始唤醒」深色按钮 + 白字钉在 (820,726)~(1100,816)，
          模板匹配认（均值 >165 的旧判据对登录页失效——底部深色横带把均值
          拉到 ~150）。命中后点按钮中心 (0.5w, 0.71h)。
        - START 页：底部中央的亮黄菱形 START——**实测中心在 (0.474w,
          0.949h)**，盲点 (0.5w, 0.91h) 会擦边点空。量**黄色**像素（r 高
          g 中 b 低）的质心当点击点：主界面亮皮肤、公告弹窗的底部都是亮色
          的，按亮度判会把主界面误判成 START 页，在主界面上连点几十下
          （2026-11-17 实测）。
        """
        try:
            from PIL import Image, ImageStat
            data = self.shot()
            if not data:
                return None
            im = Image.open(io.BytesIO(data)).convert('RGB')
            w, h = im.size

            class _Shot:
                def __init__(self, d):
                    self.d = d

                def shot(self):
                    return self.d

            tpl = _wakeup_tpl()
            if tpl is not None and ImageStat.Stat(im.convert('L')).mean[0] > 130:
                s, _, _ = _fixed_match(_Shot(data), tpl, 820, 726, rng=12)
                if s < _WAKEUP_MAX:
                    return ('login', int(w * 0.5), int(h * 0.71))
            x0, x1 = int(0.40 * w), int(0.60 * w)
            y0, y1 = int(0.85 * h), int(0.99 * h)
            region = im.crop((x0, y0, x1, y1))
            px = list(region.getdata())
            rw = x1 - x0
            sx = sy = n = 0
            for i, q in enumerate(px):
                r, g, b = q
                if r > 190 and g > 140 and b < 130 and r - b > 80:
                    sx += x0 + i % rw
                    sy += y0 + i // rw
                    n += 1
            if n >= 300:
                return ('start', sx / n, sy / n)
        except Exception:
            pass
        return None


    def ready(self):
        """adb 通道自检：返回 ``(ok, 说明)``

        为什么要先打这一发：打包成窗口程序后，子进程没带 ``CREATE_NO_WINDOW``
        时 adb 会去开新控制台，句柄不干净时命令直接哑掉——表现是「黑框一直闪、
        游戏一动不动」（2026-11-17 用户报的正是这个）。不先自检的话，代码会
        一路点空、最后报「翻了几十屏没找到这张卡片」，把人往模板过期上带。

        地址对不上也在这里兜：MAA 里可能写 ``emulator-5554``，也可能写
        ``127.0.0.1:5555``，同一台机器两个名字。``get-state`` 不通就拿
        ``adb devices`` 里第一个在线设备顶上。
        """
        out = self._run(['get-state'], timeout=15)
        if (out or b'').decode('utf-8', 'ignore').strip() == 'device':
            self.heal()
            return True, 'adb 正常（%s）' % self.address

        out = self._run(['devices'], timeout=15)
        text = (out or b'').decode('utf-8', 'ignore')
        for line in text.splitlines():
            parts = line.split('\t')
            if len(parts) >= 2 and parts[1].strip().startswith('device'):
                cand = parts[0].strip()
                if not cand or cand == self.address:
                    continue
                self.address = cand
                self._size = None
                out = self._run(['get-state'], timeout=15)
                if (out or b'').decode('utf-8', 'ignore').strip() == 'device':
                    return True, 'adb 正常（地址换成 %s）' % cand
        return False, ('adb 连不上设备「%s」（adb: %s）'
                       % (self.address, self.adb))


# ---------------------------------------------------------------- 位置判断
def probe(touch):
    """看一眼当前停在哪一页，返回 'map' / 'theme' / 'store' / 'home' / ''

    为什么非要知道：主界面点活动大图是记忆位置的（见模块注释），不判断的话
    连排几下必踩空——实测会一路点进**活动商店**，那地方乱点有误消费风险。

    三个特征都是从真机截图上量的（1920×1080）：

        ==========  ==========  ==========  ==========
        页面        底部白占比   中部纯白     中部亮度
        ==========  ==========  ==========  ==========
        活动地图     0.01        0.00        106
        主界面       0.37        0.02         69
        活动主题页   0.86        0.00        227
        活动商店     0.52        0.32        193
        ==========  ==========  ==========  ==========

    「底部白占比」区分地图/其它；「中部纯白」是商店的商品白卡片，其它页面
    几乎没有；主题页再靠中部亮度（亮）和主界面（暗）分开。
    """
    try:
        from PIL import Image
    except Exception:
        return ''
    try:
        data = touch.shot()
        if not data:
            return ''
        im = Image.open(io.BytesIO(data)).convert('RGB')
        w, h = im.size

        def stats(x1, y1, x2, y2):
            px = list(im.crop((int(x1 * w), int(y1 * h),
                               int(x2 * w), int(y2 * h))).getdata())
            n = len(px)
            if not n:
                return 0.0, 0.0, 0.0
            avg = sum(sum(p) for p in px) / (3.0 * n)
            white = sum(1 for p in px
                        if p[0] > 200 and p[1] > 200 and p[2] > 200) / n
            pure = sum(1 for p in px
                       if p[0] > 248 and p[1] > 248 and p[2] > 248) / n
            return avg, white, pure

        _, tab_white, _ = stats(0.01, 0.86, 0.35, 0.94)
        mid_avg, _, mid_pure = stats(0.30, 0.35, 0.70, 0.55)

        if tab_white < _MAP_WHITE_MAX:
            return 'map'
        if mid_pure > 0.20:          # 商品白卡片：只在商店成片出现
            return 'store'
        if tab_white > 0.70 and mid_avg > 150:
            return 'theme'
        return 'home'
    except Exception:
        return ''


def zone_rows_of(stage_code):
    """关卡代号 → 它在活动地图上的区域行号（0 起）

    两条路，先精确后兜底：

    1. **本地资源库查得到 zoneId**（``TL-5`` → ``act55side_05`` →
       ``act55side_zone1``）：同一活动前缀下的 zone 按 id 排序就是游戏里区域
       列表的顺序。实测 ``TL-5`` → 0（残阳终尽）。
    2. **查不到**（``arknights.json`` 只收录到发布那天，当期新活动的
       ``TL-S-*`` / ``TL-EX-*`` 常常还没有）：退回代号后缀推。活动关的通行排布
       是「基础关 → EX 挑战关 → S 特别关」，实测 ``TL-EX-1`` → 1、
       ``TL-S-1`` → 2，和游戏里看到的完全一致。

    主线关返回 None：``1-7`` 这类纯数字开头，或 ``S4-1`` / ``R8-2`` 这种
    「1 个字母 + 章节号」——那不是活动关，MAA 自己能找。
    """
    from . import resources

    if not stage_code:
        return None
    try:
        rows = resources._stages(True)
    except Exception:
        rows = []

    sid = next((s['stageId'] for s in rows if s.get('code') == stage_code), None)
    if sid and str(sid).startswith('act'):
        act = str(sid).split('_')[0]
        zones = []
        for s in rows:
            if str(s.get('stageId') or '').split('_')[0] != act:
                continue
            z = s.get('zoneId')
            if z and z not in zones:
                zones.append(z)
        mine = next((s.get('zoneId') for s in rows if s.get('stageId') == sid), None)
        if zones and mine:
            try:
                return sorted(zones).index(mine)
            except ValueError:
                pass

    # 兜底：看代号本身。带下划线的是内部 id（act55side_s01），调用方该传代号
    if '_' in stage_code:
        return None
    m = re.match(r'^([A-Za-z]+)', stage_code)
    if not m or len(m.group(1)) < 2:
        return None
    for key, idx in (points().get('zoneRowOverride') or {}).items():
        if stage_code.upper().startswith(str(key).upper()):
            try:
                return int(idx)
            except (TypeError, ValueError):
                pass
    rest = stage_code[len(m.group(1)):].lstrip('-').upper()
    if rest.startswith('EX'):
        return 1
    if rest.startswith('S-') or rest == 'S':
        return 2
    if re.match(r'^\d', rest):
        return 0
    return None


# ---------------------------------------------------------------- 老故事集
# 卡片模板图，文件名 = 活动代号（MN.png / WD.png …），1920 口径截的
# 「曲谱 → 乐章收录」网格里那张活动卡片。搜索时按屏幕宽度等比缩放。
NAVTPL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'navtpl')

def _navtpl_dirs():
    """模板目录候选，按优先级排

    打包版里 backend 包编进了 PYZ，``__file__`` 指向的 .pyc 并不真实存在，
    光靠 NAVTPL_DIR 会找不到随包发布的模板——表现为 ``tpl_path_of`` 返回空、
    明明配了模板却走「当期活动区域」那条错误分支（2026-11-17 打包版实测）。
    所以这里把 PyInstaller 的两个数据目录也列上。
    """
    dirs = []
    try:
        from .settings import CONFIG_DIR
        dirs.append(os.path.join(CONFIG_DIR, 'navtpl'))
    except Exception:
        pass
    dirs.append(NAVTPL_DIR)
    me = getattr(sys, '_MEIPASS', '')
    if me:
        dirs.append(os.path.join(me, 'backend', 'core', 'navtpl'))
    if getattr(sys, 'frozen', False):
        dirs.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                                 '_internal', 'backend', 'core', 'navtpl'))
    return [d for d in dirs if d and os.path.isdir(d)]

# 关卡代号形态：CW-1 / MN-EX-1 / 15-3 / TR-1。导航只吃这种形态，别的
# （活动 id、中文活动名）得先在调用侧换掉，见 skills.norm_stage。
_STAGE_CODE_RE = re.compile(r'^(?:\d{1,2}|[A-Za-z][A-Za-z0-9]*)(?:-[A-Za-z]+)?-\d+')

# 活动总表：代号 → 中文名。**这是导航功能的底账**（2026-11-17 用真机把
# 「乐章收录」整个网格扫了一遍：48 张卡片逐个导航进图，读落图上的关卡前缀
# （如 SV-1）反推真实代号，再用 wiki 核对有战斗关的活动集）。
# 只收目前还能进、有战斗关的活动集——插曲/别传常驻关全在内；
# 纯剧情故事集（战地秘闻、红松林、春分、踏寻往昔之风、昨日海、丛林症结、
# 磨律动等，进图落在剧情回放页、无 XX-1 关卡）与联动活动都不列。
# 例外：去咧嘴谷( TG)、雪山降临1101(OS)、未许之地(UR) 是故事集但有战斗关，
# 已收。易错对照：生路=BP、银心湖列车=RS、画中人=WR、登临意=WB、
# 不义之财=CV、崔林特尔梅之金=ZT、出苍白海=EP、众生行记=MT、
# 挽歌燃烧殆尽=EA、雅赛努斯复仇记=ME、熔炉"还魂"记=KR、日暮寻路=AW、墟=AT。
EVENTS = {
    # ---- 插曲（常驻） ----
    'DM': '生于黑夜', 'WD': '遗尘漫步', 'SV': '覆潮之下', 'SN': '愚人号',
    'BB': '巴别塔', 'GO': '追迹日落以西', 'CW': '孤星',
    # ---- 别传（常驻） ----
    'GT': '骑兵与猎人', 'OF': '火蓝之心',
    'TW': '沃伦姆德的薄暮', 'RI': '密林悍将归来', 'MN': '玛莉娅·临光',
    'MB': '孤岛风云', 'WR': '画中人', 'DH': '多索雷斯假日', 'NL': '长夜临光',
    'BI': '风雪过境', 'IW': '将进酒', 'GA': '吾导先路', 'LE': '尘影余音',
    'DV': '绿野幻梦', 'IC': '理想城：长夏狂欢季', 'IS': '叙拉古人',
    'FC': '照我以火', 'HE': '空想花庭', 'BP': '生路', 'WB': '登临意',
    'SL': '火山旅梦', 'CV': '不义之财', 'ZT': '崔林特尔梅之金',
    'EA': '挽歌燃烧殆尽', 'HS': '怀黍离', 'OR': '相见欢', 'MT': '众生行记',
    'PV': '揭幕者们', 'RS': '银心湖列车', 'OS': '雪山降临1101',
    'AS': '太阳甩在身后', 'UR': '未许之地', 'EP': '出苍白海',
    'AD': '红丝绒', 'ME': '雅赛努斯复仇记',
    # ---- 故事集带战斗关 / 新活动 ----
    'AT': '墟', 'AW': '日暮寻路', 'TG': '去咧嘴谷', 'KR': '熔炉"还魂"记',
}

def event_of(stage_code):
    """关卡代号 → ``(活动代号, 活动名)``；不在活动清单里返回 ``('', '')``

    只认 ``SV-1`` 这种 ``前缀-序号`` 形态；主线 ``15-3``、突袭 ``H15-1``、
    TR/EX 前缀都按前缀规则正常解析（``SV-EX-1`` → SV）。
    """
    m = re.match(r'^([A-Za-z][A-Za-z0-9]*)-', stage_code or '')
    if not m:
        return '', ''
    name = m.group(1)
    if name in EVENTS:
        return name, EVENTS[name]
    up = name.upper()
    if up in EVENTS:
        return up, EVENTS[up]
    return '', ''

# 卡片在网格里的相似度阈值（1/4 降采样后的灰度平均绝对差）：
# 实测真卡 0~7，无关卡 21~31，但个别暗色模板（出苍白海/将进酒/登临意）在
# 相邻暗卡上能拿 14~19 分——阈值 18 会放行误匹配，点进别的活动（2026-11-17
# 全量测试实测：RS 模板点进了 MN 的地图）。收紧到 12：真卡全部 ≤7，安全。
_CARD_MAX_SCORE = 12.0

# 提前结束的阈值：低于这个分数才认为「肯定是它」
_EARLY_STOP = 6.0
# 曲谱网格最多翻几屏。整张表（顶部「主题曲」EP 网格 + 各 Story Line 分组
# + 未归组的新活动）2026-11-18 实测约 15 屏、5400px（1080 口径）；这里留
# 56 屏的余量只是兜底上限——正常情况下「连着两屏画面没动」就判定到底提前
# 退出了，不会真扫 56 屏。
_MAX_SCREENS = 56
# 每屏往下滑多少（1280 口径）。**别为了快把这个调大**：560 那版实测会漏屏
# ——玛莉娅·临光（MN）正好落在两次截图之间，翻完 34 屏报「没找到」，而它其实
# 在屏幕上掠过过（2026-11-17 用 360 重扫，第 5 屏 1.0 分命中）。
# 360 仍会**截断卡片**：网格行高（1080 口径）约 280px，540 的步长下滑时
# 约一半的行停在「上下各截一半」的相位上，截断卡的匹配分会从 4 分涨到
# 19 分，直接掉出 12 分阈值（2026-11-17 傍晚：DH/GO/IW 全栽在这）。
# 240（1080 口径 360px）保证每行至少有一屏基本完整；整表 15 屏，够。
_SWIPE_STEP = 240
# 粗扫（1/4 尺度）分数高于这个值就不做全分辨率精修了：滚动时绝大多数屏
# 都是无关卡片，省下来的时间够多扫好几屏。真卡粗扫不会超过 20 分。
_COARSE_SKIP = 32.0


def tpl_path_of(stage_code):
    """关卡代号 → 卡片模板路径；没有模板返回 ''

    先找用户配置目录（config/navtpl），再找内置的（backend/core/navtpl）。
    内置目录随程序打包走，用户自己加的放 config 里，升级不会覆盖。
    """
    m = re.match(r'^([A-Za-z][A-Za-z0-9]*)-', stage_code or '')
    if not m:
        return ''
    name = m.group(1)
    for d in _navtpl_dirs():
        for cand in (name, name.upper(), name.capitalize()):
            p = os.path.join(d, cand + '.png')
            if os.path.isfile(p):
                return p
    return ''


def find_card_bytes(data, tpl_file, max_score=_CARD_MAX_SCORE):
    """在**一张截图字节**里找模板卡片，返回 ``(像素x, 像素y, 分数)``

    和 :func:`find_card` 同一套算法，区别只是不自己去截图——扫描循环要先
    等画面停稳、再拿那一帧去匹配（见 :func:`_settled_shot`），否则会对着
    滑动动画中的模糊帧匹配，分数虚高直接判「没找到」。
    """
    from PIL import Image, ImageChops, ImageStat

    if not data:
        return 0, 0, 999.0
    try:
        screen = Image.open(io.BytesIO(data)).convert('L')
        tpl0 = Image.open(tpl_file).convert('L')
    except Exception:
        return 0, 0, 999.0

    S = 4
    small = screen.resize((screen.width // S, screen.height // S),
                          Image.BILINEAR)
    SW, SH = small.size
    best = (999.0, 0, 0, 0, 0)          # (分数, x, y, 模板宽, 模板高)

    for zoom in (1.0, 0.96, 1.04):
        sc = screen.width / 1920.0 * zoom
        tw = max(16, int(round(tpl0.width * sc)))
        th = max(12, int(round(tpl0.height * sc)))
        t = tpl0.resize((max(4, tw // S), max(4, th // S)), Image.BILINEAR)
        TW, TH = t.size
        if TW >= SW or TH >= SH:
            continue

        # --- 1) 粗扫
        coarse = (999.0, 0, 0)
        step = 4
        for y in range(0, SH - TH, step):
            for x in range(0, SW - TW, step):
                s = ImageStat.Stat(ImageChops.difference(
                    small.crop((x, y, x + TW, y + TH)), t)).mean[0]
                if s < coarse[0]:
                    coarse = (s, x, y)

        # 粗扫都明显不像就别做全分辨率精修了（那一步最贵，两轮上千次全图
        # 比对）。翻屏时 90% 的屏都是无关卡片，跳过能省一半时间。
        if coarse[0] > _COARSE_SKIP:
            continue

        # --- 2) 精修
        _, cx, cy = coarse
        for y in range(max(0, cy - step), min(SH - TH, cy + step) + 1):
            for x in range(max(0, cx - step), min(SW - TW, cx + step) + 1):
                s = ImageStat.Stat(ImageChops.difference(
                    small.crop((x, y, x + TW, y + TH)), t)).mean[0]
                if s < coarse[0]:
                    coarse = (s, x, y)

        # --- 3) 全分辨率复核
        # 1/4 尺度上「玛莉娅·临光」和隔壁「长夜临光」能同时拿到 10 分，
        # 1/4 分不出来（实测：拿 MN 模板去认，跑到长夜临光那张卡上）。全分辨
        # 率再比一次就拉开了：同一张卡 0~3 分，相邻卡 15 甚至 30 分。
        fx = coarse[1] * S
        fy = coarse[2] * S
        fw, fh = tw, th
        fine = (coarse[0], fx, fy)
        # 两轮：先 ±16 走 4 步（补上 1/4 尺度 16 像素的粒度），再 ±4 走 1 步
        for rng, st in ((16, 4), (4, 1)):
            bx, by = fine[1], fine[2]
            for dy in range(-rng, rng + 1, st):
                for dx in range(-rng, rng + 1, st):
                    px, py = bx + dx, by + dy
                    if px < 0 or py < 0 or px + fw > screen.width or py + fh > screen.height:
                        continue
                    d = ImageStat.Stat(ImageChops.difference(
                        screen.crop((px, py, px + fw, py + fh)), tpl0)).mean[0]
                    if d < fine[0]:
                        fine = (d, px, py)

        if fine[0] < best[0]:
            best = (fine[0], fine[1], fine[2], fw, fh)
        if best[0] <= _EARLY_STOP:
            break

    score, x, y, bw, bh = best
    if not bw or not bh:
        return 0, 0, score
    return x + bw // 2, y + bh // 2, score


def find_card(touch, tpl_file, max_score=_CARD_MAX_SCORE):
    """在**当前画面**上找模板卡片，返回 ``(像素x, 像素y, 分数)``

    找不到时 x/y 为 0。分数越小越像，``max_score`` 以内才算命中。

    做法：**先粗扫再精修**。
    1. 粗扫：整屏缩到 1/4，按 4 像素步长（≈16 实际像素）扫一遍，记住最像的
       那个格子。1/4 尺度下 480×270，候选 120×67 ≈ 8000 个，但真正做逐像素
       比对的是 C 层的 ``ImageChops``，一遍 0.3 秒左右。
    2. 精修：在最像的格子周围 ±5 像素（1/4 尺度）穷举，拿到准确位置。

    **为什么不再用「行/列投影预筛」**：那套只取前 5 行候选，遇到上下两头都
    很暗、行均值几乎没起伏的卡片（比如 孤岛风云 MB）会把真位置整个筛掉，最后
    返回一个 20 多分的假位置。真卡自匹配是 6 分，差得远，但代码不知道。
    """
    return find_card_bytes(touch.shot(), tpl_file, max_score)


def is_extra(stage_code):
    """这个代号是不是 EX 挑战关（``MN-EX-1`` / ``MNEX1`` 都算）"""
    return bool(re.search(r'EX[-_]?\d', (stage_code or '').upper()))


def ex_point(stage_code):
    """EX 切换按钮的位置；按活动前缀可以单独覆盖"""
    p = points()
    pre = re.match(r'^([A-Za-z][A-Za-z0-9]*)', stage_code or '')
    table = p.get('exSwitchByEvent') or {}
    if pre:
        v = table.get(pre.group(1).upper()) or table.get(pre.group(1))
        if isinstance(v, list) and len(v) == 2:
            return v[0], v[1]
    v = p.get('exSwitch') or [1199, 620]
    if isinstance(v, list) and len(v) == 2:
        return v[0], v[1]
    return 1199, 620


def at_home(touch):
    """是不是在主界面（带皮肤的那套也算）

    两个特征都是 2026-11-17 量的（1920 口径）：

        ==========  ==================  ============
        页面        底部功能格白像素     终端卡亮度
        ==========  ==================  ============
        主界面       0.45               173
        曲谱网格     0.03                72
        MN 详情页    0.00                71
        活动地图     0.03                39
        ==========  ==================  ============

    为什么要专门判这个：老故事集的起点可能在曲谱系的任意一层（网格 / 时间轴
    列表 / 活动详情页），三层要按几次返回才回到主界面并不固定。**只有确认到
    了主界面才停手**，否则多按一次就会弹「退出游戏」。
    """
    try:
        from PIL import Image
        data = touch.shot()
        if not data:
            return False
        im = Image.open(io.BytesIO(data)).convert('RGB')
        w, h = im.size

        def white(x1, y1, x2, y2):
            px = list(im.crop((int(x1 * w), int(y1 * h),
                               int(x2 * w), int(y2 * h))).getdata())
            n = len(px)
            if not n:
                return 0.0
            return sum(1 for p in px if p[0] > 225 and p[1] > 225 and p[2] > 225) / n

        def bright(x1, y1, x2, y2):
            px = list(im.crop((int(x1 * w), int(y1 * h),
                               int(x2 * w), int(y2 * h))).getdata())
            n = len(px)
            return (sum(sum(p) for p in px) / (3.0 * n)) if n else 0.0

        # 只用底部功能格那一片（任务/基建/仓库 四块白卡）。别用「终端卡」
        # 亮度：活动详情页的奖励卡片也是亮的，会误判成主界面。
        return white(0.52, 0.80, 0.95, 0.93) > 0.20
    except Exception:
        return False


def in_story_page(touch):
    """当前是不是曲谱系（曲谱时间轴 / 乐章收录网格）

    **两个条件都要满足**，2026-11-17 在真机四张页面上量的（1920×1080）：

        ==============  ================  ==============
        页面            底部左 tab 白占比   左上角灰度均值
        ==============  ================  ==============
        曲谱页           0.012             43
        乐章收录网格      0.000             53
        终端页（有 tab）  0.100            130
        功能网格主界面    0.208             99   ← 只看灰度会误判
        ==============  ================  ==============

    只看「左上角暗不暗」是要命的：主界面皮肤立绘压在左侧，灰度 98.5，正好
    卡在 100 下面，于是被当成「已经进曲谱了」——接着去点右上角的「乐章收录」、
    开始翻三十几屏，全在主界面上空转，最后报「翻了几十屏都没找到这张卡片」。
    用户 2026-11-17 报的「登录成功但跳关卡失败」就是这个（失败截图停在主界面，
    根本不是卡在网格里）。

    底部那条空白占比是主界面与曲谱系最硬的分界：曲谱系底部是半透明黑 tab 栏，
    主界面底部是 任务/基建/仓库 那一排白卡（0.208），差一个数量级。
    """
    try:
        from PIL import Image, ImageStat
        data = touch.shot()
        if not data:
            return False
        im = Image.open(io.BytesIO(data)).convert('RGB')
        w, h = im.size

        px = list(im.crop((int(0.01 * w), int(0.86 * h),
                           int(0.35 * w), int(0.94 * h))).getdata())
        n = len(px) or 1
        tab_white = sum(1 for p in px
                        if p[0] > 225 and p[1] > 225 and p[2] > 225) / n
        if tab_white >= 0.05:
            return False

        left = im.crop((int(0.02 * w), int(0.05 * h),
                        int(0.20 * w), int(0.35 * h))).convert('L')
        return ImageStat.Stat(left).mean[0] < 90
    except Exception:
        return False


def in_score_grid(touch):
    """是不是「乐章收录」的卡片网格（曲谱右上「乐章收录」切过去那一页）

    判据只看顶部那排标签「默认进度 / 奖励进度 / 完成进度」：网格视图里三选一
    必然有一个是**蓝字 + 蓝色下划线**，曲谱时间轴同一片区域是纯暗背景
    （2026-11-18 实测蓝像素占比：网格 0.035，时间轴 0.000，阈值 0.008）。

    为什么要判：点完「乐章收录」如果没进去（按钮挪位置 / 切页动画没吃完），
    后面就要在时间轴页上翻四十屏找卡片，最后报「模板过期」，把人往错的方向带。
    """
    try:
        from PIL import Image
        data = touch.shot()
        if not data:
            return False
        im = Image.open(io.BytesIO(data)).convert('RGB')
        w, h = im.size
        px = list(im.crop((int(0.55 * w), int(0.03 * h),
                           int(0.80 * w), int(0.10 * h))).getdata())
        n = len(px) or 1
        blue = sum(1 for q in px
                   if q[2] > 150 and q[2] - q[0] > 50) / n
        return blue > 0.008
    except Exception:
        return False


def in_story_dialog(touch):
    """当前是不是剧情对话 / CG 回放页（右上角有「自动 OFF 跳过▶」）

    2026-11-17 真机实测（去咧嘴谷 幕间回放，1920×1080）：右上「跳过▶」
    固定在 x1655~1840 / y50~110。

    **两任判据都翻过车，现在是第三版**：
    1. 白像素占比——主界面右上角同一片区域是货币数字（合成玉 / 342），
       白像素一样过线，主界面被当成剧情页，「排水」在主界面按返回键弹
       「退出游戏」又点中确认，游戏就这么被自己关掉了（反复「游戏莫名
       被关」的真凶）。
    2. 灰度模板——模板里烤进了 TG 的亮天空背景，换一张暗背景的剧情就
       失配；而且最初把「模板中心」当「窗口左上角」用，全体错位。
    3. 现在用**字形掩码 XOR**：两边都二值化（>235 只留白字核心，背景
       无关），比对掩码差异占比。实测：TG 亮 CG 0.00、沙地 CG 0.14，
       主界面 2.11 / 桌面 1.00 / START 1.16 / 登录页 1.00，阈值 0.55。
       登录页天然排除（没有字形），不再需要额外防误判。
    """
    try:
        from PIL import Image, ImageChops
        tpl = _story_skip_tpl()
        if tpl is None:
            return False
        data = touch.shot()
        if not data:
            return False
        im = Image.open(io.BytesIO(data)).convert('L')
        w, h = im.size
        k = w / 1920.0
        tw = max(4, int(tpl.width * k))
        th = max(4, int(tpl.height * k))
        tmask = tpl.resize((tw, th), Image.BILINEAR).point(
            lambda v: 255 if v > 235 else 0)
        tc = sum(1 for v in tmask.getdata() if v)
        if not tc:
            return False
        bx, by = int(1655 * k), int(50 * k)
        best = 9.9
        for dy in range(-10, 11, 2):
            for dx in range(-10, 11, 2):
                x, y = bx + dx, by + dy
                if x < 0 or y < 0 or x + tw > w or y + th > h:
                    continue
                win = im.crop((x, y, x + tw, y + th)).point(
                    lambda v: 255 if v > 235 else 0)
                diff = sum(1 for a, b in zip(win.getdata(), tmask.getdata())
                           if a != b)
                best = min(best, diff / tc)
        return best < _STORY_SKIP_MAX
    except Exception:
        return False


def _moved(a, b, thr=3.0):
    """两张截图是不是明显不同（界面真的换了页）

    阈值 3.0：页面切换是整屏背景变化（差值几十），动画底噪只有 1~2。
    """
    try:
        from PIL import Image, ImageChops, ImageStat
        ia = Image.open(io.BytesIO(a)).convert('L')
        ib = Image.open(io.BytesIO(b)).convert('L')
        if ia.size != ib.size:
            return True
        return ImageStat.Stat(ImageChops.difference(ia, ib)).mean[0] > thr
    except Exception:
        return True


def _settled_shot(touch, wait=2.2):
    """滑动后等画面停稳再截图

    2026-11-18 实测：``input swipe`` 之后画面要 1.8~2.2s 才真正停住（惯性
    逐帧差 22 → 11 → 3.8 → 2.1 → 1.6），1.4s 时截到的还是拖影帧：卡片被
    拉成两半、顶部多一条半透明标题带，拿去匹配分数虚高，于是「卡片明明在
    屏幕上」却报没找到，用户看到的就是「有几张卡认不出来 / 导航失败」。

    **为什么不再连拍两帧比差**：卡片图有呼吸/闪光动画，同一位置连拍两帧也
    有 1.76 的差，判「停稳」的阈值只能定到 2.5 左右，跟真实惯性的尾部（2.1）
    几乎重叠，结果几乎每屏都要多等一轮——每屏多花 1.5s，扫完整页白烧 20 秒。
    现在固定等 2.2s 拍一帧；万一撞上拖影帧，靠两件事兜：步长 240 小于卡片在
    屏内的可见高度，同一张卡在相邻 3~4 屏里都会完整出现；整屏一个候选都没有
    （999）时扫描循环会补拍一帧复核（多半是缩略图还没加载出来）。
    """
    time.sleep(wait)
    return touch.shot()


def _find_check_btn(touch):
    """「是否跳过剧情」弹窗的 ✓ 圆钮中心（找不到返回 None）

    2026-11-17 真机实测：弹窗淡入要 0.8~3.8s 不等；而且 ✓ 的位置**随文案
    长度漂移**——同一段回放里一次 (1189,672)、一次 (1109,703)（1920 口径），
    固定坐标必踩空（点偏了只会推进一行台词）。✓ 在弹窗右半带
    （0.55w~0.72w、0.58h~0.72h）是一团白色圆钮，白像素攒够 280 个才算它
    真的渲染出来了，量这团白的质心当中心。
    """
    try:
        from PIL import Image
        data = touch.shot()
        if not data:
            return None
        im = Image.open(io.BytesIO(data)).convert('RGB')
        w, h = im.size
        x0, x1 = int(0.55 * w), int(0.72 * w)
        y0, y1 = int(0.58 * h), int(0.72 * h)
        px = list(im.crop((x0, y0, x1, y1)).getdata())
        cw = x1 - x0
        xs = ys = n = 0
        for i, q in enumerate(px):
            if q[0] > 225 and q[1] > 225 and q[2] > 225:
                xs += x0 + i % cw
                ys += y0 + i // cw
                n += 1
        if n >= 280:
            return (xs / n, ys / n)
        return None
    except Exception:
        return None


def _skip_story_replay(touch, log, rounds=40):
    """在剧情回放里连续跳幕，直到脱离剧情页 / 断定回放在循环重播

    每轮：返回键弹「是否跳过剧情」→ 轮询 ✓ 圆钮（最多 6s）→ 点它的实测
    中心。弹窗偶尔不出（幕间过场），空过一轮无妨。返回：

        'out'   —— 脱离剧情页（**连续两帧**不在剧情页才算：幕间过场会把
                    「跳过」短暂藏起来，单帧判断会提前收工）
        'stuck' —— 跳了 rounds 轮还在剧情里，或检测到回放循环重播
                    （同幕后半张脸隔 3 轮又出现，连续命中两次才认定，
                    避免不同幕共用同一张黑底 CG 的误伤）
    """
    seen = {}
    loop_hits = 0
    calm = 0
    for rnd in range(rounds):
        if not in_story_dialog(touch):
            calm += 1
            if calm >= 2:
                # 追加 2.5s 确认：幕与幕的转场会把「跳过」藏起来两三秒，
                # 两帧安静就当真出来会在转场里误判（2026-11-17 实测：登录
                # 续播回放，排水反复「跳出」又掉回去，轮次白白耗尽）
                time.sleep(2.5)
                if not in_story_dialog(touch):
                    return 'out'
            time.sleep(1.2)
            continue
        calm = 0
        # 循环重播检测：给这幕的上半屏（避开正在打字的台词区）拍个粗指纹
        key = None
        try:
            from PIL import Image
            data = touch.shot()
            if data:
                im = Image.open(io.BytesIO(data)).convert('L')
                w, h = im.size
                key = (im.crop((0, 0, w, int(0.55 * h))).resize((48, 27))
                       .point(lambda v: v // 24).tobytes())
        except Exception:
            pass
        if key is not None and key in seen and rnd - seen[key] >= 3:
            loop_hits += 1
            log('第 %d 轮又见到第 %d 轮的同一幕（命中 %d/2）' %
                (rnd, seen[key], loop_hits))
            if loop_hits >= 2:
                return 'stuck'
        if key is not None:
            seen[key] = rnd
        touch.back()
        hit = None
        for _ in range(20):
            time.sleep(0.3)
            hit = _find_check_btn(touch)
            if hit:
                break
        if hit:
            touch.tap_px(*hit)
            time.sleep(2.2)
        else:
            time.sleep(1.8)
    return 'stuck'


def _scroll_top(touch, steps=12):
    """把网格甩到最顶（快速拖拽，过冲的回弹无害）

    为什么要甩而不是精确滑：这页现在有「主题曲 EP 网格 + 各 Story Line 分组」，
    总高约 5400px（1080 口径），16×440 的旧写法只走 10560px 的 720 口径……
    从列表底部出发根本到不了顶，扫描就从中间开始，上半部分的卡永远扫不到
    （密林悍将归来 RI 就在第 12 屏左右，正是半路起点最容易漏的深度）。
    14 下 × 800 = 11200（720 口径）足够盖住整页。
    """
    for _ in range(steps):
        touch.swipe(20, 180, 20, 980, 240)
        time.sleep(0.35)


def _navcache_file():
    """打开记录文件路径（config/navcache.json，用户配置目录，升级不覆盖）"""
    try:
        from .settings import CONFIG_DIR
        return os.path.join(CONFIG_DIR, 'navcache.json')
    except Exception:
        return ''


def _load_card_offset(prefix):
    """上次这张卡在第几像素（1080 口径，从网格顶部算）；没有返回 0"""
    try:
        f = _navcache_file()
        if not f or not os.path.exists(f):
            return 0
        with open(f, encoding='utf-8') as fh:
            data = json.load(fh) or {}
        return int((data.get('cardOffset') or {}).get(prefix) or 0)
    except Exception:
        return 0


def _save_card_offset(prefix, px):
    """记下这张卡的位置，下次直接翻过去（活动顺序一变就会验不过，自动回退全扫）"""
    try:
        f = _navcache_file()
        if not f or not prefix or px <= 0:
            return
        data = {}
        if os.path.exists(f):
            with open(f, encoding='utf-8') as fh:
                data = json.load(fh) or {}
        data.setdefault('cardOffset', {})[prefix] = int(px)
        with open(f, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _prefix_of(stage_code):
    m = re.match(r'^([A-Za-z][A-Za-z0-9]*)-', stage_code or '')
    return m.group(1).upper() if m else ''


def goto_story_activity(touch, stage_code, log=None, tpl=None):
    """老故事集：主界面 → 曲谱 → 乐章收录 → 网格找卡片 → 进入活动

    这条路是 2026-11-17 在真机上按截图点出来的：

        主界面   --中央「终端」大字------> 带底部 tab 的那一页
        底部 tab --第 2 个（曲谱）-------> 曲谱：左边 Story Line 时间轴
        曲谱     --右上「乐章收录」------> 网格视图（一行几个活动卡片）
        网格     --往下滑 + 模板匹配-----> 目标活动的卡片
        卡片     --「进入活动」---------> 该活动的关卡地图

    到关卡地图就不用管了：Copilot 的 ``ClickStageName`` 会在整屏 OCR 找
    ``MN-7`` 这种代号，点进去开打。

    **为什么必须模板匹配、不能用固定坐标**：网格里卡片的位置取决于滚到哪
    了，而滚到哪又取决于上次退出时的位置；新活动还会插进来把顺序挤动。
    实测把网格滚到顶后位置是稳定的，但活动一多就会变，所以按图找。
    """
    log = log or (lambda *_: None)
    tpl = tpl or tpl_path_of(stage_code)
    if not tpl:
        m = re.match(r'^([A-Za-z][A-Za-z0-9]*)-', stage_code or '')
        return False, ('没有「%s」的卡片模板：需要一张 1920×1080 截图里该活动'
                       '卡片的裁图，存成 navtpl/%s.png'
                       % (stage_code, (m.group(1) if m else stage_code)))

    p = points()
    # 游戏没在运行时，返回键 / ESC / 模板匹配全是在安卓桌面上空转——
    # 2026-11-17 中午实测：StartUp 假成功后导航照跑，nav-fail 现场图就是一张
    # 桌面截图。进程不在就先拉起来；restart_app 里的 force-stop 对没开着的
    # 游戏是无害 no-op。
    if not touch.game_alive():
        log('游戏进程不在运行，先启动游戏')
        if not touch.restart_app(log):
            return False, '游戏没能启动（检查模拟器是否开着）'

    _code, _name = event_of(stage_code)
    log('走曲谱：曲谱 → 乐章收录 → 找卡片 %s%s'
        % (os.path.basename(tpl), ('（%s）' % _name) if _name else ''))

    # 0) 不管起点在哪，先回到主界面。曲谱那几层（网格 / 时间轴 / 活动详情）
    #    背景都是暗的，主界面是亮的，所以用「亮不亮」当判据，**确认到了主界面
    #    才停手**——多按一次返回就弹「退出游戏」了。
    # 「真的回到主界面」= 底部功能格够白 **且** 不在暗底页上 **且** 不在剧情
    # 回放里 **且** 不在登录页。只看第一条件会在三处翻车：带白色 EX 徽章的
    # 活动地图（TW 右下角大白按钮把白像素撑过阈值，2026-11-17 EX 抽查实测）；
    # 去咧嘴谷那种明亮沙地 CG 回放（整屏都是亮的，底部白像素照样过线，
    # 2026-11-17 复测实测：从第 15 个活动起连续 34 屏 999 就是它）；登录页
    # 浅灰底 + 白 logo（2026-11-17 重启实测：误报回主界面，导航全程空转）。
    def really_home():
        return (at_home(touch) and not in_story_page(touch)
                and not in_story_dialog(touch) and not on_login_page(touch))

    restarted = False
    login_taps = 0
    home = False
    for i in range(10):
        home = really_home()
        if home:
            break
        # 每轮先补一发「鼠标抬起」：注入被打断会让按键卡在按下状态，之后
        # 所有 tap 全被吃掉（2026-11-17 真机踩过，keyevent 却好使，极具迷惑性）
        touch.heal()
        # 剧情页要**最优先**处理：明亮 CG 连 at_home 都能骗过，不先排掉的话
        # 后面的返回键 / ESC / 「<」全是在剧情页上瞎点。
        if in_story_dialog(touch):
            log('剧情 / CG 回放页，连续跳幕（back 弹「跳过剧情」，点实测 ✓）')
            if _skip_story_replay(touch, log) == 'stuck':
                # 回放**循环重播**，点击永远出不去，只能重启 App（约 1 分钟）
                if restarted or not touch.restart_app(log):
                    return False, '剧情回放循环重播，跳不出去（重启也没能回主界面）'
                restarted = True
            continue
        # 登录页直接点「开始唤醒」（按钮居 0.5w, 0.71h）：返回键在这里只会
        # 弹「退出游戏」，越按越糟。连点 3 次还没进去就是误判/卡死，落到
        # 后面的重启兜底——别让这一条把 10 轮全吃光（2026-11-17 复测实测：
        # 详情页误判成登录页，连点 10 次直到 FAIL）
        if on_login_page(touch) and login_taps < 3:
            login_taps += 1
            log('停在登录页，点「开始唤醒」（第 %d 次）' % login_taps)
            w, h = touch.size()
            touch.tap_px(int(w * 0.5), int(h * 0.71))
            time.sleep(STEP_DELAY)
            continue
        # 前两次用系统返回键；还不行就点左上角那个「<」——有些详情页不响应
        # keyevent 4（实测 MN 的奖励详情页就是这样，按了原地不动）
        if i < 1:
            log('不在主界面，按返回键')
            touch.back()
        elif i < 4:
            # 系统返回键在详情页上常常没反应，ESC 好使（用户提醒的）
            log('返回键没反应，按 ESC')
            touch.esc()
        elif i < 7:
            log('ESC 也没反应，点左上角「<」')
            touch.tap(*p['backCorner'])
        elif not restarted:
            # 还出不去：剧情回放是**循环重播**的（跳完全幕又从第一幕来），
            # 点击永远出不去，只能重启 App（约 1 分钟）
            restarted = True
            if not touch.restart_app(log):
                return False, '重启 App 后仍未回到主界面'
        else:
            log('重启后仍不在主界面，点左上角「<」')
            touch.tap(*p['backCorner'])
        time.sleep(STEP_DELAY)
    if not home:
        # 循环里最后一次判定和这里隔了好几百毫秒，页面可能刚好在切页动画中间；
        # 再确认一次，别把「刚回到主界面」误判成「卡在弹窗里」——2026-11-18
        # 实测 RI 导航就这么被自己判失败过一次（日志 4 秒就 FAIL，游戏其实在主界面）。
        time.sleep(1.2)
        home = really_home()
    if not home:
        return False, '回不到主界面（可能卡在某个弹窗里）'

    # 1) 主界面 → 曲谱。
    #    第一步是**按 16:9 固定位置点「终端」大卡**，不去认「终端」两个字：
    #    很多人换了皮肤，这两个字会被立绘盖住，但按钮区域很大而且一定在。
    def to_story():
        """从当前页走到曲谱，返回是否成功

        主界面点「终端」大卡：功能网格那套会翻出底部 tab 栏；带 tab 的那套
        会进当期活动主题页（也带 tab）。两种情况下 tab 栏都在，再点第 2 个
        tab 就是曲谱。**已经在曲谱/终端页时不要再点大卡**——那会误进当期
        活动详情（多绕一层，有时还会被「进入活动」按钮带跑）。
        """
        if at_home(touch):
            touch.tap(*p['terminalCard'])
            time.sleep(STEP_DELAY)
        touch.tap(*p['tabStory'])
        time.sleep(MAP_DELAY)
        return in_story_page(touch)

    if not to_story():
        log('第一次没进曲谱，回终端页再来一次')
        touch.tap(*p['tabTerminal'])
        time.sleep(STEP_DELAY)
        if not to_story():
            return False, '进不去曲谱页（界面结构可能变了）'

    # 2) 右上「乐章收录」→ 网格。
    #    **进去后必须点一下「默认进度」**：这一页会记住上次选的标签，停在
    #    「完成进度」/「奖励进度」时每张卡左上角都盖着进度徽章（38/39 那种
    #    蓝色菱形 + 勾），卡片图被改掉一大块，模板匹配从 5 分直接涨到 20 分
    #    以上，后果是整轮扫描白跑、报「翻了 56 屏没找到」（2026-11-18 用户
    #    报密林悍将归来 RI 导航失败，现场截图正停在「完成进度」）。
    for i in range(2):
        touch.tap(*p['scoreBook'])
        time.sleep(MAP_DELAY)
        if in_score_grid(touch):
            break
        log('点了「乐章收录」还没进网格（第 %d 次）' % (i + 1))
    if not in_score_grid(touch):
        return False, ('进不去「乐章收录」网格（点右上角「乐章收录」没反应，'
                       '界面结构可能变了）')
    touch.tap(*p['tabProgress'])
    time.sleep(STEP_DELAY)
    log('切到「默认进度」标签（保证卡片上没有进度徽章）')

    # 3) 先看当前这一屏：网格会记住上次浏览的位置，用户刚看过这个活动的话
    #    一次就中，省掉「滚到顶 + 翻十几屏」那一两分钟。
    best = (999.0, 0, 0)
    prefix = _prefix_of(stage_code)

    def _scan(start=0):
        """从当前位置一屏一屏往下找，返回 ``(分数, x, y, 绝对屏号)``

        ``start`` 是「从网格顶部已经翻了几屏」，只影响日志和位置缓存的记录。
        步长 240（720 口径）< 卡片在屏内的可见高度，同一张卡在相邻 3~4 屏里
        都会完整出现，偶尔一屏糊了不影响。
        """
        prev_frame = None
        still = 0
        hit = (999.0, 0, 0, -1)
        for i in range(_MAX_SCREENS - start):
            # **等画面停稳再截图**：滑动惯性要 1.8~2.2s 才停，中途截到的帧
            # 整屏带拖影（卡片被拉成两半），匹配分数虚高 → 明明在屏幕上却报没找到。
            frame = _settled_shot(touch)
            x, y, sc = find_card_bytes(frame, tpl)
            if sc >= 999.0:
                # 整屏连一个候选都没有：多半是缩略图还没加载出来（黑占位）。
                # 补拍一帧，图到了就有分。
                x, y, sc = find_card_bytes(_settled_shot(touch, wait=0.9), tpl)
                if sc < 999.0:
                    log('第 %d 屏补拍后有了候选（%.1f 分）' % (start + i + 1, sc))
            if x and sc < hit[0]:
                hit = (sc, x, y, start + i)
                log('第 %d 屏更新最佳：%.1f' % (start + i + 1, sc))
            if sc <= _EARLY_STOP:
                break
            # 到底了就停：连着两屏「画面没动」说明滑不动了。以前靠固定 56 屏
            # 兜底，到了底还在原地空转 40 屏（一屏 4 秒，白烧三分钟）。
            if prev_frame is not None and not _moved(prev_frame, frame, thr=3.0):
                still += 1
                if still >= 2:
                    log('滚到底了（第 %d 屏）' % (start + i + 1))
                    break
            else:
                still = 0
            prev_frame = frame
            # 滑动要**慢**：滑快了惯性会把整屏带过去，也会被判成点击。
            # 起点必须在**最左边缘**（20）的空白/时间轴圆点上：起点压在卡片上
            # 会被游戏当成点击，直接跳进那个活动的详情页——2026-11-17 就是
            # 这么「扫着扫着进了覆潮之下详情页，之后几十屏分数一动不动」。
            touch.swipe(20, 660, 20, 660 - _SWIPE_STEP, 700)
            time.sleep(0.4)
        return hit

    # 3) 先看当前这一屏：网格会记住上次浏览的位置，用户刚看过这个活动的话
    #    一次就中，省掉「滚到顶 + 翻十几屏」那一两分钟。
    x, y, sc = find_card_bytes(_settled_shot(touch), tpl)
    if x and sc <= _CARD_MAX_SCORE:
        best = (sc, x, y)
        log('当前这一屏就找到了（%.1f 分）' % sc)
    else:
        # 4) 滚到顶。这一步必须做：整页约 15 屏（顶部还有「主题曲」EP 网格），
        #    从列表底部出发到不了顶，扫描就会从中间开始、上半部分的卡永远扫不到。
        _scroll_top(touch)

        # 4.2) 上次扫到过这张卡的话，直接翻到它附近往下扫（活动加新的会把顺序
        #      挤动，所以只当提示用；一路扫到底都没有就回顶老实重扫一遍）。
        #      少翻一屏：回顶后的第一下拖拽常被顶部回弹吃掉一点，实测差一屏。
        cached = _load_card_offset(prefix)
        n = (max(0, int(round(cached / (_SWIPE_STEP * 1.5))) - 1)
             if cached else 0)
        if n >= 1:
            n = max(0, int(round(cached / (_SWIPE_STEP * 1.5))) - 1)
            log('按上次记录直接翻到第 %d 屏' % (n + 1))
            for _ in range(n):
                touch.swipe(20, 660, 20, 660 - _SWIPE_STEP, 700)
                time.sleep(0.5)
            sc, x, y, idx = _scan(start=n)
            if x and sc <= _CARD_MAX_SCORE:
                best = (sc, x, y)
                _save_card_offset(prefix, idx * _SWIPE_STEP * 1.5)
            else:
                log('缓存位置往下扫到底也没有（%.0f 分），回顶重扫' % sc)
                _scroll_top(touch)

        if best[0] > _CARD_MAX_SCORE:
            sc, x, y, idx = _scan(0)
            if x and sc <= _CARD_MAX_SCORE:
                best = (sc, x, y)
                _save_card_offset(prefix, idx * _SWIPE_STEP * 1.5)

    sc, x, y = best
    if not x or sc > _CARD_MAX_SCORE:
        return False, ('把整张「乐章收录」网格翻到底都没找到这张卡片（最像 %.0f 分，'
                       '模板可能过期了）' % sc)
    log('找到卡片（相似度 %.1f），点进去' % sc)
    shot_grid = touch.shot()
    touch.tap_px(x, y)
    time.sleep(MAP_DELAY)
    shot_detail = touch.shot()
    if not _moved(shot_grid, shot_detail):
        # 点了没反应：多半是模板对上了相邻/相似卡片之间的空隙，或者卡片
        # 本身不可点。继续往下只会把「进入活动」点在网格上空处。
        return False, '点了卡片但详情页没开（模板可能对错了卡片）'
    touch.tap(*p['enterActivity'])
    time.sleep(MAP_DELAY)
    shot_map = touch.shot()
    if not _moved(shot_detail, shot_map):
        # 详情页开了但「进入活动」没点中：有的活动详情按钮位置不一样，
        # 有的（纯剧情活动）压根没有这个按钮。
        return False, '详情页开了，但点「进入活动」画面没变（该活动可能没有战斗关）'

    # 有的活动（实测去咧嘴谷 TG）「进入活动」之后会**自动播剧情回放**，
    # 幕数多还可能循环重播。当场连续跳幕——跳完落在活动页（含关卡入口）
    # 就算成功；跳不干净（循环重播）只能重启 App，但重启后回不到这张地图
    # 了，必须如实报失败，不能假装导航成功。
    if in_story_dialog(touch):
        log('进入活动后自动播剧情回放，连续跳幕')
        if _skip_story_replay(touch, log) == 'stuck':
            log('剧情回放循环重播，跳不干净，重启 App 回主界面')
            touch.restart_app(log)
            return False, ('进入该活动后自动循环播剧情回放，跳过也退不出去，'
                           '重启后也回不到关卡地图；建议人工进一次把剧情看完，'
                           '之后自动导航就能过了')
        if really_home():
            # 剧情跳完了，但落在主界面而不是活动地图：目标页丢了，如实报失败
            return False, '跳过剧情后落在了主界面，不在活动关卡地图'


    # 要的是 EX 关：进地图后点右下角那个圆按钮切过去（点一下切换形态，
    # 再点一下切回普通关，所以只点一次，别写循环）
    if is_extra(stage_code):
        nx, ny = ex_point(stage_code)
        log('切 EX 关：点右下角 EX 按钮')
        touch.tap(nx, ny)
        time.sleep(MAP_DELAY)
        if not _moved(shot_map, touch.shot()):
            return False, ('EX 按钮没反应（这个活动的 EX 切换点可能不在右下角，'
                           '往 config/nav.json 的 exSwitchByEvent 写一个）')
        return True, '已进入该活动的 EX 关卡地图'

    return True, '已进入该活动的关卡地图'




# ---------------------------------------------------------------- 主流程
def goto_stage_list(touch, stage_code, log=None):
    """点几下，把界面带到 stage_code 所在的关卡列表

    返回 ``(ok, 说明)``。主线关 / 找不到区域时返回 ``(False, 原因)``，调用方
    按「不导航，直接让 Copilot 自己试」处理——总比乱点强。

    每一步都看状态再决定，同一段代码要能从主界面、活动主题页、活动地图三种
    起点都收敛到同一个结果。
    """
    log = log or (lambda *_: None)

    # 兜底：调用方可能塞进来「活动 id」（act1d3d3）或光秃秃的前缀（CW）。
    # 下面 event_of / zone_rows_of 都认不出活动 id——zone_rows_of 还会兜底成
    # 「第 1 区」，于是走进当期活动分支去点活动大图，必然报「没进到活动地图」
    # （2026-11-17 用户实测：导航愚人号时模型把 act1d3d3 当 stage 传下来）。
    stage_code = str(stage_code or '').strip()
    if not _STAGE_CODE_RE.match(stage_code):
        m = re.fullmatch(r'([A-Za-z][A-Za-z0-9]*)', stage_code)
        if m and m.group(1).upper() in EVENTS:
            # 「CW」「MN」这种纯前缀：补个 -1 就是该活动的第 1 关
            stage_code = m.group(1).upper() + '-1'
            log('「%s」按活动前缀处理 → %s' % (m.group(1), stage_code))
        else:
            return False, ('「%s」不是关卡代号（要像 CW-1 / MN-EX-1 这样）；'
                           '先换成真实关卡代号再导航' % stage_code)

    # 有卡片模板、或者代号在活动总表（EVENTS）里，就先走「曲谱」：这是显式
    # 配置过的意图。**两道闸都要在**——打包环境里模板目录偶尔解析不到
    # （PYZ 的 __file__ 指向不存在的 .pyc），只看模板会把 MN-7 错当成当期
    # 活动去点区域列表（2026-11-17 打包版实测）；反过来只看 EVENTS 又会让
    # 用户自己加的模板活动落空。不能靠 ``zone_rows_of`` 判断——它对没收录的
    # 代号一律兜底成「第 1 区」，会把老故事集错当成当期活动。
    _code, _name = event_of(stage_code)
    if tpl_path_of(stage_code) or _code:
        return goto_story_activity(touch, stage_code, log)

    idx = zone_rows_of(stage_code)
    if idx is None:
        return False, '「%s」不是活动关，也不需要这段导航' % stage_code

    p = points()
    rows = p['zoneRows']
    row = rows[idx] if idx < len(rows) else rows[-1]
    log('导航到「%s」：活动第 %d 个区域' % (stage_code, idx + 1))

    # 0) 已经在活动地图就别动了：活动地图是全屏的，底部那条不是 tab 栏，
    #    点它反而会把界面带跑
    state = probe(touch)

    # 误入活动商店：先退出去（左上返回）。商店里乱点有误消费风险，必须挡住
    if state == 'store':
        log('当前在活动商店，先退出来')
        touch.tap(*p['backCorner'])
        time.sleep(STEP_DELAY)
        state = probe(touch)

    if state == 'theme':
        # 主题页：一步到活动地图
        touch.tap(*p['themeZone'])
        time.sleep(MAP_DELAY)
        state = probe(touch)
    elif state != 'map':
        # 主界面（或其它页）：先点 tab 归位，再点活动大图
        touch.tap(*p['tabTerminal'])
        time.sleep(STEP_DELAY)
        state = probe(touch)

        # 记忆位置会让大图有时候直接进地图，所以最多试三次，进了就停
        for _ in range(3):
            if state == 'map':
                break
            touch.tap(*p['activityCard'])
            time.sleep(MAP_DELAY)
            state = probe(touch)
            if state == 'store':
                log('点到活动商店了，退出来重试')
                touch.tap(*p['backCorner'])
                time.sleep(STEP_DELAY)
                state = probe(touch)

        # 还停在主题页：点当期区域名进活动地图
        if state == 'theme':
            touch.tap(*p['themeZone'])
            time.sleep(MAP_DELAY)
            state = probe(touch)

    if state != 'map':
        return False, '没进到活动地图（活动可能已结束，或入口挪位置了）'

    # 点区域列表第 N 行。在活动地图上是切区域；万一判断有偏差，这一下
    # 多半落在空白处，不会把界面搞乱。
    touch.tap(*row)
    time.sleep(MAP_DELAY)
    return True, '已到第 %d 个区域的关卡列表' % (idx + 1)


def dump(touch, path):
    """把当前画面存成 png（导航点空了的时候看这个，比猜快）"""
    try:
        data = touch.shot()
        if not data:
            return False
        with open(path, 'wb') as f:
            f.write(data)
        return True
    except Exception:
        return False
