# -*- coding: utf-8 -*-
"""QA 第二轮：GridClock 行为复验（★ A 低票数长段自由跑 + 再锚跳变；★ E 真跳转边界）。
模拟 0.5s 识别节奏：off 在原曲时间轴上推进，速度 = 真实播放速率 r_true。"""
import sys, types
sys.path.insert(0, r"P:\AutoVJ\src")
import beatgrid as BG

BL = 60.0 / 128.0     # 128 BPM -> 0.46875 s/拍
ANCHOR = 1.0

class FakeClock:
    t = 0.0
def fake():
    return FakeClock.t


def drive(seq, bl=BL, anchor=ANCHOR, start_r=1.0):
    """seq: list of (ref_t, sid, off, r, votes) —— 每个 0.5s 一个识别参考。
    返回 trace: list of (ref_t, pos_before, update_applied, pos_after, hard)。
    复刻 engine._beat_pos_grid 的门控：trusted(votes>=180) 或 换歌 才 update。"""
    c = BG.GridClock()
    trace = []
    for (rt, sid, off, r, votes) in seq:
        trusted = (votes is None) or (int(votes) >= 180)
        pos_before = c.pos(rt)
        applied = False
        if trusted or c.sid != sid:
            c.update(sid, bl, anchor, off, r, rt)
            applied = True
        pos_after = c.pos(rt)
        trace.append((rt, pos_before, applied, pos_after, c.sid, c.r))
    return c, trace


print("=== ★E-1 真跳转修复边界：单次跳转量 -> 是否硬重锚 ===")
for back in (2.0, 3.0, 3.9, 4.0, 4.1, 8.0, -3.9, -4.0, -4.1, -32.0):
    seq = []
    t = 0.0
    for k in range(6):                     # 先稳定 3s
        seq.append((t, 7, 1.0 + 1.0 * t, 1.0, 300)); t += 0.5
    base_off = 1.0 + 1.0 * t
    # 施加跳转：回跳 back 拍（back>0 表示音乐位置后退 back*(BL) 秒）；负=正向
    jump_off = base_off - back * BL
    seq.append((t, 7, jump_off, 1.0, 300)); t += 0.5
    for k in range(6):
        seq.append((t, 7, jump_off + 1.0 * 0.5 * (k + 1), 1.0, 300)); t += 0.5
    c, tr = drive(seq)
    # 找跳转那一帧的 pos 变化
    idx = 6
    delta = tr[idx][3] - tr[idx][1]
    print("  跳转 %+6.1f 拍 -> 该步 pos 变化 = %+8.3f 拍   (|Δ|>=4 即硬重锚=跳变)" % (-back if back>0 else -back, delta))

print()
print("=== ★E-2 连续小幅回跳 2 / 3 / 3.9 拍，间隔 1s，看是否累积跑偏 ===")
seq = []; t = 0.0
for k in range(6):
    seq.append((t, 7, ANCHOR + 1.0 * t, 1.0, 300)); t += 0.5
off = ANCHOR + 1.0 * t
for back in (2.0, 3.0, 3.9):
    off -= back * BL
    seq.append((t, 7, off, 1.0, 300)); t += 0.5
    seq.append((t, 7, off + 0.5 * 1.0, 1.0, 300)); t += 0.5   # 中间一帧正常
# 之后正常跑 20s，测残余滞后
for k in range(40):
    off += 0.5 * 1.0
    seq.append((t, 7, off, 1.0, 300)); t += 0.5
c, tr = drive(seq)
for i, (rt, pb, ap, pa, sid, rr) in enumerate(tr):
    if i in (5, 6, 8, 10, 11, 12, 13, 20, 30, 50):
        # 真实音乐位置 -> 参考拍位
        off_i = seq[i][2]
        refp = (off_i - ANCHOR) / BL
        print("  t=%5.1fs  clock_pos=%9.3f  音乐参考拍=%9.3f  滞后=%+7.3f 拍  update=%s"
              % (rt, pa, refp, pa - refp, ap))

print()
print("=== ★E-3 4 拍 Loop vs 4.1 拍 Loop（每 2s 打一次，共 10 次）===")
for loopb in (4.0, 4.1, 3.9):
    seq = []; t = 0.0
    off = ANCHOR
    loop_start = ANCHOR + 0.0
    for k in range(4):
        seq.append((t, 7, off, 1.0, 300)); t += 0.5; off += 0.5
    # 从 loop 起点每 2s 回跳 loopb 拍
    base = off
    for it in range(10):
        # 正常推进 2s
        for k in range(4):
            off += 0.5
            seq.append((t, 7, off, 1.0, 300)); t += 0.5
        # 回跳到 loop 起点 + 微差
        off = base - 0.0
    c, tr = drive(seq)
    # 简单：测每次回跳附近 pos 变化幅度
    print("  Loop=%4.1f 拍：最终 clock_pos=%.3f（音乐参考 %.3f）" % (loopb, tr[-1][3], (seq[-1][2]-ANCHOR)/BL))

print()
print("=== ★M3 恰踩八拍头 beats_to_phrase ===")
import math
c = BG.GridClock()
c.pos0 = 16.0; c.sid = 1; c.t0 = 0.0; c.bl = BL; c.r = 1.0; c.last_pos = None
print("  pos=16.000（恰八拍头） -> beats_to_phrase =", c.beats_to_phrase(0.0))
c.pos0 = 16.0 + 0.0000001; c.last_pos = None
print("  pos=16.0000001          -> beats_to_phrase =", c.beats_to_phrase(0.0))
c.pos0 = 17.5; c.last_pos = None
print("  pos=17.5                -> beats_to_phrase =", c.beats_to_phrase(0.0))
