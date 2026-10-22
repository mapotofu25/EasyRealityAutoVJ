# -*- coding: utf-8 -*-
"""守株待兔：盯住 AutoVJ 主窗口的「输出预览」区，画面连续不动就抓现场。

每 0.5s 抓一次窗口内容（**用 PrintWindow，窗口在后台也能抓**），算与上一帧的平均像素差。
- 连续 >= FREEZE_N 次几乎不变 ⇒ 判定"冻住"，立刻：
    · 存下整窗截图 + 预览区截图
    · 跑 py-spy dump（含 --locals）存栈 —— 哪条线程卡在哪一行
    · 追加 ui_stall.log 尾部
- 同时统计"最长连续不动时长"，即使没到阈值也报出来。

用法：
    venv\\Scripts\\python.exe -u tools/_catch_freeze.py <分钟> [输出目录]
⚠ 不抢前台、不点任何东西。若 PrintWindow 抓到全黑（少见），自动退回"仅前台采样"。
"""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import tempfile
import time

import numpy as np

u32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
u32.SetProcessDPIAware()
u32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
ENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYSPY = os.path.join(ROOT, "venv", "Scripts", "py-spy.exe")
STALL = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "ui_stall.log")

# 预览区在窗口里的相对位置（左三分之一、中下部）
FX0, FX1, FY0, FY1 = 0.02, 0.32, 0.68, 0.86
FREEZE_N = 4          # 连续这么多次几乎不变 ⇒ 判定冻住（0.5s × 4 = 2s）


class BIH(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
                ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


def _dib(mem, bmp, w, h):
    bi = BIH()
    bi.biSize = ctypes.sizeof(BIH)
    bi.biWidth = w
    bi.biHeight = -h
    bi.biPlanes = 1
    bi.biBitCount = 32
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
    a = np.frombuffer(buf.raw, dtype=np.uint8).reshape(h, w, 4)
    return a[:, :, :3].astype(np.int16)          # BGR


def grab_hwnd(hwnd):
    """PrintWindow 抓整窗（PW_RENDERFULLCONTENT=2）——窗口不在前台也能抓到内容。"""
    r = wt.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    hdc = u32.GetDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    ok = u32.PrintWindow(hwnd, mem, 2)
    arr = _dib(mem, bmp, w, h)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    u32.ReleaseDC(hwnd, hdc)
    return arr, ok


def grab_screen_region(hwnd):
    """退回方案：从屏幕抓该窗口矩形（仅当前台/未被遮挡时有效）。"""
    r = wt.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    hdc = u32.GetDC(0)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    gdi32.BitBlt(mem, 0, 0, w, h, hdc, r.left, r.top, 0x00CC0020)
    arr = _dib(mem, bmp, w, h)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    u32.ReleaseDC(0, hdc)
    return arr


def find_main():
    hit = []

    def cb(hwnd, _):
        n = u32.GetWindowTextLengthW(hwnd)
        if not n or not u32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u32.GetWindowTextW(hwnd, buf, n + 1)
        if buf.value == "Easy Reality AutoVJ":
            hit.append(hwnd)
        return True

    u32.EnumWindows(ENUMPROC(cb), 0)
    return hit[0] if hit else None


def win_pid(hwnd):
    pid = wt.DWORD(0)
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def save_png(arr, out):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QImage
    h, w, _ = arr.shape
    rgb = np.ascontiguousarray(arr[:, :, ::-1].astype(np.uint8))
    QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy().save(out)
    return out


def main():
    # ⚠ 本脚本是**人工取证工具**（长时间采样抓冻结现场），不是自测脚本：
    #   `outdir` 里的截图**就是产物**（下面 `print("输出目录:", outdir)` 给人看）。
    #   ⇒ **有意不做 `finally` 清理** —— 自动删会把用户刚要看的取证图销毁。
    #   本项目的「测试夹具必须清理」铁律不适用于此类工具。
    #   建议显式传参指定输出目录：`python tools/_catch_freeze.py <分钟> <目录>`
    mins = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
    outdir = sys.argv[2] if len(sys.argv) > 2 else tempfile.mkdtemp(prefix="avj_catch_")
    print("输出目录:", outdir, flush=True)
    t_end = time.time() + mins * 60
    prev = None
    streak = 0
    longest = 0.0
    events = 0
    samples = 0
    mode = None
    while time.time() < t_end:
        hwnd = find_main()
        if not hwnd:
            print("窗口没了，结束", flush=True)
            break
        if mode is None:
            full, ok = grab_hwnd(hwnd)
            dark = float(full.mean())
            mode = "printwindow" if (ok and dark > 3.0) else "screen"
            print("抓取方式: %s（PrintWindow ok=%s 平均亮度=%.1f）" % (mode, ok, dark), flush=True)
            if mode == "screen":
                save_png(full, os.path.join(outdir, "probe_full.png"))
        if mode == "printwindow":
            full, _ = grab_hwnd(hwnd)
        else:
            if u32.GetForegroundWindow() != hwnd:
                time.sleep(0.5)
                continue
            full = grab_screen_region(hwnd)
        h, w = full.shape[:2]
        cur = full[int(h * FY0):int(h * FY1), int(w * FX0):int(w * FX1)]
        samples += 1
        if prev is not None and prev.shape == cur.shape:
            d = float(np.mean(np.abs(cur - prev)))
            if d < 0.8:
                streak += 1
                longest = max(longest, streak * 0.5)
                if streak == FREEZE_N:
                    events += 1
                    ts = time.strftime("%H:%M:%S")
                    stem = os.path.join(outdir, "freeze_%s_%d" % (ts.replace(":", ""), events))
                    save_png(cur, stem + "_preview.png")
                    save_png(full, stem + "_win.png")
                    with open(stem + "_pyspy.txt", "w", encoding="utf-8") as fh:
                        try:
                            fh.write(subprocess.run(
                                [PYSPY, "dump", "--pid", str(win_pid(hwnd)), "--locals"],
                                capture_output=True, text=True, timeout=60).stdout)
                        except Exception as e:                      # noqa: BLE001
                            fh.write("py-spy 失败: %s\n" % e)
                    try:
                        with open(STALL, "r", encoding="utf-8", errors="replace") as fh:
                            tail = fh.readlines()[-6:]
                        with open(stem + "_stall.txt", "w", encoding="utf-8") as fh:
                            fh.writelines(tail)
                    except Exception:                              # noqa: BLE001
                        pass
                    print("[%s] !! 预览区连续 %.1fs 不动（diff=%.2f）⇒ 已抓取证 %s"
                          % (ts, streak * 0.5, d, stem), flush=True)
            else:
                streak = 0
            if samples % 60 == 0:
                print("[%s] 采样 %d 次  最长不动 %.1fs  冻结次数 %d  (diff=%.2f)"
                      % (time.strftime("%H:%M:%S"), samples, longest, events, d), flush=True)
        prev = cur
        time.sleep(0.5)
    print("=== 结束：采样 %d 次，最长连续不动 %.1fs，抓到冻结事件 %d 次 ==="
          % (samples, longest, events), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
