# -*- coding: utf-8 -*-
"""当期活动导航（新活动那套入口 UI）

为什么单独写一段（2026-11-19 用户逐屏标注的步骤）：当期活动的入口已经不是
``stagenav.goto_stage_list`` 里那条「主界面活动大图 → 主题页 → 右下区域列表」
了。新活动的入口在**终端页的活动提示条 / 活动卡片**上，进去是散点式活动地图，
分区靠地图上的节点图标切换：

    任意界面 --底部第1个tab「终端」--> 主界面
    主界面   --固定位置点「终端」大卡---> 终端页
    终端页   --左下横幅（当期活动）-----> 活动主页（约 5 秒过场动画）
    终端页   --ATTENTION 区活动卡片-----> 活动地图（已结束的旧活动走这条）
    活动主页 --节点（圣巡之旅）--------> 普通关卡界面（基础区）
    活动主页 --节点（变革之路）--------> EX 关卡界面
    活动主页 --节点（喀兰之心）--------> S 关卡界面

到关卡列表就不用管了：Copilot 的 ``ClickStageName`` 会 OCR 认出 ``OS-EX-1``
点进去开打（``stagenav`` 顶部注释里写过这条）。

**两种入口并存**（2026-11-22 用户实测）：当期活动在终端页左下角的横幅上
（``<雪山降临1101>活动已开放``）；活动结束之后横幅会被下一个活动占走，旧活动
退到 ATTENTION 区第二格卡片、状态显示「作战已结束」，点卡片照样能进地图——
但地图上关卡分区已经关了，只剩商店。所以``closed`` 的活动只保留 ``tools``。

**为什么模板识别 + 坐标兜底**：这排节点在不同界面里的高度不一样，活动换版整排
还会挪，所以坐标只当兜底，主判据是模板匹配（用户给的红框就是「区域 + 图像
识别」）。识别不到就按标定坐标点，并在日志里说清楚是兜底。

**数据在 ``ENTRIES`` 里，一条活动一项，老的别删**：活动会复刻，复刻活动 UI
不变、模板还能用（只是分区可能重排，所以日志里会提示过期）。``until`` 是
活动结束日，过期只提示不失效。

模板图片放 ``backend/core/navtpl/events/``（用户配置目录
``config/navtpl/events/`` 同名文件优先，升级不覆盖）。加一条新活动：

1. 用户给几张带红框的截图 + 步骤；
2. 从截图里把按钮抠出来存成 ``<前缀>_z1/z2/z3.png``（1920 口径，红框往里缩
   6~12px，别把红线抠进去）；
3. 在 ``ENTRIES`` 里加一项：``until`` 写活动结束日，``zones`` 按基础 / EX / S
   写 ``tpl`` + 兜底 ``tap`` + 识别区域 ``roi``；
4. 跑 ``python tools/zone_calib.py`` 之外不需要任何标定——模板就是标定。
"""
from __future__ import annotations

import datetime
import io
import json
import os
import re
import time

from . import stagenav

