# -*- coding: utf-8 -*-
"""取证：把正在运行的 AutoVJ 窗口抓到 PNG（含整屏），用于判断"卡在哪一层"。

用法：
    venv\\Scripts\\python.exe -u tools/_grab_now.py [输出目录]
不传输出目录就写系统临时目录。会在拍照前把窗口抢到前台（每次重抢）。
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys
import tempfile
import time

u32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

u32.SetProcessDPIAware()
u32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
ENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

wins = []


def _cb(hwnd, _):
    n = u32.GetWindowTextLengthW(hwnd)
    if not n or not u32.IsWindowVisible(hwnd):
        return True
    buf = ctypes.create_unicode_buffer(n + 1)
    u32.GetWindowTextW(hwnd, buf, n + 1)
    t = buf.value
    if "Easy Reality" in t:
        r = wt.RECT()
        u32.GetWindowRect(hwnd, ctypes.byref(r))
        wins.append((t, hwnd, r.left, r.top, r.right - r.left, r.bottom - r.top))
    return True


def grab(x, y, w, h, out):
    """屏幕区域 → PNG（用 GDI StretchBlt，不依赖第三方库）。"""
    hdc = u32.GetDC(0)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    # SRCCOPY
    gdi32.BitBlt(mem, 0, 0, w, h, hdc, x, y, 0x00CC0020)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                    ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
                    ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                    ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                    ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]

    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth = w
    bi.biHeight = -h          # 负数 = 自上而下
    bi.biPlanes = 1
    bi.biBitCount = 32
    bi.biCompression = 0
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)

    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    u32.ReleaseDC(0, hdc)

    # BGRA → PNG（用 Qt，项目里现成有）
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QImage
    img = QImage(bytes(buf), w, h, w * 4, QImage.Format_ARGB32)
    img.save(out)
    return out


def main():
    # ⚠ 本脚本是**人工取证工具**，不是自测脚本：`outdir` 里的截图**就是产物**
    #   （下面会 print 出来给人看）。⇒ **有意不做 `finally` 清理** —— 自动删掉会
    #   把用户刚要看的截图销毁掉。本项目的「测试夹具必须清理」铁律不适用于此类工具。
    #   建议显式传参指定输出目录，便于自己管理：`python tools/_grab_now.py <目录>`
    outdir = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="avj_shot_")
    u32.EnumWindows(ENUMPROC(_cb), 0)
    if not wins:
        print("没找到 'Easy Reality' 窗口（可能已退出）")
        return 1
    for t, hwnd, x, y, w, h in wins:
        print("窗口 %r  (%d,%d) %dx%d" % (t, x, y, w, h))
    # 抢前台（先 show/restore，再抢）
    main_win = [v for v in wins if "Output" not in v[0]]
    if main_win:
        hwnd = main_win[0][1]
        u32.ShowWindow(hwnd, 9)          # SW_RESTORE
        u32.SetForegroundWindow(hwnd)
        time.sleep(0.8)
        t, hwnd, x, y, w, h = main_win[0]
        p1 = grab(x, y, w, h, os.path.join(outdir, "main.png"))
        print("主窗口截图:", p1)
    # 整屏
    vx = u32.GetSystemMetrics(76)
    vy = u32.GetSystemMetrics(77)
    vw = u32.GetSystemMetrics(78)
    vh = u32.GetSystemMetrics(79)
    p2 = grab(vx, vy, vw, vh, os.path.join(outdir, "screen.png"))
    print("整屏截图:", p2, "(%dx%d @%d,%d)" % (vw, vh, vx, vy))
    return 0


if __name__ == "__main__":
    sys.exit(main())
