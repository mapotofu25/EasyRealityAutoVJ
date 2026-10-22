# -*- coding: utf-8 -*-
"""监视**已经在运行的** exe 进程（不启动新实例、不干扰它）

用途：判断「GL 上下文泄漏是否修好」这类问题 —— 关键看**线程/内存是否随时间增长**。
安全性：只做只读系统调用（GetProcessTimes / CreateToolhelp32Snapshot /
GetProcessMemoryInfo / GetProcessHandleCount），不改目标进程任何状态。

⚠ 采样自身的开销算在**本脚本**进程里，不影响目标进程读数。

用法：venv\\Scripts\\python.exe tools\\_proc_watch.py [秒数=90] [采样间隔=5]
"""
import ctypes
import ctypes.wintypes as wt
import os
import re
import subprocess
import sys
import time

EXE = "EasyRealityAutoVJ.exe"
k32 = ctypes.windll.kernel32
psapi = ctypes.WinDLL("psapi")
k32.GetCurrentProcess.restype = wt.HANDLE


class TE(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
                ("th32ThreadID", ctypes.c_ulong), ("th32OwnerProcessID", ctypes.c_ulong),
                ("tpBasePri", ctypes.c_long), ("tpDeltaPri", ctypes.c_long), ("dwFlags", ctypes.c_ulong)]


class PMC(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


class FT(ctypes.Structure):
    _fields_ = [("lo", ctypes.c_ulong), ("hi", ctypes.c_ulong)]


def find_pid():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq %s" % EXE], capture_output=True,
                         text=True, encoding="gbk", errors="replace").stdout
    pids = re.findall(r"%s\s+(\d+)" % re.escape(EXE), out)
    return int(pids[0]) if pids else None


def proc_cpu_sec(h):
    c, e, k, u = FT(), FT(), FT(), FT()
    if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
        return 0.0
    return ((k.hi << 32) | k.lo) / 1e7 + ((u.hi << 32) | u.lo) / 1e7


def read(pid):
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return None
    try:
        s = k32.CreateToolhelp32Snapshot(0x00000004, 0)
        n = 0
        te = TE()
        te.dwSize = ctypes.sizeof(TE)
        if k32.Thread32First(s, ctypes.byref(te)):
            while True:
                if te.th32OwnerProcessID == pid:
                    n += 1
                if not k32.Thread32Next(s, ctypes.byref(te)):
                    break
        k32.CloseHandle(s)
        p = PMC()
        p.cb = ctypes.sizeof(PMC)
        psapi.GetProcessMemoryInfo(h, ctypes.byref(p), p.cb)
        hc = ctypes.c_ulong(0)
        k32.GetProcessHandleCount(h, ctypes.byref(hc))
        return n, hc.value, p.WorkingSetSize / 1048576.0, p.PagefileUsage / 1048576.0, proc_cpu_sec(h)
    finally:
        k32.CloseHandle(h)


OUT = []


def w(s=""):
    print(s, flush=True)
    OUT.append(str(s))


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 90.0
    iv = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0

    pid = find_pid()
    if pid is None:
        w("★ %s 没在跑" % EXE)
        return
    w("=" * 88)
    w("监视运行中的 %s（PID %d）   总时长 %.0f 秒，每 %.0f 秒采样一次   %s"
      % (EXE, pid, secs, iv, time.strftime("%H:%M:%S")))
    w("=" * 88)

    r0 = read(pid)
    if r0 is None:
        w("打不开进程（权限？）")
        return
    w("  t=  0s   线程 %4d   句柄 %5d   工作集 %6.0f MB   提交 %6.0f MB   CPU(累计) %7.2f s"
      % (r0[0], r0[1], r0[2], r0[3], r0[4]))

    t_end = time.perf_counter() + secs
    prev_t, prev_c = 0.0, r0[4]
    rows = []
    while time.perf_counter() < t_end:
        time.sleep(iv)
        el = secs - (t_end - time.perf_counter())
        r = read(pid)
        if r is None:
            w("  进程已退出")
            break
        inst = (r[4] - prev_c) / max(el - prev_t, 1e-6)
        avg = (r[4] - r0[4]) / max(el, 1e-6)
        w("  t=%3.0fs   线程 %4d (%+d)   句柄 %5d (%+d)   工作集 %6.0f MB   提交 %6.0f MB   "
          "瞬时 %5.2f 核   平均 %5.2f 核"
          % (el, r[0], r[0] - r0[0], r[1], r[1] - r0[1], r[2], r[3], inst, avg))
        rows.append((el, r, inst, avg))
        prev_t, prev_c = el, r[4]

    if rows:
        w("")
        last = rows[-1][1]
        w("  ── 结论 ──")
        dt = last[0] - r0[0]
        dcm = last[3] - r0[3]
        w("  线程 %+d 个 / %.0f 秒（%.2f 个/秒）" % (dt, rows[-1][0], dt / rows[-1][0]))
        w("  提交内存 %+.0f MB / %.0f 秒（%.2f MB/秒）" % (dcm, rows[-1][0], dcm / rows[-1][0]))
        if dt <= 3 and dcm < 20:
            w("  ✓ **稳定**：线程与内存都没有增长趋势 —— 没有泄漏")
        else:
            w("  ⚠ 有增长趋势，需要继续观察（若持续线性增长则是泄漏）")
        w("  平均 CPU %.2f 核（= 整机 %.1f%%，按 16 核算）"
          % (rows[-1][3], rows[-1][3] / 16 * 100))
        w("  ⚠ 采样含「切素材」造成的正常波动；判断泄漏看**趋势**不看单点")

    w("")
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                           "_proc_watch.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))
    w("→ 已写入 _proc_watch.txt")


if __name__ == "__main__":
    main()
