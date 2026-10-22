# -*- coding: utf-8 -*-
"""侦察（修正版）：正确声明 ctypes argtypes，全量枚举窗口，避免 64 位句柄截断。"""
import ctypes
import subprocess
import sys
import time
from ctypes import wintypes

CUE = r"<项目根>\EasyRealityCue"
PY = CUE + r"\venv\Scripts\python.exe"

u32 = ctypes.windll.user32
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
u32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
u32.EnumWindows.restype = ctypes.c_bool
u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
u32.GetWindowThreadProcessId.restype = wintypes.DWORD
u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
u32.GetWindowTextW.restype = ctypes.c_int
u32.IsWindowVisible.argtypes = [wintypes.HWND]
u32.IsWindowVisible.restype = ctypes.c_bool


def all_windows():
    out = []

    def cb(hwnd, _):
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        n = u32.GetWindowTextLengthW(hwnd)
        b = ctypes.create_unicode_buffer(n + 1)
        u32.GetWindowTextW(hwnd, b, n + 1)
        out.append((int(hwnd), int(pid.value), b.value, bool(u32.IsWindowVisible(hwnd))))
        return True

    u32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


p = subprocess.Popen([PY, r"src\ercue\main.py"], cwd=CUE)
print("启动 PID=%d" % p.pid)
time.sleep(12)
print("存活：", p.poll() is None)

ws = all_windows()
print("\n=== 目标 PID 的窗口 ===")
mine = [w for w in ws if w[1] == p.pid]
print("  ", mine if mine else "(无)")

print("\n=== 含 Cue/cue/码表 的窗口（全量）===")
hit = [w for w in ws if ("cue" in w[2].lower() or "码表" in w[2])]
print("  ", hit if hit else "(无)")

print("\n=== 全部可见窗口（前 18 个）===")
for w in [x for x in ws if x[3]][:18]:
    print("   pid=%-6d %r" % (w[1], w[2]))

p.terminate()
time.sleep(1)
if p.poll() is None:
    p.kill()
print("\n已收尾")
sys.exit(0)
