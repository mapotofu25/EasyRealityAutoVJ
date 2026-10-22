# -*- coding: utf-8 -*-
"""【旧引擎行为对照·非现役】QA 第二轮：门槛 180 的初始锁定漏洞（★A 延伸）。

⚠ 本脚本**复刻的是第二轮旧 engine** 的门控：
    `if trusted or self._grid_clock.sid != sid: update(...)`
  即换歌/首次识别时**无视票数**硬锚定 —— 用于①留下旧缺陷证据、②对照。
  ★ 第三轮已修：首次锁定改走「信任判据 + 连续 2 个可信窗一致」，见
     engine._beat_pos_grid（逐窗去重 _grid_seen + _grid_pending 一致性）。
  现役引擎的实测见 **tools/_fix3_engine.py ★B-①**（首个坏值 +16 拍 → 误差 +0.000）。"""
import sys
sys.path.insert(0, r"P:\AutoVJ\src")
import beatgrid as BG

BL = 60.0 / 128.0
ANCHOR = 1.0


def run(first_off_err_beats, second_off_err_beats=0.0, votes_low=100, votes_hi=None,
        n_low=20, bl=BL):
    c = BG.GridClock()
    t = 0.0
    log = []
    # 首次识别：votes 低（歧义），sid 新 -> 仍硬锚定
    off = ANCHOR + 10.0 + first_off_err_beats * bl   # +10s 处的正常位置，外加错拍
    c.update(7, bl, ANCHOR, off, 1.0, t)
    # 之后 n_low 个正确 offset 的低票数窗
    for k in range(n_low):
        t += 0.5
        correct_off = ANCHOR + 10.0 + 0.5 * (k + 1) + second_off_err_beats * bl
        if votes_low >= 180:
            c.update(7, bl, ANCHOR, correct_off, 1.0, t)
        # else: 门控拦截，不 update（复刻 engine）
    pos_at_low_end = c.pos(t)
    ref_correct = (ANCHOR + 10.0 + 0.5 * n_low - ANCHOR) / bl
    # 然后出现一个高票数窗
    t += 0.5
    hi_off = ANCHOR + 10.0 + 0.5 * (n_low + 1)
    c.update(7, bl, ANCHOR, hi_off, 1.0, t)
    pos_at_hi = c.pos(t)
    return (pos_at_low_end - ref_correct), (pos_at_hi - ref_correct)


print("=== 首次锁定无视票数：错误 offset 会持续多久 ===")
for first_err in (2.0, 4.0, 16.0):
    low_err, hi_err = run(first_err, n_low=20)   # 10s 低票数窗
    print("  首次 offset 错 %+5.1f 拍 -> 低票数段末仍错 %+6.2f 拍；出现高票数窗后残余 %+6.2f 拍"
          % (first_err, low_err, hi_err))

print()
print("=== 对照：首次 offset 正确、但中段低票数 + r 偏差 -> 再锚 ===")
# 已在 _qa2_gridclock2.py 覆盖（★A），此处略
print("  （见 _qa2_gridclock2.py）")
