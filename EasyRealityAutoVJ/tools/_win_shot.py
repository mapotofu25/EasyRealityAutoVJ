# -*- coding: utf-8 -*-
"""找主窗口矩形 + 放大工具栏区域，便于精确定位"演出台"按钮的屏幕坐标。"""
import ctypes
import ctypes.wintypes as wt
import sys

import cv2

u32 = ctypes.windll.user32
u32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
ENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

found = []


def cb(hwnd, _):
    n = u32.GetWindowTextLengthW(hwnd)
    if not n:
        return True
    buf = ctypes.create_unicode_buffer(n + 1)
    u32.GetWindowTextW(hwnd, buf, n + 1)
    t = buf.value
    if "Easy Reality" in t and u32.IsWindowVisible(hwnd):
        r = wt.RECT()
        u32.GetWindowRect(hwnd, ctypes.byref(r))
        found.append((t, r.left, r.top, r.right, r.bottom))
    return True


u32.EnumWindows(ENUMPROC(cb), 0)
if not found:
    print("没找到 Easy Reality 窗口")
    sys.exit(1)

shot = cv2.imread(r"P:\AutoVJ\_screen.png")
for t, l, tp, r, b in found:
    print("窗口 %-28s rect=(%d,%d)-(%d,%d) %dx%d" % (t[:28], l, tp, r, b, r - l, b - tp))

# 取第一个窗口的工具栏区域放大 3 倍（左上角 760x90）
t, l, tp, r, b = found[0]
roi = shot[max(0, tp):tp + 90, max(0, l):l + 780]
big = cv2.resize(roi, None, fx=2.4, fy=2.4, interpolation=cv2.INTER_NEAREST)
# 画刻度线帮助读数（每 50 原始像素一条）
for x in range(0, 780, 50):
    cx = int(x * 2.4)
    cv2.line(big, (cx, 0), (cx, 24), (0, 0, 255), 2)
    cv2.putText(big, str(l + x), (cx + 2, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                (0, 0, 255), 2)
ok, buf = cv2.imencode(".png", big)
with open(r"P:\AutoVJ\_toolbar.png", "wb") as f:
    f.write(buf.tobytes())          # ⚠ 中文路径下 cv2.imwrite 会静默失败
print("工具栏放大图已存 _toolbar.png（红线上的数字 = 屏幕 x 坐标）")
