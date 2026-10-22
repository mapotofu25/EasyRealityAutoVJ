# -*- coding: utf-8 -*-
"""台架探针：直接构造这几素材的播放器，跑 7 秒，看它走哪条路 / 出不出帧 / reason。

模拟 app 的状态：这些文件的编码都是 dxv ⇒ `item._is_dxv = True`（app 里由后台普查填）。
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("AUTO_VJ_GPU", "1")

import media_manager as mm            # noqa: E402

LIB = r"Y:\EasyRealityAutoVJ\素材库"
BAD = ["dusk oct ring.mov", "4 (2).mov", "30.mov"]
GOOD = ["17.mov", "4.mov"]


def find(name):
    for root, _d, files in os.walk(LIB):
        for f in files:
            if f.lower() == name.lower():
                return os.path.join(root, f)
    return None


def run(path, tag):
    print("=" * 78)
    print("[%s] %s" % (tag, os.path.basename(path)))
    item = mm.MediaItem(path)
    item._is_dxv = True                      # app 里由后台编码普查填；这几个文件确实是 dxv
    t0 = time.perf_counter()
    p = mm.create_video_player(item)
    print("  工厂耗时 %.0fms  → 播放器类型 %s" % ((time.perf_counter() - t0) * 1000,
                                                 type(p).__name__))
    p.set_max_size(2880, 1620)
    p.set_active(True)
    last = None
    for i in range(14):
        time.sleep(0.5)
        h = p.health() if hasattr(p, "health") else {}
        img = p.current()
        f = h.get("frames")
        mark = ""
        if last is not None and f == last:
            mark = "   <<< 没涨"
        print("  t=%.1fs  类型=%s  frames=%s  ended=%s  gpu=%s  gpu_want=%s  有图=%s%s"
              % ((i + 1) * 0.5, type(p).__name__, f, h.get("ended"),
                 getattr(p, "gpu", "-"), getattr(p, "gpu_want", "-"),
                 img is not None, mark))
        print("        reason=%r" % (getattr(p, "reason", ""),))
        last = f
    try:
        p.close()
    except Exception:                                                 # noqa: BLE001
        pass


def main():
    print("gpu_decode_enabled() =", mm.gpu_decode_enabled())
    try:
        import glctx
        w = glctx.GLWorker.get()
        print("GLWorker.ensure() =", w.ensure(), " available =", w.available())
    except Exception as e:                                            # noqa: BLE001
        print("GL 探测失败:", e)
    for n in BAD:
        pp = find(n)
        if pp:
            run(pp, "解不出的")
    for n in GOOD:
        pp = find(n)
        if pp:
            run(pp, "对照-能播的")


if __name__ == "__main__":
    sys.exit(main())
