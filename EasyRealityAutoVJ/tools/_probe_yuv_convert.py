# -*- coding: utf-8 -*-
"""机制探针 v6：DXV-YUV 帧的像素格式与**可行的转换**，以及 `seek(0)` 后 decode 是否阻塞。

只读；对每个文件打印前几帧的 pix_fmt 和多种 to_ndarray 结果。
"""
import os
import sys
import threading
import time
import traceback

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, SRC)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import av                                                          # noqa: E402

LIB = r"Y:\EasyRealityAutoVJ\素材库"
NAMES = sys.argv[1:] or ["dusk oct ring.mov"]


def find(name):
    for root, _d, files in os.walk(LIB):
        for f in files:
            if f.lower() == name.lower():
                return os.path.join(root, f)
    return None


def try_conv(fr, fmt):
    try:
        a = fr.to_ndarray(format=fmt)
        return "OK shape=%r dtype=%s" % (getattr(a, "shape", None), getattr(a, "dtype", None))
    except Exception as e:                                          # noqa: BLE001
        return "!! %s: %s" % (type(e).__name__, str(e)[:80])


for name in NAMES:
    path = find(name)
    print("=" * 78)
    print(os.path.basename(path))
    c = av.open(path, metadata_errors="replace")
    st = c.streams.video[0]
    print("  codec=%s pix_fmt=%s %dx%d"
          % (st.codec_context.name, st.codec_context.pix_fmt,
             st.codec_context.width, st.codec_context.height))
    n = 0
    for fr in c.decode(video=0):
        n += 1
        print("  帧%d: format=%s pts=%s  native=%s"
              % (n, fr.format.name, fr.pts, try_conv(fr, None)))
        for f in ("bgra", "rgba", "rgb24", "bgr24", "argb"):
            print("       →%s: %s" % (f, try_conv(fr, f)))
        if n >= 3:
            break
    # seek(0) 后再 decode 是否阻塞？
    done = threading.Event()
    res = {"line": ""}

    def job():
        c.seek(0)
        m = 0
        try:
            for _fr in c.decode(video=0):
                m += 1
                if m >= 2:
                    break
        except Exception as e:                                      # noqa: BLE001
            res["line"] = "异常 %s" % e
        res["line"] = "seek 后 decode 出 %d 帧" % m
        done.set()

    t = threading.Thread(target=job, daemon=True)
    t.start()
    if not done.wait(3.0):
        print("  !! seek(0) 后 decode **阻塞**（栈）：")
        for line in traceback.format_stack(sys._current_frames()[t.ident])[-5:]:
            sys.stdout.write("".join("      " + x for x in [line]))
    else:
        print("  %s" % res["line"])
    try:
        c.close()
    except Exception:
        pass
