# -*- coding: utf-8 -*-
"""`RateEstimator`（音乐播放倍率估计）单测 —— 变速对齐的**前馈基准**。

为什么必须有这一份：码表播放器的倍速 = `it.speed × r`，`r` 由本类给出。
`r` 估错 ⇒ 码表按错的时间基播 ⇒ 稳态偏差 = `RATE_CATCHUP_SEC × |r−真值|`
（1.5 × 0.2 = 0.3s），叠上指纹抖动就够到 seek 门槛 ⇒ **每 2 秒强跳一次**。

★ 2026-10-18 真机教训：**必须带真实抖动**跑。识别每 0.5s 给一次 offset，而 offset
  有 ±0.2~0.3s 抖动 ⇒ 用"相邻两次"算速率，噪声 σ≈0.4（与信号同量级）。
  早期测试**不带抖动**，所以"EMA 收敛得很漂亮"是假绿；真机上 0.80× 跑 50s 只到 0.958。
  这里所有用例都按真机节奏喂：dt=0.5s + 抖动 + 偶发野匹配。

覆盖（每条都能**反向复现**）：
  A 稳定倍率要收敛（1.10 / 0.90 / 1.20 / 0.80），且**8 秒内**
  B ★ 0.80 是旧 `MIN_R=0.8` 边界的**反向复现**：旧代码会一直停在 1.0
  C 中途变速要跟得上（不是只管开机那一次）
  D 单次离群（野匹配）不许动估计
  E 持续另一档速率必须跟过去（**绝不永久卡死**，铁律 0）
  F ±0.2s 抖动下，估计要稳在真值 ±0.04 内
  G 断档（识别卡 5s）后不许把"断档"当成行走
"""
from __future__ import annotations

import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from audio_engine import RateEstimator   # noqa: E402

FAILS = []


def chk(name, cond, msg=""):
    print("  [%s] %s%s" % ("ok  " if cond else "FAIL", name, ("  " + msg) if msg else ""))
    if not cond:
        FAILS.append(name)


def run(rate, seconds=30.0, dt=0.5, jitter=0.0, wild=0.0, seed=7, start=100.0):
    """按 dt 秒喂一次 offset（真机节奏）。返回 (估计器, 每个采样的 rate 快照)。"""
    rnd = random.Random(seed)
    e = RateEstimator()
    off = start
    t = 0.0
    out = []
    while t < seconds:
        o = off + (rnd.uniform(-jitter, jitter) if jitter else 0.0)
        if wild and rnd.random() < wild:
            o += rnd.choice((-1, 1)) * rnd.uniform(20.0, 250.0)   # 野匹配：跳到歌里另一处
        e.update(4242, o, t)
        out.append(e.rate)
        off += rate * dt
        t += dt
    return e, out


def main():
    print("RateEstimator 单测（真机节奏：dt=0.5s + 抖动 + 野匹配）")

    # A 稳定倍率（带真机量级的抖动）
    for rate in (1.10, 0.90, 1.20, 0.80):
        e, out = run(rate, 30.0, jitter=0.20, wild=0.06)
        at8 = out[min(len(out) - 1, int(8.0 / 0.5) - 1)]     # 8 秒时的估计
        chk("A rate=%.2f ⇒ 8s 时 r=%.3f、末值 %.3f（误差 %+.3f）"
            % (rate, at8, e.rate, e.rate - rate),
            abs(at8 - rate) <= 0.05 and abs(e.rate - rate) <= 0.04)

    # B 反向复现：旧 MIN_R=0.8 会把 0.80 判成越界 ⇒ 永远返回 1.0
    e, out = run(0.80, 30.0, jitter=0.20, wild=0.06)
    chk("B 0.80 不再被量程挡掉（旧写法会停在 1.000）",
        abs(e.rate - 0.80) <= 0.04 and abs(out[-1] - 1.0) > 0.1,
        "末值 %.3f" % e.rate)

    # C 中途变速
    rnd = random.Random(11)
    e = RateEstimator()
    off, t = 100.0, 0.0
    for _ in range(40):                       # 20s @1.0
        e.update(77, off + rnd.uniform(-0.2, 0.2), t); off += 0.5; t += 0.5
    mid = e.rate
    for _ in range(60):                       # 30s @0.85
        e.update(77, off + rnd.uniform(-0.2, 0.2), t); off += 0.85 * 0.5; t += 0.5
    chk("C 1.00 → 0.85 跟得上（中段 %.3f ⇒ 末段 %.3f）" % (mid, e.rate),
        abs(mid - 1.0) <= 0.04 and abs(e.rate - 0.85) <= 0.04)

    # D 单次野匹配
    e, _ = run(1.0, 20.0, jitter=0.20)
    base = e.rate
    e.update(77, 100.0 + 20.0 + 5.0, 20.0)
    chk("D 单次野匹配不动估计（%.3f → %.3f）" % (base, e.rate), abs(e.rate - base) < 0.02)

    # E 持续另一档速率必须跟（不能永久卡死）
    rnd = random.Random(5)
    e = RateEstimator()
    off, t = 100.0, 0.0
    for _ in range(40):
        e.update(77, off, t); off += 0.5; t += 0.5
    for _ in range(40):
        e.update(77, off, t); off += 0.70 * 0.5; t += 0.5
    chk("E 1.00 → 0.70 跟过去（r=%.3f，期望 ≈0.70）" % e.rate, abs(e.rate - 0.70) <= 0.04)

    # F 抖动抑制
    for rate in (1.10, 0.80):
        e, _ = run(rate, 60.0, jitter=0.25, wild=0.08)
        chk("F rate=%.2f + ±0.25s 抖动 + 8%% 野匹配 ⇒ r=%.3f（±0.04）" % (rate, e.rate),
            abs(e.rate - rate) <= 0.04)

    # G 断档不许当成"没走"
    e = RateEstimator()
    off, t = 100.0, 0.0
    for _ in range(20):
        e.update(9, off, t); off += 0.5; t += 0.5
    t += 5.0                                   # 识别卡 5s（off 不动）
    for _ in range(20):
        e.update(9, off, t); off += 0.5; t += 0.5
    chk("G 断档 5s 后仍稳在 1.00（没把断档当成走快）", abs(e.rate - 1.0) <= 0.04,
        "r=%.3f" % e.rate)

    print("\n结论：%s" % ("PASS —— 倍率估计收敛快、抗抖、抗野匹配、且不会永久卡死"
                        if not FAILS else "FAIL：" + "；".join(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
