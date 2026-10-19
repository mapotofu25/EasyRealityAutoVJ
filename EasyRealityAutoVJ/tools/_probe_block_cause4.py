# -*- coding: utf-8 -*-
"""机制探针 v5：把 `_soft_frames` 的循环体**逐行内联**并打标记，定位到底卡在哪一步。
同时与 v3 的"直接 for c.decode()"做同进程 A/B（各用独立容器）。
"""
import os
import sys
import threading
import time
import traceback

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, SRC)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage                                   # noqa: E402
import av                                                          # noqa: E402
import media_manager as mm                                          # noqa: E402

LIB = r"Y:\EasyRealityAutoVJ\素材库"
NAME = sys.argv[1] if len(sys.argv) > 1 else "dusk oct ring.mov"


def find(name):
    for root, _d, files in os.walk(LIB):
        for f in files:
            if f.lower() == name.lower():
                return os.path.join(root, f)
    return None


def step(tag, path, mode, out):
    """mode='plain' = 直接 for decode；mode='inline' = 复刻 _soft_frames 循环体。"""
    p = mm.GpuDxvPlayer.__new__(mm.GpuDxvPlayer)
    p._av = av
    p.path = path
    p.fps = 30.0
    p._src_w = 0
    p._src_h = 0
    p.gpu = False
    p.gpu_want = False
    p._fmt = 0
    p.reason = ""
    p.out_alpha = True
    p._qfmt = QImage.Format_ARGB32
    p._max_w = 2880
    p._max_h = 1620
    out["%s.stage" % tag] = "open…"
    p.c, p.st = p._open()
    out["%s.stage" % tag] = "open done reason=%r" % p.reason
    if mode == "plain":
        n = 0
        for _fr in p.c.decode(video=0):
            n += 1
            if n >= 5:
                break
        out["%s.stage" % tag] = "plain decoded %d" % n
        return
    # inline：复刻 _soft_frames 循环体，逐步打标记
    out["%s.stage" % tag] = "about decode"
    it = p.c.decode(video=0)
    out["%s.stage" % tag] = "got iterator"
    fr = next(it)
    out["%s.stage" % tag] = "got frame %r" % (fr,)
    arr = fr.to_ndarray(format="bgra")
    out["%s.stage" % tag] = "to_ndarray %r" % (getattr(arr, "shape", None),)
    h, w = arr.shape[:2]
    if p._max_w and p._max_h:
        s = min(p._max_w / w, p._max_h / h)
        if s < 1.0:
            import cv2
            arr = cv2.resize(arr, (max(1, int(w * s)), max(1, int(h * s))),
                             interpolation=cv2.INTER_AREA)
            h, w = arr.shape[:2]
    img = QImage(arr.data, w, h, 4 * w, p._qfmt).copy()
    out["%s.stage" % tag] = "OK image %dx%d" % (img.width(), img.height())
    p.c.close()


def main():
    path = find(NAME)
    print("文件=%s" % path)
    for mode, tag in (("plain", "A"), ("inline", "B")):
        out = {}
        th = threading.Thread(target=step, args=(tag, path, mode, out),
                              daemon=True, name="probe-" + tag)
        th.start()
        th.join(5.0)
        print("  [%s/%s] %s  卡住=%s" % (mode, tag, out.get("%s.stage" % tag), th.is_alive()))
        if th.is_alive():
            for line in traceback.format_stack(sys._current_frames()[th.ident])[-5:]:
                sys.stdout.write("".join("      " + x for x in [line]))


if __name__ == "__main__":
    main()
