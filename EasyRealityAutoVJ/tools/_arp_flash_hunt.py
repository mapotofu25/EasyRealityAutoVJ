# -*- coding: utf-8 -*-
"""找出「每秒闪一个 arp.exe 控制台」的来源进程，并验证修复。

用途：用户报「一直有个弹窗在闪」。
判据：控制台窗口的 **父进程** 是谁 —— 若父进程是 EasyRealityAutoVJ.exe，
      就是 ERAVJ 的 `lan_audio.lan_hosts()` 在没加 CREATE_NO_WINDOW 的情况下跑 `arp -a`。

用法：python tools/_arp_flash_hunt.py [采样秒数]
"""
import ctypes
import ctypes.wintypes as W
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

u = ctypes.windll.user32
k = ctypes.windll.kernel32
u.SetProcessDPIAware()
u.IsWindowVisible.argtypes = [W.HWND]

WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, W.HWND, W.LPARAM)
u.GetWindowThreadProcessId.restype = W.DWORD
u.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]

TH32CS_SNAPPROCESS = 0x00000002


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", W.DWORD), ("cntUsage", W.DWORD),
        ("th32ProcessID", W.DWORD), ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", W.DWORD), ("cntThreads", W.DWORD),
        ("th32ParentProcessID", W.DWORD), ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", W.DWORD), ("szExeFile", ctypes.c_wchar * 260),
    ]


def process_table():
    """返回 {pid: (parent_pid, exe_name)}。"""
    snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return {}
    out = {}
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out[int(e.th32ProcessID)] = (int(e.th32ParentProcessID), e.szExeFile)
            ok = k.Process32NextW(snap, ctypes.byref(e))
    finally:
        k.CloseHandle(snap)
    return out


def consoles():
    """返回 [(hwnd, title, pid), ...]，只含可见的 ConsoleWindowClass。"""
    got = []

    def cb(h, _lp):
        try:
            if not u.IsWindowVisible(h):
                return True
            b = ctypes.create_unicode_buffer(256)
            u.GetClassNameW(h, b, 256)
            if b.value == "ConsoleWindowClass":
                t = ctypes.create_unicode_buffer(512)
                u.GetWindowTextW(h, t, 512)
                pid = W.DWORD(0)
                u.GetWindowThreadProcessId(h, ctypes.byref(pid))
                got.append((int(h), t.value, int(pid.value)))
        except Exception:
            pass
        return True

    u.EnumWindows(WNDENUMPROC(cb), 0)
    return got


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
    base = set(h for h, _t, _p in consoles())
    t0 = time.perf_counter()
    seen = []
    while time.perf_counter() - t0 < secs:
        tab = process_table()
        for h, title, pid in consoles():
            if h in base:
                continue
            base.add(h)
            par, exe = tab.get(pid, (0, "?"))
            pex_name = tab.get(par, (0, "?"))[1]
            seen.append((round(time.perf_counter() - t0, 2), title, pid, exe, par, pex_name))
        time.sleep(0.02)

    print("采样 %.1fs，新出现的控制台窗口 %d 个" % (secs, len(seen)))
    from collections import Counter
    parents = Counter(s[5] for s in seen)
    print("=== 父进程统计 ===")
    for name, c in parents.most_common():
        print("   x%-4d 父进程 = %s" % (c, name))
    for t, title, pid, exe, par, pex in seen[:8]:
        print("   +%.2fs  pid=%-6d %-10s  父=%-26s  title=%r"
              % (t, pid, exe, "%s(%d)" % (pex, par), title))
    print("")
    if any(s[5].lower().startswith("easyreality") for s in seen):
        print("结论：❌ 确认是 ERAVJ 在弹控制台（需加 CREATE_NO_WINDOW）")
    elif seen:
        print("结论：⚠ 有控制台闪，但父进程不是 ERAVJ（另找来源）")
    else:
        print("结论：✅ 期间无控制台窗口闪现")


if __name__ == "__main__":
    main()
