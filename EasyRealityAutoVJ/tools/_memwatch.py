# -*- coding: utf-8 -*-
"""零干扰内存监视器（2026-09-26 内存泄漏排查用）

**只做只读系统调用**，绝不 attach 目标进程、不暂停它任何线程：
  · `GetProcessMemoryInfo`  工作集 / 提交 / 峰值提交
  · `GetProcessHandleCount` 句柄数
  · `CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD)` 线程数（读快照，不影响目标）

PID 变了（用户重启了软件）会自动跟到新进程，并在日志里标注。

用法：venv\\Scripts\\python.exe tools\\_memwatch.py [总分钟=15] [间隔秒=30]
"""
import ctypes
import ctypes.wintypes as wt
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "_memwatch.txt")
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


def find_pid():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq %s" % EXE], capture_output=True,
                         text=True, encoding="gbk", errors="replace").stdout
    pids = re.findall(r"%s\s+(\d+)" % re.escape(EXE), out)
    return int(pids[0]) if pids else None


def read(pid):
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return None
    try:
        p = PMC()
        p.cb = ctypes.sizeof(PMC)
        if not psapi.GetProcessMemoryInfo(h, ctypes.byref(p), p.cb):
            return None
        hc = ctypes.c_ulong(0)
        k32.GetProcessHandleCount(h, ctypes.byref(hc))
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
        return (p.WorkingSetSize / 1048576.0, p.PagefileUsage / 1048576.0,
                p.PeakPagefileUsage / 1048576.0, hc.value, n)
    finally:
        k32.CloseHandle(h)


def main():
    mins = float(sys.argv[1]) if len(sys.argv) > 1 else 15.0
    iv = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0

    lines = ["=" * 100,
             "零干扰内存监视  开始 %s   （只读 GetProcessMemoryInfo / 快照，不 attach）"
             % time.strftime("%Y-%m-%d %H:%M:%S"),
             "%-10s %-8s %10s %10s %10s %8s %7s %9s" % (
                 "时间", "PID", "工作集MB", "提交MB", "峰值提交", "句柄", "线程", "比上次Δ提交"),
             "=" * 100]
    with open(LOG, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    cur = None
    prev_cm = None
    base_cm = None
    t_end = time.perf_counter() + mins * 60
    while time.perf_counter() < t_end:
        pid = find_pid()
        if pid is None:
            if cur is not None:
                cur = None
                with open(LOG, "a", encoding="utf-8") as fh:
                    fh.write("%-10s %s\n" % (time.strftime("%H:%M:%S"), "— 进程不见了（被关掉了）"))
            time.sleep(iv)
            continue
        if pid != cur:
            cur = pid
            prev_cm, base_cm = None, None
            with open(LOG, "a", encoding="utf-8") as fh:
                fh.write("%-10s %s\n" % (time.strftime("%H:%M:%S"),
                                          "★ 进程：PID %d（新实例，重新计基准）" % pid))
        r = read(pid)
        if r is None:
            time.sleep(iv)
            continue
        ws, cm, peak, hc, th = r
        if base_cm is None:
            base_cm = cm
        d = "" if prev_cm is None else "%+.0f" % (cm - prev_cm)
        prev_cm = cm
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write("%-10s %-8d %10.0f %10.0f %10.0f %8d %7d %9s\n"
                     % (time.strftime("%H:%M:%S"), pid, ws, cm, peak, hc, th, d))
        time.sleep(iv)

    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write("=" * 100 + "\n监视结束 %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))


if __name__ == "__main__":
    main()
