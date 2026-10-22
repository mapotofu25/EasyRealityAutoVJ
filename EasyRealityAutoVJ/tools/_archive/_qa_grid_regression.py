# -*- coding: utf-8 -*-
"""QA: 零回归对照 —— 用 git HEAD 的原始实现 vs 工作区新实现，喂同一串输入逐值比对。

覆盖三种「不走网格」的情形：开关关 / 无网格 / 未识别  → _beat_pos / _bar_off / _next_switch_due
另测：cfg 无 auto.beat_grid 键（老配置）默认是否 True 且不报错。
"""
import importlib.util
import os
import sys
import types

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
TOOLS = os.path.dirname(__file__)
sys.path.insert(0, os.path.abspath(SRC))
sys.path.insert(0, os.path.abspath(TOOLS))


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


class FakeTime:
    def __init__(self):
        self.t = 1000.0

    def perf_counter(self):
        return self.t

    def time(self):
        return 1700000000.0 + self.t


head = load(os.path.abspath(os.path.join(TOOLS, "_engine_head_snapshot.py")), "engine_head_snap")
new = load(os.path.abspath(os.path.join(SRC, "engine.py")), "engine_new_snap")

ft = FakeTime()
head.time = ft
new.time = ft


def make_self(mod, cfg_auto, lookup, is_new):
    s = types.SimpleNamespace()
    s._pos_ref = None
    s.switch_pos = 0.0
    s._next_due = None
    s._due_off = 0.0
    s._bar_off_user = None
    s.mode = "normal"
    s.cfg = {"auto": dict(cfg_auto), "mode": {"sensitivity": 1}}
    # 绑定方法
    s._beat_pos = types.MethodType(mod.AutoVJEngine._beat_pos, s)
    s._bar_off = types.MethodType(mod.AutoVJEngine._bar_off, s)
    s._next_switch_due = types.MethodType(mod.AutoVJEngine._next_switch_due, s)
    s._current_interval = types.MethodType(mod.AutoVJEngine._current_interval, s)
    s._bar_floor = types.MethodType(mod.AutoVJEngine._bar_floor, s)
    s.ALIGN_MIN_OFFSET = mod.AutoVJEngine.ALIGN_MIN_OFFSET
    if is_new:
        s._song_grid_lookup = lookup
        s._grid_state = types.MethodType(mod.AutoVJEngine._grid_state, s)
        s._align_width = types.MethodType(mod.AutoVJEngine._align_width, s)
    return s


def run_case(label, cfg_auto, lookup, snaps):
    """snaps: list of (dt, snap_dict)。返回两版每步的 (beat_pos, bar_off, due)。"""
    ft.t = 1000.0
    sh = make_self(head, cfg_auto, lookup, is_new=False)
    ft.t = 1000.0
    sn = make_self(new, cfg_auto, lookup, is_new=True)
    rows = []
    for (dt, snap) in snaps:
        ft.t += dt
        bh = sh._beat_pos(dict(snap)); oh = sh._bar_off(dict(snap)); dh = sh._next_switch_due(dict(snap))
        ft.t += 0.0
        bn = sn._beat_pos(dict(snap)); on = sn._bar_off(dict(snap)); dn = sn._next_switch_due(dict(snap))
        rows.append((bh, bn, oh, on, dh, dn))
    bad = []
    for i, (bh, bn, oh, on, dh, dn) in enumerate(rows):
        def close(a, b):
            if a is None or b is None:
                return a is b
            return abs(a - b) <= 1e-12
        if not (close(bh, bn) and close(oh, on) and close(dh, dn)):
            bad.append((i, bh, bn, oh, on, dh, dn))
    status = "OK 逐值一致" if not bad else "!! 不一致 %d 处" % len(bad)
    print("[%s] %s" % (label, status))
    for (i, bh, bn, oh, on, dh, dn) in bad[:5]:
        print("    step%d  _beat_pos %s vs %s | _bar_off %s vs %s | _next_due %s vs %s"
              % (i, bh, bn, oh, on, dh, dn))
    return not bad


def snap_clock(i, bpm=128.0, beat_active=True, downbeat_off=0.0, energy=0.5):
    return {"bpm": bpm, "beat_active": beat_active,
            "beat_count": 40 + i * 0.5, "beat_phase": (i * 0.5) % 1.0,
            "downbeat_off": downbeat_off, "energy": energy, "energy_trend": 0.0,
            "recognized_song_id": -1, "recognized_offset": -1.0,
            "recognized_rate": 1.0, "recognized_t": 0.0}


