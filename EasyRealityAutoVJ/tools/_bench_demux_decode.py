# -*- coding: utf-8 -*-
"""钉死：`_try_gpu()` 先用 `c.demux()` 消费了首包，之后 `_soft_frames()` 的
`c.decode(video=0)` 是否因此取不到帧？用"同容器 vs 全新容器"对照。

每步都带硬超时，绝不把整个探针卡死。
"""
import os
import sys
import threading
import time

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, SRC)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import av                             # noqa: E402
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


def count_decode(c, secs=3.0, label=""):
    """在后台线程里数容器能解出几帧（硬超时）。"""
    res = {"n": 0, "err": "", "done": False}

    def job():
        try:
            for fr in c.decode(video=0):
                res["n"] += 1
                if res["n"] >= 5:
                    break
        except Exception as e:                                        # noqa: BLE001
            res["err"] = "%s: %s" % (type(e).__name__, str(e)[:100])
        res["done"] = True

    t = threading.Thread(target=job, daemon=True)
    t.start()
    t.join(secs)
    print("    %s → 解出 %d 帧  完成=%s  异常=%s%s"
          % (label, res["n"], res["done"], res["err"],
             "" if res["done"] else "   **超时未返回（卡住）**"))
    return res["n"]


def run(name):
    path = find(name)
    print("=" * 78)
    print(os.path.basename(path))
    try:
        import dxvnative
        print("  dxvnative.available() =", dxvnative.available(),
              getattr(dxvnative, "LAST_ERROR", ""))
    except Exception as e:                                            # noqa: BLE001
        print("  dxvnative 导入失败:", e)

    # A：模拟播放器真实流程（_open 里 _try_gpu 已经 demux 消费了首包）
    p = mm.GpuDxvPlayer(path, av)
    c, st = p._open()
    print("  [_open 之后] gpu=%s gpu_want=%s fmt=%s reason=%r"
          % (p.gpu, p.gpu_want, p._fmt, p.reason))
    count_decode(c, 3.0, "A. 同一容器 decode（首包已被 _try_gpu 消费）")
    try:
        p.close()
    except Exception:                                                 # noqa: BLE001
        pass

    # B：对照 —— 全新容器、不消费首包
    c2 = av.open(path, metadata_errors="replace")
    count_decode(c2, 3.0, "B. 全新容器 decode（未消费首包）")
    try:
        c2.close()
    except Exception:                                                 # noqa: BLE001
        pass

    # C：先 demux 一包、再 decode（在全新容器上手动复现 A 的顺序）
    c3 = av.open(path, metadata_errors="replace")
    st3 = c3.streams.video[0]
    pk = None
    for pk2 in c3.demux(st3):
        if pk2.size:
            pk = bytes(pk2)
            break
    print("    C. 手动 demux 首包 %s 字节后再 decode：" % (len(pk) if pk else None))
    count_decode(c3, 3.0, "   C")
    try:
        c3.close()
    except Exception:                                                 # noqa: BLE001
        pass


for n in (BAD, GOOD):
    if find(n):
        run(n)
