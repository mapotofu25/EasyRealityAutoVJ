# -*- coding: utf-8 -*-
"""定位可用的解码/转换路径：DXV-YUV 帧怎么才能变成图像？
只读。测试：
  A. cv2.VideoCapture 能否直接读 DXV-YUV；
  B. fr.planes 逐平面读 + cv2.cvtColor 手工组 BGRA；
  C. reformat 显式参数；
  D. 合成一个普通 yuv420p 视频，看 sws 到底能不能转换（排除"DXV 特有"）。
"""
import os
import sys
import numpy as np

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, SRC)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2                                                         # noqa: E402
import av                                                          # noqa: E402

LIB = r"Y:\EasyRealityAutoVJ\素材库"
BAD = ["dusk oct ring.mov", "4 (2).mov", "30.mov"]


def find(name):
    for root, _d, files in os.walk(LIB):
        for f in files:
            if f.lower() == name.lower():
                return os.path.join(root, f)
    return None


print("### A. cv2.VideoCapture 直接读 DXV-YUV")
for name in BAD:
    path = find(name)
    cap = cv2.VideoCapture(path)
    ok = cap.isOpened()
    got = None
    if ok:
        r, fr = cap.read()
        got = (r, None if fr is None else fr.shape)
    print("  %-20s 打开=%s read=%s" % (name, ok, got))
    cap.release()

print("### B. fr.planes 手工读平面 + cv2 组 BGRA")
for name in BAD:
    path = find(name)
    c = av.open(path, metadata_errors="replace")
    st = c.streams.video[0]
    try:
        fr = next(c.decode(video=0))
    except Exception as e:                                          # noqa: BLE001
        print("  %-20s decode 失败: %s" % (name, e))
        c.close()
        continue
    print("  %-20s format=%s" % (name, fr.format.name))
    try:
        planes = list(fr.planes)
        print("      planes=%d" % len(planes))
        for i, pl in enumerate(planes):
            buf = bytes(pl.buffer)
            print("        [%d] buffer=%d 字节 line_size=%s" % (i, len(buf), pl.line_size))
    except Exception as e:                                          # noqa: BLE001
        print("      planes 读取失败: %s" % e)
    # 尝试把 native ndarray 用 cv2 转
    try:
        arr = fr.to_ndarray()
        print("      native ndarray shape=%r" % (arr.shape,))
        if fr.format.name == "yuv420p":
            bgr = cv2.cvtColor(arr, cv2.COLOR_YUV2BGR_I420)
            print("      cv2 YUV2BGR_I420 → %r" % (bgr.shape,))
        elif fr.format.name == "yuva420p":
            # 手工拼 I420A：Y 全分辨，U/V 半分辨，A 全分辨
            h, w = fr.height, fr.width
            y = arr[:h, :]
            print("      Y 平面 shape=%r" % (y.shape,))
    except Exception as e:                                          # noqa: BLE001
        print("      native 失败: %s" % e)
    c.close()

print("### C. reformat 显式参数")
for name in BAD:
    path = find(name)
    c = av.open(path, metadata_errors="replace")
    try:
        fr = next(c.decode(video=0))
    except Exception as e:                                          # noqa: BLE001
        print("  %-20s decode 失败: %s" % (name, e))
        c.close()
        continue
    for desc, kw in (("explicit wh rgba", dict(format="rgba", width=fr.width, height=fr.height)),
                     ("bgr24", dict(format="bgr24"))):
        try:
            out = fr.reformat(**kw)
            print("  %-20s %-18s OK → %s" % (name, desc, out.format.name))
        except Exception as e:                                      # noqa: BLE001
            print("  %-20s %-18s 失败: %s" % (name, desc, str(e)[:60]))
    c.close()

print("### D. 合成普通 yuv420p 视频再转换（sws 是否通用可用）")
tmp = os.path.join(os.environ.get("TEMP", "."), "_sws_probe_yuv420p.mov")
try:
    w = av.open(tmp, "w")
    st = w.add_stream("rawvideo", rate=25)
    st.width, st.height, st.pix_fmt = 64, 48, "yuv420p"
    fr = av.VideoFrame(64, 48, "yuv420p")
    for pl in fr.planes:
        pl.update(bytes(pl.buffer_size))
    for pk in st.encode(fr):
        w.mux(pk)
    for pk in st.encode(None):
        w.mux(pk)
    w.close()
    c = av.open(tmp)
    f2 = next(c.decode(video=0))
    print("  合成帧 format=%s" % f2.format.name)
    for fmt in (None, "bgra", "rgb24"):
        try:
            a = f2.to_ndarray(format=fmt) if fmt else f2.to_ndarray()
            print("    →%s: OK %r" % (fmt, a.shape))
        except Exception as e:                                      # noqa: BLE001
            print("    →%s: 失败 %s" % (fmt, str(e)[:60]))
    c.close()
except Exception as e:                                              # noqa: BLE001
    print("  合成失败: %s" % e)
