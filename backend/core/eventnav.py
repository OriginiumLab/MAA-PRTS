# -*- coding: utf-8 -*-
"""当期活动导航（新活动那套入口 UI）

为什么单独写一段（2026-11-19 用户逐屏标注的步骤）：当期活动的入口已经不是
``stagenav.goto_stage_list`` 里那条「主界面活动大图 → 主题页 → 右下区域列表」
了。新活动的入口在**终端页的活动提示条**上，进去是散点式活动地图，分区靠
关卡列表右下角那排「区域名」按钮切换：

    任意界面 --底部第1个tab「终端」--> 主界面
    主界面   --固定位置点「终端」大卡---> 终端页（有活动提示条 + 黄钻石进度）
    终端页   --活动提示条（区域+图像识别）-> 活动地图（约 5 秒过场动画）
    活动地图 --区域节点（残照终尽）------> 普通关卡界面（基础区）
    关卡列表 --右下区域按钮（长冬初青）--> EX 关卡界面
    关卡界面 --右下区域按钮（世纪之春）--> S 关卡界面

到关卡列表就不用管了：Copilot 的 ``ClickStageName`` 会 OCR 认出 ``TL-EX-1``
点进去开打（``stagenav`` 顶部注释里写过这条）。

**为什么模板识别 + 坐标兜底**：这排区域按钮在不同界面里的高度不一样（用户
2026-11-19 的截图里 长冬初青 在 1280 口径 y≈573，世纪之春 在 y≈662，差 90px），
活动换版整排还会挪，所以坐标只当兜底，主判据是模板匹配（用户给的红框就是
「区域 + 图像识别」）。识别不到就按标定坐标点，并在日志里说清楚是兜底。

**数据在 ``ENTRIES`` 里，一条活动一项，老的别删**：活动会复刻，复刻活动 UI
不变、模板还能用（只是分区可能重排，所以日志里会提示过期）。``until`` 是
活动结束日，过期只提示不失效。

模板图片放 ``backend/core/navtpl/events/``（用户配置目录
``config/navtpl/events/`` 同名文件优先，升级不覆盖）。加一条新活动：

1. 用户给几张带红框的截图 + 步骤；
2. 从截图里把按钮抠出来存成 ``<前缀>_z1/z2/z3.png``（1920 口径原样抠，红框
   往里缩 6~8px，别把红线抠进去）；
3. 在 ``ENTRIES`` 里加一项：``until`` 写活动结束日，``zones`` 按基础 / EX / S
   写 ``tpl`` + 兜底 ``tap`` + 识别区域 ``roi``；
4. 跑 ``python tools/zone_calib.py`` 之外不需要任何标定——模板就是标定。
"""
from __future__ import annotations

import datetime
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
        'until': '2026-11-25',          # 用户 2026-11-19 给的结束日
        'wrote': '2026-11-19',
        'entry': {                      # 终端页上的活动提示条（黄钻石进度行）
            'tpl': '_entry_track.png',
            'roi': [80, 500, 300, 130],
            'tap': [218, 551],
            # 认出来的是提示条下半截的黄钻石行，真正要点的是上面那行文字
            # （用户红框画在文字上），所以往上挪 51 再点
            'offset': [0, -51],
            'wait': 5.0,                # 点完有过场动画，用户实测 5 秒
        },
        'zones': {
            'base': {'tpl': 'TL_z1.png', 'label': '残照终尽',
                     'roi': [380, 540, 320, 180], 'tap': [532, 628]},
            'ex': {'tpl': 'TL_z2.png', 'label': '长冬初青',
                   'roi': [960, 495, 320, 135], 'tap': [1110, 547]},
            's': {'tpl': 'TL_z3.png', 'label': '世纪之春',
                  'roi': [960, 630, 320, 90], 'tap': [1141, 662]},
        },
    },
}

#: 用户说「最新活动 / 当期活动」时按这些词认（助手不一定知道活动代号）
ALIASES = ('最新活动', '当期活动', '当前活动', '新活动', '刚开的活动',
           '正在进行的活动', '当期关卡', '最新关卡')

