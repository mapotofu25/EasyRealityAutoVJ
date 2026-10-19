# -*- coding: utf-8 -*-
"""阻塞项 1 配套：允许 pos 后退（硬重锚）后，_auto_switch_tick 是否仍稳。

构造一个最小引擎替身，把「正常前进 → 4 拍 Loop → 正常前进」的真实拍位序列喂进
`_beat_pos` + `_auto_switch_tick`，验证：
  ① 不抛异常；
  ② Loop 期间 pos 反复后退，但切换次数不发散（不出现每帧连切）；
  ③ Loop 结束后恢复正常切换节奏。
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
        "engine_integ", os.path.join(ROOT, "src", "engine.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["engine_integ"] = m
    spec.loader.exec_module(m)
    return m


eng = load_engine()


class FT:
    def __init__(self):
        self.t = 1000.0

    def perf_counter(self):
        return self.t


FTM = FT()
eng.time = FTM


class Layer:
    def __init__(self):
        self.fixed = -1
        self.clips = [1, 2, 3]
        self.trans_ms = 500
        self.is_kv = False


def make_self():
    s = types.SimpleNamespace()
    s._pos_ref = None
    s.switch_pos = 0.0
    s._next_due = None
    s._due_off = 0.0
    s._bar_off_user = None
    s.mode = "normal"
    s.locked = False
    s.layers = [Layer()]
    s.switch_count = 0
    s.cfg = {"auto": {"beat_grid": True, "cooldown_sec": 20, "seed": 0},
             "mode": {"sensitivity": 1}}
    s._song_grid_lookup = {7: {"beat_len": 0.5, "anchor": 0.0, "bpm": 120.0, "src": "vdj"}}
    s._grid_clock = GridClock()
    s._grid_active_sid = None
    s._grid_coord_delta = 0.0
    s._grid_last_pos = 0.0
    for nm in ("_beat_pos", "_beat_pos_grid", "_grid_state", "_align_width", "_bar_off",
               "_next_switch_due", "_current_interval", "_auto_switch_tick"):
        setattr(s, nm, types.MethodType(getattr(eng.AutoVJEngine, nm), s))
    s.GRID_MIN_VOTES = eng.AutoVJEngine.GRID_MIN_VOTES
    s.ALIGN_MIN_OFFSET = eng.AutoVJEngine.ALIGN_MIN_OFFSET
    s._switch_layer_random = types.MethodType(
        lambda self, lay, snap: setattr(self, "switch_count", self.switch_count + 1), s)
    return s


def off_true(t):
    """真实原曲位置（秒）：正常 → 4 拍 Loop(2s) → 正常。"""
    if t < 20.0:
        return 100.0 + t
    if t < 40.0:
        return 100.0 + 20.0 + ((t - 20.0) % 2.0)     # 4 拍 Loop
    return 100.0 + 20.0 + (t - 40.0)


def main():
    s = make_self()
    per_sec = {}
    prev_pos = None
    max_back = 0.0
    t = 0.0
    while t <= 70.0:
        FTM.t = 1000.0 + t
        tick = round(t * 2) / 2          # 识别每 0.5s
        snap = {"bpm": 128.0, "beat_active": True,
                "beat_count": 0.0, "beat_phase": 0.0,
                "downbeat_off": 0.0, "energy": 0.5, "energy_trend": 0.0,
                "recognized_song_id": 7, "recognized_offset": off_true(tick),
                "recognized_rate": 1.0, "recognized_t": 1000.0 + tick,
                "recognized_votes": 300}
        before = s.switch_count
        s._auto_switch_tick(snap)                # 内部会调 _beat_pos
        pos = s._beat_pos(dict(snap))
        if prev_pos is not None:
            d = pos - prev_pos
            if d < 0:
                max_back = max(max_back, abs(d))
        prev_pos = pos
        sec = int(t)
        per_sec[sec] = per_sec.get(sec, 0) + (s.switch_count - before)
        t += 0.1

    # 每秒切换次数（应远小于每秒 _auto_switch_tick 调用数=10）
    worst_sec = max(per_sec.values())
    n_switch = s.switch_count
    print("=" * 78)
    print("引擎完整性：正常 → 4拍Loop → 正常，共 70s（_auto_switch_tick 每 0.1s 一次）")
    print("  总切换次数 = %d" % n_switch)
    print("  单秒最多切换 = %d 次（每帧调用=10；若为 10 说明每帧连切=抽搐）" % worst_sec)
    print("  Loop 期间最大单帧后退 = %.3f 拍（硬重锚允许后退）" % max_back)
    ok = (worst_sec <= 1) and (n_switch >= 1)
    print("  [%s] 允许 pos 后退不导致切换抽搐/异常" % ("PASS" if ok else "FAIL"))
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
