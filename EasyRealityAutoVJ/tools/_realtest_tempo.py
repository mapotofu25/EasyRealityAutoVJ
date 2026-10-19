# -*- coding: utf-8 -*-
"""**真机实测：音乐变速（加速 / 减速）**。

用户要求：「音乐变速测试一下 加速减速都试试。有问题的话真机测试 修好再给我」

做法（和 `_realtest_all3.py` 同一套真机台架，不模拟）：
  · 真 exe（打包版）启动
  · 用 `sounddevice` 把 **SOLA 时间伸缩后**的音频真放到声卡（被 WASAPI loopback 采集）
  · 真指纹库认歌 → 真码表 → 真输出 → 截 Qt 窗口图
  · 解析 `cue_health.log`（`_cue_diag`，2s 一行）

四个相位各**独立重启** exe（冷启动，互不污染）：
  1.20× / 1.10×（加速）  0.90× / 0.80×（减速），揽月 140 BPM

判据（每相位，**只统计"已装表并开始出画面"之后**的采样）：
  1. `sid` 以揽月为主（允许识别瞬断，但不得被别的歌顶掉）
  2. `dmax`（子层 |play_sec − 播放器位置|）≤ 0.6s
  3. `sub_active` 不许掉到 0（掉了 = 码表闪出普通图层）
  4. 位置速率 Δpb/Δt ≈ (bpm/60)×rate，超 1.4 倍算"窜动"
  5. `r=`（位置层实测音乐倍率）必须收敛到 ≈rate —— 这条**直接证明**前馈在起作用
  6. 下发倍速 `spd` 必须跟着 rate 走（中位数落在 rate×(1±RATE_MAX) 内）
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
DIST = os.path.join(ROOT, os.environ.get("AUTOVJ_DIST", "dist_r36"), "EasyRealityAutoVJ")
EXE = os.path.join(DIST, "EasyRealityAutoVJ.exe")
LOCAL = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ")
LOG = os.path.join(LOCAL, "cue_health.log")
SHOTS = os.path.join(ROOT, "tools", "_cueshots")
TEMPO = os.path.join(ROOT, "tools", "_tempo")

BPM = 140.00
SID_EXPECT = 346225955          # 揽月 - Dreamira（离线核对得到）
PLAY_SEC = 90.0                 # 变速下"首次装表"可能花 30~45s（票数低、确认慢），必须留够

# 相位 = (标签, [(wav, 从几秒开始, 放多久), ...], 末段变速比, 期望拍速率上限用的比值)
#  · 单段 = 恒定变速；两段 = **播放中途变速**（DJ 推拉速度）。
#  · 两段拼接点按"原曲秒相同"算：前段 t 秒 ⇒ 原曲 180 + t·r1；后段从 t·r1/r2 起 ⇒ 同一处。
PLAN = [
    ("加速 1.20x", [("lanyue_1.20.wav", 0.0, PLAY_SEC)], 1.20, 1.20),
    ("加速 1.10x", [("lanyue_1.10.wav", 0.0, PLAY_SEC)], 1.10, 1.10),
    ("减速 0.90x", [("lanyue_0.90.wav", 0.0, PLAY_SEC)], 0.90, 0.90),
    ("减速 0.80x", [("lanyue_0.80.wav", 0.0, PLAY_SEC)], 0.80, 0.80),
    # ← 中途变速：先 30s 按 1.10，再 50s 按 1.20（拼接处原曲位置连续）
    ("中途变速 1.10→1.20",
     [("lanyue_1.10.wav", 0.0, 30.0), ("lanyue_1.20.wav", 30.0 * 1.10 / 1.20, 50.0)],
     1.20, 1.20),
]

ROW = re.compile(r"\[(\d\d):(\d\d):(\d\d)\].*?sid=(-?\d+).*?status=(\w+) pb=(\S+).*?"
                 r"sub=(\d+)/(\d+) dmax=(-?[\d.]+).*?spd=(\S+) fw=(\d) why=(\S+)"
                 r"(?: r=([\d.]+))?(?: dark=([\d.]+)/(\d+))?")


def now_s():
    t = time.localtime()
    return t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec


def parse_all():
    try:
        with io.open(LOG, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        m = ROW.search(ln)
        if not m:
            continue
        hh, mm, ss, sid, st, pb, ns, na, dm, spd, fw, why, r, dk, dn = m.groups()
        out.append({"t": int(hh) * 3600 + int(mm) * 60 + int(ss), "sid": int(sid),
                    "st": st, "pb": None if pb == "None" else float(pb),
                    "ns": int(ns), "na": int(na), "dmax": float(dm),
                    "spd": spd, "fw": int(fw), "why": why,
                    "r": float(r) if r is not None else None,
                    "dark": float(dk) if dk is not None else None,
                    "darkn": int(dn) if dn is not None else None})
    return out


def play(path, stop, dur, start=0.0):
    import numpy as np
    import sounddevice as sd
    import soundfile as sf
    sr = sf.info(path).samplerate
    data, _sr = sf.read(path, dtype="float32", always_2d=True, start=int(start * sr),
                        frames=int(dur * sr) + 1)
    print("   播放 %s 从 %.1fs 起 %.1fs（%d Hz）"
          % (os.path.basename(path), start, len(data) / _sr, _sr))
    sd.play(np.ascontiguousarray(data), _sr)
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


def kill():
    subprocess.call(["taskkill", "/F", "/IM", "EasyRealityAutoVJ.exe"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def one_phase(label, segs, rate_end, rate_peak, idx):
    """跑一个相位，返回 (ok, 报告行列表)。`segs` = [(wav, 起始秒, 时长), ...]。"""
    for wav, _s, _d in segs:
        if not os.path.isfile(os.path.join(TEMPO, wav)):
            return False, ["!! 缺素材 %s" % os.path.join(TEMPO, wav)]
    print("\n=== [%d/%d] %s ===" % (idx + 1, len(PLAN), label))
    kill()
    time.sleep(1.0)
    t0 = now_s() + 1                     # 只统计这之后的取证行
    proc = subprocess.Popen([EXE], cwd=DIST)
    time.sleep(22)
    hwnd = find_hwnd()
    print("   窗口 hwnd=%s" % hwnd)
    stop = threading.Event()
    for i, (wav, start, dur) in enumerate(segs):
        # 段间无缝：上一段放完立刻起下一段（`sd.play` 会先停掉上一段）
        th = threading.Thread(target=play, args=(os.path.join(TEMPO, wav), stop, dur, start),
                              daemon=True)
        th.start()
        t_seg = time.time()
        shot_done = False
        while time.time() - t_seg < dur:
            time.sleep(0.4)
            if (not shot_done) and hwnd and time.time() - t_seg >= dur * 0.85:
                shot_done = True
                shot(hwnd, "tempo_%d_%s_s%d.png"
                     % (idx + 1, label.split()[-1].replace(".", "").replace("→", "-"), i + 1))
        stop.clear()
    time.sleep(2.0)
    try:
        proc.terminate()
    except Exception:                                          # noqa: BLE001
        pass
    time.sleep(1.0)
    kill()

    rows = [r for r in parse_all() if r["t"] >= t0]
    if not rows:
        return False, ["!! 没读到取证行（cue_health.log）"]

    # ★ 装表期剔除：从"第一次真的出画面（sub 里有活着的层）"那一行开始算。
    #   冷启动时 `rt=0`（no_rt）是正常的装载过程，不是"闪出"。
    k0 = next((i for i, r in enumerate(rows) if r["na"] > 0), None)
    if k0 is None:
        return False, ["!! 全程没出过画面（na 一直为 0）；why 分布：%s"
                       % {r["why"] for r in rows}]
    warm = rows[k0:]
    skipped = k0

    sid_seen = [r["sid"] for r in warm if r["sid"] >= 0]
    n_ok_sid = sum(1 for s in sid_seen if s == SID_EXPECT)
    others = sorted({s for s in sid_seen if s != SID_EXPECT})
    act = [r["na"] for r in warm]

    # ── 重定位瞬态（野匹配自愈 / 真跳转）──
    #   位置层允许"大跳变"直接改锚（那是**修正**，不是 bug）；但这一瞬间 `play_sec` 会跳，
    #   `dmax` 必然尖峰。判"稳态质量"必须把重定位前后各 2 个采样剔掉，否则每次自愈都会被
    #   当成"窜动"。重定位次数另外单独计数（太多才说明指纹不稳）。
    #   ⚠ 判据必须用**速率**（拍/秒），不能用"Δ拍"：正常 2s 采样本来就走 ~4.7 拍，
    #     拿 2 拍当阈值会把每一次正常推进都判成跳变（2026-10-18 踩过）。
    exp_v = BPM / 60.0 * rate_peak       # 期望拍/秒（按最快的那一段）
    repos = []
    for i in range(1, len(warm)):
        ra, rb = warm[i - 1], warm[i]
        a, b = ra["pb"], rb["pb"]
        if a is None or b is None:
            continue
        dt = rb["t"] - ra["t"]
        if dt <= 0:
            continue
        v = (b - a) / dt
        if v > max(6.0, exp_v * 2.2) or v < -1.0:
            repos.append(i)
    drop = set()
    for i in repos:
        for j in range(i - 2, i + 3):
            if 0 <= j < len(warm):
                drop.add(j)
    steady = [r for k, r in enumerate(warm) if k not in drop]

    dms = [r["dmax"] for r in steady if r["dmax"] >= 0]
    whys = {}
    for r in warm:
        whys[r["why"]] = whys.get(r["why"], 0) + 1

    rate_max = 0.0
    rate_bad = 0
    for a, b in zip(steady, steady[1:]):
        if a["pb"] is None or b["pb"] is None:
            continue
        dt = b["t"] - a["t"]
        if dt <= 0:
            continue
        v = (b["pb"] - a["pb"]) / dt
        rate_max = max(rate_max, v)
        if v > exp_v * 1.4:
            rate_bad += 1

    # 下发倍速（每个采样取各子层的 [lo, hi]，中位数看整体水平）
    lo, hi = 9.9, -9.9
    mids = []
    for r in warm:
        try:
            a, b = r["spd"].split("~")
            a, b = float(a), float(b)
        except ValueError:
            continue
        lo, hi = min(lo, a), max(hi, b)
        mids.append((a + b) / 2.0)
    mids.sort()
    spd_med = mids[len(mids) // 2] if mids else -1.0

    # 实测音乐倍率 r（位置层前馈基准）
    rs = [r["r"] for r in warm if r["r"] is not None]
    r_tail = rs[-5:] if len(rs) >= 5 else rs
    r_end = (sum(r_tail) / len(r_tail)) if r_tail else -1.0

    _dk = [r for r in warm if r.get("dark") is not None]
    dark_max = _dk[-1]["dark"] if _dk else -1.0
    dark_n = _dk[-1]["darkn"] if _dk else -1
    rep = ["  采样 %d 行（剔除装表期 %d 行，装表 %ds）；稳态 %d 行；sid 命中 %d/%d，其它 sid=%s"
           % (len(warm), skipped, skipped * 2, len(steady), n_ok_sid, len(sid_seen),
              others or "无"),
           "  重定位 %d 次（野匹配自愈，已从稳态判据里剔除）%s"
           % (len(repos), "" if not repos else " 位置：%s" % [warm[i]["t"] for i in repos]),
           "  dmax（稳态）：max=%.2fs mean=%.2fs  >0.6 的点=%d"
           % (max(dms) if dms else -1,
              (sum(dms) / len(dms)) if dms else -1,
              sum(1 for x in dms if x > 0.6)),
           "  sub_active：min=%d  ==0 的点=%d" % (min(act), sum(1 for a in act if a == 0)),
           "  why 分布：%s" % whys,
           "  位置速率（稳态）：max=%.2f 拍/秒（期望≈%.2f）；>1.4 倍的点=%d"
           % (rate_max, exp_v, rate_bad),
           "  下发倍速：%.2f~%.2f（中位 %.2f；本相位需要 ≈%.2f）" % (lo, hi, spd_med, rate_end),
           "  实测音乐倍率 r：末段均值 %.3f（真值 %.2f，误差 %+.3f）"
           % (r_end, rate_end, r_end - rate_end),
           "  ★ 帧级暗场（码表该出画却没出）：最长 %.3fs，≥50ms 的次数 %d"
           % (dark_max, dark_n),
           "  自由运行 fw=1 的点：%d/%d" % (sum(1 for r in warm if r["fw"]), len(warm))]

    bad = []
    if n_ok_sid < max(3, len(sid_seen) * 0.8):
        bad.append("认歌不稳：揽月 %d/%d，混入 %s" % (n_ok_sid, len(sid_seen), others))
    if len(steady) < 8:
        bad.append("稳态采样不足（%d 行）—— 装表太慢或播放太短" % len(steady))
    if dms and max(dms) > 0.6:
        bad.append("稳态 dmax 超 0.6（%.2fs）" % max(dms))
    if min(act) == 0:
        bad.append("码表闪出（sub_active=0）")
    if len(repos) > 3:
        bad.append("重定位过多 %d 次（指纹野匹配太频繁）" % len(repos))
    if dark_max > 0.12:
        bad.append("帧级暗场 %.2fs（>0.12s 用户看得见）" % dark_max)
    if rate_bad:
        bad.append("位置窜动 %d 次" % rate_bad)
    if abs(r_end - rate_end) > 0.06:
        bad.append("前馈倍率没跟上（r=%.3f，应≈%.2f）" % (r_end, rate_end))
    if not (rate_end * 0.75 <= spd_med <= rate_end * 1.25):
        bad.append("下发倍速中位 %.2f 不在 %.2f×(1±25%%) 内" % (spd_med, rate_end))
    rep.append("  → %s" % ("✅ 过" if not bad else "❌ " + "；".join(bad)))
    return (not bad), rep


def warmup_once():
    """预热一遍 exe + 音频：首次启动要付"新 exe 被 Defender 扫描 + 模型冷加载"的钱
    （实测第 1 相位装表要 60s+，后面只要 20s）⇒ 先跑一遍不计分的，让后面每个相位公平。"""
    print("预热：启动 exe 并放 25s 音频（不计分）…")
    kill()
    time.sleep(1.0)
    proc = subprocess.Popen([EXE], cwd=DIST)
    time.sleep(22)
    stop = threading.Event()
    th = threading.Thread(target=play, args=(os.path.join(TEMPO, "lanyue_1.10.wav"), stop, 25.0, 0.0),
                          daemon=True)
    th.start()
    time.sleep(25.0)
    stop.set()
    time.sleep(1.5)
    try:
        proc.terminate()
    except Exception:                                          # noqa: BLE001
        pass
    time.sleep(1.0)
    kill()
    time.sleep(1.0)


def main():
    if not os.path.isfile(EXE):
        print("!! 缺 exe：%s" % EXE)
        return 1
    os.makedirs(SHOTS, exist_ok=True)
    print("台架：%s" % EXE)
    warmup_once()
    only = None
    if len(sys.argv) > 1:
        only = [int(x) for a in sys.argv[1:] for x in a.replace(",", " ").split() if x]
        print("只跑相位：%s" % [PLAN[i][0] for i in only])
    results = []
    for i, (label, segs, rate_end, rate_peak) in enumerate(PLAN):
        if only is not None and i not in only:
            continue
        ok, rep = one_phase(label, segs, rate_end, rate_peak, i)
        results.append((label, ok, rep))
    print("\n================ 汇总 ================")
    for label, ok, rep in results:
        print("\n── %s ──" % label)
        for ln in rep:
            print(ln)
    ok_all = all(ok for _l, ok, _r in results)
    print("\n总判定（共 %d 相）：%s" % (len(results), "✅ 全过" if ok_all else "❌ 有问题"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
