# -*- coding: utf-8 -*-
"""截当前窗口（用 PIL ImageGrab，依赖已装）。"""
import ctypes
import ctypes.wintypes as W
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
u = ctypes.windll.user32
u.SetProcessDPIAware()
h = int(sys.argv[1]) if len(sys.argv) > 1 else 15928712
out = sys.argv[2] if len(sys.argv) > 2 else r"P:\ERAVJ\AutoVJ\_shot.png"

u.ShowWindow(h, 9)
try:
    u.SetForegroundWindow(h)
except Exception:
    pass
time.sleep(0.8)
r = W.RECT()
u.GetWindowRect(h, ctypes.byref(r))
print("rect", r.left, r.top, r.right, r.bottom)

from PIL import ImageGrab  # noqa: E402
img = ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom), all_screens=True)
img.save(out)
print("saved", out, img.size)
