# -*- coding: utf-8 -*-
"""实测 5：随机访问（seek）能力与耗时 —— 编辑器拖播放头/入出点必需。

AutoVJ 的播放器是「顺序拉帧、无 seek」；编辑器必须有 seek。
本脚本量化 PyAV 的 container.seek 与 OpenCV 的 CAP_PROP_POS_FRAMES/MPEG 定位。
"""
import time, glob, os
MP4 = sorted(glob.glob("P:/avi~/*.mp4"))[0]
MOV = None
for p in sorted(glob.glob("Y:/EasyRealityAutoVJ/素材库/*.mov")):
    if "DXV" in os.path.basename(p) or "代码" in p:
        MOV = p; break
MOV = MOV or sorted(glob.glob("Y:/EasyRealityAutoVJ/素材库/*.mov"))[0]
print("mp4:", MP4)
print("mov:", MOV)
print("=" * 78)

import av, numpy as np

def pyav_seek_test(path, n=8):
    ts = []
    c = av.open(path, metadata_errors="replace")
    st = c.streams.video[0]
    dur = float(st.duration * st.time_base) if st.duration else 3.0
    fps = float(st.average_rate or 30)
    for i in range(n):
        t = dur * (i + 0.5) / n
        t0 = time.perf_counter()
        # 时间基 seek
        try:
            c.seek(int(t / float(st.time_base)), stream=st, backward=True)
        except Exception as e:
            ts.append(("seek_err", str(e)[:30])); continue
        fr = None
        for f in c.decode(video=0):
            fr = f; break
        dt = (time.perf_counter() - t0) * 1000
        ts.append(round(dt, 1))
    c.close()
    return ts

def pyav_seek_anyframe(path, n=8):
    ts = []
    c = av.open(path, metadata_errors="replace")
    st = c.streams.video[0]
    dur = float(st.duration * st.time_base) if st.duration else 3.0
    for i in range(n):
        t = dur * (i + 0.5) / n
        from fractions import Fraction
        pts = int(Fraction(t) / st.time_base)
        t0 = time.perf_counter()
        try:
            c.seek(pts, stream=st, backward=True, any_frame=True)
        except Exception:
            try:
                c.seek(pts, stream=st, backward=True)
            except Exception as e:
                ts.append("err"); continue
        for f in c.decode(video=0):
            break
        ts.append(round((time.perf_counter() - t0) * 1000, 1))
    c.close()
    return ts

def cv_seek_test(path, n=8):
    import cv2
    ts = []
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return ["open_fail"]
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    for i in range(n):
        fidx = int(total * (i + 0.5) / n)
        t0 = time.perf_counter()
        cap.set(cv2.CAP_PROP_POS_FRAMES, fidx)
        ok, f = cap.read()
        ts.append(round((time.perf_counter() - t0) * 1000, 1) if ok else "read_fail")
    cap.release()
    return ts

for name, p in (("mp4", MP4), ("mov", MOV)):
    print(f"--- {name} ---")
    try:
        print("  PyAV seek(backward)        ms:", pyav_seek_test(p))
    except Exception as e:
        print("  PyAV seek FAIL:", type(e).__name__, e)
    try:
        print("  PyAV seek(any_frame=True)  ms:", pyav_seek_anyframe(p))
    except Exception as e:
        print("  PyAV any_frame FAIL:", type(e).__name__, e)
    try:
        print("  OpenCV CAP_PROP_POS_FRAMES ms:", cv_seek_test(p))
    except Exception as e:
        print("  OpenCV seek FAIL:", type(e).__name__, e)

print("=" * 78)
print("连续解 30 帧耗时（预览合成输入预算参考）")
c = av.open(MP4, metadata_errors="replace")
st = c.streams.video[0]
t0 = time.perf_counter(); k = 0
for f in c.decode(video=0):
    arr = f.to_ndarray(format="rgb24"); k += 1
    if k >= 30: break
dt = time.perf_counter() - t0
print(f"  PyAV 解 {k} 帧 {arr.shape} 用时 {dt*1000:.0f}ms → {k/dt:.1f} fps（纯解码）")
c.close()
