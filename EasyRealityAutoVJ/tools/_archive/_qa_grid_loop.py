# -*- coding: utf-8 -*-
"""QA: 真实 DJ 场景下「网格时钟」相位漂移量化。

场景 A：4 拍 Loop（120BPM，loop 长 2s）连打 30 秒
场景 B：Beat Jump 往回到 32 拍前，然后正常播放
场景 C：持续搓盘（offset 来回抖）
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from beatgrid import GridClock  # noqa: E402

BL = 0.5          # 120 BPM
ANCHOR = 0.0
SID = 1


def run(title, offset_at, dur, note):
    clk = GridClock()
    t = 0.0
    dt = 0.5      # 识别周期
    step = 0
    last_true = None
    print("\n=== %s ===" % title)
    print("  %s" % note)
    while t <= dur:
        off = offset_at(t)
        clk.update(SID, BL, ANCHOR, off, 1.0, t)
        p = clk.pos(t)
        true_pos = off / BL
        err = p - true_pos
        if step % 4 == 0 or t == 0:
            print("   t=%5.1fs  真实拍位=%8.2f  时钟拍位=%8.2f  误差=%+8.2f 拍 (mod8=%+5.2f)"
                  % (t, true_pos, p, err, ((err % 8) + 4) % 8 - 4))
        t += dt
        step += 1
    return clk


# A: 4 拍 loop（每 2s 回到 loop 起点）
def loopA(t):
    loop_start = 100.0
    return loop_start + (t % 2.0)   # 2s = 4 拍


run("场景A：4 拍 Loop 连打 30 秒", loopA, 30.0,
    "真实音乐一直停在 loop 内（offset 每 2s 回到 loop 起点），识别持续成功")

# B: 跳到 32 拍前（=16s @120bpm）后正常播放
def jumpB(t):
    if t < 10.0:
        return 100.0 + t
    return (100.0 + 10.0 - 16.0) + (t - 10.0)   # 往回 16s


run("场景B：Beat Jump 往回 32 拍后正常播放", jumpB, 60.0,
    "跳变后音乐正常前进，识别持续成功（空档 0.5s，不触发 >2s 硬重锚）")

# C: 搓盘（offset 绕 100 来回 ±2s）
import math
def scratchC(t):
    return 100.0 + 2.0 * math.sin(t * 2.0)


run("场景C：持续搓盘（offset 来回抖 ±2s）", scratchC, 20.0,
    "offset 反复前后，识别持续成功")
