# -*- coding: utf-8 -*-
"""第三轮 ★F 验收：GridClock 在「E 不退化 / 抖动不误判 / 真实远距离 seek」上的实测。

覆盖：
  #2 E 不退化：4 拍 Loop 连打 20s（误差<1 拍）；单次 −1/−2 拍 Beat Jump（≤1s 收敛<1 拍）；
              前向 +1/+2 拍仍跟上。
  #3 抖动不误判：±50ms 与 ±186ms（≈±0.4 拍）量化噪声 ⇒ **硬重锚 0 次**。
  #4 真实远距离 seek：旧坐标 → 新坐标（+400 拍）⇒ 在 ≤2 个识别窗内跟上。

硬重锚检测：`GridClock.update` 硬重锚时会清空单调保护的 `last_pos`（置 None）；
每步 update 后若 `last_pos is None` 即记一次硬重锚。

用法： P:\\AutoVJ\\venv\\Scripts\\python.exe tools/_fix3_f_accept.py
"""
import math
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from beatgrid import GridClock  # noqa: E402

BL = 0.5           # 120BPM ⇒ 1 拍 = 0.5s
ANCHOR = 0.0
SID = 1
TICK = 0.5         # 识别周期


def ref_pos(off):
    return (off - ANCHOR) / BL


def run(offset_at, dur, noise=0.0, seed=0):
    """跑一段识别 tick 序列。返回 (reanchors, errs:[(t,err)], locked_bad_windows)。"""
    random.seed(seed)
    clk = GridClock()
    t = 0.0
    reanchors = 0
    errs = []
    while t <= dur + 1e-9:
        off = offset_at(t) + (random.uniform(-noise, noise) if noise else 0.0)
        clk.update(SID, BL, ANCHOR, off, 1.0, t)
        # t=0 的「换歌/首次」也会清 last_pos（首次锚定，不是重锚）⇒ 从第 2 个窗起才计
        if t > 1e-9 and clk.last_pos is None:            # update 内部硬重锚 ⇒ last_pos 被清空
            reanchors += 1
        err = clk.pos(t) - ref_pos(offset_at(t))
        errs.append((t, err))
        t += TICK
    return reanchors, errs


def first_converge(errs, t0, thresh=1.0):
    """自 t0 起 |err|<thresh 首次出现的时刻；未收敛返回 None。"""
    for t, e in errs:
        if t >= t0 - 1e-9 and abs(e) < thresh:
            return t
    return None


def line(name, ok, msg):
    print("  [%s] %-26s %s" % ("PASS" if ok else "FAIL", name, msg))
    return ok


results = []

print("=" * 78)
print("#2 E 不退化（120BPM, beat_len=0.5s, 识别周期 0.5s）")
print("=" * 78)

# A) 4 拍 Loop 连打 20s（2s 循环），之后正常播
d0, d1, loop0 = 5.0, 25.0, 100.0


def loopA(t):
    if t < d0:
        return loop0 + t
    if t < d1:
        return loop0 + ((t - d0) % 2.0)
    return loop0 + (t - d1)


re, errs = run(loopA, 45.0)
win = max(abs(e) for t, e in errs if d0 - 1e-9 <= t <= d1 + 1e-9)
results.append(line("4拍Loop 连打20s", win < 1.0, "扰动窗 max|err|=%.3f 拍（<1 合格）" % win))

# B) 单次 −1 / −2 拍 Beat Jump（瞬时回跳），测收敛
for dbeats in (-1, -2):
    jt = 10.0

    def jump(t, db=dbeats, jt=jt):
        return (100.0 + t) if t < jt else (100.0 + jt + db * BL + (t - jt))
    re, errs = run(jump, 25.0)
    conv = first_converge(errs, jt)
    el = (conv - jt) if conv is not None else None
    ok = (el is not None and el <= 1.0 + 1e-9)
    results.append(line("单次 %d 拍 Beat Jump" % dbeats, ok,
                        "收敛到<1拍用时 %s" % (("%.1fs" % el) if el is not None else "未收敛")))

# C) 前向 +1 / +2 拍
for dbeats in (1, 2):
    jt = 10.0

    def jumpf(t, db=dbeats, jt=jt):
        return (100.0 + t) if t < jt else (100.0 + jt + db * BL + (t - jt))
    re, errs = run(jumpf, 25.0)
    conv = first_converge(errs, jt)
    el = (conv - jt) if conv is not None else None
    ok = (el is not None and el <= 1.0 + 1e-9)
    results.append(line("前向 +%d 拍" % dbeats, ok,
                        "收敛到<1拍用时 %s" % (("%.1fs" % el) if el is not None else "未收敛")))

print()
print("=" * 78)
print("#3 抖动不误判（不应触发硬重锚）")
print("=" * 78)
for ms, noise in ((50, 0.050), (186, 0.186)):
    re, errs = run(lambda t: 100.0 + t, 100.0, noise=noise)
    mx = max(abs(e) for _, e in errs)
    ok = (re == 0) and (mx < 1.0)
    results.append(line("±%dms 抖动 100s" % ms, ok,
                        "硬重锚 %d 次；max|err|=%.4f 拍" % (re, mx)))

print()
print("=" * 78)
print("#4 真实远距离 seek（+400 拍）应在 ≤2 个窗内跟上")
print("=" * 78)
for dbeats in (400, -300):
    st = 10.0

    def seek(t, db=dbeats, st=st):
        return (100.0 + t) if t < st else (100.0 + st + db * BL + (t - st))
    re, errs = run(seek, 30.0)
    conv = first_converge(errs, st)
    nwin = None if conv is None else int(round((conv - st) / TICK)) + 1
    ok = (nwin is not None and nwin <= 2)
    results.append(line("远距离 seek %+d 拍" % dbeats, ok,
                        "跟上用 %s 个窗" % (nwin if nwin is not None else "未跟上")))

print()
print("-" * 78)
print("合计：%d/%d 通过" % (sum(1 for r in results if r), len(results)))