#: 1280×720 口径。``roi`` = [x, y, w, h]，只在这个框里认模板（同一张图上半区
#: 可能也有同名按钮，框外命中一律当没认到）；``tap`` 是识别不到时的兜底坐标。
ENTRIES = {
    'TL': {
        'name': '时间啊，向前！',
        'until': '2026-11-28',          # 关卡已结束，商店还开着（用户 2026-11-22 截图）
        'wrote': '2026-11-22',
        # 活动已结束（2026-11-22 用户口径）：关卡分区和小游戏入口都不可用了，
        # 只留商店。入口也不再是左下角的横幅——那个横幅现在归当期活动，TL 的
        # 卡片退到终端页 ATTENTION 区第二格，点它才进活动地图。
        'closed': True,
        'entry': {
            'tpl': 'TL_card.png',
            'roi': [620, 300, 260, 150],
            'tap': [753, 371],
            'wait': 5.0,
        },
        'zones': {},                    # 关卡导航关闭（活动已结束）
        # 活动地图上除了关卡分区，还有「入口节点」，点进去不是关卡列表：
        # 小游戏和活动商店。这两项由 MAA 的 Custom 任务接手，所以导航只负责
        # 把界面点到门口（见 maatools.py 顶部注释）。
        'tools': {
            # 名字是用户 2026-11-22 截图里的写法（之前标成「炮火所掩埋的」）
            'shop': {'tpl': 'TL_shop.png', 'label': '熄火所掩埋的',
                     'roi': [880, 120, 200, 130], 'tap': [975, 185],
                     'wait': 3.0},
        },
    },
    'OS': {
        'name': '雪山降临1101',
        'until': '2026-12-02',          # 截图「作战结束时间 2026/12/02 03:59」
        'wrote': '2026-11-22',
        # 复刻活动：去年打过的号今年没有首通奖励，突袭那档要问用户打不打
        # （见 agent 提示词里的「复刻活动突袭」）。前端快捷栏也靠这个标「复刻」。
        'reprint': True,
        'entry': {                      # 终端页左下角的活动横幅（<雪山降临1101>活动已开放）
            # 2026-11-22 晚重抠：模板直接从**真机 1920×1080 截图**上裁，
            # roi/tap 换算回 1280 口径。之前那份是从用户窗口截图（1176×700
            # 含标题栏）放大来的，真机上一律认不出（22 分 > 阈值 12），
            # 于是 _to_map 第一步就失败、整个导航退回老路径。
            'tpl': 'OS_entry.png',
            'roi': [100, 536, 267, 31], 'tap': [233, 551],
            'wait': 5.0,
        },
        # 点横幅进的是散点式活动主页：六个入口绕成一圈，分区和工具入口长一个样，
        # 所以每个都要单独的模板 + ROI（单看模板容易认错到隔壁去）。
        # 2026-11-22 晚：模板全部改用**真机 1920×1080 截图**重裁，roi 是那块的
        # 1280 口径包围盒（以前从窗口截图放大，真机上一律 16~20 分认不出）。
        'zones': {
            'base': {'tpl': 'OS_z1.png', 'label': '圣巡之旅',
                     'roi': [869, 513, 113, 167], 'tap': [925, 596]},
            'ex': {'tpl': 'OS_z2.png', 'label': '变革之路',
                   'roi': [992, 481, 134, 167], 'tap': [1059, 565]},
            's': {'tpl': 'OS_z3.png', 'label': '喀兰之心',
                  'roi': [1120, 410, 130, 157], 'tap': [1185, 488]},
        },
        'tools': {
            'minigame': {'tpl': 'OS_mini.png', 'label': '喀兰贸易技术研发部',
                         'roi': [1102, 122, 136, 179], 'tap': [1170, 211],
                         'wait': 3.0},
            'shop': {'tpl': 'OS_shop.png', 'label': '观测站后勤处',
                     'roi': [113, 400, 120, 67], 'tap': [172, 460],
                     'wait': 3.0},
            # 活动奖励（神谕谜题任务页）：进去后还要点「一键领取」，
            # 见 claim_reward()
            'reward': {'tpl': 'OS_reward.png', 'label': '神谕谜题',
                       'roi': [288, 507, 123, 122], 'tap': [349, 568],
                       'wait': 3.0},
        },
        'claim': {'tpl': 'OS_claim.png', 'label': '一键领取',
                  'roi': [900, 130, 300, 130], 'tap': [1146, 177],
                  'wait': 2.0},
    },
}

#: 用户说「最新活动 / 当期活动」时按这些词认（助手不一定知道活动代号）
ALIASES = ('最新活动', '当期活动', '当前活动', '新活动', '刚开的活动',
           '正在进行的活动', '当期关卡', '最新关卡')

# 模板相似度阈值：模板就是从当前 UI 上抠的，真命中 0~6 分，误命中 15 以上
_MAX_SCORE = 12.0

# 「活动提示条」走定点比对（见 _entry_here），阈值放宽一点：这条横幅每次
# 重绘的亮度/底色都有细微差别，同一位置实测 0~11 分；而别的页面上同一片
# 区域是 60 分以上，16 分足够拉开。
_ENTRY_MAX = 16.0


def _tpl_path(name):
    """模板路径：用户配置目录优先，其次内置（和 stagenav._navtpl_dirs 一致）"""
    if not name:
        return ''
    for d in stagenav._navtpl_dirs():
        p = os.path.join(d, 'events', name)
        if os.path.isfile(p):
            return p
    return ''


def _date(s):
    try:
        y, m, d = [int(x) for x in str(s).split('-')[:3]]
        return datetime.date(y, m, d)
    except Exception:
        return None


