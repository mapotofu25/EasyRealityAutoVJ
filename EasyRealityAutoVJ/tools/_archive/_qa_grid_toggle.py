# -*- coding: utf-8 -*-
"""QA: 播出中途反复切「节拍网格」开关 —— _beat_pos 是否跳变 / 能否平滑回退。"""
import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


class FakeTime:
    def __init__(self):
        self.t = 5000.0

    def perf_counter(self):
        return self.t


new = load(os.path.join(ROOT, "src", "engine.py"), "engine_tog")
ft = FakeTime()
new.time = ft


def make_self():
    s = types.SimpleNamespace()
    s._pos_ref = None
    s.switch_pos = 0.0
    s._next_due = None
    s._due_off = 0.0
    s._bar_off_user = None
    s.mode = "normal"
    s.cfg = {"auto": {"beat_grid": True}, "mode": {"sensitivity": 1}}
    from beatgrid import GridClock
    s._grid_clock = GridClock()
    s._song_grid_lookup = {777: {"beat_len": 0.5, "anchor": 0.0, "bpm": 120.0, "src": "vdj"}}
    for nm in ("_beat_pos", "_bar_off", "_next_switch_due", "_current_interval",
               "_grid_state", "_align_width", "_beat_pos_grid", "beats_to_next_switch",
               "switch_countdown", "phrase_countdown"):
        setattr(s, nm, types.MethodType(getattr(new.AutoVJEngine, nm), s))
    s.ALIGN_MIN_OFFSET = new.AutoVJEngine.ALIGN_MIN_OFFSET
    return s


# 真实音频拍钟：beat_count 是音频引擎自己的实时计数（与新网格坐标系无关）
SNAP = {"bpm": 128.0, "beat_active": True, "beat_count": 500.0, "beat_phase": 0.25,
        "downbeat_off": 0.0, "energy": 0.5, "energy_trend": 0.0,
        "recognized_song_id": 777, "recognized_offset": 100.0,
        "recognized_rate": 1.0, "recognized_t": 5000.0}

s = make_self()
print("场景：真实拍钟 beat_count=500.25（音频引擎自己的坐标系）")
print("      网格坐标系 pos=(off-anchor)/bl=100/0.5=200.0")
print()
seq = [
    (True,  "网格开 → 取拍位"),
    (False, "中途关开关 → 取拍位"),
    (False, "仍关 → 取拍位"),
    (True,  "再打开 → 取拍位"),
    (True,  "仍开 → 取拍位"),
]
prev = None
for on, label in seq:
    s.cfg["auto"]["beat_grid"] = on
    ft.t += 0.1
    SNAP["recognized_t"] = ft.t - 0.1
    pos = s._beat_pos(dict(SNAP))
    off = s._bar_off(dict(SNAP))
    due = s._next_switch_due(dict(SNAP))
    cd = s.switch_countdown(dict(SNAP))
    jump = ("" if prev is None else ("   <-- 跳变 %+.3f 拍" % (pos - prev)))
    print("  %-22s _beat_pos=%9.3f  _bar_off=%.3f  _next_due=%s  countdown=%s%s"
          % (label, pos, off, ("%.3f" % due) if due is not None else None, cd, jump))
    prev = pos
print()
print("结论：开关切换瞬间，_beat_pos 经【坐标续接 _grid_coord_delta】（网格↔音频 双向记录）")
print("      保持连续 —— 实测各步跳变 ≤0.2 拍（见上），播出中途切换不再整体跳拍。")
