# -*- coding: utf-8 -*-
"""对**正在运行的** exe 做线程级 CPU 归因（只读，不干扰目标进程）

为什么需要它：进程总 CPU（如 8.6 核）看不出「谁在烧」。`threading.enumerate()` 只能看到
Python 线程且拿不到 CPU 时间；本工具用 Windows API 枚举**所有**线程（含 FFmpeg / NVIDIA
驱动 / Qt 池线程），读 `GetThreadTimes` 拿每个线程的 CPU，并尝试 `GetThreadDescription`
取线程名（CPython 在 Windows 上会设置它）。

用法：venv\\Scripts\\python.exe tools\\_thread_cpu.py [采样秒数=8] [进程名=EasyRealityAutoVJ.exe]
"""
import ctypes
import ctypes.wintypes as wt
import os
import re
import subprocess
import sys
import time

k32 = ctypes.windll.kernel32
psapi = ctypes.WinDLL("psapi")

TH32CS_SNAPTHREAD = 0x00000004
THREAD_QUERY_LIMITED_INFORMATION = 0x0800
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

k32.OpenThread.restype = wt.HANDLE
k32.OpenThread.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.CloseHandle.argtypes = [wt.HANDLE]
try:
    k32.GetThreadDescription.restype = ctypes.c_long
    k32.GetThreadDescription.argtypes = [wt.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
    _HAS_DESC = True
except Exception:                                                    # noqa: BLE001
    _HAS_DESC = False


class TE(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
                ("th32ThreadID", ctypes.c_ulong), ("th32OwnerProcessID", ctypes.c_ulong),
                ("tpBasePri", ctypes.c_long), ("tpDeltaPri", ctypes.c_long), ("dwFlags", ctypes.c_ulong)]


class FT(ctypes.Structure):
    _fields_ = [("lo", ctypes.c_ulong), ("hi", ctypes.c_ulong)]


def _ft(v):
    return ((v.hi << 32) | v.lo) / 1e7


def tids_of(pid):
    s = k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    out = []
    te = TE()
    te.dwSize = ctypes.sizeof(TE)
    if k32.Thread32First(s, ctypes.byref(te)):
        while True:
            if te.th32OwnerProcessID == pid:
                out.append(te.th32ThreadID)
            if not k32.Thread32Next(s, ctypes.byref(te)):
                break
    k32.CloseHandle(s)
    return out


def tname(h):
    if not _HAS_DESC:
        return ""
    p = ctypes.c_wchar_p()
    try:
        if k32.GetThreadDescription(h, ctypes.byref(p)) == 0 and p.value:
            return p.value
    except Exception:                                                # noqa: BLE001
        pass
    return ""


def tinfo(tid):
    h = k32.OpenThread(THREAD_QUERY_LIMITED_INFORMATION, False, tid)
    if not h:
        return None
    try:
        c, e, kt, u = FT(), FT(), FT(), FT()
        if not k32.GetThreadTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(u)):
            return None
        return _ft(kt) + _ft(u), tname(h)
    finally:
        k32.CloseHandle(h)


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
    exe = sys.argv[2] if len(sys.argv) > 2 else "EasyRealityAutoVJ.exe"
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq %s" % exe], capture_output=True,
                         text=True, encoding="gbk", errors="replace").stdout
    pids = re.findall(r"%s\s+(\d+)" % re.escape(exe), out)
    if not pids:
        print("★ %s 没在跑" % exe)
        return
    pid = int(pids[0])

    ph = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    print("=" * 84)
    print("线程级 CPU 归因   PID %d   采样 %.0f 秒   %s" % (pid, secs, time.strftime("%H:%M:%S")))
    print("=" * 84)

    a = {}
    for tid in tids_of(pid):
        r = tinfo(tid)
        if r:
            a[tid] = r
    t0 = time.perf_counter()
    time.sleep(secs)
    wall = time.perf_counter() - t0

    rows = []
    for tid in tids_of(pid):
        r = tinfo(tid)
        if not r or tid not in a:
            continue
        d = r[0] - a[tid][0]
        if d > 0.001:
            rows.append((d / wall, r[1] or "(无名)", tid))
    rows.sort(reverse=True)

    total = sum(x[0] for x in rows)
    print("有 CPU 消耗的线程 %d 个，合计 %.2f 核" % (len(rows), total))
    print()
    print("  %-30s %10s   %s" % ("线程名", "核当量", "tid"))
    print("  " + "-" * 60)
    for cpu, name, tid in rows[:26]:
        print("  %-30s %10.3f   %d" % (name[:30], cpu, tid))

    # 按前缀聚组
    print()
    print("=== 按类别聚合 ===")
    groups = {}
    for cpu, name, tid in rows:
        base = name
        m = re.match(r"^([A-Za-z_\-]+)", name)
        if m:
            base = m.group(1)
        if re.match(r"^Thread-\d+", name):
            base = "解码/其它 Python 线程"
        groups[base] = groups.get(base, 0.0) + cpu
    for k, v in sorted(groups.items(), key=lambda x: -x[1])[:14]:
        print("  %-30s %7.3f 核   %5.1f%%" % (k[:30], v, v / max(total, 1e-9) * 100))
    print()
    print("  进程合计 %.2f 核（整机 %.1f%%，按 16 核）" % (total, total / 16 * 100))
    print()
    print("  提示：无名线程多为 FFmpeg / NVIDIA 驱动 / Qt 线程池 —— 数量多但每个很便宜是正常的；")
    print("        真正要看的是「哪一类占了核」。")
    k32.CloseHandle(ph)

    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                           "_thread_cpu.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join("%-32s %8.3f 核" % (n, c) for c, n, _ in rows))
    print("→ 明细已写入 _thread_cpu.txt")


if __name__ == "__main__":
    main()
