# -*- coding: utf-8 -*-
"""侦察：找出 EasyRealityCue 进程的窗口状态（可见性 / 尺寸 / 标题）。"""
import ctypes
import time
from ctypes import wintypes

u32 = ctypes.windll.user32
TARGET_PID = int(__import__("sys").argv[1]) if len(__import__("sys").argv) > 1 else 4324

found = []


def cb(hwnd, _):
    pid = wintypes.DWORD()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value == TARGET_PID:
        n = u32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        u32.GetWindowTextW(hwnd, buf, n + 1)
        rect = wintypes.RECT()
        u32.GetWindowRect(hwnd, ctypes.byref(rect))
        found.append({
            "hwnd": hwnd,
            "title": buf.value,
            "visible": bool(u32.IsWindowVisible(hwnd)),
            "iconic": bool(u32.IsIconic(hwnd)),
            "size": (rect.right - rect.left, rect.bottom - rect.top),
            "class": (lambda: (
                (lambda b: (u32.GetClassNameW(hwnd, b, 256), b.value)[1])(
                    ctypes.create_unicode_buffer(256))))(),
        })
    return True


CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
u32.EnumWindows(CB(cb), 0)

print("PID %d 的顶层窗口数：%d" % (TARGET_PID, len(found)))
for w in found:
    print("  hwnd=%s title=%r visible=%s iconic=%s size=%s class=%r"
          % (w["hwnd"], w["title"], w["visible"], w["iconic"], w["size"], w["class"]))

# 也看看线程数（是否卡在某个线程）
import subprocess
r = subprocess.run(["tasklist", "/FI", "PID eq %d" % TARGET_PID, "/FO", "CSV", "/V"],
                   capture_output=True, text=True, errors="replace")
print()
print("=== tasklist ===")
for line in (r.stdout or "").splitlines()[-4:]:
    print(" ", line[:200])
