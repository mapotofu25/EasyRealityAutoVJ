# -*- coding: utf-8 -*-
"""监控正在运行的 EasyRealityAutoVJ：CPU / 内存 / 线程 / 磁盘IO / GPU（若有 nvidia-smi）。

用法：venv/Scripts/python.exe -u tools/_perf_watch.py [秒数=60] [间隔=3]
"""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

k32 = ctypes.windll.kernel32
k32.OpenProcess.restype = wt.HANDLE
k32.GetProcessTimes.argtypes = [wt.HANDLE, ctypes.POINTER(ctypes.c_ulonglong),
                                ctypes.POINTER(ctypes.c_ulonglong),
                                ctypes.POINTER(ctypes.c_ulonglong),
                                ctypes.POINTER(ctypes.c_ulonglong)]
psapi = ctypes.windll.psapi
psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_ulong]


class PMC(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


class IOC(ctypes.Structure):
    _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong)]


def pids():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq EasyRealityAutoVJ.exe",
                          "/FO", "CSV", "/NH"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stdout
    res = []
    for line in out.splitlines():
        parts = [x.strip('"') for x in line.split('","')]
        if len(parts) >= 2 and parts[0].lower().startswith("easyreality"):
            try:
                res.append(int(parts[1]))
            except Exception:                                      # noqa: BLE001
                pass
    return res


def stat(pid):
    h = k32.OpenProcess(0x0400 | 0x0010, False, pid)     # QUERY_INFORMATION | VM_READ
    if not h:
        return None
    try:
        c = ctypes.c_ulonglong()
        e = ctypes.c_ulonglong()
        k32.GetProcessTimes(h, ctypes.byref(ctypes.c_ulonglong()),
                            ctypes.byref(ctypes.c_ulonglong()),
                            ctypes.byref(c), ctypes.byref(e))
        mem = PMC()
        mem.cb = ctypes.sizeof(mem)
        psapi.GetProcessMemoryInfo(h, ctypes.byref(mem), mem.cb)
        io = IOC()
        k32.GetProcessIoCounters(h, ctypes.byref(io))
        return dict(cpu=(c.value + e.value) / 1e7,
                    mem=mem.WorkingSetSize / 1048576.0,
                    rd=io.ReadTransferCount, wr=io.WriteTransferCount)
    finally:
        k32.CloseHandle(h)


def gpu():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,power.draw",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True,
                             timeout=3).stdout.strip().splitlines()
        return out[0] if out else ""
    except Exception:                                              # noqa: BLE001
        return ""


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    gap = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
    print("=" * 100)
    print("监控 EasyRealityAutoVJ（每 %.0fs 一次，共 %.0fs）｜ GPU 列 = 使用率 0-100, 显存MB, 功耗W" % (gap, secs))
    print("%-9s %6s %9s %8s %8s %8s %8s  %s" % (
        "时刻", "实例", "CPU%", "内存MB", "读MB/s", "写MB/s", "线程", "GPU"))
    print("-" * 100)
    prev = {}
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < secs:
        time.sleep(gap)
        ps = pids()
        if not ps:
            print("!! 没找到正在运行的 EasyRealityAutoVJ.exe")
            return 1
        tot = dict(cpu=0.0, mem=0.0, rd=0.0, wr=0.0)
        for pid in ps:
            s = stat(pid)
            if not s:
                continue
            tot["cpu"] += s["cpu"]
            tot["mem"] += s["mem"]
            tot["rd"] += s["rd"]
            tot["wr"] += s["wr"]
        p = prev.get("t", None)
        d = (tot["cpu"] - prev["cpu"]) if p else 0.0
        rds = ((tot["rd"] - prev["rd"]) / 1048576.0 / gap) if p else 0.0
        wrs = ((tot["wr"] - prev["wr"]) / 1048576.0 / gap) if p else 0.0
        thr = 0
        try:
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq EasyRealityAutoVJ.exe",
                                  "/FO", "CSV", "/NH"], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace").stdout
            thr = out.count("EasyReality")     # 只作占位
        except Exception:                                          # noqa: BLE001
            pass
        print("%-9s %6d %9.1f %8.0f %8.1f %8.1f %8s  %s" % (
            time.strftime("%H:%M:%S"), len(ps), d / gap * 100.0, tot["mem"],
            rds, wrs, "-", gpu()))
        prev = dict(t=1, cpu=tot["cpu"], rd=tot["rd"], wr=tot["wr"])
    print("-" * 100)
    print("说明：CPU% = 该进程在这段时间占用的 CPU 时间 / 挂钟时间（多核可超 100%）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
