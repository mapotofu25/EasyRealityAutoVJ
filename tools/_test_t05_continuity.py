# -*- coding: utf-8 -*-
"""T05①② 自测：跨段 continuity + HUD 当前段显示。

① 跨段 continuity（已知限制）：分段边界**不在八拍头上**时，`GridClock.reanchor` 会让
   网格坐标整体换系（实测一次 −256 拍）。旧实现里 `_beat_pos` 的坐标续接 delta 只在
   「网格↔音频」边沿重算 ⇒ 换系后返回拍位**跳变** ⇒ `_auto_switch_tick` 的
   `pos < self.switch_pos` 把它当「拍钟被重置」：`switch_pos = pos`、`_next_due = None`
   ⇒ **倒计时从头数、已排好的下一次切换被丢掉**。

   本脚本用**同一条时间线跑两遍**自证：
     · 「修复前」= 把 `_reanchor_across_segment` 替换成裸 `clock.reanchor`（等价旧行为）；
     · 「修复后」= 现状实现。
   断言：修复后跨段那一拍 `switch_pos` 不变、`_next_due` 不重置、且**按时切一次**；
        修复前则 `switch_pos` 被重置、切换丢失。

② 无 manual 的曲目逐值不变：用同一时间线跑「无 segments/anchors」的朴素网格，
   断言 `_grid_segkey` 恒为 None、`_grid_coord_delta` 锁定后恒定（新代码路径根本没被走到）。

③ HUD：`ui_main._hud_grid_seg` 在有分段/多锚时给出 `  |  段2 155BPM · 锚120.03`，
   无 manual / 无快照时返回空串（HUD 逐字不变）。

用法：venv\\Scripts\\python.exe tools\\_test_t05_continuity.py
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import config as cfgmod                       # noqa: E402
from audio_engine import AudioState           # noqa: E402
from engine import AutoVJEngine               # noqa: E402
from ui_main import _hud_grid_seg             # noqa: E402

SID = 7
BL1 = 60.0 / 128.0            # 段1：128 BPM
BL2 = 60.0 / 155.0            # 段2：155 BPM
A1 = 0.1                      # 段1锚点（故意不是整秒）
T_BOUND = 120.0               # 段边界（秒，原曲时间轴）
A2 = 120.2                    # 段2锚点（故意不接段1的网格）

DT_WALL = 0.002               # 每帧推进 2ms（原曲同步 2ms，r=1.0）
WIN = 0.5                     # 识别窗间隔（秒）——与真实 audio_engine 一致
START = 117.0                 # 模拟起点（原曲秒）
END = 123.0                   # 模拟终点
SEED_AHEAD = 6.5              # 起手把「下一次切换」排在 pos+6.5 拍（**跨过**段边界之后才到期）


def _grid_with_segments():
    return {"bpm": 128.0, "beat_len": BL1, "anchor": A1, "src": "manual",
            "conf": 0.95, "dur": 300.0,
            "segments": [
                {"t0": 0.0, "bpm": 128.0, "beat_len": BL1, "anchor": A1},
                {"t0": T_BOUND, "bpm": 155.0, "beat_len": BL2, "anchor": A2}]}


def _grid_plain():
    return {"bpm": 128.0, "beat_len": BL1, "anchor": A1, "src": "vdj",
            "conf": 0.95, "dur": 300.0}


class _FakeLayer:
    """只为了给 `_auto_switch_tick` 一个「非固定、有素材、非 Kv」的图层。

    `_switch_layer_random` 被实例级替换成计数器，所以不会真去解码素材。
    """

    def __init__(self):
        self.fixed = -1
        self.clips = [object()]
        self.is_kv = False
        self.trans_ms = 0


def _new_engine(grid, legacy=False):
    cfg = cfgmod.Config()
    cfg["auto"]["beat_grid"] = True
    eng = AutoVJEngine(cfg, AudioState(), None)
    eng.set_song_grids({SID: grid})
    eng.layers = [_FakeLayer()]
    eng.locked = False
    if legacy:
        # 等价「修复前」：跨段只做裸重锚（不做坐标续接、不同步相位）
        eng._reanchor_across_segment = (
            lambda clock, bl, anchor, off, r, ref_t: clock.reanchor(bl, anchor, off, r, ref_t))
    return eng


def _run(eng, grid):
    """跑一条固定时间线，返回测量结果（跨段前后 switch_pos / _next_due / 切换次数）。"""
    fake = [1000.0]
    real_perf = time.perf_counter
    time.perf_counter = lambda: fake[0]
    try:
        switches = [0]

        def _bump(_lay, _snap):
            switches[0] += 1

        eng._switch_layer_random = _bump
        seg1_key = ("seg", round(BL1, 4), round(A1, 4))
        seg2_key = ("seg", round(BL2, 4), round(A2, 4))
        switch_offs = []
        segkeys_seen = set()
        prev_sw = 0

        snap = {"bpm": 128.0, "beat_active": True, "beat_count": 0, "beat_phase": 0.0,
                "downbeat_off": 0.0, "energy": 0.6, "energy_trend": 0.0, "level": 0.5,
                "recognized_song_id": SID, "recognized_offset": START,
                "recognized_t": fake[0], "recognized_rate": 1.0,
                "recognized_votes": 300}

        n = int(round((END - START) / DT_WALL))
        next_win = 0.0
        seeded = False
        cross = None                 # 跨段那一帧的记录
        prev = None                  # 上一帧记录
        pos_first = None
        delta_after_lock = None
        delta_drift = 0.0
        segkey_moves = 0
        last_segkey = None
        trace = []

        for i in range(n):
            fake[0] = 1000.0 + i * DT_WALL
            off = START + i * DT_WALL
            if fake[0] >= next_win:
                next_win = fake[0] + WIN
                snap["recognized_offset"] = off
                snap["recognized_t"] = fake[0]
            snap["beat_count"] = int(off * 2.0)          # 锁定前用的原拍钟（120BPM 虚拟）
            snap["beat_phase"] = (off * 2.0) % 1.0

            eng._auto_switch_tick(snap)
            if switches[0] != prev_sw:
                prev_sw = switches[0]
                switch_offs.append(off)
            pos = getattr(eng, "_grid_last_out", None)
            if pos is None:
                continue
            key = getattr(eng, "_grid_segkey", None)
            segkeys_seen.add(key)
            if key != last_segkey:
                if last_segkey is not None:
                    segkey_moves += 1
                last_segkey = key
            if pos_first is None:
                pos_first = pos
            locked = eng._grid_clock.sid == SID
            if locked and delta_after_lock is None:
                delta_after_lock = eng._grid_coord_delta
            elif locked and delta_after_lock is not None:
                delta_drift = max(delta_drift, abs(eng._grid_coord_delta - delta_after_lock))

            # 锁定后立刻把「下一次切换」排在 pos+SEED_AHEAD（跨过段边界）
            if locked and not seeded:
                eng.switch_pos = pos - 0.5
                eng._next_due = pos + SEED_AHEAD
                eng._due_off = eng._bar_off(snap)
                eng._due_w = eng._align_width(snap)
                seeded = True
                trace.append((off, pos, None))

            rec = {"off": off, "pos": pos, "switches": switches[0],
                   "switch_pos": eng.switch_pos, "next_due": eng._next_due,
                   "segkey": key}
            if cross is None and key == seg2_key:
                cross = {"now": rec, "prev": prev}
            trace.append((off, pos, eng._next_due))
            prev = rec
    finally:
        time.perf_counter = real_perf

    return {"switches": switches[0], "cross": cross, "seeded": seeded,
            "segkey_moves": segkey_moves, "delta_drift": delta_drift,
            "trace": trace, "snap": snap, "eng": eng, "switch_offs": switch_offs,
            "segkeys": segkeys_seen}


def _gate_boundary_not_on_head():
    """前提自证：段边界 120.0s **不是**段1 的八拍头（否则测不到问题）。"""
    pos1 = (T_BOUND - A1) / BL1
    ing = pos1 % 8.0
    print("  · 段边界 %.1fs 在段1 坐标 = %.4f 拍，组内位置 %.4f（≠0 ⇒ 不在八拍头上）"
          % (T_BOUND, pos1, ing))
    assert abs(ing) > 0.05 and abs(ing - 8.0) > 0.05, ing
    shift = pos1 - (T_BOUND - A2) / BL2
    print("  · 换系跳变量 = %.2f 拍（旧坐标 %.2f → 新坐标 %.2f）"
          % (shift, pos1, (T_BOUND - A2) / BL2))
    return shift


def test_segment_continuity():
    print("== ① 跨段 continuity（分段边界不在八拍头上）==")
    _gate_boundary_not_on_head()

    legacy = _run(_new_engine(_grid_with_segments(), legacy=True), _grid_with_segments())
    fixed = _run(_new_engine(_grid_with_segments()), _grid_with_segments())

    assert legacy["cross"] is not None and fixed["cross"] is not None, "没跨到段边界"
    assert fixed["seeded"] and legacy["seeded"], "没锁定/没排上切换"

    lc, fc = legacy["cross"], fixed["cross"]
    print("  修复前：跨段帧 pos %.2f → %.2f ; switch_pos %.2f → %.2f ; _next_due %s → %s ; 总切换 %d 次"
          % (lc["prev"]["pos"], lc["now"]["pos"], lc["prev"]["switch_pos"],
             lc["now"]["switch_pos"], lc["prev"]["next_due"], lc["now"]["next_due"],
             legacy["switches"]))
    print("  修复后：跨段帧 pos %.2f → %.2f ; switch_pos %.2f → %.2f ; _next_due %s → %s ; 总切换 %d 次"
          % (fc["prev"]["pos"], fc["now"]["pos"], fc["prev"]["switch_pos"],
             fc["now"]["switch_pos"], fc["prev"]["next_due"], fc["now"]["next_due"],
             fixed["switches"]))

    # ---- 修复后：拍位连续、switch_pos/next_due 不重置、按时切一次 ----
    assert abs(fc["now"]["pos"] - fc["prev"]["pos"]) < 0.2, \
        ("跨段拍位跳变", fc["prev"]["pos"], fc["now"]["pos"])
    assert abs(fc["now"]["switch_pos"] - fc["prev"]["switch_pos"]) < 1e-9, \
        ("跨段 switch_pos 被重置", fc["prev"]["switch_pos"], fc["now"]["switch_pos"])
    assert fc["now"]["next_due"] == fc["prev"]["next_due"], \
        ("跨段 _next_due 被重置", fc["prev"]["next_due"], fc["now"]["next_due"])
    assert fixed["switches"] == 1, ("应恰好切 1 次（排程未被破坏）", fixed["switches"])
    assert fixed["switch_offs"] and fixed["switch_offs"][0] > T_BOUND, \
        ("那次切换应当发生在跨段之后（原定目标在边界后面）", fixed["switch_offs"])
    print("  修复后：切换发生在原曲 %.3fs（段边界 %.1fs 之后）✓" % (fixed["switch_offs"][0], T_BOUND))
    # 拍位单调（跨段也不许倒退）
    prev_p = None
    for _off, p, _d in fixed["trace"]:
        if prev_p is not None:
            assert p >= prev_p - 1e-6, ("拍位倒退", prev_p, p)
        prev_p = p
    print("  修复后：拍位全程单调不减 ✓ ; 跨段 switch_pos 不变 ✓ ; _next_due 不重置 ✓ ; "
          "恰好切 1 次 ✓")

    # ---- 修复前：把丢掉的排程/被重置的 switch_pos 也数字留档 ----
    broke_pos = lc["now"]["pos"] - lc["prev"]["pos"]
    print("  修复前：跨段拍位跳变 %.2f 拍、switch_pos 被重置为 %.2f、_next_due 由 %.2f 被拽回 %.2f"
          % (broke_pos, lc["now"]["switch_pos"], float(lc["prev"]["next_due"]),
             float(lc["now"]["next_due"])))
    print("  修复前：那次切换 %s ⇒ 丢排程/倒计时从 16 拍重数"
          % ("丢失（0 次切换）" if legacy["switches"] == 0
             else "在 %.3fs 触发（时点错乱）" % (legacy["switch_offs"] or [-1])[0]))
    assert lc["now"]["switch_pos"] != lc["prev"]["switch_pos"], "修复前应当复现重置"
    assert legacy["switches"] == 0, ("修复前应丢失那次切换", legacy["switches"])
    return fixed, legacy


def test_plain_no_regression():
    print("== ② 无 manual 曲目（零回归）: segkey 恒 None、delta 锁定后恒定 ==")
    res = _run(_new_engine(_grid_plain()), _grid_plain())
    assert res["seeded"], "朴素网格也应当能锁定"
    assert res["segkey_moves"] == 0, ("朴素网格不该触发跨段重锚", res["segkey_moves"])
    assert res["segkeys"] == {None}, ("朴素网格 segkey 必须恒为 None", res["segkeys"])
    assert res["delta_drift"] < 1e-12, ("朴素网格 delta 必须恒定", res["delta_drift"])
    print("  · 跨段重锚次数 = %d（应为 0）; segkey 恒 None ✓ ; 锁定后 delta 漂移 = %.1e ✓"
          % (res["segkey_moves"], res["delta_drift"]))
    print("  · 朴素网格仍按时切 %d 次（行为未变）" % res["switches"])
    return res


def test_hud_text():
    print("== ③ HUD 当前段文本（ui_main._hud_grid_seg）==")
    seg_us = {"grid_seg": {"kind": "seg", "n": 2, "bpm": 155.0, "anchor": 120.03}}
    anc_us = {"grid_seg": {"kind": "anchor", "n": 1, "bpm": 140.0, "anchor": 28.8}}
    print("  分段：%r" % _hud_grid_seg(seg_us))
    print("  多锚：%r" % _hud_grid_seg(anc_us))
    print("  无 manual / 无快照：%r / %r" % (_hud_grid_seg({"grid_seg": None}),
                                            _hud_grid_seg({})))
    assert _hud_grid_seg(seg_us).endswith("段2 155BPM · 锚120.03"), _hud_grid_seg(seg_us)
    assert _hud_grid_seg(anc_us).endswith("多锚 140BPM · 锚28.80"), _hud_grid_seg(anc_us)
    assert _hud_grid_seg({"grid_seg": None}) == "" and _hud_grid_seg({}) == ""
    return seg_us


def test_engine_segment_info(fixed_res):
    print("== ④ engine.grid_segment_info（HUD 数据源，与 ① 同一时间线）==")
    eng = fixed_res["eng"]
    snap = dict(fixed_res["snap"])
    # 段2（off 已过 120s）→ 应报 段2 / 155BPM / 锚 120.20
    info2 = eng.grid_segment_info(snap)
    print("  段2：%s" % info2)
    assert info2 and info2["kind"] == "seg" and info2["n"] == 2, info2
    assert abs(info2["bpm"] - 155.0) < 1.0 and abs(info2["anchor"] - A2) < 1e-6, info2
    # 段1
    snap1 = dict(snap)
    snap1["recognized_offset"] = 100.0
    info1 = eng.grid_segment_info(snap1)
    print("  段1（off=100s）：%s" % info1)
    assert info1 and info1["n"] == 1 and abs(info1["bpm"] - 128.0) < 1.0, info1
    # 朴素网格 → None（HUD 一字不变）
    eng2 = _new_engine(_grid_plain())
    _run(eng2, _grid_plain())
    info_none = eng2.grid_segment_info(dict(fixed_res["snap"]))
    print("  无 manual：%r（HUD 不追加）" % (info_none,))
    assert info_none is None, info_none
    return info2


def main():
    fixed, _legacy = test_segment_continuity()
    test_plain_no_regression()
    seg_us = test_hud_text()
    info2 = test_engine_segment_info(fixed)
    hud = "常规  |  还有 12 拍切换  |  距八拍头 3.2 拍（1.5 秒）" + _hud_grid_seg(seg_us)
    print("\n  HUD 实际显示文本：%s" % hud)
    print("\n[T05①②] 全部通过 ✓  (段2 = %s)" % info2)


if __name__ == "__main__":
    main()
