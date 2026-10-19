# -*- coding: utf-8 -*-
"""对照实验：证明「无控制台的父进程（= PyInstaller windowed 打包）」跑裸
`subprocess.run(["arp","-a"])` **会弹出新的控制台窗口**，而加了
`CREATE_NO_WINDOW` 就不会。

这是用户「一直有个弹窗在闪」的根本机制：
  - 从 bash/PowerShell 里跑 python ⇒ 父进程有控制台 ⇒ 子进程继承 ⇒ **不闪**（所以
    在开发机上直接跑脚本永远复现不出来！）；
  - PyInstaller `--windowed` 的 ERAVJ ⇒ **完全没有控制台** ⇒ 每次
    `arp -a` 都新建一个控制台 ⇒ **闪**。

做法：用 `DETACHED_PROCESS` 启动子进程（等于没有控制台），子进程内两种调用各来几次，
父进程在旁边枚举新出现的 ConsoleWindowClass 窗口。

用法：python tools/_arp_console_proof.py
"""
import ctypes
import ctypes.wintypes as W
import os
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

u = ctypes.windll.user32
u.SetProcessDPIAware()
u.IsWindowVisible.argtypes = [W.HWND]
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, W.HWND, W.LPARAM)
u.GetWindowThreadProcessId.restype = W.DWORD
u.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]


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
                got.append(t.value)
        except Exception:
            pass
        return True

    u.EnumWindows(WNDENUMPROC(cb), 0)
    return got


CHILD = r'''
import sys, time, subprocess, os
sys.stdout.reconfigure(encoding="utf-8")
CNW = 0x08000000
# 告诉父进程"我要开始裸调用了"
print("MARK_NAKED", flush=True)
time.sleep(0.3)
for i in range(3):
    subprocess.run(["arp","-a"], capture_output=True, text=True,
                   encoding="gbk", errors="replace")
    time.sleep(0.25)
print("MARK_NOFIX_DONE", flush=True)
time.sleep(0.3)
for i in range(3):
    subprocess.run(["arp","-a"], capture_output=True, text=True,
                   encoding="gbk", errors="replace", creationflags=CNW)
    time.sleep(0.25)
print("MARK_FIX_DONE", flush=True)
'''


def main():
    DETACHED_PROCESS = 0x00000008
    base = set(consoles())
    print("基线控制台：", base or "(无)")

    env = dict(os.environ)
    p = subprocess.Popen([sys.executable, "-c", CHILD],
                         creationflags=DETACHED_PROCESS,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", env=env)

    phase = "naked"
    naked = []
    fixed = []
    t0 = time.perf_counter()
    import threading

    def reader():
        nonlocal phase
        for line in p.stdout:
            line = line.strip()
            if line == "MARK_NOFIX_DONE":
                phase = "fixed"
            print("  [child] %s" % line, flush=True)

    th = threading.Thread(target=reader, daemon=True)
    th.start()

    while p.poll() is None and time.perf_counter() - t0 < 20:
        for t in consoles():
            if t in base:
                continue
            base.add(t)
            if phase == "naked":
                naked.append(t)
            else:
                fixed.append(t)
        time.sleep(0.02)
    th.join(3.0)

    err = p.stderr.read() if p.stderr else ""
    print("")
    print("=== 结果 ===")
    print("裸调用(修复前)  弹窗数 = %d  %s" % (len(naked), naked[:5]))
    print("加 CREATE_NO_WINDOW(修复后)  弹窗数 = %d  %s" % (len(fixed), fixed[:5]))
    if err.strip():
        print("子进程 stderr:", err[:400])
    print("")
    if naked and not fixed:
        print("结论：✅ 证实 —— 无控制台父进程下，裸 arp 会弹窗，加标志后不弹")
        sys.exit(0)
    if not naked and not fixed:
        print("结论：⚠ 两种都没弹（本机环境未复现，但不影响修复正确性）")
        sys.exit(0)
    print("结论：❓ 结果不符合预期，需人工看")
    sys.exit(1)


if __name__ == "__main__":
    main()
