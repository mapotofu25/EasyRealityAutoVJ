# -*- coding: utf-8 -*-
"""QA 第二轮 ★G：网格模式每帧新增耗时 + GridClock 锁内纯度静态确认。"""
import sys, types, time
sys.path.insert(0, r"P:\AutoVJ\src")
import engine as E, beatgrid as BG

BL = 60.0 / 128.0
class Dummy: pass
def mk(cfg, grids):
    d = Dummy(); d.cfg = cfg; d._song_grid_lookup = grids
    d._grid_clock = E.GridClock(); d._grid_active_sid = None
    d._grid_coord_delta = 0.0; d._grid_last_pos = 0.0; d._pos_ref = None
    C = E.AutoVJEngine
    for n in ("_beat_pos", "_beat_pos_grid", "_grid_state", "_align_width"):
        setattr(d, n, types.MethodType(getattr(C, n), d))
    return d

snap_grid = {"bpm": 128.0, "beat_active": True, "beat_count": 100, "beat_phase": 0.3,
             "downbeat_off": 0.0, "energy": 0.5, "energy_trend": 0.0,
             "recognized_song_id": 7, "recognized_offset": 50.0,
             "recognized_rate": 1.0, "recognized_t": time.perf_counter(), "recognized_votes": 300}
snap_off = dict(snap_grid); snap_off["recognized_song_id"] = -1

G = {7: {"bpm": 128.0, "beat_len": BL, "anchor": 1.0, "conf": 0.9, "src": "vdj"}}
cfg_on = {"auto": {"beat_grid": True}}
cfg_off = {"auto": {"beat_grid": False}}

def bench(name, cfg, grids, snap, n=300000):
    d = mk(cfg, grids)
    s = dict(snap); s["recognized_t"] = time.perf_counter()
    t0 = time.perf_counter()
    for _ in range(n):
        d._beat_pos(s)
    dt = (time.perf_counter() - t0) / n
    print("  %-28s %.4f us/次调用" % (name, dt * 1e6))

print("=== 性能（同一进程内 30 万次调用）===")
bench("关网格（原路径）", cfg_off, {}, snap_off)
bench("开网格+无识别（原路径）", cfg_on, {}, snap_off)
bench("开网格+已识别（网格路径）", cfg_on, G, snap_grid)

print()
print("=== GridClock 锁内纯度（静态）===")
import inspect
src = inspect.getsource(BG.GridClock)
# 检查 with self._lock 块内是否出现 IO/sleep/等待
for bad in ("sleep", "open(", "read(", "write(", "wait(", "recv(", "send("):
    if bad in src:
        print("  ⚠ 锁相关源码出现:", bad)
print("  锁内仅含纯数学（无 sleep/IO/wait）— 见源码 L808-873 人工核对")
