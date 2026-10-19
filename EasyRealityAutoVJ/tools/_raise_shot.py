# -*- coding: utf-8 -*-
"""把主窗口提到最前 + 间隔连拍，用来看"金黄底"是不是跟着正在播的素材走。

用法：venv/Scripts/python.exe -u tools/_raise_shot.py [张数=3] [间隔秒=14]
"""
import ctypes
import ctypes.wintypes as wt
import sys
import time

import cv2

u32 = ctypes.windll.user32
ENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
found = []


def cb(hwnd, _):
    n = u32.GetWindowTextLengthW(hwnd)
    if not n:
        return True
    buf = ctypes.create_unicode_buffer(n + 1)
    u32.GetWindowTextW(hwnd, buf, n + 1)
    if "Easy Reality" in buf.value and u32.IsWindowVisible(hwnd):
        found.append(hwnd)
    return True


u32.EnumWindows(ENUMPROC(cb), 0)
if not found:
    print("没找到 Easy Reality 窗口")
    sys.exit(1)
hwnd = found[0]
u32.ShowWindow(hwnd, 9)              # SW_RESTORE
try:
    u32.SetForegroundWindow(hwnd)
except Exception:
    pass
time.sleep(0.8)

def _raise():
    """每次拍照前都抢一次前台 —— 中间穿插的其它窗口会把 app 盖住。"""
    u32.ShowWindow(hwnd, 9)
    try:
        u32.SetForegroundWindow(hwnd)
        u32.BringWindowToTop(hwnd)
    except Exception:
        pass
    time.sleep(0.7)


n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
gap = float(sys.argv[2]) if len(sys.argv) > 2 else 14.0

from PySide6.QtWidgets import QApplication               # noqa: E402
from PySide6.QtGui import QGuiApplication                # noqa: E402

app = QApplication([])
for k in range(n):
    _raise()
    QGuiApplication.primaryScreen().grabWindow(0).save(
        r"P:\AutoVJ\_raise%d.png" % k, "PNG")
    print("shot", k)
    if k < n - 1:
        time.sleep(gap)

# 拼一张对比图：只取"图层 2"那一行（x 650..1400）
import numpy as np                                       # noqa: E402

tops = []
for k in range(n):
    im = cv2.imread(r"P:\AutoVJ\_raise%d.png" % k)
    roi = im[330:470, 650:1420]
    lab = np.zeros((26, roi.shape[1], 3), np.uint8)
    cv2.putText(lab, "shot %d" % k, (6, 19), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                (255, 255, 255), 1)
    tops.append(np.vstack([lab, roi]))
out = np.vstack(tops)
ok, buf = cv2.imencode(".png", out)
with open(r"P:\AutoVJ\_raise_cmp.png", "wb") as f:
    f.write(buf.tobytes())
print("对比图 _raise_cmp.png", out.shape)
