# -*- coding: utf-8 -*-
"""侦察：精确记录 EasyRealityCue 的启动时间线（进程存活 / 窗口出现 / 退出码）。"""
import ctypes
import subprocess
import sys
import time
from ctypes import wintypes

CUE = r"P:\ERAVJ\EasyRealityCue"
PY = CUE + r"\venv\Scripts\python.exe"
u32 = ctypes.windll.user32


def windows_of(pid):
    out = []

    def cb(hwnd, _):
        p = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid:
            n = u32.GetWindowTextLengthW(hwnd)
            b = ctypes.create_unicode_buffer(n + 1)
            u32.GetWindowTextW(hwnd, b, n + 1)
            out.append((b.value, bool(u32.IsWindowVisible(hwnd))))
        return True

    u32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)(cb), 0)
    return out


p = subprocess.Popen([PY, r"src\ercue\main.py"], cwd=CUE,
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
print("t=0.0s  启动 PID=%d" % p.pid)

t0 = time.time()
last_alive = True
for i in range(16):                      # 16 × 3s = 48s
    time.sleep(3)
    el = time.time() - t0
    rc = p.poll()
    alive = rc is None
    wins = windows_of(p.pid) if alive else []
    print("t=%.1fs 存活=%s 窗口=%d %s%s"
          % (el, alive, len(wins),
             ("可见:" + str([w for w in wins if w[1]])) if wins else "",
             ("" if alive else "  ★ 退出码=%s" % rc)))
    if not alive:
        break
    last_alive = alive

if p.poll() is not None:
    out = p.stdout.read() or b""
    txt = out.decode("utf-8", "replace")
    print("\n=== 进程输出（%d 字节）===" % len(out))
    print(txt[:4000] if txt.strip() else "(完全无输出 —— 静默退出)")
else:
    print("\n进程仍在运行（PID=%d），窗口数=%d" % (p.pid, len(windows_of(p.pid))))
    print("保留进程供进一步观察。")
sys.exit(0)
