# -*- coding: utf-8 -*-
"""QA: GridClock 边界行为实测（倒退 / 单调 / 空档重锚 / 噪声）。

只读取 src/beatgrid.py 的 GridClock，不改 src。
用法： P:\\AutoVJ\\venv\\Scripts\\python.exe tools/_qa_gridclock.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from beatgrid import GridClock  # noqa: E402


# ---------------------------------------------------------------- 1. 正常前进
print("=" * 70)
print("用例 1：正常前进（r=1, 120BPM, beat_len=0.5s）,每 0.5s 一次识别")
snaps = [(i * 0.5, 100.0 + i * 0.5) for i in range(1, 13)]
clk = GridClock()
prev = None
viol = 0
for (t, off) in snaps:
    clk.update(42, 0.5, 0.0, off, 1.0, t)
    p = clk.pos(t)
    b = clk.beats_to_phrase(t)
    if prev is not None and p < prev - 1e-9:
        viol += 1
    prev = p
    print("  t=%5.2f off=%7.2f pos=%8.3f  b2p=%6.3f" % (t, off, p, b))
print("  单调违例=%d (期望 0)" % viol)

# ---------------------------------------------------------------- 2. ★ 倒退 Loop
print("\n" + "=" * 70)
print("用例 2（重点）：DJ 打 Loop 往回跳 8 秒（offset 100->92），识别持续成功")
print("  beat_len=0.5 => 倒退 = 16 拍；空档仅 0.5s（远小于 big_gap=2s，不触发硬重锚）")
clk = GridClock()
# 先稳定识别若干拍
for i in range(1, 9):
    t = i * 0.5
    off = 100.0 + i * 0.5
    clk.update(42, 0.5, 0.0, off, 1.0, t)
t_jump = 4.5
pos_before = clk.pos(t_jump)
off_at_jump = 92.0  # 原曲位置跳回
print("  跳变前 pos=%.3f（ref_pos@100.5s=%.1f）" % (pos_before, (100.0 + 4.5) / 0.5))
clk.update(42, 0.5, 0.0, off_at_jump, 1.0, t_jump)
pos_at_jump = clk.pos(t_jump)
print("  跳变后立刻 pos=%.3f   (ref_pos=%.1f)" % (pos_at_jump, off_at_jump / 0.5))
# 之后音乐从 92s 继续正常前进，每 0.5s 一次识别
print("  之后继续喂：off 从 92 起每 0.5s +0.5（音乐确实在往前走）")
froze = 0
prev = None
for i in range(1, 41):
    t = t_jump + i * 0.5
    off = 92.0 + i * 0.5
    clk.update(42, 0.5, 0.0, off, 1.0, t)
    p = clk.pos(t)
    ref_pos = off / 0.5
    gap = p - ref_pos
    if prev is not None and abs(p - prev) < 1e-9:
        froze += 1
    prev = p
    if i <= 8 or i % 10 == 0:
        print("    +%5.1fs t=%5.2f off=%7.2f pos=%8.3f  ref_pos=%7.1f  相位误差(拍)=%+7.2f"
              % (i * 0.5, t, off, p, ref_pos, gap))
print("  → 相位误差长期不收敛：时钟位置比真实音乐位置**超前**约 %.1f 拍，"
      "需要 %.0f 秒才能靠 ±0.05 拍/tick 的校正追平"
      % (pos_at_jump - off_at_jump / 0.5, (pos_at_jump - off_at_jump / 0.5) / (0.05 * 2)))
print("  → pos() 冻结 tick 数=%d（单调保护导致时钟不再后退，只被卡在高位）" % froze)
print("  → 期间 beats_to_phrase 仍按错误相位输出 → 切换会落在错误的八拍位置")

# ---------------------------------------------------------------- 3. 空档 >2s 硬重锚
print("\n" + "=" * 70)
print("用例 3：识别空档 >2s（big_gap）→ 硬重锚是否生效")
clk = GridClock()
for i in range(1, 5):
    clk.update(42, 0.5, 0.0, 100.0 + i * 0.5, 1.0, i * 0.5)
print("  重锚前 pos=%.3f" % clk.pos(2.0))
clk.update(42, 0.5, 0.0, 92.0, 1.0, 6.0)   # 空档 4s > 2s
print("  空档 4s 后喂 off=92 → pos=%.3f (期望硬重锚到 ref_pos=184.0)" % clk.pos(6.0))
print("  → 硬重锚只在【参考时刻间隔 gap>2s】时触发，与 offset 是否倒退无关")

# ---------------------------------------------------------------- 4. ±50ms 噪声单调
print("\n" + "=" * 70)
print("用例 4：±50ms 识别噪声下单调性与误差（复核工程师 0.096 拍的说法）")
import random
random.seed(1)
clk = GridClock()
t = 0.0
pos_true = 100.0 / 0.5
max_err = 0.0
viol = 0
prev = None
for i in range(200):
    t = i * 0.5
    true_off = 100.0 + i * 0.5
    noisy_off = true_off + random.uniform(-0.05, 0.05)
    clk.update(42, 0.5, 0.0, noisy_off, 1.0, t)
    p = clk.pos(t)
    ref_true = true_off / 0.5
    max_err = max(max_err, abs(p - ref_true))
    if prev is not None and p < prev - 1e-9:
        viol += 1
    prev = p
print("  100s 内最大拍位误差 = %.4f 拍；单调违例 = %d" % (max_err, viol))
