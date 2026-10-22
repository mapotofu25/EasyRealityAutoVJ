# -*- coding: utf-8 -*-
"""QA 第二轮：M1 坐标续接复验（★C）。反复切换开关，测每帧跳变与累积偏差。"""
import sys, types, math
sys.path.insert(0, r"P:\AutoVJ\src")
import engine as E

class _C:
    t = 1000.0
def fp():
    return _C.t
E.time = types.SimpleNamespace(perf_counter=fp, monotonic=fp)

BL = 60.0 / 128.0
ANCHOR = 1.0
SID = 7
GRID = {"bpm": 128.0, "beat_len": BL, "anchor": ANCHOR, "conf": 0.9, "src": "vdj"}


class Dummy:
    pass


def mk(cfg):
    d = Dummy()
    d.cfg = cfg
    d._song_grid_lookup = {SID: GRID}
    d._grid_clock = E.GridClock()
    d._grid_active_sid = None
    d._grid_coord_delta = 0.0
    d._grid_last_pos = 0.0
    d._pos_ref = None
    d.switch_pos = 0.0
    d._next_due = None
    d._due_off = 0.0
    d._bar_off_user = None
    d.mode = "normal"
    d.ALIGN_MIN_OFFSET = 1.0
    C = E.AutoVJEngine
    for n in ("_beat_pos", "_beat_pos_grid", "_grid_state", "_align_width", "_bar_off",
              "_bar_floor", "_next_switch_due", "_current_interval",
              "beats_to_next_switch", "switch_countdown"):
        setattr(d, n, types.MethodType(getattr(C, n), d))
    # 额外方法（存在则绑）
    if hasattr(C, "_reset_grid_clock"):
        d._reset_grid_clock = types.MethodType(C._reset_grid_clock, d)
    if hasattr(C, "set_song_grids"):
        d.set_song_grids = types.MethodType(C.set_song_grids, d)
    return d


def snap_at(t, audio_bpm=128.0, recognized=True, votes=300):
    if audio_bpm and audio_bpm > 0:
        be = 60.0 / audio_bpm
        cnt = int(t / be); ph = (t / be) - cnt
    else:
        cnt = 0; ph = 0.0
    return {
        "bpm": audio_bpm, "beat_active": (audio_bpm > 0), "beat_count": cnt, "beat_phase": ph,
        "downbeat_off": 0.0, "energy": 0.5, "energy_trend": 0.0,
        "recognized_song_id": (SID if recognized else -1),
        "recognized_offset": (ANCHOR + t if recognized else -1.0),
        "recognized_rate": 1.0, "recognized_t": _C.t, "recognized_votes": votes,
    }


def run_toggle(label, n_toggles=20, interval=2.0, audio_bpm=128.0, on_off_fn=None,
               recognized_fn=None):
    d = mk({"auto": {"beat_grid": True}, "mode": {"sensitivity": 1}})
    dt = 1/60.0
    total = n_toggles * interval + 2.0
    steps = int(total / dt)
    on = True
    last_toggle = 0.0
    prev = None
    jumps = []
    deltas = []
    n = 0
    for i in range(steps):
        t = i * dt
        _C.t = 1000.0 + t
        # 到点翻转开关
        if t - last_toggle >= interval and n < n_toggles:
            on = not on
            d.cfg["auto"]["beat_grid"] = on
            last_toggle = t
            n += 1
        rec = True if recognized_fn is None else recognized_fn(t)
        snap = snap_at(t, audio_bpm=audio_bpm, recognized=rec)
        pos = d._beat_pos(snap)
        if prev is not None:
            j = abs(pos - prev)
            if j > 0.5:
                jumps.append((t, j, on))
        prev = pos
        deltas.append(d._grid_coord_delta)
    print("[%s]" % label)
    print("  切换 %d 次；单帧跳变 >0.5 拍 的个数 = %d" % (n, len(jumps)))
    for (tj, j, on_) in jumps[:8]:
        print("     t=%.2fs 跳变 %.3f 拍 (此时开关=%s)" % (tj, j, "开" if on_ else "关"))
    print("  delta 末值=%.3f，量级范围 [%.3f, %.3f]" % (deltas[-1], min(deltas), max(deltas)))
    return d, jumps


print("=== C-1 连续切换 20 次（每 2s 切一次，音频 bpm 与网格一致）===")
run_toggle("bpm一致(128)", n_toggles=20, interval=2.0, audio_bpm=128.0)

print()
print("=== C-2 audio bpm 与网格不一致（音频检测成半速 64）===")
run_toggle("bpm不一致(音频64/网格128)", n_toggles=20, interval=2.0, audio_bpm=64.0)

print()
print("=== C-2b 音频检测不到拍（bpm=0，走 120BPM 回退钟；网格 128）===")
run_toggle("bpm=0回退钟/网格128", n_toggles=20, interval=2.0, audio_bpm=0.0)

print()
print("=== C-3 切换恰逢识别丢失（t in [5,7] 未识别）===")
run_toggle("识别丢失段", n_toggles=10, interval=2.0,
           recognized_fn=lambda t: not (5.0 <= t <= 7.0))

print()
print("=== C-4 换歌瞬间切换（t>6 换 sid=8，但 lookup 里没有 → 视为无网格）===")
def rec_fn(t):
    return True
d = mk({"auto": {"beat_grid": True}, "mode": {"sensitivity": 1}})
# 换歌：把 lookup 里 sid 换成 8（无网格）
dt = 1/60.0; prev = None; jumps = []
on = True
for i in range(int(20/dt)):
    t = i*dt; _C.t = 1000.0+t
    if abs(t - 10.0) < dt/2:
        d._song_grid_lookup = {}      # 换到没网格的歌
        d.cfg["auto"]["beat_grid"] = False
    if abs(t - 14.0) < dt/2:
        d._song_grid_lookup = {SID: GRID}; d.cfg["auto"]["beat_grid"] = True
    snap = snap_at(t)
    if t > 10 and t < 14:
        snap["recognized_song_id"] = -1
    pos = d._beat_pos(snap)
    if prev is not None and abs(pos-prev) > 0.5:
        jumps.append((t, abs(pos-prev)))
    prev = pos
print("[换歌瞬间] 跳变>0.5 的个数=%d" % len(jumps))
for tj, j in jumps[:8]:
    print("     t=%.2fs 跳变 %.3f 拍" % (tj, j))

print()
print("=== C-5 delta 在 set_song_grids / _reset_grid_clock 是否清零 ===")
d = mk({"auto": {"beat_grid": True}, "mode": {"sensitivity": 1}})
# 制造非零 delta：开->关
dt = 1/60.0
for i in range(int(3/dt)):
    t = i*dt; _C.t = 1000.0+t
    d._beat_pos(snap_at(t))
d.cfg["auto"]["beat_grid"] = False
for i in range(int(0.5/dt)):
    t = 3.0 + i*dt; _C.t = 1000.0+t
    d._beat_pos(snap_at(t))
print("  关掉后 delta=%.3f, active_sid=%r, last_pos=%.3f" % (d._grid_coord_delta, d._grid_active_sid, d._grid_last_pos))
d._reset_grid_clock()
print("  _reset_grid_clock 后 delta=%.3f, active_sid=%r, last_pos=%.3f" % (d._grid_coord_delta, d._grid_active_sid, d._grid_last_pos))
print("  clock.sid=%r (reset 是否清了 GridClock)" % d._grid_clock.sid)
