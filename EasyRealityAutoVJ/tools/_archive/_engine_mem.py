# -*- coding: utf-8 -*-
"""完整引擎的内存增长复现（2026-09-26 系统卡死事件的根因定位）

现场事实：用户开着 4 图层 + NDI 输出，内存从 1.4 GB 涨到 9.7 GB（约 20 分钟），
最终整机内存耗尽 → 系统卡死 → 重启。
已排除：GPU 解码播放器本身（`tools/gpu_ctx_soak.py` 与纯播放器 90 秒测试都平稳）。

本脚本起**完整引擎**（读用户真实 config），按现场配置跑，观察：
  · 工作集 / 提交内存是否持续增长
  · 线程数、句柄数
  · Python 堆 Top 分配点（tracemalloc）

用法：venv\\Scripts\\python.exe tools\\_engine_mem.py [秒数=150] [采样间隔=15]
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys
import time
import tracemalloc

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

k32 = ctypes.windll.kernel32
psapi = ctypes.WinDLL("psapi")
k32.GetCurrentProcess.restype = wt.HANDLE
k32.GetProcessTimes.argtypes = [wt.HANDLE] + [ctypes.POINTER(ctypes.c_ulong * 2)] * 0 or None


class FT(ctypes.Structure):
    _fields_ = [("lo", ctypes.c_ulong), ("hi", ctypes.c_ulong)]


class PMC(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


k32.GetProcessTimes.argtypes = [wt.HANDLE, ctypes.POINTER(FT), ctypes.POINTER(FT),
                                ctypes.POINTER(FT), ctypes.POINTER(FT)]
_H = k32.OpenProcess(0x1000, False, os.getpid())


def mem():
    p = PMC()
    p.cb = ctypes.sizeof(PMC)
    psapi.GetProcessMemoryInfo(_H, ctypes.byref(p), p.cb)
    return p.WorkingSetSize / 1048576.0, p.PagefileUsage / 1048576.0


def threads():
    class TE(ctypes.Structure):
        _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
                    ("th32ThreadID", ctypes.c_ulong), ("th32OwnerProcessID", ctypes.c_ulong),
                    ("tpBasePri", ctypes.c_long), ("tpDeltaPri", ctypes.c_long), ("dwFlags", ctypes.c_ulong)]
    s = k32.CreateToolhelp32Snapshot(0x00000004, 0)
    n = 0
    te = TE()
    te.dwSize = ctypes.sizeof(TE)
    if k32.Thread32First(s, ctypes.byref(te)):
        while True:
            if te.th32OwnerProcessID == os.getpid():
                n += 1
            if not k32.Thread32Next(s, ctypes.byref(te)):
                break
    k32.CloseHandle(s)
    return n


OUT = []


def w(s=""):
    print(s, flush=True)
    OUT.append(str(s))


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 150.0
    iv = float(sys.argv[2]) if len(sys.argv) > 2 else 15.0

    try:
        import main  # noqa: F401  与真实运行一致（线程数限制）
    except Exception:
        pass

    from PySide6.QtWidgets import QApplication
    app = QApplication([])

    import json
    cfgp = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "config.json")
    try:
        c = json.load(open(cfgp, encoding="utf-8"))
        w("配置：render_fps=%s gpu_decode=%s ndi=%s spout=%s 输出=%sx%s 图层=%d" % (
            (c.get("perf") or {}).get("render_fps"), (c.get("perf") or {}).get("gpu_decode"),
            (c.get("output") or {}).get("ndi_enabled"), (c.get("output") or {}).get("spout_enabled"),
            (c.get("output") or {}).get("width"), (c.get("output") or {}).get("height"),
            len(c.get("layers") or [])))
    except Exception as e:                                            # noqa: BLE001
        w("读配置失败：%s" % e)

    w("=" * 90)
    w("完整引擎内存复现   %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    w("=" * 90)

    t0 = time.perf_counter()
    from ui_main import MainWindow
    win = MainWindow()
    w("MainWindow 构造 %.2fs" % (time.perf_counter() - t0))

    def pump(until):
        while time.perf_counter() < until:
            app.processEvents()
            time.sleep(0.004)

    w("预热 12 秒…（等后台补图/普查跑一跑）")
    pump(time.perf_counter() + 12)
    tracemalloc.start(20)

    win.toggle_run()
    w("已开始 VJ")
    w("")

    ws0, cm0 = mem()
    th0 = threads()
    w("基线  工作集 %.0f MB  提交 %.0f MB  线程 %d" % (ws0, cm0, th0))
    w("")
    w("=== 跑 %.0f 秒，每 %.0f 秒采样 ===  (现在 %s)" % (secs, iv, time.strftime("%H:%M:%S")))
    t = time.perf_counter()
    nxt = iv
    while time.perf_counter() - t < secs:
        app.processEvents()
        time.sleep(0.004)
        el = time.perf_counter() - t
        if el >= nxt:
            nxt += iv
            ws, cm = mem()
            w("  t=%4.0fs  工作集 %6.0f MB (%+6.0f)  提交 %6.0f MB (%+6.0f)  线程 %d (%+d)   %s"
              % (el, ws, ws - ws0, cm, cm - cm0, threads(), threads() - th0, time.strftime("%H:%M:%S")))

    ws1, cm1 = mem()
    w("")
    w("→ 这段净增：工作集 %+.0f MB  提交 %+.0f MB（%.1f MB/分钟）"
      % (ws1 - ws0, cm1 - cm0, (cm1 - cm0) / (secs / 60.0) if secs else 0))
    w("  判据：< 30 MB/分钟 算正常波动；> 100 MB/分钟 就是泄漏")
    w("")
    snap = tracemalloc.take_snapshot()
    w("=== Python 堆 Top 10 ===")
    for st in snap.statistics("lineno")[:10]:
        w("  %s" % str(st)[:130])
    w("")
    w("=== Top 3 的调用链 ===")
    for st in snap.statistics("traceback")[:3]:
        w("  --- %s" % str(st).split("\n")[0][:110])
        for ln in str(st).split("\n")[1:6]:
            w("      " + ln.strip()[:110])

    w("")
    with open(os.path.join(ROOT, "_engine_mem.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))
    w("→ 已写入 _engine_mem.txt")


if __name__ == "__main__":
    main()
