"""活动地图「分区图标」坐标标定小工具

活动地图右下角是一排分区图标（基础关 / EX / S / MO），点哪格切哪个分区。
**哪一格是什么分区不用标**：程序按资源库 zoneId 排序算（基础 0 → EX 1 →
S/MO 2），所以只需要给出这一排图标的坐标。

    python tools/zone_calib.py 活动地图.png
    python tools/zone_calib.py 活动地图.png --pick 2,4,5 --name DM

输出候选峰（1280 口径）和一张带编号竖线的预览图 `*_zone.png`。先看预览图
确定哪几根线落在图标上，再用 `--pick` 指定编号，工具会把它们收到图标中心
并打印可直接粘进 `zoneIcons` 的一行。

原理：图标是花纹密集的徽记，逐列灰度标准差高；背景是平滑插画，接近 0。
峰落点可能在图标的一侧（菱形徽记左右两个尖各出一个峰），所以 `--pick`
之后再算一次局部质心收中。

截图要求：16:9 游戏画面。带标题栏的整窗截图（雷电/夜神常见 1920×1118）
会自动去掉顶部多出来的部分。
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from PIL import Image, ImageDraw                        # noqa: E402

from backend.core import stagenav                       # noqa: E402


def game_area(im):
    """去掉窗口标题栏，返回纯游戏画面（16:9）"""
    w, h = im.size
    want = int(round(w * 9 / 16.0))
    if h <= want:
        return im
    return im.crop((0, h - want, w, h))       # 多出来的在顶部


def refine(prof, x0, pk, half):
    """把一个峰收入局部质心（阈值取窗口内中位数），返回 1920 口径 x"""
    lo = max(0, pk - half - x0)
    hi = min(len(prof), pk + half - x0)
    seg = [(i + x0, prof[i]) for i in range(lo, hi)]
    if not seg:
        return pk
    thr = sorted(v for _, v in seg)[len(seg) // 2]
    den = sum(max(0.0, v - thr) for _, v in seg)
    if den <= 0:
        return pk
    return sum(x * max(0.0, v - thr) for x, v in seg) / den


def main():
    ap = argparse.ArgumentParser(description='活动地图分区图标坐标标定')
    ap.add_argument('shot', help='活动地图截图（png/jpg）')
    ap.add_argument('--name', default='', help='活动代号前缀（DM/MN…）')
    ap.add_argument('--y', type=int, default=608,
                    help='图标行纵向位置，1280 口径（默认 608 = 1080 口径的 950）')
    ap.add_argument('--max', type=int, default=6, help='最多列出几个候选（默认 6）')
    ap.add_argument('--pick', default='',
                    help='确认是图标的候选编号（1 起，逗号分隔），会收中并输出配置行')
    args = ap.parse_args()

    im = game_area(Image.open(args.shot).convert('RGB'))
    w, h = im.size
    gray = im.convert('L')
    peaks = [p for p in stagenav._icon_peaks_im(gray, args.y) if p > w * 0.55]
    if not peaks:
        print('没找到图标峰。确认这张图是活动地图（不是详情页/乐章收录网格），'
              '或者手动量一下 x 再调 --y')
        return 1
    peaks = peaks[-args.max:]
    _, x0, prof = stagenav._icon_profile(gray, args.y)

    def to1280(px):
        return int(round(px * 1280.0 / w))

    print('截图 %s  %dx%d（游戏区）' % (args.shot, w, h))
    print('候选（左→右，1280 口径）：')
    for i, pk in enumerate(peaks, 1):
        print('  %d) [%d, %d]' % (i, to1280(pk), args.y))

    picked = []
    if args.pick:
        for tok in args.pick.replace('，', ',').split(','):
            tok = tok.strip()
            if not tok.isdigit() or not (1 <= int(tok) <= len(peaks)):
                print('--pick 里有无效编号：%r' % tok)
                return 2
            picked.append(peaks[int(tok) - 1])
        picked.sort()
        gaps = [b - a for a, b in zip(picked, picked[1:])]
        # 窗口别跨到隔壁图标：取最小间距的 45%，最多 110px（1920 口径）。
        # 峰多半落在图标的一侧，窗口太窄会把图标另一半切掉、质心继续偏。
        half = int(min(110, min(gaps) * 0.45)) if gaps else 110
        centers = [refine(prof, x0, pk, half) for pk in picked]
        pts = [(to1280(c), args.y) for c in centers]
        print('\n收中后（左→右，1280 口径）：')
        for x, y in pts:
            print('  [%d, %d]' % (x, y))
        if args.name:
            print('\n写进 DEFAULT_POINTS["zoneIcons"]（或 config/nav.json）:')
            print('  %s: [%s]' % (args.name.upper(),
                                  ', '.join('[%d, %d]' % p for p in pts)))
        print('\n**顺序必须是「基础关 → EX → S/MO」**，数量要和该活动的分区数一致'
              '（分区数见 stagenav.zone_rows_of 的结果 +1）。')

    prev = im.copy()
    d = ImageDraw.Draw(prev)
    for i, pk in enumerate(peaks, 1):
        d.line([(pk, int(h * 0.70)), (pk, h)],
               fill=(255, 140, 0) if pk in picked else (0, 255, 0), width=3)
        d.text((pk + 4, int(h * 0.71)), str(i), fill=(0, 255, 0))
    out = os.path.join(os.path.dirname(os.path.abspath(args.shot)),
                       os.path.splitext(os.path.basename(args.shot))[0] + '_zone.png')
    prev.save(out)
    print('\n预览图：%s（绿线=候选，橙线=已选定）' % out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
