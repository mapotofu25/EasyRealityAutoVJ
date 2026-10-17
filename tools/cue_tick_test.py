# -*- coding: utf-8 -*-
"""S4 自检：`_cuesheet_tick`（只算不画）—— 一致性 / 每帧开销 / 异常安全 / 零副作用。

覆盖团队给的 5 项判据：
  1. `cs_states` 与 Step1 `CueRuntime.update()` **逐值一致**（TOL=1e-6），5 类场景；
  2. **空状态早退 < 5µs**；
  3. **3 个活跃片段 < 50µs**；
  4. fps 退化（由实测 µs 换算占比，< 1%）；
  5. 守卫另跑（本文件只证明"不出画"：cue 图层 `player is None`）。
另测：**异常安全**（update 抛异常 ⇒ 不冒泡、清空本帧、限流记一次、下帧恢复）与
**零副作用**（tick 期间 `make_player` 一次都没被调用）。

运行：`python tools/cue_tick_test.py`（exit 0 = 全过）
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from PySide6.QtGui import QImage                       # noqa: E402

import engine                                          # noqa: E402
import cuesheet                                        # noqa: E402
import cuesheet_runtime as crt                         # noqa: E402

TOL = 1e-6
CMP_FIELDS = ("lyr_id", "z", "item_id", "clip_id", "clip_path", "missing",
              "src_in_sec", "src_out_sec", "play_sec", "speed", "crop", "xform",
              "blend", "opacity", "alpha", "trans", "trans_p", "active",
              "src_ended", "ended")


# ==========================================================================
# 构造
# ==========================================================================
def build_sheet():
    """2 层 3 片段：L1 有边界与 src_out（测跨边界 / 冻结），L2 常驻（保证多活跃）。"""
    s = cuesheet.new_sheet("S4 自检", res_w=1440, res_h=1080)
    te = cuesheet.new_track("T", "A", 300.0, 128.0)
    L1 = cuesheet.CompLayer(lyr_id="L1", name="背景", z=0)
    L1.items.append(cuesheet.CueItem(item_id="i1", clip_id="c1", at_beats=8.0,
                                     end_beats=24.0, src_in_sec=0.0, src_out_sec=4.0,
                                     trans_in="fade", trans_in_beats=2.0,
                                     trans_out="fade", trans_out_beats=2.0).normalized())
    L1.items.append(cuesheet.CueItem(item_id="i2", clip_id="c2", at_beats=24.0,
                                     trans_in="cut", trans_out="cut").normalized())
    L2 = cuesheet.CompLayer(lyr_id="L2", name="LOGO", z=10, blend="screen", opacity=0.85)
    L2.items.append(cuesheet.CueItem(item_id="i3", clip_id="c1", at_beats=0.0,
                                     trans_in="cut", trans_out="cut").normalized())
    te.layers += [L1, L2]
    s.tracks.append(te)
    s.media.append(cuesheet.MediaRef(clip_id="c1", name="a.mp4"))
    s.media.append(cuesheet.MediaRef(clip_id="c2", name="b.mp4"))
    return s, te


def new_engine(sheet=None, track=None, cue_layer=True, n_extra=1):
    eng = engine.AutoVJEngine.__new__(engine.AutoVJEngine)
    eng.canvas = QImage(1920, 1080, QImage.Format_ARGB32)
    eng.layers = []
    eng._health_log = None
    eng._cue_tick_fail = 0
    eng._cue_err_at = 0.0
    if cue_layer:
        lc = engine.Layer("码表层")
        lc.is_cuesheet = True
        eng.layers.append(lc)
    for i in range(n_extra):
        eng.layers.append(engine.Layer("普通%d" % i))
    if sheet is not None and track is not None:
        eng._cue_sheet = sheet
        eng._cue_track = track
        eng._cue_rt = crt.CueRuntime(sheet, track, media_paths={"c1": "P:/x/a.mp4",
                                                               "c2": "P:/x/b.mp4"})
    else:
        eng._cue_rt = None
    eng._cue_pos_beats = None
    return eng


def cue_layer_of(eng):
    return next(l for l in eng.layers if getattr(l, "is_cuesheet", False))


def _eq(a, b):
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) <= TOL
        except (TypeError, ValueError):
            return a == b
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_eq(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(_eq(a[k], b[k]) for k in a)
    return a == b


# ==========================================================================
# ① 与 Step1 runtime 逐值一致（5 类场景）
# ==========================================================================
SCENARIOS = [
    ("正常前进 0→40 拍（步长 0.25）", [i * 0.25 for i in range(0, 161)]),
    ("回退 30→10 拍（DJ 回拉）", [30.0 - i * 0.5 for i in range(0, 41)]),
    ("大跳变 5→40 拍（单帧 +35）", [5.0, 40.0]),
    ("跨片段边界 7.5→9.0（i1 在 8 拍进）", [7.5 + i * 0.1 for i in range(0, 16)]),
    ("src_ended 之后 20→30（i1 出点 4s=8.53 拍，已冻结）", [20.0 + i * 0.5 for i in range(0, 21)]),
]


def check_consistency():
    rows = []
    sheet, track = build_sheet()
    for name, seq in SCENARIOS:
        eng = new_engine(sheet, track)
        lay = cue_layer_of(eng)
        ref = crt.CueRuntime(sheet, track, media_paths={"c1": "P:/x/a.mp4",
                                                      "c2": "P:/x/b.mp4"})
        bad = None
        n = 0
        for pos in seq:
            eng.set_cue_position(pos)
            eng._cuesheet_tick()
            want = ref.update(pos)
            got = lay.cs_states
            n += 1
            if len(got) != len(want):
                bad = "pos=%.3f 层数 %d != %d" % (pos, len(got), len(want))
                break
            for g, w in zip(got, want):
                for f in CMP_FIELDS:
                    if not _eq(getattr(g, f, None), getattr(w, f, None)):
                        bad = ("pos=%.3f %s.%s 实际=%r 期望=%r"
                               % (pos, g.lyr_id, f, getattr(g, f, None),
                                  getattr(w, f, None)))
                        break
                if bad:
                    break
            if bad:
                break
        rows.append((name, n, bad))
    return rows


# ==========================================================================
# ②③ 每帧开销
# ==========================================================================
def bench(fn, n=20000, warm=2000):
    for _ in range(warm):
        fn()
    t0 = time.perf_counter()
    for _ in range(n):
        fn()
    return (time.perf_counter() - t0) / n * 1e6          # µs/次


def check_cost():
    sheet, track = build_sheet()
    out = {}

    # 空 1：完全没有码表图层
    eng0 = new_engine(None, None, cue_layer=False, n_extra=3)
    out["empty_no_cue_layer"] = bench(lambda: eng0._cuesheet_tick(), n=40000)

    # 空 2：有码表图层，但没装表
    eng1 = new_engine(None, None, cue_layer=True, n_extra=3)
    out["empty_no_runtime"] = bench(lambda: eng1._cuesheet_tick(), n=40000)

    # 空 3：装了表但没有拍位
    eng2 = new_engine(sheet, track, cue_layer=True, n_extra=3)
    out["empty_no_pos"] = bench(lambda: eng2._cuesheet_tick(), n=40000)

    # 空 4：装了表、有拍位，但码表图层不可见
    eng3 = new_engine(sheet, track, cue_layer=True, n_extra=3)
    cue_layer_of(eng3).visible = False
    eng3.set_cue_position(10.0)
    out["empty_invisible"] = bench(lambda: eng3._cuesheet_tick(), n=40000)

    # 活跃：3 个片段都激活（pos=9.0：i1 fade-in + i3 常驻；再取 26.0 让 i2 也活）
    eng4 = new_engine(sheet, track, cue_layer=True, n_extra=3)
    eng4.set_cue_position(9.0)
    eng4._cuesheet_tick()
    out["active_2_items"] = bench(lambda: eng4._cuesheet_tick(), n=20000)

    sheet2, track2 = build_sheet()
    L3 = cuesheet.CompLayer(lyr_id="L3", name="第三层", z=20)
    L3.items.append(cuesheet.CueItem(item_id="i4", clip_id="c2", at_beats=0.0,
                                     trans_in="cut", trans_out="cut").normalized())
    track2.layers.append(L3)
    eng5 = new_engine(sheet2, track2, cue_layer=True, n_extra=3)
    eng5.set_cue_position(9.0)
    eng5._cuesheet_tick()
    n_active = len(cue_layer_of(eng5).cs_states)
    out["active_3_items"] = bench(lambda: eng5._cuesheet_tick(), n=20000)
    out["active_3_n"] = n_active
    return out


# ==========================================================================
# ④ 异常安全
# ==========================================================================
class _BoomRuntime:
    jumped = False

    def __init__(self):
        self.calls = 0

    def update(self, pos):
        self.calls += 1
        raise RuntimeError("boom-%d" % self.calls)


def check_exception_safety():
    res = {}
    sheet, track = build_sheet()
    eng = new_engine(sheet, track)
    good = eng._cue_rt
    boom = _BoomRuntime()
    eng._cue_rt = boom
    eng.set_cue_position(10.0)
    lay = cue_layer_of(eng)
    lay.cs_states = [object()]          # 预置垃圾：确认异常后被清空
    raised = None
    try:
        eng._cuesheet_tick()
    except Exception as e:                                    # noqa: BLE001
        raised = e
    res["no_raise"] = raised is None
    res["raised"] = repr(raised)
    res["cleared"] = lay.cs_states == [] and lay.cs_active is False
    res["fail_count"] = eng._cue_tick_fail
    res["update_calls"] = boom.calls
    # 限流：连续失败多次，健康日志只写一条
    logs = []

    class _Log:
        def write(self, s):
            logs.append(s)

    eng._health_log = _Log()
    eng._cue_err_at = 0.0
    for _ in range(5):
        eng._cuesheet_tick()
    res["log_count"] = len(logs)
    res["log_sample"] = logs[0] if logs else ""
    # 下一帧恢复：换回好 runtime ⇒ 立刻恢复出状态（不永久关闭）
    eng._cue_rt = good
    eng._cue_err_at = 0.0
    eng.set_cue_position(10.0)
    eng._cuesheet_tick()
    res["recovered"] = bool(lay.cs_states) and lay.cs_active is True
    res["fail_reset"] = eng._cue_tick_fail
    return res


# ==========================================================================
# ⑤ 零副作用
# ==========================================================================
def check_no_side_effects():
    res = {}
    sheet, track = build_sheet()
    eng = new_engine(sheet, track)
    calls = []

    def _boom_make_player(*a, **k):
        calls.append((a, k))
        raise AssertionError("_cuesheet_tick 里不许建解码器！")

    real = engine.make_player
    engine.make_player = _boom_make_player
    try:
        for pos in (0.0, 8.0, 12.0, 24.0, 30.0):
            eng.set_cue_position(pos)
            eng._cuesheet_tick()
        res["make_player_calls"] = len(calls)
        lay = cue_layer_of(eng)
        res["cue_players_empty"] = lay._cue_players == {}
        # 也不许碰 seek：所有 cue player 都不存在，自然没有 seek；额外确认
        # `_cue_seek_state` 没被 tick 写过
        res["seek_state_untouched"] = lay._cue_seek_state == {}
    finally:
        engine.make_player = real
    return res


def main():
    fails = []

    def check(name, cond, detail=""):
        print("  [%s] %s%s" % ("ok  " if cond else "FAIL", name,
                               "" if cond else "   ← " + str(detail)))
        if not cond:
            fails.append(name)

    print("① cs_states 与 Step1 CueRuntime.update() 逐值一致（TOL=1e-6）")
    for name, n, bad in check_consistency():
        check("%-52s 共 %d 帧" % (name, n), bad is None, bad)

    print("\n②③ 每帧开销（µs/次，含 2000 次预热）")
    cost = check_cost()
    for k in ("empty_no_cue_layer", "empty_no_runtime", "empty_no_pos",
              "empty_invisible"):
        check("%-20s < 5µs" % k, cost[k] < 5.0, "%.3f µs" % cost[k])
        print("        实测 %.3f µs" % cost[k])
    for k, lim in (("active_2_items", 50.0), ("active_3_items", 50.0)):
        check("%-20s < 50µs" % k, cost[k] < lim, "%.3f µs" % cost[k])
        print("        实测 %.3f µs（活跃 %d 个片段）"
              % (cost[k], 2 if k == "active_2_items" else cost.get("active_3_n", 3)))

    print("\n④ fps 退化估算（拿最贵的一项算）")
    print("        3 活跃 = %.2f µs / 帧；60fps 预算 16666.7 µs ⇒ 占 %.4f%%"
          % (cost["active_3_items"], cost["active_3_items"] / 16666.7 * 100.0))
    check("3 活跃时占一帧 < 1%", cost["active_3_items"] / 16666.7 * 100.0 < 1.0,
          cost["active_3_items"])
    print("        空状态 = %.2f µs / 帧 ⇒ 占 %.5f%%"
          % (cost["empty_no_cue_layer"], cost["empty_no_cue_layer"] / 16666.7 * 100.0))

    print("\n⑤ 异常安全（update 抛异常 ⇒ 不冒泡 / 清空本帧 / 限流 / 下帧恢复）")
    ex = check_exception_safety()
    check("不冒泡（调用方无感）", ex["no_raise"], ex["raised"])
    check("本帧清空（cs_states=[] / cs_active=False）", ex["cleared"])
    check("失败计数 = 1", ex["fail_count"] == 1, ex["fail_count"])
    check("异常后仍在下帧重试（update 累计被调用）", ex["update_calls"] >= 1, ex["update_calls"])
    check("健康日志限流：连跑 6 帧失败只写 1 条", ex["log_count"] == 1, ex["log_count"])
    if ex["log_sample"]:
        print("        日志样例：%s" % ex["log_sample"])
    check("换回好 runtime ⇒ 下一帧立刻恢复", ex["recovered"], ex)
    check("恢复后失败计数归零", ex["fail_reset"] == 0, ex["fail_reset"])

    print("\n⑥ 零副作用（tick 里不许建解码器 / 不写 seek 状态）")
    se = check_no_side_effects()
    check("make_player 调用次数 == 0", se["make_player_calls"] == 0, se["make_player_calls"])
    check("_cue_players 仍为空", se["cue_players_empty"])
    check("_cue_seek_state 未被 tick 写", se["seek_state_untouched"])

    print("")
    if fails:
        print("TICK_FAIL  %d 项不通过：%s" % (len(fails), "、".join(fails)))
        return 1
    print("TICK_OK  一致 / 早退 / 开销 / 异常安全 / 零副作用 全过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