# 模板相似度阈值：模板就是从当前 UI 上抠的，真命中 0~6 分，误命中 15 以上
_MAX_SCORE = 12.0


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
                    'zones': sorted((ent.get('zones') or {}).keys())})
    out.sort(key=lambda r: r['until'])
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


def run(touch, stage_code, log=None):
    """把界面带到某关所在的分区关卡列表 → ``(ok, 说明)``"""
    log = log or (lambda *_: None)
    pre = prefix_of(stage_code)
    ent = ENTRIES.get(pre)
    if not ent:
        return False, '「%s」没有当期活动导航' % (pre or stage_code)
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

    p = stagenav.points()
    entry = ent.get('entry') or {}
    data = touch.shot()

    # ---- 1) 到「终端页」：有活动提示条就说明已经在了 ----
    if not _find(data, entry)[0]:
        for i in range(2):
            # 固定位置点「终端」大卡，**不认字**：换皮肤会把「终端」两个字
            # 盖掉，但这块按钮区域一直在（用户 2026-11-19 明确要求）
            log('点固定位置的「终端」大卡')
            touch.tap(*p['terminalCard'])
            time.sleep(2.0)
            data = touch.shot()
            if _find(data, entry)[0]:
                break
            log('还没看到活动提示条，点底部「终端」tab 再点一次（第 %d 次）'
                % (i + 1))
            touch.tap(*p['tabTerminal'])
            time.sleep(1.5)
            data = touch.shot()
            if _find(data, entry)[0]:
                break
        else:
            # 可能根本不在主界面（在活动地图 / 商店里点大卡没用）
            if not _to_home(touch, log):
                return False, '回不到主界面（可能卡在某个弹窗里）'
            touch.tap(*p['terminalCard'])
            time.sleep(2.0)
            data = touch.shot()
            if not _find(data, entry)[0]:
                _fail_shot(touch, pre, log)
                return False, ('终端页上没找到活动提示条（活动可能已结束，'
                               '或这版 UI 改了，要重新标定导航）')

    # ---- 2) 点活动提示条 → 活动地图（约 5 秒过场动画）----
    before = data
    _tap(touch, data, entry, log, '活动提示条')
    time.sleep(float(entry.get('wait') or 5.0))
    after = touch.shot()
    if before and after and not stagenav._moved(before, after):
        time.sleep(3.0)                 # 动画慢的时候第二帧才动
        after = touch.shot()
    if before and after and not stagenav._moved(before, after):
        _fail_shot(touch, pre, log)
        return False, '点了活动提示条画面没变（活动可能已结束了）'
    log('进活动地图了')

    # ---- 3) 活动地图 → 基础区（普通关卡界面）----
    base = (ent.get('zones') or {}).get('base')
    if not base:
        return False, '这条导航没写基础区（zones.base）'
    _tap(touch, after, base, log, '基础区「%s」' % (base.get('label') or ''))
    time.sleep(stagenav.MAP_DELAY)
    if kind == 'base':
        return True, '已到基础关卡列表（%s）' % (base.get('label') or '')

    # ---- 4/5) 关卡列表右下角的区域按钮：切 EX / S ----
    tgt = (ent.get('zones') or {}).get(kind)
    if not tgt:
        return False, '「%s」这条导航没写 %s 分区（只有 %s）' % (
            ent.get('name') or pre, kind.upper(),
            '、'.join((ent.get('zones') or {}).keys()))
    cur = touch.shot()
    _tap(touch, cur, tgt, log, '%s 分区「%s」' % (kind.upper(),
                                                tgt.get('label') or ''))
    time.sleep(stagenav.MAP_DELAY)
    if cur and not stagenav._moved(cur, touch.shot()):
        log('点了「%s」画面没变，判定本来就停在这个分区'
            % (tgt.get('label') or ''))
    return True, '已切到 %s 分区（%s）' % (kind.upper(), tgt.get('label') or '')


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
            'zones': sorted((ent.get('zones') or {}).keys()),
            'stage': '%s-1' % pre,
        })
    rows.sort(key=lambda r: (r['expired'], r['until']))
    return rows