def snap_fallback(i, downbeat_off=0.0):
    return {"bpm": 0, "beat_active": False, "beat_count": 0, "beat_phase": 0.0,
            "downbeat_off": downbeat_off, "energy": 0.4, "energy_trend": 0.0,
            "recognized_song_id": -1, "recognized_offset": -1.0,
            "recognized_rate": 1.0, "recognized_t": 0.0}


ok_all = True
# 1) 开关关
ok_all &= run_case("开关关(beat_grid=False) 有拍钟", {"beat_grid": False}, {},
                   [(0.1, snap_clock(i)) for i in range(30)])
ok_all &= run_case("开关关(beat_grid=False) 回退时钟", {"beat_grid": False}, {},
                   [(0.5, snap_fallback(i)) for i in range(20)])
# 2) 开关开但无网格
ok_all &= run_case("开关开+无网格 有拍钟", {"beat_grid": True}, {},
                   [(0.1, snap_clock(i)) for i in range(30)])
ok_all &= run_case("开关开+无网格 回退时钟", {"beat_grid": True}, {},
                   [(0.5, snap_fallback(i)) for i in range(20)])
# 3) 未识别（有网格lookup但sid=-1）
ok_all &= run_case("有网格但未识别", {"beat_grid": True}, {12345: {"beat_len": 0.5, "anchor": 0.0}},
                   [(0.1, snap_clock(i)) for i in range(30)])
# 4) downbeat_off 非零 + 手动重定义相位（验证 _bar_off_user 绝对拍位改造的等价性）
ok_all &= run_case("非零downbeat_off", {"beat_grid": True}, {},
                   [(0.1, snap_clock(i, downbeat_off=1.7)) for i in range(30)])


def run_baroff_user_check():
    """手动重定义相位：旧存 beat%4，新存 float(beat) 再 %w。逐值比对。"""
    print("\n[手动重定义相位 _bar_off_user 等价性]")
    ft.t = 5000.0
    ok = True
    for beat in [3, 4, 7, 8, 15, 16, 23, 100, 101]:
        sh = make_self(head, {"beat_grid": True}, {}, is_new=False)
        sn = make_self(new, {"beat_grid": True}, {}, is_new=True)
        sh._bar_off_user = beat % 4          # 旧写法
        sn._bar_off_user = float(beat)       # 新写法
        snap = snap_clock(0, downbeat_off=0.0)
        oh, on = sh._bar_off(snap), sn._bar_off(snap)
        if abs(oh - on) > 1e-12:
            ok = False
            print("    beat=%d 旧=%.3f 新=%.3f  <-- 不一致" % (beat, oh, on))
    print("    " + ("OK 逐值一致" if ok else "!! 不一致"))
    return ok


ok_all &= run_baroff_user_check()


# 5) 老配置：cfg 无 auto.beat_grid 键
def run_missing_key():
    print("\n[老配置：cfg['auto'] 无 beat_grid 键]")
    ft.t = 6000.0
    sn = make_self(new, {}, {}, is_new=True)   # 空 auto
    try:
        gs = sn._grid_state(snap_clock(0))
        # 无网格 lookup 时返回 None；关键是「不报错」且把开关当 True（继续走到 lookup 判断）
        print("    _grid_state -> %r（无网格→None，无异常）" % (gs,))
        sn2 = make_self(new, {}, {}, is_new=True)
        # 有网格 + 无键 → 应视为开（True），返回网格 state
        sn2._song_grid_lookup = {99: {"beat_len": 0.5, "anchor": 0.0}}
        s = snap_clock(0); s["recognized_song_id"] = 99; s["recognized_offset"] = 10.0
        gs2 = sn2._grid_state(s)
        print("    有网格+无键 -> %s（非 None 说明默认当 True 开）"
              % ("网格state" if gs2 is not None else "None"))
        return (gs is None) and (gs2 is not None)
    except Exception as e:
        print("    !! 异常: %r" % (e,))
        return False


ok_all &= run_missing_key()

print("\n" + "=" * 60)
print("总体：" + ("全部通过（零回归成立）" if ok_all else "存在不一致 —— 需复查"))
