# -*- coding: utf-8 -*-
"""侦察：在同一进程内完成「启动 → 探栈 → 收尾」，避免子进程随父进程退出。

用法：P:\\ERAVJ\\AutoVJ\\venv\\Scripts\\python.exe tools\\_recon_stack.py
"""
import ctypes
import subprocess
import sys
import time
from ctypes import wintypes

CUE = r"P:\ERAVJ\EasyRealityCue"
PY = CUE + r"\venv\Scripts\python.exe"
PYSPY = r"P:\ERAVJ\AutoVJ\venv\Scripts\py-spy.exe"
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


p = subprocess.Popen([PY, r"src\ercue\main.py"], cwd=CUE)
print("启动 PID=%d" % p.pid)

for wait in (5, 10, 15):
    time.sleep(wait)
    print("  t+=%ds 存活=%s 窗口=%s" % (wait, p.poll() is None, windows_of(p.pid)))

print()
print("=" * 66)
print("py-spy dump（看卡在哪一行）")
print("=" * 66)
r = subprocess.run([PYSPY, "dump", "--pid", str(p.pid)],
                   capture_output=True, text=True, errors="replace")
print("rc=%s" % r.returncode)
print(r.stdout[:3500] if r.stdout else "(无 stdout)")
if r.stderr:
    print("--- stderr ---")
    print(r.stderr[:800])

print()
print("收尾：结束进程")
try:
    p.terminate()
    time.sleep(2)
    if p.poll() is None:
        p.kill()
    print("已结束")
except Exception as e:                                            # noqa: BLE001
    print("结束失败：", e)
sys.exit(0)
