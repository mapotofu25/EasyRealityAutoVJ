# -*- coding: utf-8 -*-
"""端到端验证：从源码以「无控制台」方式跑起 ERAVJ，观察是否还会闪 arp 控制台。

## 为什么必须"无控制台"启动
开发机上从 bash/PowerShell 跑脚本 ⇒ 父进程有控制台 ⇒ `arp.exe` **继承**控制台
⇒ 永远复现不出闪烁。只有 `pythonw.exe` / PyInstaller `--windowed` 那种
**完全没有控制台**的进程，`subprocess` 才会给子进程**新建**一个控制台窗口。

## 做法
1. 用 `venv/Scripts/pythonw.exe`（无控制台）启动 `src/main.py`；
2. 同时在本脚本里枚举"新出现的 ConsoleWindowClass"，并查它的父进程；
3. 跑 N 秒；只要出现 **父进程 = 我们启动的 pythonw** 的控制台 ⇒ 修复无效。

用法：python tools/_lan_live_nowin_test.py [秒数]
"""
import ctypes
import ctypes.wintypes as W
import os
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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
        ("th32ProcessID", W.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
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
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 40.0
    pyw = os.path.join(ROOT, "venv", "Scripts", "pythonw.exe")
    if not os.path.isfile(pyw):
        print("找不到 pythonw.exe:", pyw)
        sys.exit(2)
    main_py = os.path.join(ROOT, "src", "main.py")
    print("启动（无控制台）：%s  %s" % (pyw, main_py))

    DETACHED_PROCESS = 0x00000008
    p = subprocess.Popen([pyw, main_py], cwd=ROOT,
                         creationflags=DETACHED_PROCESS,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    my_child = p.pid
    print("ERAVJ 子进程 pid = %d" % my_child)

    base = set(h for h, _t, _p in consoles())
    found = []
    # 等它把接收端起来（LAN 宣告线程 1s 节拍）
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < secs:
        tab = ptab()
        for h, title, pid in consoles():
            if h in base:
                continue
            base.add(h)
            par, exe = tab.get(pid, (0, "?"))
            found.append((round(time.perf_counter() - t0, 2), title, pid,
                          exe, par, tab.get(par, (0, "?"))[1]))
        time.sleep(0.02)

    try:
        p.terminate()
    except Exception:
        pass
    try:
        p.wait(timeout=8)
    except Exception:
        try:
            subprocess.run(["taskkill", "/PID", str(my_child), "/F"],
                           capture_output=True)
        except Exception:
            pass

    print("采样 %.0fs，新出现的控制台窗口 %d 个" % (secs, len(found)))
    mine = [f for f in found if f[4] == my_child]
    for t, title, pid, exe, par, pex in found:
        tag = "  <== 来自本次启动的 ERAVJ" if par == my_child else ""
        print("   +%.2fs pid=%-6d %-12s 父=%s(%d)%s"
              % (t, pid, exe, pex, par, tag))
    print("")
    if mine:
        print("结论：❌ ERAVJ 仍在弹控制台窗口（修复无效）")
        sys.exit(1)
    print("结论：✅ ERAVJ 运行 %.0fs，未弹任何自己产生的控制台窗口"
          % secs)
    sys.exit(0)


if __name__ == "__main__":
    main()
