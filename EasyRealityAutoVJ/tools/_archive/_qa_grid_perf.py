# -*- coding: utf-8 -*-
"""QA: 网格模式每帧新增耗时（仅数学，纯 CPU；用户未运行软件时执行）。"""
import importlib.util
import os
import sys
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


new = load(os.path.join(ROOT, "src", "engine.py"), "engine_perf")


def make_self(cfg_auto, lookup):
    s = types.SimpleNamespace()
    s._pos_ref = None
    s.switch_pos = 0.0
    s._next_due = None
    s._due_off = 0.0
    s._bar_off_user = None
    s.mode = "normal"
    s.cfg = {"auto": dict(cfg_auto), "mode": {"sensitivity": 1}}
    s._song_grid_lookup = lookup
    from beatgrid import GridClock as _GC
    s._grid_clock = _GC()
    for nm in ("_beat_pos", "_bar_off", "_next_switch_due", "_current_interval",
               "_grid_state", "_align_width", "_beat_pos_grid", "phrase_countdown"):
        setattr(s, nm, types.MethodType(getattr(new.AutoVJEngine, nm), s))
    s.ALIGN_MIN_OFFSET = new.AutoVJEngine.ALIGN_MIN_OFFSET
    return s


def bench(fn, n=200000):
    t0 = time.perf_counter()
    for _ in range(n):
        fn()
    return (time.perf_counter() - t0) / n * 1e6   # us/call


SNAP_G = {"bpm": 128.0, "beat_active": True, "beat_count": 40.0, "beat_phase": 0.5,
          "downbeat_off": 0.0, "energy": 0.5, "energy_trend": 0.0,
          "recognized_song_id": 777, "recognized_offset": 60.0,
          "recognized_rate": 1.0, "recognized_t": 555.0}
SNAP_N = dict(SNAP_G, recognized_song_id=-1, recognized_offset=-1.0)
GRID = {777: {"beat_len": 0.5, "anchor": 1.0, "bpm": 120.0, "conf": 0.9, "src": "vdj"}}

# 网格模式（开关开 + 有网格 + 已识别）
sg = make_self({"beat_grid": True}, GRID)
# 非网格（开关关）
sn = make_self({"beat_grid": False}, GRID)

beat_g = bench(lambda: sg._beat_pos(dict(SNAP_G)))
beat_n = bench(lambda: sn._beat_pos(dict(SNAP_N)))
pc = bench(lambda: sg.phrase_countdown(dict(SNAP_G)))

# 纯 GridClock
from beatgrid import GridClock
clk = GridClock()
clk.update(777, 0.5, 1.0, 60.0, 1.0, 555.0)
t0 = time.perf_counter()
N = 500000
for _ in range(N):
    clk.update(777, 0.5, 1.0, 60.0, 1.0, 555.0)
    clk.pos(555.0)
clock_us = (time.perf_counter() - t0) / N * 1e6

print("每帧单次调用耗时（微秒, us）：")
print("  _beat_pos  网格模式      : %8.3f us" % beat_g)
print("  _beat_pos  非网格模式    : %8.3f us" % beat_n)
print("  phrase_countdown 网格    : %8.3f us" % pc)
print("  GridClock.update+pos     : %8.3f us" % clock_us)
print("  网格模式单次新增 ≈ %.3f us" % (beat_g - beat_n))
print()
print("帧预算推算（每次 _beat_pos 调用；引擎一帧通常调用数次 + HUD 一次）：")
print("  若一帧共 10 次 _beat_pos：新增 %.1f us；60fps 帧预算 16667us → 占比 %.4f%%"
      % ((beat_g - beat_n) * 10, (beat_g - beat_n) * 10 / 16667 * 100))
