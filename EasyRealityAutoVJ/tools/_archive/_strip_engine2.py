# -*- coding: utf-8 -*-
"""摘除码表（第 1b 步）：engine.py 里最后残留的 cue 专用 crop 支持。

`crop` 参数唯一来源就是码表子层的 `ps.crop`，删掉码表后没有任何调用方传它 ⇒
连同 `paint_cover` 的源矩形分支一起删干净（否则 drawImage 的 if/else 也是死代码）。
"""
import io
import os
import sys

P = r"P:\AutoVJ\src\engine.py"
PAT = []


def rep(old, new, cnt=1):
    PAT.append((old, new, cnt))


# 1) _draw_layer 去掉 crop 形参
rep("""    def _draw_layer(self, p, lay, img, W, H, prog, tr, pulse, snap, drift_x=0.0, drift_y=0.0,
                    crop=None):
""",
    """    def _draw_layer(self, p, lay, img, W, H, prog, tr, pulse, snap,
                    drift_x=0.0, drift_y=0.0):
""")

# 2) paint_cover 去掉 crop 形参 + 裁切实现
rep("""        def paint_cover(painter, image, item, scale=1.0, dx=0.0, dy=0.0, a=1.0, crop=None):
            if image is None or image.isNull():
                return
            custom, ux, uy, us, rot = _t(item)
            iw, ih = image.width(), image.height()
            # ★ 裁切（码表 cue 的 crop = [x, y, w, h] 归一化；普通图层 crop=None）
            #   用 drawImage 的「源矩形」重载，**不做每帧 copy**。
            src = None
            if crop:
                try:
                    cw = max(1, int(round(float(crop[2]) * iw)))
                    ch = max(1, int(round(float(crop[3]) * ih)))
                    cx = max(0, min(iw - 1, int(round(float(crop[0]) * iw))))
                    cy = max(0, min(ih - 1, int(round(float(crop[1]) * ih))))
                    cw = min(cw, iw - cx)
                    ch = min(ch, ih - cy)
                    if cw > 0 and ch > 0:
                        src = QRectF(cx, cy, cw, ch)
                        iw, ih = cw, ch       # cover 的计算基准换成裁切后的尺寸
                except (TypeError, ValueError, IndexError):
                    src = None
""",
    """        def paint_cover(painter, image, item, scale=1.0, dx=0.0, dy=0.0, a=1.0):
            if image is None or image.isNull():
                return
            custom, ux, uy, us, rot = _t(item)
            iw, ih = image.width(), image.height()
""")

# 3) 两处 if src is None / else 分支 → 直接画
rep("""                if src is None:
                    painter.drawImage(_tgt, image)
                else:
                    painter.drawImage(_tgt, image, src)
""",
    """                painter.drawImage(_tgt, image)
""", 2)

# 4) 5 个调用点去掉 crop 实参
rep(", crop)", ")", 5)


def main():
    s = io.open(P, encoding="utf-8").read()
    for i, (a, b, cnt) in enumerate(PAT, 1):
        n = s.count(a)
        if n != cnt:
            print("✗ 补丁 %d：命中 %d 次（应为 %d），整批不写" % (i, n, cnt))
            print("   锚点首行：", a.splitlines()[0][:90])
            return 1
        s = s.replace(a, b)
    io.open(P, "w", encoding="utf-8", newline="\n").write(s)
    print("crop 支持已移除，engine.py 现 %d 行" % (s.count("\n") + 1))
    import re
    left = [(i + 1, l) for i, l in enumerate(s.splitlines())
            if re.search(r"crop|src is None|码表|cuesheet|_cue", l, re.I)]
    print("残留 %d 处" % len(left))
    for ln, l in left:
        print("   %5d| %s" % (ln, l.strip()[:110]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
