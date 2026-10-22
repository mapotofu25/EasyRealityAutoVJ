# -*- coding: utf-8 -*-
"""解码/常驻组件 CPU 归因 —— 分层测量，找出「进程 4.8 核」到底是哪些线程吃的。

不涉及 Qt 界面，纯后台线程，把每一层的进程 CPU 单独量出来。

用法： python tools/decode_attrib.py [每层秒数]
"""
import os
import sys
import time
import ctypes as C
import ctypes.wintypes as wt

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))
out = []


def w(s=""):
    print(s)
    out.append(s)


class FILETIME(C.Structure):
    _fields_ = [("lo", wt.DWORD), ("hi", wt.DWORD)]


k32 = C.WinDLL("kernel32", use_last_error=True)
k32.GetCurrentProcess.restype = wt.HANDLE
k32.GetProcessTimes.argtypes = [wt.HANDLE, C.POINTER(FILETIME), C.POINTER(FILETIME),
                                C.POINTER(FILETIME), C.POINTER(FILETIME)]
NCORE = os.cpu_count() or 4


def proc_cpu():
    c, e, k, u = FILETIME(), FILETIME(), FILETIME(), FILETIME()
    if not k32.GetProcessTimes(k32.GetCurrentProcess(), C.byref(c), C.byref(e),
                               C.byref(k), C.byref(u)):
        return None
    f = lambda t: (t.hi << 32) | t.lo
    return (f(k) + f(u)) / 1e7


def measure(sec, tag, note=""):
    """后台跑 sec 秒，量进程 CPU"""
    c0 = proc_cpu()
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < sec:
        time.sleep(0.1)
    el = time.perf_counter() - t0
    c1 = proc_cpu()
    if c0 is None or c1 is None:
        w("   %-40s 无法读取 CPU" % tag)
        return None
    cores = (c1 - c0) / el
    w("   %-40s %6.2f 核   占整机 %5.1f%%   %s" % (tag, cores, cores / NCORE * 100, note))
    return cores


def main():
    sec = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
    w("=" * 88)
    w("CPU 归因分层测量   本机 %d 核   每层 %.0f 秒%s" % (NCORE, sec, "（含空转基线）" if True else ""))
    w("=" * 88)
    w("")

    # ---------- L0 空转 ----------
    w("[L0] 纯空转（只 import numpy）")
    import numpy as np  # noqa
    base = measure(sec, "L0 空转")

    # ---------- L1 import 各库 ----------
    w("")
    w("[L1] 导入各库后空转（看有没有赖在后台的线程池）")
    import cv2
    a1 = measure(sec, "L1a +cv2（含内部池）")
    import onnxruntime as ort  # noqa
    a2 = measure(sec, "L1b +onnxruntime")
    try:
        import av  # noqa
        a3 = measure(sec, "L1c +PyAV")
    except Exception:
        a3 = None
    try:
        import soundcard as sc  # noqa
        a4 = measure(sec, "L1d +soundcard")
    except Exception:
        a4 = None
    try:
        from PySide6.QtWidgets import QApplication  # noqa
        a5 = measure(sec, "L1e +PySide6(QApplication前)")
    except Exception:
        a5 = None

    # ---------- 找素材 ----------
    d = r"Y:\EasyRealityAutoVJ\素材库"
    clips = []
    if os.path.isdir(d):
        fs = sorted(os.listdir(d))
        fs = [f for f in fs if f.lower().endswith((".mov", ".mp4", ".mkv", ".avi"))]
        # 取 4 个中等的（模拟 4 图层）
        mid = fs[len(fs) // 3: len(fs) // 3 + 4]
        clips = [os.path.join(d, f) for f in mid]
    if len(clips) < 4:
        w("素材不足，退出")
        return

    # ---------- L2 播放器（不节流 vs 节流） ----------
    from media_manager import create_video_player, MediaItem
    w("")
    w("[L2] N 个解码播放器（不带界面）")
    players = []
    for p in clips:
        it = MediaItem(p)
        pl = create_video_player(it)
        pl.set_max_size(1920, 1080)
        players.append(pl)
    w("   已创建 %d 个播放器（构造不阻塞、容器在解码线程里开）" % len(players))
    time.sleep(2.0)          # 等容器打开
    for i, pl in enumerate(players):
        w("   #%d fps=%.1f  已就绪=%s" % (i, getattr(pl, "fps", 0) or 0,
                                          getattr(pl, "_img", None) is not None))

    # 不活跃
    for pl in players:
        pl.set_active(False)
    l2a = measure(sec, "L2a 4 播放器 · 全部不活跃（挂起）")

    # 活跃（满速解码 —— 真实运行时是节流的，这里看上限）
    for pl in players:
        pl.set_active(True)
    l2b = measure(sec, "L2b 4 播放器 · 全活跃（按各自帧率节流）")

    # ---------- L3 只 keep 最新帧（模拟渲染线程取帧） ----------
    w("")
    w("[L3] 叠加「渲染线程每帧取最新帧」的代价（60fps）")
    def pull():
        t0 = time.perf_counter()
        n = 0
        while time.perf_counter() - t0 < sec:
            for pl in players:
                try:
                    pl.current()
                except Exception:
                    pass
            n += 1
            time.sleep(1 / 60.0)
        return n
    c0 = proc_cpu()
    t0 = time.perf_counter()
    n = pull()
    el = time.perf_counter() - t0
    c1 = proc_cpu()
    l3 = (c1 - c0) / el
    w("   %-40s %6.2f 核   占整机 %5.1f%%   （本线程 %d 帧）"
      % ("L3 4 播放器 + 取帧@60fps", l3, l3 / NCORE * 100, n))

    for pl in players:
        pl.set_active(False)
        pl.close()
    time.sleep(0.5)

    # ---------- 汇总 ----------
    w("")
    w("=" * 88)
    w("汇总（进程 CPU，占几个核）")
    w("=" * 88)
    rows = [("L0 空转", base), ("+cv2", a1), ("+onnxruntime", a2), ("+PyAV", a3),
            ("+soundcard", a4), ("+PySide6", a5),
            ("4 播放器(挂起)", l2a), ("4 播放器(活跃)", l2b),
            ("4 播放器+取帧60fps", l3)]
    prev = 0.0
    for name, v in rows:
        if v is None:
            continue
        w("   %-26s %6.2f 核   （比上一行 %+.2f）" % (name, v, v - prev))
        prev = v
    w("")
    w("读法：某一行的「比上一行 +」就是那一步引入的开销。")
    open(os.path.join(BASE, "_decode_attrib.txt"), "w", encoding="utf-8").write("\n".join(out))


if __name__ == "__main__":
    main()
