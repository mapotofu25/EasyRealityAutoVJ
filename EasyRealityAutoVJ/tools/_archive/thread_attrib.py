# -*- coding: utf-8 -*-
"""进程内**全部**线程的 CPU 归因（含 C 库开的线程，Python 看不到的那些）。

Python 的 threading.enumerate() 只能看到 Python 线程；OpenCV/FFmpeg/OpenBLAS/
MediaFoundation 起的线程全在视野外 —— 这正是「进程 4.8 核但线程相加只有 0.7 核」的原因。
本工具用 Toolhelp32 快照枚举进程内所有线程，逐个 GetThreadTimes，
并标注它是不是 Python 线程、起始地址落在哪个 DLL。

用法（作为模块被别的脚本调用，或独立跑一段指定时长的采样）：
    python tools/thread_attrib.py [秒数]
"""
import os
import sys
import time
import ctypes as C
import ctypes.wintypes as wt

k32 = C.WinDLL("kernel32", use_last_error=True)
NCORE = os.cpu_count() or 4

TH32CS_SNAPTHREAD = 0x00000004
THREAD_QUERY_INFORMATION = 0x0040
MAX_PATH = 260

k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
k32.Thread32First.argtypes = [wt.HANDLE, C.c_void_p]
k32.Thread32Next.argtypes = [wt.HANDLE, C.c_void_p]
k32.OpenThread.restype = wt.HANDLE
k32.OpenThread.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.CloseHandle.argtypes = [wt.HANDLE]
k32.GetCurrentProcess.restype = wt.HANDLE
k32.GetCurrentProcessId.restype = wt.DWORD


class THREADENTRY32(C.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                ("th32ThreadID", wt.DWORD), ("th32OwnerProcessID", wt.DWORD),
                ("tpBasePri", C.c_long), ("tpDeltaPri", C.c_long),
                ("dwFlags", wt.DWORD)]


class FILETIME(C.Structure):
    _fields_ = [("lo", wt.DWORD), ("hi", wt.DWORD)]


k32.GetThreadTimes.argtypes = [wt.HANDLE, C.POINTER(FILETIME), C.POINTER(FILETIME),
                               C.POINTER(FILETIME), C.POINTER(FILETIME)]


def _ft(t):
    return ((t.hi << 32) | t.lo) / 1e7


def enum_threads():
    """返回 {tid: cpu_sec}"""
    pid = k32.GetCurrentProcessId()
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if snap == wt.HANDLE(-1).value or not snap:
        return {}
    res = {}
    te = THREADENTRY32()
    te.dwSize = C.sizeof(THREADENTRY32)
    ok = k32.Thread32First(snap, C.byref(te))
    while ok:
        if te.th32OwnerProcessID == pid:
            h = k32.OpenThread(THREAD_QUERY_INFORMATION, False, te.th32ThreadID)
            if h:
                c, e, k, u = FILETIME(), FILETIME(), FILETIME(), FILETIME()
                if k32.GetThreadTimes(h, C.byref(c), C.byref(e), C.byref(k), C.byref(u)):
                    res[te.th32ThreadID] = _ft(k) + _ft(u)
                k32.CloseHandle(h)
        ok = k32.Thread32Next(snap, C.byref(te))
    k32.CloseHandle(snap)
    return res


# ---- 起始地址 → 所属模块（尽力而为） ----
# ⚠ `GetThreadStartAddress` 是 **调试器 API**（dbghelp/kernel32 里的那个只在被调试进程可用），
# 在普通进程里查不到。正确做法是 ntdll 的 `NtQueryInformationThread` +
# `ThreadQuerySetWin32StartAddress`(class 9)。踩过：用 k32 查恒返回 None → 模块列全空。
ntdll = None
try:
    ntdll = C.WinDLL("ntdll")
    ntdll.NtQueryInformationThread.restype = C.c_long
    ntdll.NtQueryInformationThread.argtypes = [wt.HANDLE, C.c_int, C.c_void_p,
                                               C.c_ulong, C.POINTER(C.c_ulong)]
except OSError:
    ntdll = None

ThreadQuerySetWin32StartAddress = 9

k32.GetModuleHandleExW.argtypes = [wt.DWORD, C.c_void_p, C.POINTER(wt.HANDLE)]
k32.GetModuleFileNameW.argtypes = [wt.HANDLE, wt.LPWSTR, wt.DWORD]

GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS = 0x00000004
GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT = 0x00000002


def thread_start_addr(h):
    if ntdll is None:
        return None
    buf = C.c_void_p()
    ret = C.c_ulong()
    st = ntdll.NtQueryInformationThread(h, ThreadQuerySetWin32StartAddress,
                                        C.byref(buf), C.sizeof(buf), C.byref(ret))
    return buf.value if st == 0 else None


def module_of_addr(addr):
    if not addr:
        return ""
    h = wt.HANDLE()
    ok = k32.GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                                GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                                C.c_void_p(addr), C.byref(h))
    if not ok:
        return "?"
    buf = C.create_unicode_buffer(MAX_PATH)
    if k32.GetModuleFileNameW(h, buf, MAX_PATH):
        return os.path.basename(buf.value)
    return "?"


def thread_modules():
    """{tid: 起始模块名}"""
    pid = k32.GetCurrentProcessId()
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    res = {}
    if not snap or snap == wt.HANDLE(-1).value:
        return res
    te = THREADENTRY32()
    te.dwSize = C.sizeof(THREADENTRY32)
    ok = k32.Thread32First(snap, C.byref(te))
    while ok:
        if te.th32OwnerProcessID == pid:
            h = k32.OpenThread(THREAD_QUERY_INFORMATION, False, te.th32ThreadID)
            if h:
                addr = thread_start_addr(h)
                if addr:
                    res[te.th32ThreadID] = module_of_addr(addr)
                k32.CloseHandle(h)
        ok = k32.Thread32Next(snap, C.byref(te))
    k32.CloseHandle(snap)
    return res


def sample(seconds, label, show_all=False, top=25):
    """采样 seconds 秒，返回 (进程核数, [(tid, cores, name, module)])"""
    import threading
    py = {getattr(t, "native_id", None): t.name for t in threading.enumerate()}

    a = enum_threads()
    pa0 = _proc_cpu()
    t0 = time.perf_counter()
    time.sleep(seconds)
    el = time.perf_counter() - t0
    b = enum_threads()
    pa1 = _proc_cpu()

    rows = []
    for tid, s0 in a.items():
        s1 = b.get(tid)
        if s1 is None:
            continue                      # 采样期间退出的线程
        cores = max(0.0, s1 - s0) / el
        rows.append((tid, cores))
    rows.sort(key=lambda x: -x[1])

    mods = thread_modules()
    total = (pa1 - pa0) / el if (pa0 is not None and pa1 is not None) else sum(r[1] for r in rows)

    print("-- %s（采样 %.1f 秒）--" % (label, seconds))
    print("   【进程合计】%.2f 核  = 占整机 %.1f%%（%d 核）" % (total, total / NCORE * 100, NCORE))
    print("   线程数 %d，其中 Python 可见 %d" % (len(rows), sum(1 for r in rows if r[0] in py)))
    print("   %-9s %-9s %-9s %-24s %s" % ("TID", "核数", "占整机%", "Python线程名", "起始模块"))
    shown = 0
    for tid, cores in rows:
        if not show_all and cores < 0.005 and shown >= 8:
            continue
        print("   %-9d %-9.3f %-9.2f %-24s %s"
              % (tid, cores, cores / NCORE * 100, py.get(tid, "—"), mods.get(tid, "?")))
        shown += 1
        if shown >= top:
            rest = len(rows) - shown
            if rest > 0:
                print("   … 其余 %d 条线程合计 %.3f 核" % (rest, sum(r[1] for r in rows[shown:])))
            break
    return total, rows


def _proc_cpu():
    class FT(C.Structure):
        _fields_ = [("lo", wt.DWORD), ("hi", wt.DWORD)]
    k32.GetProcessTimes.argtypes = [wt.HANDLE, C.POINTER(FT), C.POINTER(FT),
                                    C.POINTER(FT), C.POINTER(FT)]
    c, e, k, u = FT(), FT(), FT(), FT()
    if not k32.GetProcessTimes(k32.GetCurrentProcess(), C.byref(c), C.byref(e),
                               C.byref(k), C.byref(u)):
        return None
    return _ft(k) + _ft(u)


if __name__ == "__main__":
    sec = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
    print("=" * 92)
    print("线程归因（空载基线，本机 %d 核）" % NCORE)
    print("=" * 92)
    sample(sec, "空载", show_all=True)