def prefix_of(stage_code):
    """``TL-EX-1`` → ``TL``"""
    m = re.match(r'^([A-Za-z][A-Za-z0-9]*)-', str(stage_code or '').strip())
    return m.group(1).upper() if m else ''


def entry_of(stage_code):
    """关卡代号 → 该活动的导航条目（没有返回 None）"""
    return ENTRIES.get(prefix_of(stage_code))


def has(stage_code):
    return entry_of(stage_code) is not None


def is_expired(prefix, today=None):
    """导航标定是不是过了有效期（只用来提示，不阻止使用）"""
    ent = ENTRIES.get(str(prefix or '').upper())
    if not ent:
        return False
    d = _date(ent.get('until'))
    if not d:
        return False
    return (today or datetime.date.today()) > d


def active(today=None):
    """有效期内的当期活动（按结束日从近到远）"""
    today = today or datetime.date.today()
    out = []
    for pre, ent in ENTRIES.items():
        d = _date(ent.get('until'))
        if d and d < today:
            continue
        out.append({'prefix': pre, 'name': ent.get('name') or '',
                    'until': ent.get('until') or '',
                    'stage': '%s-1' % pre,
                    'closed': bool(ent.get('closed')),
                    'reprint': bool(ent.get('reprint')),
                    'zones': sorted((ent.get('zones') or {}).keys())})
    # 已结束的活动（只剩商店）排后面：用户说「最新活动」时要给正在开的那期
    out.sort(key=lambda r: (bool(r.get('closed')), r['until']))
    return out


def _norm(s):
    """去掉空格/标点，方便「时间啊，向前！」和「时间啊向前」对上"""
    return re.sub(r'[\s，。！？、·「」“”"\'（）()：:；;—\-]+', '',
                  str(s or '')).lower()


def match_name(text):
    """用户说的活动 → 关卡代号（``最新活动`` → ``TL-1``）

    只认「活动名 / 代号 / 指代词」，认不出返回空串（交给 resources 兜底）。
    """
    s = str(text or '').strip()
    if not s:
        return ''
    key = _norm(s)
    for pre, ent in ENTRIES.items():
        if key == pre.lower() or key == _norm(ent.get('name')):
            return '%s-1' % pre
    if s in ALIASES or key in {_norm(a) for a in ALIASES}:
        rows = active()
        if rows:
            return rows[0]['stage']
    # 活动名带后缀（资源库里叫「时间啊，向前！ · TL 系列」）：包含即认
    if len(key) >= 3:
        for pre, ent in ENTRIES.items():
            nm = _norm(ent.get('name'))
            if nm and len(nm) >= 3 and (nm in key or key in nm):
                return '%s-1' % pre
    return ''


def _find(data, spec):
    """在截图里认模板 → ``(像素x, 像素y, 分数)``；不在 ROI 内算没认到

    **整张截图交给 ``find_card_bytes``**，别先裁 ROI 再递进去：那个函数按
    ``截图宽 / 1920`` 缩放模板，喂一张裁过的图模板缩放就全错了。
    """
    tpl = _tpl_path((spec or {}).get('tpl') or '')
    if not tpl or not data:
        return 0, 0, 999.0
    x, y, sc = stagenav.find_card_bytes(data, tpl)
    if not x or sc > _MAX_SCORE:
        return 0, 0, sc
    roi = (spec or {}).get('roi')
    if roi and len(roi) == 4:
        try:
            from PIL import Image
            import io as _io
            w, h = Image.open(_io.BytesIO(data)).size
            sx, sy = w / 1280.0, h / 720.0
            x1, y1 = roi[0] * sx, roi[1] * sy
            x2, y2 = (roi[0] + roi[2]) * sx, (roi[1] + roi[3]) * sy
            if not (x1 <= x <= x2 and y1 <= y <= y2):
                return 0, 0, sc
        except Exception:
            pass
    return x, y, sc


