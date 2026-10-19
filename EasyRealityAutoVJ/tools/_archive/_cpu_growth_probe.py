# -*- coding: utf-8 -*-
"""CPU / 内存增长探针：跑真实 MainWindow（离屏），定时采样，看有没有"越来越高"。

采样项：进程 CPU 累计时间、RSS、线程数、QObject/QImage 计数、各定时器是否在跑、
        引擎侧几个可能增长的容器大小（_cue_items / cs_states / _postfx_trig / 日志行数）。

用法：venv/Scripts/python.exe -u tools/_cpu_growth_probe.py [秒数=120] [间隔=15]
"""
import ctypes
import ctypes.wintypes as wt
import gc
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

from PySide6.QtCore import QObject, QTimer                     # noqa: E402
from PySide6.QtGui import QImage                               # noqa: E402
from PySide6.QtWidgets import QApplication                     # noqa: E402

app = QApplication([])
import ui_main                                                 # noqa: E402


class _PMC(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


def _mem_mb():
    try:
        import os as _os
        k32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        h = k32.GetCurrentProcess()
        c = _PMC()
        c.cb = ctypes.sizeof(c)
        if psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
            return c.WorkingSetSize / 1048576.0
    except Exception:                                          # noqa: BLE001
        pass
    return 0.0


def _cpu_sec():
    """本进程累计 CPU 秒数（user+system，用 os.times，稳）。"""
    try:
        ts = os.times()
        return float(ts.user) + float(ts.system)
    except Exception:                                          # noqa: BLE001
        return 0.0


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0
    gap = float(sys.argv[2]) if len(sys.argv) > 2 else 15.0

    w = ui_main.MainWindow()
    w.show()
    app.processEvents()
    # 起采集 + 开始跑（和用户现场的条件尽量一致；采集不到设备也不影响观察增长趋势）
    try:
        w.start_audio(silent=True)
    except Exception as e:                                     # noqa: BLE001
        print("start_audio 失败（不影响趋势观察）：", type(e).__name__, e)
    try:
        w.toggle_run()
    except Exception as e:                                     # noqa: BLE001
        print("toggle_run 失败：", type(e).__name__, e)
    # 打开码表编排页（这是用户这几轮一直在用的界面）
    try:
        w.switch_page(2)
    except Exception as e:                                     # noqa: BLE001
        print("switch_page 失败：", type(e).__name__, e)

    print("=" * 104)
    print("%6s %10s %10s %8s %8s %8s %8s %8s %8s" % (
        "t(s)", "CPU%", "RSS(MB)", "线程", "QObj", "QImg", "cue项", "cs态", "日志行"))
    print("-" * 104)
    t0 = time.perf_counter()
    c0 = _cpu_sec()
    rows = []
    while time.perf_counter() - t0 < secs:
        app.processEvents()
        time.sleep(min(0.2, gap / 5))
        el = time.perf_counter() - t0
        if el < (len(rows) + 1) * gap:
            continue
        gc.collect()
        cpu = _cpu_sec() - c0
        rss = _mem_mb()
        objs = sum(1 for _ in gc.get_objects() if isinstance(_, QObject))
        imgs = sum(1 for _ in gc.get_objects() if isinstance(_, QImage))
        cue_items = 0
        cs_states = 0
        for lay in w.engine.layers:
            cue_items += len(getattr(lay, "_cue_items", None) or {})
            cs_states += len(getattr(lay, "cs_states", None) or [])
        lines = 0
        try:
            ed = w.cue_editor
            lines = ed.txt.document().blockCount() if ed is not None else 0
        except Exception:                                      # noqa: BLE001
            pass
        import threading
        rows.append((el, cpu / el * 100.0, rss, threading.active_count(),
                     objs, imgs, cue_items, cs_states, lines))
        r = rows[-1]
        print("%6.0f %10.1f %10.1f %8d %8d %8d %8d %8d %8d" % r)
    print("-" * 104)
    if len(rows) >= 3:
        a, b = rows[len(rows) // 3], rows[-1]
        print("趋势（首个采样 → 末个采样）：CPU %.1f%% → %.1f%% ｜ RSS %.0f → %.0f MB ｜ "
              "线程 %d → %d ｜ QObj %d → %d ｜ QImg %d → %d" % (
                  a[1], b[1], a[2], b[2], a[3], b[3], a[4], b[4], a[5], b[5]))
    try:
        w.toggle_run()
    except Exception:                                          # noqa: BLE001
        pass
    w.close()
    # 强制退出（后台线程是 daemon，不用等）
    os._exit(0)


if __name__ == "__main__":
    main()
