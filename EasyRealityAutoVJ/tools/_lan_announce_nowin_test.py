# -*- coding: utf-8 -*-
"""验证：加了 CREATE_NO_WINDOW 的宣告循环不会再弹控制台窗口。

模拟 `lan_audio_recv._announce_worker` 的行为：每秒调一次 `_announce()`，
即 `lan_audio.lan_hosts()`（内部跑 arp -a）。

判据：整个采样窗内，**新出现的控制台窗口 = 0**。
（修复前会每秒出现一个 ARP.EXE 控制台。）

用法：python tools/_lan_announce_nowin_test.py [秒数]
"""
import os
import sys
import threading
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"P:\ERAVJ\AutoVJ\src")

import ctypes
import ctypes.wintypes as W

u = ctypes.windll.user32
k = ctypes.windll.kernel32
u.SetProcessDPIAware()
u.IsWindowVisible.argtypes = [W.HWND]
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, W.HWND, W.LPARAM)
u.GetWindowThreadProcessId.restype = W.DWORD
u.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]

TH32CS_SNAPPROCESS = 0x00000002


class PE32(ctypes.Structure):
    _fields_ = [
        ("dwSize", W.DWORD), ("cntUsage", W.DWORD),
        ("th32ProcessID", W.DWORD), ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", W.DWORD), ("cntThreads", W.DWORD),
        ("th32ParentProcessID", W.DWORD), ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", W.DWORD), ("szExeFile", ctypes.c_wchar * 260),
    ]


def ptab():
    snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return {}
    out = {}
    try:
        e = PE32(); e.dwSize = ctypes.sizeof(PE32)
        ok = k.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out[int(e.th32ProcessID)] = (int(e.th32ParentProcessID), e.szExeFile)
            ok = k.Process32NextW(snap, ctypes.byref(e))
    finally:
        k.CloseHandle(snap)
    return out


def consoles():
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
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0

    import lan_audio as LA

    mypid = os.getpid()
    found = []
    calls = [0]

    def worker():
        while not stop:
            try:
                LA.lan_hosts()
                calls[0] += 1
            except Exception as e:
                print("  调用异常:", e)
            time.sleep(1.0)

    stop = False
    base = set(h for h, _t, _p in consoles())
    th = threading.Thread(target=worker, daemon=True)
    th.start()
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < secs:
        tab = ptab()
        for h, title, pid in consoles():
            if h in base:
                continue
            base.add(h)
            par, exe = tab.get(pid, (0, "?"))
            pex = tab.get(par, (0, "?"))[1]
            found.append((round(time.perf_counter() - t0, 2), title, pid,
                          exe, pex, par))
        time.sleep(0.02)
    stop = True
    th.join(1.0)

    print("lan_hosts() 调用 %d 次 / 采样 %.1fs" % (calls[0], secs))
    print("本测试进程 pid = %d" % mypid)
    print("期间新出现的控制台窗口：%d" % len(found))
    for t, title, pid, exe, pex, par in found[:10]:
        tag = "  <== 本进程产生" if par == mypid else ""
        print("   +%.2fs %r  exe=%s 父=%s(%d)%s" % (t, title, exe, pex, par, tag))
    print("")
    mine = [f for f in found if f[5] == mypid]
    if mine:
        print("结论：❌ 本进程仍在弹控制台（修复无效）")
        sys.exit(1)
    print("结论：✅ 本进程调用 %d 次 arp，期间 0 个控制台窗口"
          % calls[0])
    sys.exit(0)


if __name__ == "__main__":
    main()