def _entry_here(data, entry):
    """终端页上「活动提示条」在不在 → ``(在不在, 分数, 中心像素x, 中心像素y)``

    这条横幅的位置是**钉死的**（终端页底部、tab 栏上方，活动换来换去也在这
    一格），所以按固定位置 ±8 像素比，别用整屏模板搜索。

    为什么专门写这么一段（2026-11-22 实测，雪山降临导航偶发失败）：它是一行
    400×44 的长条字，逐像素比对的「像」极其尖锐——对齐是 10 分，错开 2 像素
    就 30 分。整屏搜索要先缩到 1/4 粗扫，这种细长低对比的模板缩完笔画快没了，
    粗扫很容易锁到背景渐变的谷底上，精修只在附近几十像素里找，回不到正确位置
    （同一帧：直接定点比 10 分，整屏搜索报 22 分、坐标跑到下面别的行上）。
    结果就是日志里「点了六次终端大卡都看不到活动提示条」，白等二十多秒。

    位置口径：``roi``（1280 口径左上角）优先，没有就按 ``tap``（1280 口径中心）
    反推；两者都要乘 1.5 换到 1920 像素。
    """
    tpl_p = _tpl_path((entry or {}).get('tpl') or '')
    if not tpl_p or not data:
        return False, 999.0, 0, 0
    try:
        from PIL import Image, ImageChops, ImageStat
        im = Image.open(io.BytesIO(data)).convert('L')
        tpl = Image.open(tpl_p).convert('L')
    except Exception:
        return False, 999.0, 0, 0
    w, h = im.size
    k = w / 1920.0
    tw = max(4, int(round(tpl.width * k)))
    th = max(4, int(round(tpl.height * k)))
    try:
        t = tpl.resize((tw, th), Image.BILINEAR)
    except Exception:
        return False, 999.0, 0, 0

    roi = entry.get('roi')
    tap = entry.get('tap')
    if roi and len(roi) == 4:
        bx, by = int(roi[0] * 1.5 * k), int(roi[1] * 1.5 * k)
    elif tap and len(tap) == 2:
        bx = int((tap[0] * 1.5 - tpl.width / 2.0) * k)
        by = int((tap[1] * 1.5 - tpl.height / 2.0) * k)
    else:
        return False, 999.0, 0, 0

    best = (999.0, bx, by)
    # 步长必须是 1：这条横幅靠像素级对齐，隔一格就差十来分（实测 805 对 10 分、
    # 804 对 13 分、806 对 21 分），走 2 会正好跳过最像的那一行
    for dy in range(-6, 7):
        for dx in range(-6, 7):
            x, y = bx + dx, by + dy
            if x < 0 or y < 0 or x + tw > w or y + th > h:
                continue
            s = ImageStat.Stat(ImageChops.difference(
                im.crop((x, y, x + tw, y + th)), t)).mean[0]
            if s < best[0]:
                best = (s, x, y)
    score, x, y = best
    return (score <= _ENTRY_MAX, score,
            x + tw // 2, y + th // 2)


def _tap(touch, data, spec, log, what):
    """认模板 → 点了；认不到按兜底坐标点。返回是否靠识别点中的"""
    x, y, sc = _find(data, spec)
    if x:
        off = spec.get('offset') or [0, 0]
        try:
            from PIL import Image
            import io as _io
            w, h = Image.open(_io.BytesIO(data)).size
            sx, sy = w / 1280.0, h / 720.0
        except Exception:
            sx = sy = 1.5
        px = x + float(off[0]) * sx
        py = y + float(off[1]) * sy
        log('图像识别到%s（%.1f 分），点 (%d,%d)' % (what, sc, px, py))
        touch.tap_px(px, py)
        return True
    tap = spec.get('tap')
    if tap and len(tap) == 2:
        log('没认出%s（最像 %.0f 分），按标定坐标 (%d,%d) 兜底点一下'
            % (what, sc, tap[0], tap[1]))
        touch.tap(int(tap[0]), int(tap[1]))
    else:
        log('没认出%s（最像 %.0f 分），也没有兜底坐标' % (what, sc))
    return False


def _to_home(touch, log, rounds=5):
    """不管现在在哪一页，退回主界面（每轮先判定，确认到了才停手）"""
    for i in range(rounds):
        if (stagenav.at_home(touch) and not stagenav.in_story_dialog(touch)
                and not stagenav.on_login_page(touch)):
            return True
        touch.heal()
        if stagenav.in_story_dialog(touch):
            log('剧情 / CG 回放页，先跳幕')
            stagenav._skip_story_replay(touch, log)
        elif i < 2:
            log('不在主界面，按返回键')
            touch.back()
        elif i < 4:
            log('返回键没反应，按 ESC')
            touch.esc()
        else:
            log('点左上角「<」')
            touch.tap(*stagenav.points()['backCorner'])
        time.sleep(1.6)
    return bool(stagenav.at_home(touch))


def _fail_shot(touch, name, log):
    """失败现场存一张图（比来回问用户快）"""
    try:
        d = ''
        from .settings import CONFIG_DIR
        d = os.path.join(CONFIG_DIR, 'navfail')
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, 'event-%s-%d.png' % (name or 'x', int(time.time())))
        if stagenav.dump(touch, p):
            log('现场截图：%s' % p)
    except Exception:
        pass


