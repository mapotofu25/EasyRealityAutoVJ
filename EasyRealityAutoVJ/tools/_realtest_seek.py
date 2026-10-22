# -*- coding: utf-8 -*-
"""真机「手动拖动」测试 —— 用户 2026-10-18：

> "我音频播到五分钟，然后我手动突然跳到三分钟，视频半天都不跳的"

流程：真 exe + 点开始 VJ + 声卡真放《西行谣》
  · 先放 200s 处 25 秒
  · 再**突然跳到 60s** 处放 20 秒（模拟手动拖回 140 秒）
判据：跳转发生后，码表位置（`pb`）要在 **4 秒内**跟上新的 offset（偏差 <1.5 秒）。
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
DIST = os.path.join(ROOT, os.environ.get("AUTOVJ_DIST", "dist_r66"), "EasyRealityAutoVJ")
EXE = os.path.join(DIST, "EasyRealityAutoVJ.exe")
LOG = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "cue_health.log")
AUDIO = r"Y:/EasyRealityAutoVJ/码表素材/西行谣 feat. 凛花 - Sociable State, 凛花, Northad, DJ Tranceair.flac"
SID = 3112469840
BL = 60.0 / 142.0
A_FROM, A_DUR = 200.0, 45.0
B_FROM, B_DUR = 60.0, 30.0

ROW = re.compile(r"\[(\d\d):(\d\d):(\d\d)\].*?sid=(-?\d+).*?off=(-?[\d.]+) status=(\w+) pb=(\S+)")


def now_s():
    t = time.localtime()
    return t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec


def play(path, stop, dur, start):
    import numpy as np
    import sounddevice as sd
    import soundfile as sf
    sr = sf.info(path).samplerate
    data, _sr = sf.read(path, dtype="float32", always_2d=True,
                        start=int(start * sr), frames=int(dur * sr) + 1)
    print("   播放 %.0fs 起 %.0fs" % (start, dur))
    sd.play(np.ascontiguousarray(data), _sr)
    t = time.time()
    while not stop.is_set() and time.time() - t < dur:
        time.sleep(0.2)
    sd.stop()


def kill():
    subprocess.call(["taskkill", "/F", "/IM", "EasyRealityAutoVJ.exe"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def lh_size():
    p = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "layer_health.log")
    try:
        return os.path.getsize(p)
    except OSError:
        return -1


def ensure_vj(hwnd):
    a = lh_size()
    time.sleep(1.5)
    if lh_size() > a:
        print("   VJ 已在运行")
        return True
    import ctypes
    u32 = ctypes.windll.user32
    u32.SetForegroundWindow(int(hwnd))
    u32.keybd_event(0x20, 0, 0, 0)
    time.sleep(0.06)
    u32.keybd_event(0x20, 0, 2, 0)
    time.sleep(2.5)
    c = lh_size()
    time.sleep(1.5)
    ok = lh_size() > c
    print("   %s" % ("✅ 已开始 VJ" if ok else "!! 没起来"))
    return ok


def find_hwnd(title=""):
    import ctypes
    import ctypes.wintypes as wt
    u32 = ctypes.windll.user32
    ENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    found = {}

    def cb(h, _):
        n = u32.GetWindowTextLengthW(h)
        if n:
            b = ctypes.create_unicode_buffer(n + 1)
            u32.GetWindowTextW(h, b, n + 1)
            if title in b.value:
                found[b.value] = h
        return True
    u32.EnumWindows(ENUMPROC(cb), 0)
    return next(iter(found.values()), None)


def main():
    if not os.path.isfile(EXE):
        print("!! 缺 exe：%s" % EXE)
        return 1
    kill()
    time.sleep(1.0)
    t0 = now_s()
    print("启动 exe …")
    proc = subprocess.Popen([EXE], cwd=DIST)
    time.sleep(22)
    if not ensure_vj(find_hwnd("Easy Reality AutoVJ")):
        return 1

    stop = threading.Event()
    th = threading.Thread(target=play, args=(AUDIO, stop, A_DUR, A_FROM), daemon=True)
    th.start()
    time.sleep(A_DUR)
    t_jump = now_s()
    print("   ⇒ 此刻手动跳到 %.0fs" % B_FROM)
    th2 = threading.Thread(target=play, args=(AUDIO, stop, B_DUR, B_FROM), daemon=True)
    th2.start()
    time.sleep(B_DUR + 1.0)
    stop.set()
    time.sleep(1.5)
    try:
        proc.terminate()
    except Exception:                                          # noqa: BLE001
        pass
    time.sleep(1.0)
    kill()

    rows = []
    for ln in io.open(LOG, encoding="utf-8", errors="replace"):
        if "[R]" in ln:
            continue
        m = ROW.search(ln)
        if not m:
            continue
        hh, mm, ss, sid, off, st, pb = m.groups()
        t = int(hh) * 3600 + int(mm) * 60 + int(ss)
        if t < t0 or int(sid) != SID or pb == "None":
            continue
        rows.append((t, float(off), float(pb) * BL))

    print("\n采样 %d 行（含跳转前后）" % len(rows))
    if not rows:
        return 1
    # 找跳转：off 单次变化 > 50 秒
    j = None
    for i in range(1, len(rows)):
        if abs(rows[i][1] - rows[i - 1][1]) > 50.0:
            j = i
            break
    if j is None:
        print("!! 日志里没抓到跳转（可能没识别到）")
        return 1
    print("跳转发生在 %02d:%02d:%02d，off %.1f → %.1f"
          % (rows[j][0] // 3600, (rows[j][0] % 3600) // 60, rows[j][0] % 60,
             rows[j - 1][1], rows[j][1]))
    for t, off, pos in rows[j:j + 8]:
        print("   +%2ds  off=%.1f  pos=%.1f  偏差 %+.1fs"
              % (t - rows[j][0], off, pos, pos - off))
    hit = next((t - rows[j][0] for t, off, pos in rows[j:] if abs(pos - off) <= 1.5), None)
    print("\n跟上耗时：%s 秒" % (hit if hit is not None else "未跟上"))
    ok = hit is not None and hit <= 4.0
    print("结论：%s" % ("✅ 拖动后 %.0f 秒内跟上（画面跟随跳转）" % hit if ok
                      else "❌ 拖动后没跟上/超过 4 秒"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
