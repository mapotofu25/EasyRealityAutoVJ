# -*- coding: utf-8 -*-
"""钉死：DXV-YUV 素材被 _try_gpu 拒绝后，软解生成器到底发生了什么。"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import media_manager as mm            # noqa: E402

LIB = r"Y:\EasyRealityAutoVJ\素材库"
BAD = "dusk oct ring.mov"
GOOD = "17.mov"


def find(name):
    for root, _d, files in os.walk(LIB):
        for f in files:
            if f.lower() == name.lower():
                return os.path.join(root, f)
    return None


def run(name):
    path = find(name)
    print("=" * 78)
    print(os.path.basename(path))
    p = mm.GpuDxvPlayer(path, __import__("av"))
    c, st = p._open()
    print("  _open() → 容器=%s  流=%s" % (c is not None, st is not None))
    print("  gpu=%s gpu_want=%s _fmt=%s" % (p.gpu, p.gpu_want, p._fmt))
    print("  reason=%r" % (p.reason,))
    print("  软解生成器直接取帧：")
    gen = p._soft_frames()
    for i in range(6):
        t0 = time.perf_counter()
        try:
            img = next(gen)
            print("    #%d  出图 %dx%d  (%.0fms)" % (i, img.width(), img.height(),
                                                     (time.perf_counter() - t0) * 1000))
        except StopIteration:
            print("    #%d  StopIteration（生成器收工）" % i)
            break
        except Exception as e:                                        # noqa: BLE001
            print("    #%d  异常 %s: %s" % (i, type(e).__name__, str(e)[:120]))
            break
    print("  直接用容器 decode(video=0) 试 3 帧：")
    try:
        n = 0
        for fr in c.decode(video=0):
            n += 1
            print("    容器直解 #%d  %dx%d  %s" % (n, fr.width, fr.height, fr.format.name))
            if n >= 3:
                break
        if n == 0:
            print("    **容器直解一帧都没有**")
    except Exception as e:                                            # noqa: BLE001
        print("    异常 %s: %s" % (type(e).__name__, str(e)[:120]))
    try:
        p.close()
    except Exception:                                                 # noqa: BLE001
        pass


for n in (BAD, GOOD):
    if find(n):
        run(n)