def _to_terminal(touch, entry, log, rounds=6):
    """从任意一页收敛到「终端页」（活动提示条可见的那一页）

    返回 ``(截图, 提示条中心像素x, 中心像素y, 分数)``；没到位返回 ``(b'', 0, 0, 999)``。

    **为什么不直接盲点终端大卡**（2026-11-22 手测踩到的）：那个坐标是按主界面
    量的（1280 口径 913,173），只在主界面 / 终端页成立。跑完一关之后游戏停在
    **关卡列表**上，同一坐标正好压在关卡卡片上，一戳就打开关卡详情页，接着点
    底部「终端」tab 又会戳到列表底部——连跑几个号就会莫名多进一次关卡。在
    活动地图上则可能戳到奖励 / 商店节点。

    所以先判自己在哪：底部有四块功能格（``stagenav.at_home``）才算主界面系，
    可以放心点大卡；否则先 ``_to_home`` 一层层退（关卡列表 → 活动地图 →
    终端页）。返回键在**主界面**上会弹退出确认框，所以退这一步必须挂在
    at_home 判断之后，不能无条件按。
    """
    p = stagenav.points()
    homed = 0
    for i in range(rounds):
        data = touch.shot()
        ok, sc, ex, ey = _entry_here(data, entry)
        if ok:
            return data, ex, ey, sc
        if not stagenav.at_home(touch):
            # 关卡列表 / 活动地图 / 主题页 / 剧情页：交给 _to_home 一层层退回
            # 主界面（它自己会跳剧情、退化到 ESC、最后点左上角「<」）。退回去
            # 以后重新判一次——退一两层可能已经到终端页了，就不用再点大卡。
            if homed >= 2:
                break
            homed += 1
            log('不在主界面，先退回主界面')
            _to_home(touch, log)
            continue
        # 固定位置点「终端」大卡，**不认字**：换皮肤会把「终端」两个字
        # 盖掉，但这块按钮区域一直在（用户 2026-11-19 明确要求）
        log('点固定位置的「终端」大卡')
        touch.tap(*p['terminalCard'])
        time.sleep(2.0)
        data = touch.shot()
        ok, sc, ex, ey = _entry_here(data, entry)
        if ok:
            return data, ex, ey, sc
        log('还没看到活动提示条（最像 %.0f 分），点底部「终端」tab 再试（第 %d 次）'
            % (sc, i + 1))
        touch.tap(*p['tabTerminal'])
        time.sleep(1.6)
    return b'', 0, 0, 999.0


def _to_map(touch, pre, ent, log):
    """「终端页 → 点活动提示条 → 活动地图」→ ``(ok, 截图, 说明)``

    关卡导航和小游戏/商店入口都要先走这一段，所以单独拎出来：两处如果各写
    一遍，改一处忘另一处就会出现「关卡能进、商店进不去」。
    """
    entry = ent.get('entry') or {}
    data, ex, ey, sc = _to_terminal(touch, entry, log)
    if not data:
        _fail_shot(touch, pre, log)
        return False, b'', ('终端页上没找到活动提示条（最像 %.0f 分，活动可能'
                            '已结束，或这版 UI 改了，要重新标定导航）' % sc)

    # ---- 2) 点活动提示条 → 活动地图（约 5 秒过场动画）----
    # 点哪一下用固定位置比出来的中心，不再整屏搜索（见 _entry_here）
    before = data
    log('点活动提示条（%.1f 分，(%d,%d)）' % (sc, ex, ey))
    touch.tap_px(ex, ey)
    time.sleep(float(entry.get('wait') or 5.0))
    after = touch.shot()
    if before and after and not stagenav._moved(before, after):
        time.sleep(3.0)                 # 动画慢的时候第二帧才动
        after = touch.shot()
    if before and after and not stagenav._moved(before, after):
        _fail_shot(touch, pre, log)
        return False, b'', '点了活动提示条画面没变（活动可能已结束了）'
    log('进活动地图了')
    return True, after, '活动地图'


