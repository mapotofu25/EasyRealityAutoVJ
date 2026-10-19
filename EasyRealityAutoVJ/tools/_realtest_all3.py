# -*- coding: utf-8 -*-
"""**真机实测三首歌**（用户要求：不要脚本模拟，要真机）。

流程：启动打包 exe → 依次用**声卡真放**三首歌（各从编排区间内开始）→
      全程抓 `cue_health.log` + 截真窗口图 → 按曲目分组统计。

判据（每首，只统计"已锁定该曲目"之后）：
  · `dmax` 最大偏差（>0.6 记一次"大偏差"）
  · `sub_active` 是否掉到 0（= 码表闪出普通图层）
  · `why` 分布（`no_rt`/`no_pos`/`hidden` = 三种"没接管"的原因）
  · 位置速率：Δpb/Δt 应 ≈ 1/beat_len；明显 >1.3× 说明"切来切去"（位置在往前窜）
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
DIST = os.path.join(ROOT, os.environ.get("AUTOVJ_DIST", "dist_r28"), "EasyRealityAutoVJ")
EXE = os.path.join(DIST, "EasyRealityAutoVJ.exe")
LOCAL = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ")
LOG = os.path.join(LOCAL, "cue_health.log")
SHOTS = os.path.join(ROOT, "tools", "_cueshots")
PKG = r"Y:/EasyRealityAutoVJ/码表素材/测试.ercue"
BASE = r"Y:/EasyRealityAutoVJ/码表素材"

# (曲目关键字, 音频文件, 起始秒, 放多久)
# (名称, 音频文件, 起始秒, 放多久, bpm)
PLAN = [
    ("揽月", "揽月 - Dreamira.flac", 200.0, 40.0, 140.00),
    ("西行谣", "西行谣 feat. 凛花 - Sociable State, 凛花, Northad, DJ Tranceair.flac",
     30.0, 40.0, 142.00),
    ("Nimbus", "Nimbus Spark 1999 - Supa7onyz.flac", 40.0, 40.0, 143.55),
]
# sid → bpm（判"位置速率"用；正常播放 = bpm/60 拍/秒）
SID_BPM = {}


def read_new(path, skip):
    try:
        with io.open(path, encoding="utf-8", errors="replace") as f:
            rows = f.readlines()
        return rows[skip:]
    except OSError:
        return []


def parse(rows):
    out = []
    for ln in rows:
        m = re.match(r"\[(\d\d):(\d\d):(\d\d)\].*?sid=(-?\d+).*?status=(\w+) pb=(\S+).*?"
                     r"sub=(\d+)/(\d+) dmax=(-?[\d.]+).*?spd=(\S+) fw=(\d) why=(\S+)", ln)
        if not m:
            continue
        hh, mm, ss, sid, st, pb, ns, na, dm, spd, fw, why = m.groups()
        out.append({"t": int(hh) * 3600 + int(mm) * 60 + int(ss), "sid": int(sid),
                    "st": st, "pb": None if pb == "None" else float(pb),
                    "ns": int(ns), "na": int(na), "dmax": float(dm),
                    "spd": spd, "fw": int(fw), "why": why})
    return out


def play(path, start, dur, stop):
    import numpy as np
    import sounddevice as sd
    import soundfile as sf
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    i0 = max(0, int(start * sr))
    seg = data[i0:i0 + int(dur * sr)]
    sd.play(np.ascontiguousarray(seg), sr)
    t0 = time.time()
    while not stop.is_set() and time.time() - t0 < dur:
        time.sleep(0.2)
    sd.stop()


def shot(hwnd, name):
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        pm = app.primaryScreen().grabWindow(int(hwnd))
        p = os.path.join(SHOTS, name)
        pm.save(p, "PNG")
        return os.path.getsize(p)
    except Exception:                                          # noqa: BLE001
        return -1


def find_hwnd():
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
            if "Easy Reality AutoVJ" in b.value:
                found[b.value] = h
        return True
    u32.EnumWindows(ENUMPROC(cb), 0)
    return next(iter(found.values()), None)


def main():
    if not os.path.isfile(EXE):
        print("!! 缺 exe：%s" % EXE)
        return 1
    os.makedirs(SHOTS, exist_ok=True)
    # ⚠ 不能用"行号"当基线：日志是**环形裁剪**的（写满就砍掉前面），行号会漂。
    #   改用**时刻**：只统计启动这一刻之后的取证行。
    _lt = time.localtime()
    _t0s = _lt.tm_hour * 3600 + _lt.tm_min * 60 + _lt.tm_sec
    subprocess.call(["taskkill", "/F", "/IM", "EasyRealityAutoVJ.exe"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("启动 exe …")
    proc = subprocess.Popen([EXE], cwd=DIST)
    time.sleep(22)
    hwnd = find_hwnd()
    print("窗口 hwnd=%s" % hwnd)

    for i, (name, fn, start, dur, _bpm) in enumerate(PLAN):
        path = os.path.join(BASE, fn)
        if not os.path.isfile(path):
            print("!! 缺音频 %s" % path)
            continue
        print("\n=== [%d/3] %s：从 %.0fs 放 %.0fs ===" % (i + 1, name, start, dur))
        stop = threading.Event()
        th = threading.Thread(target=play, args=(path, start, dur, stop), daemon=True)
        th.start()
        time.sleep(dur * 0.5)
        if hwnd:
            shot(hwnd, "all3_%d_%s.png" % (i + 1, name))
        time.sleep(dur * 0.5 + 1.0)
        stop.set()
        time.sleep(4.0)          # 让它察觉"丢了"，切到下一首

    time.sleep(4)
    try:
        proc.terminate()
    except Exception:                                          # noqa: BLE001
        pass
    time.sleep(1.5)
    subprocess.call(["taskkill", "/F", "/IM", "EasyRealityAutoVJ.exe"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    rows = parse(read_new(LOG, 0))
    rows = [r for r in rows if r["t"] >= _t0s]
    if not rows:
        print("!! 没读到取证行")
        return 1
    # 按**出现顺序**把 sid 对到 PLAN 的名字（测试就是按 PLAN 顺序放的）
    _seen = []
    for r in rows:
        if r["sid"] >= 0 and r["sid"] not in _seen:
            _seen.append(r["sid"])
    for _i, _sid in enumerate(_seen[:len(PLAN)]):
        SID_BPM[_sid] = PLAN[_i][0]
    by_sid = {}
    for r in rows:
        if r["sid"] >= 0:
            by_sid.setdefault(r["sid"], []).append(r)

    print("\n================ 结果 ================")
    ok_all = True
    for sid, rs in by_sid.items():
        # ★ 装表期剔除：从"第一次真的出画面（sub 里有活着的层）"那一行开始算。
        #   冷启动 `rt=0`（no_rt）是正常装载过程，不是"闪出"（用固定"跳过前 5 个采样"
        #   会随启动快慢误报，2026-10-18 变速测试就踩过）。
        _k0 = next((i for i, r in enumerate(rs) if r["na"] > 0), None)
        if _k0 is None:
            print("\n── sid=%s（全程没出过画面！）──" % sid)
            ok_all = False
            continue
        rs = rs[_k0:]
        _exp = next((b / 60.0 for n, _f, _s, _d, b in PLAN if SID_BPM.get(sid) == n), 2.33)
        dms = [r["dmax"] for r in rs if r["dmax"] >= 0]
        act = [r["na"] for r in rs]
        whys = {}
        for r in rs:
            whys[r["why"]] = whys.get(r["why"], 0) + 1
        # 位置速率（拍/秒）：正常应 ≈ 1/beat_len
        rate_bad = 0
        rate_max = 0.0
        for a, b in zip(rs, rs[1:]):
            if a["pb"] is None or b["pb"] is None:
                continue
            dt = b["t"] - a["t"]
            if dt <= 0:
                continue
            v = (b["pb"] - a["pb"]) / dt
            rate_max = max(rate_max, v)
            if v > _exp * 1.4:      # 正常 = bpm/60 拍/秒；超 1.4 倍才算"位置在窜"
                rate_bad += 1
        print("\n── sid=%s（%d 个采样）──" % (sid, len(rs)))
        print("  dmax：max=%.2fs mean=%.2fs  >0.6 的点=%d"
              % (max(dms) if dms else -1,
                 (sum(dms) / len(dms)) if dms else -1,
                 sum(1 for x in dms if x > 0.6)))
        print("  sub_active：min=%d  ==0 的点=%d" % (min(act), sum(1 for a in act if a == 0)))
        print("  why 分布：%s" % whys)
        print("  位置速率：max=%.2f 拍/秒（正常≈%.2f）；>1.4 倍的点=%d"
              % (rate_max, _exp, rate_bad))
        bad = []
        if dms and max(dms) > 0.6:
            bad.append("dmax 超 0.6")
        if min(act) == 0:
            bad.append("码表闪出（sub_active=0）")
        if rate_bad:
            bad.append("位置窜动 %d 次" % rate_bad)
        print("  → %s" % ("✅ 过" if not bad else "❌ " + "；".join(bad)))
        ok_all = ok_all and not bad
    print("\n总判定：%s" % ("✅ 三首全过" if ok_all else "❌ 仍有问题"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
