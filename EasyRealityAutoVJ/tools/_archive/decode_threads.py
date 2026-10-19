# -*- coding: utf-8 -*-
"""解码线程数 vs 墙钟 / 进程 CPU —— 找"CPU 被解码吃光"的真凶。

背景：cv2.setNumThreads() 只管 OpenCV 自己的算法（resize/cvtColor），
**管不到 VideoCapture 底层 FFmpeg 解码器的线程**。DXV 的 DXT 解压是逐块独立的，
FFmpeg 会开 slice 线程并行 —— 单帧 CPU 时间被放大，而墙钟收益很小。

用法： python tools/decode_threads.py [帧数]
"""
import os
import sys
import time
import ctypes as C
import ctypes.wintypes as wt
import glob

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
out = []


def w(s=""):
    print(s)
    out.append(s)


# ---------- 进程 CPU 时间（内核+用户） ----------
class FILETIME(C.Structure):
    _fields_ = [("lo", wt.DWORD), ("hi", wt.DWORD)]


k32 = C.WinDLL("kernel32", use_last_error=True)
k32.GetCurrentProcess.restype = wt.HANDLE
k32.GetProcessTimes.argtypes = [wt.HANDLE, C.POINTER(FILETIME), C.POINTER(FILETIME),
                                C.POINTER(FILETIME), C.POINTER(FILETIME)]


def proc_cpu_sec():
    c, e, k, u = FILETIME(), FILETIME(), FILETIME(), FILETIME()
    if not k32.GetProcessTimes(k32.GetCurrentProcess(), C.byref(c), C.byref(e),
                               C.byref(k), C.byref(u)):
        return None
    f = lambda t: (t.hi << 32) | t.lo
    return (f(k) + f(u)) / 1e7


def pick_clips():
    d = r"Y:\EasyRealityAutoVJ\素材库"
    if not os.path.isdir(d):
        return []
    fs = [f for f in sorted(os.listdir(d))
          if f.lower().endswith((".mov", ".mp4", ".mkv", ".avi"))]
    clips = []
    for f in fs:
        p = os.path.join(d, f)
        clips.append((os.path.getsize(p), p))
    clips.sort(reverse=True)
    # 挑 1 个最大的 + 1 个中等的（模拟"大素材 + 小素材"）
    pick = []
    if clips:
        pick.append(clips[0][1])
    if len(clips) > 3:
        pick.append(clips[len(clips) // 3][1])
    return pick


def run(path, nthreads, frames=60):
    import cv2
    params = [cv2.CAP_PROP_N_THREADS, nthreads] if nthreads else []
    try:
        cap = cv2.VideoCapture(path, cv2.CAP_FFMPEG, params)
        if not cap.isOpened():
            cap = cv2.VideoCapture(path)
            if nthreads:
                cap.set(cv2.CAP_PROP_N_THREADS, nthreads)
    except Exception:
        cap = cv2.VideoCapture(path)
        if nthreads:
            cap.set(cv2.CAP_PROP_N_THREADS, nthreads)
    if not cap.isOpened():
        return None
    wpx = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    hpx = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps_src = cap.get(cv2.CAP_PROP_FPS)
    for _ in range(8):            # 预热
        ok, _f = cap.read()
        if not ok:
            break
    c0 = proc_cpu_sec()
    t0 = time.perf_counter()
    n = 0
    while n < frames:
        ok, f = cap.read()
        if not ok:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, f = cap.read()
            if not ok:
                break
        n += 1
    el = time.perf_counter() - t0
    c1 = proc_cpu_sec()
    cap.release()
    if n == 0 or el <= 0:
        return None
    cpu = (c1 - c0) if (c0 is not None and c1 is not None) else 0.0
    return {"w": wpx, "h": hpx, "fps_src": fps_src, "frames": n, "el": el,
            "fps": n / el, "ms": el / n * 1000, "cpu_sec": cpu,
            "cores": cpu / el}


def main():
    frames = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    import cv2
    w("=" * 84)
    w("解码线程数 vs 墙钟 / CPU   OpenCV %s  cv2.setNumThreads()=%d  本机 %d 核"
      % (cv2.__version__, cv2.getNumThreads(), os.cpu_count()))
    w("=" * 84)
    clips = pick_clips()
    if not clips:
        w("找不到素材库（Y:\\EasyRealityAutoVJ\\素材库）")
        return
    for path in clips:
        name = os.path.basename(path)
        w("")
        w("── %s  (%.1f MB) ──" % (name, os.path.getsize(path) / 1e6))
        w("   %-10s %-14s %-9s %-9s %-9s %s"
          % ("线程数", "分辨率", "ms/帧", "fps", "CPU核数", "相对默认CPU"))
        base = None
        for N in (0, 1, 2, 4, 8, 16):
            r = run(path, N, frames)
            if r is None:
                w("   %-10s 打不开" % N)
                continue
            tag = "默认(=核数)" if N == 0 else str(N)
            if base is None:
                base = r["cores"] or 1e-9
            w("   %-10s %-14s %-9.2f %-9.1f %-9.2f %s"
              % (tag, "%dx%d" % (r["w"], r["h"]), r["ms"], r["fps"], r["cores"],
                 "%.0f%%" % (r["cores"] / base * 100) if base else "-"))
    w("")
    w("=" * 84)
    w("结论")
    w("=" * 84)
    w("· 「CPU核数」= 解码这段期间的进程 CPU 时间 ÷ 墙钟 —— 就是任务管理器看到的占用来源。")
    w("· 若限制线程数后 **ms/帧 基本不变、CPU核数明显下降** → 说明原来的并行是白耗 CPU，")
    w("  直接限制即可（零风险、不动画面）。")
    open(os.path.join(BASE, "_decode_threads.txt"), "w", encoding="utf-8").write("\n".join(out))


if __name__ == "__main__":
    main()