def _near_map(touch, ent, log):
    """已经在活动地图上就别再绕一圈回主界面 → 截图 or ``b''``

    为什么要这一段：连跑多关时，每关导航前游戏可能就停在活动地图上（上一关
    跑完、或者小游戏/商店那次导航点回来），照「回主界面 → 终端页 → 点活动
    提示条」那一路走要多花十来秒。认得到基础区节点就说明已经在地图上，直接用。

    另外「关卡列表」上底部也露着三个分区节点，但位置比地图上低一截、模板认
    不出；那种页面按一下返回就够了（实测回到的是**活动主题页**，不是地图——
    主题页也会被后面的 ``_to_terminal`` 接住）。这一下返回反正是回主界面的
    必经步骤，不算白按。

    只做「看一眼 / 按一下返回」这种**不会误触**的动作：认不到就老实返回
    ``b''``，交给正规路径。绝不在这里盲点坐标。
    """
    zones = ent.get('zones') or {}
    base = zones.get('base') or {}
    data = touch.shot()
    if data and base and _find(data, base)[0]:
        log('已经在活动地图上，省掉回主界面那一路')
        return data
    # 关卡列表 / 分区详情：底部有条纹横幅、认不到分区节点，返回一下就回到地图
    if data and stagenav.probe_bytes(data) == 'map':
        log('看着像关卡列表，按一下返回回活动地图')
        touch.back()
        time.sleep(1.6)
        data = touch.shot()
        if data and base and _find(data, base)[0]:
            log('回到活动地图了')
            return data
    return b''


def run(touch, stage_code, log=None):
    """把界面带到某关所在的分区关卡列表 → ``(ok, 说明)``"""
    log = log or (lambda *_: None)
    pre = prefix_of(stage_code)
    ent = ENTRIES.get(pre)
    if not ent:
        return False, '「%s」没有当期活动导航' % (pre or stage_code)
    # 活动结束后关卡入口就没了（2026-11-22 TL）：提前说清楚，别导航到地图上
    # 才发现没有分区按钮，白跑一趟还让用户以为识别坏了
    if ent.get('closed') and not (ent.get('zones') or {}):
        return False, ('「%s」已经结束了，关卡入口关了——只能进活动商店'
                       % (ent.get('name') or pre))
    kind = stagenav.zone_kind(stage_code)
    if kind not in ('base', 'ex', 's', 'mo'):
        kind = 'base'
    if is_expired(pre):
        log('注意：「%s」的导航标定于 %s（有效期到 %s），活动改过版的话'
            '可能点不到，需要重新标定'
            % (ent.get('name') or pre, ent.get('wrote') or '?',
               ent.get('until') or '?'))
    log('走当期活动导航：%s（%s）→ %s 分区'
        % (ent.get('name') or pre, pre, kind.upper()))

    after = _near_map(touch, ent, log)
    if not after:
        ok, after, why = _to_map(touch, pre, ent, log)
        if not ok:
            return False, why

    # ---- 3) 活动地图 → 目标分区 ----
    # **直接点目标那个节点**，别再绕一遍基础区（2026-11-22 用户实测报「用的还是
    # 老导航」）：OS 这种新地图上「圣巡之旅 / 变革之路 / 喀兰之心」是同一屏上的
    # 三个节点，点了基础区就进关卡列表了，EX/S 的模板根本不在那一页上——`_find`
    # 认不到就按标定坐标兜底点，那一戳可能正好点在某张关卡卡片上。
    zones = ent.get('zones') or {}
    base = zones.get('base')
    if not base:
        return False, '这条导航没写基础区（zones.base）'
    tgt = base if kind == 'base' else zones.get(kind)
    if not tgt:
        return False, '「%s」这条导航没写 %s 分区（只有 %s）' % (
            ent.get('name') or pre, kind.upper(),
            '、'.join(zones.keys()))
    before = after
    _tap(touch, after, tgt, log, '%s 分区「%s」'
         % (kind.upper() if kind != 'base' else '基础区', tgt.get('label') or ''))
    time.sleep(stagenav.MAP_DELAY)
    if before and not stagenav._moved(before, touch.shot()):
        log('点了「%s」画面没变，可能没切过去（模板过期要重新标定）'
            % (tgt.get('label') or ''))
        return False, ('点了「%s」之后画面没变，没进 %s 关卡列表'
                       % (tgt.get('label') or '', kind.upper()))
    return True, '已到 %s 关卡列表（%s）' % (
        kind.upper(), tgt.get('label') or '')


