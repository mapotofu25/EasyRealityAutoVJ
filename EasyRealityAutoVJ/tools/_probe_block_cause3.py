# -*- coding: utf-8 -*-
"""机制探针 v4：精确复刻 `_loop` 的取帧方式 `next(self._soft_frames())`。

与 v3 的唯一差别：解码走**生成器** `_soft_frames()`，而不是直接 `c.decode()`。
若这里也阻塞 ⇒ 症结在生成器路径（`to_ndarray` / `yield`），而非"demux 污染"。
"""
import os
import sys
import threading
import time
import traceback
import _thread

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


def main():
    path = find(NAME)
    print("文件=%s" % path)
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
    p.speed = 1.0
    p._dead = False
    p._active = True
    p._warm = 0

    res = {"line": "?", "n": 0}

    def job():
        p.c, p.st = p._open()
        print("  open 之后 reason=%r thread_count=%s"
              % (p.reason, getattr(p.st.codec_context, "thread_count", "?")))
        gen = p._soft_frames()
        n = 0
        while n < 5:
            img = next(gen)
            if img is not None:
                n += 1
                print("  第 %d 帧 %dx%d" % (n, img.width(), img.height()))
        res["n"] = n

    th = threading.Thread(target=job, daemon=True, name="probe-soft")
    th.start()
    th.join(5.0)
    if th.is_alive():
        print("  **卡住 —— 栈：")
        for line in traceback.format_stack(sys._current_frames()[th.ident])[-8:]:
            sys.stdout.write("".join("      " + x for x in [line]))
    else:
        print("  结果：%d 帧" % res["n"])
    try:
        p.c.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
