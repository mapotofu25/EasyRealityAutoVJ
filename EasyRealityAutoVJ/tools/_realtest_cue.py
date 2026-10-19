# -*- coding: utf-8 -*-
"""**真机联调**：启动打包好的 exe + 真的放一首歌（系统声卡回放）+ 全程取证。

为什么必须有这个（用户质疑"你自己有真机测试吗"）：
  之前的验证分三类，**都不等于真机**：
    · 单元测试（假时钟、假播放器）—— 只验逻辑；
    · `_cue_real_probe.py`（offscreen + 真解码器）—— 有真解码，但**位置源是造的**，
      没有真指纹、没有真采集、没有真窗口；
    · `smoke_exe.py` —— 只验"进程活过 40 秒"。
  ⇒ 本脚本把**整条链路**跑起来：真 exe → 真 WASAPI 采集 → 真指纹 → 真码表 → 真输出，
    同时逐秒抓 `cue_health.log` 与窗口截图。

用法：`python tools/_realtest_cue.py [秒数] [音频文件]`
判据：dmax 全程 < 0.60；sub_active 从不掉到 0（除非曲目真结束）；截图不是纯黑。
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
DIST = os.path.join(ROOT, os.environ.get("AUTOVJ_DIST", "dist_r24"), "EasyRealityAutoVJ")
EXE = os.path.join(DIST, "EasyRealityAutoVJ.exe")
LOCAL = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ")
LOG = os.path.join(LOCAL, "cue_health.log")
SHOTS = os.path.join(ROOT, "tools", "_cueshots")
AUDIO = (sys.argv[2] if len(sys.argv) > 2 else
         r"Y:/EasyRealityAutoVJ/码表素材/揽月 - Dreamira.flac")


def log_tail(n=400, skip=0):
    try:
        with io.open(LOG, encoding="utf-8", errors="replace") as f:
            rows = f.readlines()
        return rows[skip:] if skip else rows[-n:]
    except OSError:
        return []


def parse(rows):
    """→ [(t, sid, status, pb, sub_states, sub_active, dmax, spd, fw), ...]"""
    out = []
    for ln in rows:
        m = re.match(r"\[(\d\d:\d\d:\d\d)\].*?sid=(-?\d+).*?status=(\w+) pb=(\S+).*?"
                     r"sub=(\d+)/(\d+) dmax=(-?[\d.]+).*?why=(\S+)", ln)
        if m:
            t, sid, st, pb, ns, na, dm, why = m.groups()
            fw = 0
            m2 = re.search(r"fw=(\d)", ln)
            if m2:
                fw = int(m2.group(1))
            out.append((t, int(sid), st, pb, int(ns), int(na), float(dm), fw, why))
    return out


def play(path, stop_evt, start_sec=0.0):
    """用系统默认输出放音频（WASAPI loopback 会被 exe 采集到）。

    ⚠ `start_sec`：码表编排只覆盖歌的一部分（揽月是 191~342s），
      从头放会一直 `sub=3/0`（编排里这段没内容）—— 那是**正确行为**，不是 bug。
      真机测试要从编排区间里开始放。
    """
    import numpy as np
    import sounddevice as sd
    import soundfile as sf
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    i0 = max(0, int(float(start_sec) * sr))
    data = data[i0:]
    print("  从 %.0fs 开始放（共 %.0fs）" % (start_sec, len(data) / float(sr)))
    sd.play(np.ascontiguousarray(data), sr)
    while not stop_evt.is_set():
        time.sleep(0.2)
    sd.stop()


def shot(hwnd, name):
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        scr = app.primaryScreen()
        pm = scr.grabWindow(int(hwnd))
        p = os.path.join(SHOTS, name)
        pm.save(p, "PNG")
        return os.path.getsize(p)
    except Exception as e:                                     # noqa: BLE001
        print("  截图失败：%s: %s" % (type(e).__name__, e))
        return -1


def find_hwnd(title_sub="Easy Reality AutoVJ"):
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
            if title_sub in b.value:
                found[b.value] = h
        return True
    u32.EnumWindows(ENUMPROC(cb), 0)
    return found


def main():
    dur = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0
    if not os.path.isfile(EXE):
        print("!! 找不到 exe：%s" % EXE)
        return 1
    os.makedirs(SHOTS, exist_ok=True)
    # ⚠ 不删日志（删除会被安全钩子拦）：记住当前行数，只统计**新增**的行
    try:
        with io.open(LOG, encoding="utf-8", errors="replace") as f:
            _base_lines = sum(1 for _ in f)
    except OSError:
        _base_lines = 0
    subprocess.call(["taskkill", "/F", "/IM", "EasyRealityAutoVJ.exe"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("启动：%s" % EXE)
    p = subprocess.Popen([EXE], cwd=DIST)
    time.sleep(22)                      # 启动 + 恢复码表 + 预热
    wins = find_hwnd()
    print("窗口：%s" % {k: v for k, v in wins.items()})
    hwnd = next((h for k, h in wins.items() if "自动" in k or "AutoVJ" in k), None)

    stop = threading.Event()
    _start = float(os.environ.get("REALTEST_START", "0"))
    th = threading.Thread(target=play, args=(AUDIO, stop, _start), daemon=True)
    print("开始放音：%s" % os.path.basename(AUDIO))
    th.start()

    t0 = time.time()
    shots = []
    while time.time() - t0 < dur:
        time.sleep(5)
        el = time.time() - t0
        if hwnd and int(el) % 30 < 5 and len(shots) < 6:
            n = "live_%03d.png" % int(el)
            sz = shot(hwnd, n)
            shots.append((n, sz))
            print("  +%3.0fs 截图 %s（%d 字节）" % (el, n, sz))
    stop.set()
    time.sleep(1.5)
    try:
        p.terminate()
    except Exception:                                          # noqa: BLE001
        pass
    time.sleep(2)
    subprocess.call(["taskkill", "/F", "/IM", "EasyRealityAutoVJ.exe"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    rows = parse(log_tail(skip=_base_lines))
    print("\n=== 取证（%d 个采样点）===" % len(rows))
    if not rows:
        print("!! 日志里没有 sub= 记录 ⇒ 这一轮码表根本没接管（或没识别到歌）")
        return 1
    # ★ 判据只针对"**已认出歌**之后"的样本：认歌之前 `sub=0/0`、`dmax=-1` 是正常的
    #   （exe 刚启动、码表还没装/还没认到歌）。锁定后再跳过前 10 秒预热（解码器开容器）。
    lock_i = next((i for i, r in enumerate(rows) if r[1] >= 0), None)
    if lock_i is None:
        print("!! 整轮都没认出歌 ⇒ 真机测试无效（换一首/换起点/查采集）")
        return 1
    warm = lock_i + 5                       # 5 个采样 ≈ 10 秒
    live = rows[warm:]
    if not live:
        print("!! 锁定后样本不足")
        return 1
    print("  锁定于第 %d 个采样；判据用其后 %d 个" % (lock_i, len(live)))
    rows = live
    dm = [r[6] for r in rows if r[6] >= 0]
    if not dm:
        print("!! 锁定后仍然 dmax=-1 ⇒ 这一段不在编排区间内（换起点）")
        return 1
    act = [r[5] for r in rows]
    fw = sum(1 for r in rows if r[7])
    print("  dmax：max=%.2fs  mean=%.2fs  >0.6 的点=%d/%d"
          % (max(dm), sum(dm) / len(dm), sum(1 for x in dm if x > 0.6), len(dm)))
    print("  sub_active：min=%d  ==0 的点=%d/%d" % (min(act), sum(1 for a in act if a == 0),
                                                    len(act)))
    print("  fw（自由运行）次数=%d" % fw)
    print("  状态分布：%s" % {s: sum(1 for r in rows if r[2] == s)
                              for s in set(r[2] for r in rows)})
    print("  截图：%s" % shots)
    bad = []
    if max(dm) > 0.6:
        bad.append("dmax 超 0.6（还有大偏差）")
    if min(act) == 0:
        bad.append("sub_active 掉到 0（画面曾整块消失）")
    if not shots or all(s < 20000 for _n, s in shots):
        bad.append("截图过小/纯黑")
    print("\n结论：%s" % ("✅ 全过" if not bad else "❌ " + "；".join(bad)))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