def run_tool(touch, tool, prefix=None, log=None):
    """把界面带到活动地图上的某个入口节点 → ``(ok, 说明)``

    ``tool`` 就是 ``ENTRIES[...]['tools']`` 的键（``minigame`` / ``shop``），
    :mod:`maatools` 按功能名映射过来。点完只做「画面有没有变」的软校验：
    小游戏和商店页面偏静态，用 ``_moved`` 硬判会误报，真正认页面的是后面的
    MaaCore 任务（它认不到就停在那儿，日志里看得出来）。
    """
    log = log or (lambda *_: None)
    pre = str(prefix or '').strip().upper()
    if not pre:
        rows = active()
        if not rows:
            return False, '现在没有有效期内的当期活动（导航表里都是过期的）'
        pre = rows[0]['prefix']
    ent = ENTRIES.get(pre)
    if not ent:
        return False, '「%s」没有当期活动导航' % pre
    spec = (ent.get('tools') or {}).get(str(tool or '').strip().lower())
    if not spec:
        return False, '「%s」这条导航没写 %s 入口（只有 %s）' % (
            ent.get('name') or pre, tool,
            '、'.join((ent.get('tools') or {}).keys()) or '无')
    if is_expired(pre):
        log('注意：「%s」的导航标定于 %s（有效期到 %s），活动改过版的话'
            '可能点不到，需要重新标定'
            % (ent.get('name') or pre, ent.get('wrote') or '?',
               ent.get('until') or '?'))

    data = _near_map(touch, ent, log)
    if not data:
        ok, data, why = _to_map(touch, pre, ent, log)
        if not ok:
            return False, why
    label = spec.get('label') or tool
    _tap(touch, data, spec, log, '「%s」' % label)
    time.sleep(float(spec.get('wait') or 3.0))
    after = touch.shot()
    if data and after and not stagenav._moved(data, after):
        log('点了「%s」画面几乎没变，可能还停在活动地图上' % label)
        return True, '点了「%s」，但画面没变化，MAA 可能认不出页面' % label
    log('进「%s」了' % label)
    return True, '已进入「%s」' % label


def claim_reward(touch, prefix=None, log=None):
    """活动奖励页 → 点「一键领取」→ ``(ok, 说明)``

    只有 ``tools.reward`` + ``claim`` 都写了的活动才有（现在只有 OS）：进奖励页
    后页面上有个「一键领取」，点一下把已完成的任务奖励一次性收掉。已经领完的
    时候按钮还在，点了没反应——所以这里不校验，点完就回。
    """
    log = log or (lambda *_: None)
    pre = str(prefix or '').strip().upper()
    ok, why = run_tool(touch, 'reward', prefix=pre or None, log=log)
    if not ok:
        return False, why
    ent = ENTRIES.get(pre) or (ENTRIES.get(active()[0]['prefix'])
                               if active() else {})
    spec = (ent or {}).get('claim')
    if not spec:
        return False, '这条导航没写领奖按钮（claim），进了奖励页但要手动点'
    data = touch.shot()
    x, y, sc = _find(data, spec)
    if x:
        touch.tap_px(x, y)
        log('点「%s」（%.1f 分）' % (spec.get('label') or '一键领取', sc))
    else:
        tap = spec.get('tap') or [0, 0]
        log('没认出「%s」（最像 %.0f 分），按标定坐标兜底点'
            % (spec.get('label') or '一键领取', sc))
        touch.tap(int(tap[0]), int(tap[1]))
    time.sleep(float(spec.get('wait') or 2.0))
    return True, '已点「%s」' % (spec.get('label') or '一键领取')


def describe():
    """给助手看的当期活动清单（有效期内的在前）"""
    today = datetime.date.today()
    rows = []
    for pre, ent in ENTRIES.items():
        d = _date(ent.get('until'))
        rows.append({
            'prefix': pre,
            'name': ent.get('name') or '',
            'until': ent.get('until') or '',
            'expired': bool(d and d < today),
            'closed': bool(ent.get('closed')),
            'reprint': bool(ent.get('reprint')),
            'zones': sorted((ent.get('zones') or {}).keys()),
            'tools': sorted((ent.get('tools') or {}).keys()),
            'stage': '%s-1' % pre,
        })
    rows.sort(key=lambda r: (r['expired'], r['closed'], r['until']))
    return rows
