# -*- coding: utf-8 -*-
"""真窗口验证：演出台"正在播放"素材的金黄底 + 金边。

步骤：抢前台 → 点「演出台」页签 → 点「开始自动 VJ」→ **把输出窗口最小化**
（它一开就盖在图层区上，不关掉根本看不到高亮）→ 等素材切一次 → 截图统计金像素。
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

u32 = ctypes.windll.user32
ENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def windows():
    out = {}

    def cb(h, _):
        n = u32.GetWindowTextLengthW(h)
        if n:
            b = ctypes.create_unicode_buffer(n + 1)
            u32.GetWindowTextW(h, b, n + 1)
            if "Easy Reality" in b.value:
                out[b.value] = h
        return True

    u32.EnumWindows(ENUMPROC(cb), 0)
    return out


def click(x, y, tag):
    u32.SetCursorPos(x, y)
    time.sleep(0.35)
    u32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.06)
    u32.mouse_event(0x0004, 0, 0, 0, 0)
    print("  点击", tag, (x, y))
    time.sleep(1.0)


ws = windows()
print("窗口：", list(ws))
main = next((h for t, h in ws.items() if "Output" not in t), None)
outs = [h for t, h in ws.items() if "Output" in t]
if main is None:
    print("没找到主窗口")
    sys.exit(1)

u32.ShowWindow(main, 9)
u32.SetForegroundWindow(main)
time.sleep(0.6)
click(240, 152, "演出台页签")
click(270, 122, "开始自动 VJ")
time.sleep(6)

# 输出窗口会盖住图层区 —— 最小化它（0=SW_HIDE, 6=SW_MINIMIZE）
for h in outs:
    u32.ShowWindow(h, 6)
    print("  已最小化输出窗口")
time.sleep(0.8)

u32.ShowWindow(main, 9)
u32.SetForegroundWindow(main)
time.sleep(1.0)

from PySide6.QtWidgets import QApplication                     # noqa: E402
from PySide6.QtGui import QGuiApplication                      # noqa: E402

app = QApplication([])
QGuiApplication.primaryScreen().grabWindow(0).save(r"P:\AutoVJ\_live.png", "PNG")

import cv2                                                     # noqa: E402
import numpy as np                                             # noqa: E402

im = cv2.imread(r"P:\AutoVJ\_live.png")
b, g, r = im[:, :, 0].astype(int), im[:, :, 1].astype(int), im[:, :, 2].astype(int)
m = ((r > 200) & (g > 150) & (g < 240) & (b < 95)).astype(np.uint8)
m[:, 1100:] = 0
m[600:, :] = 0
print("图层区金像素 =", int(m.sum()))
n, _lab, stats, _c = cv2.connectedComponentsWithStats(m, 8)
for i in range(1, n):
    x, y, w, h, a = stats[i]
    if a > 120:
        print("   金块 bbox=(%4d,%4d) %3dx%-3d 面积=%5d" % (x, y, w, h, a))
        cv2.imwrite(r"P:\AutoVJ\_live_cell.png", im[max(0, y - 6):y + h + 6,
                                                   max(0, x - 6):x + w + 6])
