# -*- coding: utf-8 -*-
"""M1（开关切换跳变）+ M2（票数门槛）引擎级实测。

M1：播出中途切「节拍网格」开关，_beat_pos 是否连续（对比"改动前"的坐标跳变）。
M2：低票档（歧义）的坏 offset 是否被挡在网格校正之外（不硬重锚、不微调）。

用法： P:\\AutoVJ\\venv\\Scripts\\python.exe tools/_grid_m1m2_test.py
"""
import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from beatgrid import GridClock  # noqa: E402


def load_engine():
    spec = importlib.util.spec_from_file_location(
        "engine_m1m2", os.path.join(ROOT, "src", "engine.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["engine_m1m2"] = m
    spec.loader.exec_module(m)
    return m


eng = load_engine()


class FakeTime:
    def __init__(self, t=9000.0):
        self.t = t

    def perf_counter(self):
        return self.t

    def time(self):
        return 1600000000.0 + self.t


FT = FakeTime()
eng.time = FT


def make_self(bl=0.5, anchor=0.0, sid=777, beat_grid=True):
    s = types.SimpleNamespace()
    s._pos_ref = None
    s.switch_pos = 0.0
    s._next_due = None
    s._due_off = 0.0
    s._bar_off_user = None
    s.mode = "normal"
    s.cfg = {"auto": {"beat_grid": beat_grid}, "mode": {"sensitivity": 1}}
    s._grid_clock = GridClock()
    s._song_grid_lookup = {sid: {"beat_len": bl, "anchor": anchor,
                                 "bpm": 120.0, "src": "vdj"}}
    s._grid_active_sid = None
    s._grid_coord_delta = 0.0
    s._grid_last_pos = 0.0
    for nm in ("_beat_pos", "_beat_pos_grid", "_grid_observe", "_grid_state",
               "_align_width", "_bar_off", "_next_switch_due", "_current_interval"):
        setattr(s, nm, types.MethodType(getattr(eng.AutoVJEngine, nm), s))
    s.GRID_MIN_VOTES = eng.AutoVJEngine.GRID_MIN_VOTES
    s.ALIGN_MIN_OFFSET = eng.AutoVJEngine.ALIGN_MIN_OFFSET
    return s


def _drive(s, snap):
    """按新 API 驱动一帧：先 `_grid_state`（内部 `_grid_observe` 喂时钟 / 锁定判定），
    仅在**已锁定**时取网格拍位；未锁定返回 None（此时引擎走原有拍钟，不进入网格模式）。"""
    gs = s._grid_state(snap)
    if gs is None:
        return None
    return s._beat_pos_grid(snap, gs)


# ======================================================================
# M1：开关切换连续性
# ======================================================================
def _snap(t, bl=0.5, votes=300):
    """音频拍钟与网格都"真实前进"：beat_count 与 recognized_offset 同步演进。"""
    return {"bpm": 128.0, "beat_active": True,
            "beat_count": 500.0 + 2.0 * t, "beat_phase": 0.0,
            "downbeat_off": 0.0, "energy": 0.5, "energy_trend": 0.0,
            "recognized_song_id": 777, "recognized_offset": 100.0 + t,
            "recognized_rate": 1.0, "recognized_t": FT.t, "recognized_votes": votes}


def test_m1():
    print("=" * 78)
    print("M1：播出中途切「节拍网格」开关 —— _beat_pos 连续性")
    print("  场景：音频拍钟 pos=500+2t，网格坐标 pos=200+2t（原点差 -300 拍）")
    print("=" * 78)
    s = make_self()
    FT.t = 9000.0
    seq = [(True, "网格开")] * 4 + [(False, "关开关")] * 4 + [(True, "再打开")] * 4
    prev = None
    max_jump = 0.0
    rows = []
    k = 0
    last_tick = None
    for on, label in seq:
        s.cfg["auto"]["beat_grid"] = on
        FT.t = 9000.0 + k * 0.1
        # 识别每 0.5s 更新一次 offset/recognized_t
        tick = round(FT.t * 2) / 2
        snap = _snap(FT.t - 9000.0, votes=300)
        snap["recognized_t"] = tick
        snap["recognized_offset"] = 100.0 + (tick - 9000.0)
        pos = s._beat_pos(dict(snap))
        jump = 0.0 if prev is None else pos - prev
        max_jump = max(max_jump, abs(jump))
        rows.append((label, pos, jump))
        prev = pos
        k += 1
    for label, pos, jump in rows:
        print("  %-8s _beat_pos=%10.3f   %s" % (label, pos, "跳变 %+.3f 拍" % jump))
    print("  → 最大单帧跳变 = %.3f 拍（改动前实测约 ±300 拍）" % max_jump)
    # 对照：改动前（无坐标续接）
    s2 = make_self()
    prev = None
    max_old = 0.0
    k = 0
    for on, label in seq:
        s2.cfg["auto"]["beat_grid"] = on
        FT.t = 9000.0 + k * 0.1
        tick = round(FT.t * 2) / 2
        snap = _snap(FT.t - 9000.0, votes=300)
        snap["recognized_t"] = tick
        snap["recognized_offset"] = 100.0 + (tick - 9000.0)
        gs = s2._grid_state(snap)
        if gs is not None:
            pos = s2._beat_pos_grid(snap, gs)
        else:                                     # 旧实现：直接返回音频坐标，无续接
            pos = snap["beat_count"] + snap["beat_phase"]
        if prev is not None:
            max_old = max(max_old, abs(pos - prev))
        prev = pos
        k += 1
    print("  [对照] 改动前（无坐标续接）最大单帧跳变 = %.3f 拍" % max_old)
    ok = max_jump < 1.0
    print("  [%s] M1 无跳变（<1 拍）" % ("PASS" if ok else "FAIL"))
    return ok


# ======================================================================
# M2：票数门槛
# ======================================================================
def test_m2():
    print("\n" + "=" * 78)
    print("M2：低票档坏 offset 是否被挡在网格校正之外")
    print("  bad=44 票时 offset 跳到错坐标（+490 拍）；good=241 票时正常校正")
    print("=" * 78)
    bl, anchor = 0.5, 0.0
    s = make_self(bl=bl, anchor=anchor)
    FT.t = 5000.0
    # 预置：先在正确位置锁定并稳定若干 tick
    for k in range(6):
        FT.t = 5000.0 + k * 0.5
        snap = {"bpm": 0, "beat_active": False,
                "recognized_song_id": 777, "recognized_offset": 100.0 + k * 0.5,
                "recognized_rate": 1.0, "recognized_t": FT.t, "recognized_votes": 241}
        _drive(s, snap)
    pos_before = _drive(s, _snap2(777, 100.0 + 3.0, FT.t, 241))
    print("  校正前网格拍位 = %.3f 拍（真值 %.3f）" % (pos_before, (100.0 + 3.0) / bl))

    # 坏 tick：votes=44，offset 跳到 +490 拍（245s）的错坐标
    FT.t += 0.5
    bad = _snap2(777, (100.0 + 3.5) + 245.0, FT.t, 44)
    pos_gated = _drive(s, bad)
    print("  喂入 votes=44(坏,offset+245s) → 网格拍位 = %.3f 拍（应仍≈%.1f，不跳）"
          % (pos_gated, (100.0 + 3.5) / bl))

    # 对照：同 offset 但 votes=300（越过门槛）。
    # ★ 断言已**按第二轮修法更新**（旧断言过时，QA 已裁决）：
    #   修法②把「单窗大 offset 立即硬重锚」改成了「|err|>jump_max(32 拍) 必须**连续 2 个
    #   同向强窗**确认才重锚」。因此**单个**强坏窗(+490 拍)现在**不**应把拍位拽走（旧断言
    #   期望它立刻重锚，正是被有意移除的行为）；**连续第 2 个同向强坏窗**才应重锚。
    #   QA 独立复核确认新行为正确：单次强坏窗 ⇒ 拍位不跳；连续第 2 个 ⇒ 才重锚。
    s2 = make_self(bl=bl, anchor=anchor)
    FT.t = 5000.0
    for k in range(6):
        FT.t = 5000.0 + k * 0.5
        snap = _snap2(777, 100.0 + k * 0.5, FT.t, 241)
        _drive(s2, snap)
    pos_before2 = _drive(s2, _snap2(777, 100.0 + 3.0, FT.t, 241))
    # 第 1 个强坏窗（votes=300, +245s）→ 单窗不重锚
    FT.t += 0.5
    bad2a = _snap2(777, (100.0 + 3.5) + 245.0, FT.t, 300)
    pos_ungated = _drive(s2, bad2a)
    print("  [对照] 第 1 个 votes=300(越过门槛,offset+245s) → 网格拍位 = %.3f 拍（应仍≈%.1f，不跳）"
          % (pos_ungated, pos_before2))
    # 第 2 个**连续同向**强坏窗 → 此时才应硬重锚到错坐标
    FT.t += 0.5
    bad2b = _snap2(777, (100.0 + 3.5) + 245.0 + 0.5, FT.t, 300)
    pos_reanch = _drive(s2, bad2b)
    print("  [对照] 连续第 2 个同向强坏窗 → 网格拍位 = %.3f 拍（应硬重锚到错坐标）"
          % pos_reanch)
    ok_gate = abs(pos_gated - pos_before) < 2.0            # 低票：完全不动
    ok_one = abs(pos_ungated - pos_before2) < 2.0          # 单窗强坏：也不动（修法②新行为）
    ok_two = abs(pos_reanch - pos_before2) > 100.0         # 连续 2 窗同向：才重锚
    ok = ok_gate and ok_one and ok_two
    print("  [%s] M2 低票档不校正（时钟保持在原位置附近，未跳到错坐标）"
          % ("PASS" if ok_gate else "FAIL"))
    print("  [%s] M2 单窗强坏窗不重锚（|Δ|=%.2f 拍，<2）"
          % ("PASS" if ok_one else "FAIL", abs(pos_ungated - pos_before2)))
    print("  [%s] M2 连续第 2 个同向强坏窗才重锚（|Δ|=%.2f 拍，>100）"
          % ("PASS" if ok_two else "FAIL", abs(pos_reanch - pos_before2)))
    return ok


def _snap2(sid, off, ref_t, votes):
    return {"bpm": 0, "beat_active": False, "recognized_song_id": sid,
            "recognized_offset": off, "recognized_rate": 1.0,
            "recognized_t": ref_t, "recognized_votes": votes}


def main():
    ok1 = test_m1()
    ok2 = test_m2()
    print("\n" + "=" * 78)
    print("总体：%s" % ("M1/M2 均通过" if (ok1 and ok2) else "存在 FAIL"))
    return 0 if (ok1 and ok2) else 1


if __name__ == "__main__":
    sys.exit(main())
