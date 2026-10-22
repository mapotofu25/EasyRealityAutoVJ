# -*- coding: utf-8 -*-
"""回归测试：静默冻帧盲区（2026-10-01 用户实测"播放中卡在某一帧、ui_stall.log 47 分钟无记录"）。

盲区根因（两条）：
  ① 活跃标记循环只遍历 `lay.cache`：`lay.player` 一旦脱离缓存（自动匹配换池 / LRU 驱逐 /
     预热切换）就**永远不会被置为活跃** ⇒ 解码线程挂起 ⇒ 画面冻在最后一帧；而看门狗
     （`_heal_stalled`）又跳过"非活跃"播放器 ⇒ **一条日志都没有**。
  ② `_heal_stalled` 顶部两条提前返回（`player is None` / `_frames<=0`）把
     `player 缺失`、`有帧前就 ended` 两类直接放过去，而引擎里没有别的地方负责恢复它们。

本测试**离屏、不启动 GUI 业务、用可控时钟**，直接跑**真实的** `AutoVJEngine._tick_locked`
（把音频/合成/输出等重活 stub 掉，只保留真实的活跃标记 + `_heal_stalled` + `_heal_silent`），
断言：
  (a) 根治证据：把 `lay.player` 从 `lay.cache` 里移走后跑一次活跃标记 ⇒ 它仍是 `_active=True`；
  (b) 反证：缓存里"非当前"的播放器仍会被挂起（省 CPU 行为没被破坏）；
  (c) `player is None` 超过宽限 ⇒ `_heal_silent` 兜底重建 + 记日志；
  (d) 有播放器但长期不活跃 ⇒ 补进缓存 + 激活 + 记日志（**不重建**）；
  (e) 健康图层 ⇒ 0 动作、0 日志；
  (f) `有帧前就 ended` ⇒ **不重建**，但每 5s 记一行（去重）；
  (g) 图片/GIF 播放器（无 `_active`）⇒ 不被当成"未激活"反复救（防日志刷屏）。

用法（离屏）：QT_QPA_PLATFORM=offscreen venv/Scripts/python.exe -u tools/_test_silent_freeze.py
退出码 0 = 全部通过；1 = 有断言失败。
"""
import os
import sys

# 必须离屏：本测试要真造一次 QApplication（引擎是 QObject，__init__ 里建 QTimer）。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

from PySide6.QtWidgets import QApplication                    # noqa: E402

app = QApplication.instance() or QApplication([])

import engine                                                  # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (("   " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


# ---------------------------------------------------------------- 捕获看门狗日志
LOGGED = []
try:
    import stallwatch
    stallwatch.log_line = lambda m: LOGGED.append(m)
except Exception:                                              # noqa: BLE001
    pass


# ---------------------------------------------------------------- 可控时钟
class _Clock:
    def __init__(self, t0=5000.0):
        self.t = float(t0)

    def perf_counter(self):
        return self.t


CLOCK = _Clock()
_real_engine_time = engine.time
engine.time = CLOCK


# ---------------------------------------------------------------- 夹具
class FakeClip:
    def __init__(self, path):
        self.path = path

    def all_tags(self):
        return set()


class FakeAudio:
    """只提供 `_tick_locked` 需要的 snapshot（含活跃标记用的 `silent_sec` / `level`）。"""

    def snapshot(self):
        return {"level": 0.05, "bass": 0.05, "mid": 0.05, "high": 0.05, "energy": 0.5,
                "bpm": 128.0, "beat_phase": 0.0, "beat_count": 0, "bar_count": 0,
                "downbeat_off": 0.0, "drop": False, "is_beat": False, "is_bar": False,
                "beat_active": True, "onset_density": 0.0, "bpm_stab": 1.0,
                "beat_clarity": 1.0, "spec_complex": 0.0, "energy_trend": 0.0,
                "vocalness": 0.0, "segment": "main", "spectrum": [0.0] * 16,
                "silent_sec": 0.0, "running": True, "lv_db": -20.0,
                "recognized_song_id": -1, "recognized_offset": -1.0,
                "recognized_rate": 1.0, "recognized_t": 0.0, "recognized_votes": None,
                "genre_tags": [], "dbg": {}}


class FakeVideoPlayer:
    """真解码器的最小替身：有 `stalled`/`health`，`set_active` 会真正写入 `_active`。"""

    def __init__(self, frames=100, active=True, last_t=0.0, ended=False):
        self._frames = int(frames)
        self._active = bool(active)
        self._last_t = float(last_t)
        self._ended = bool(ended)
        self.speed = 1.0
        self.closed = 0
        self.set_active_vals = []

    def stalled(self, secs=None):
        return False

    def health(self):
        return {"frames": self._frames, "age": None, "reason": "测试"}

    def set_active(self, a):
        self._active = bool(a)
        self.set_active_vals.append(bool(a))

    def set_speed(self, s):
        self.speed = s

    def close(self):
        self.closed += 1


class FakeImagePlayer:
    """图片/GIF 替身：`set_active` 是空操作，且**没有** `_active`/`_frames`/`stalled`。"""

    def __init__(self):
        self.speed = 1.0
        self.set_active_calls = 0

    def set_active(self, a):
        self.set_active_calls += 1

    def set_speed(self, s):
        pass

    def close(self):
        pass


class FakeLayer:
    def __init__(self, clip, player, cache=None, visible=True):
        self.name = "L"
        self.visible = visible
        self.cur = 0
        self.clips = [clip]
        self.player = player
        self.prev_player = None
        self.cache = dict(cache) if cache is not None else {}
        self.speed = 1.0
        self.is_kv = False
        self.pause_silent = False
        # 「无帧封顶后跳到下一个素材」所需字段（见 _skip_no_frame_material）
        self.recent = []
        self.prev_idx = -1
        self.trans_active = False
        self.last_switch_t = 0.0


def gp_counter(ret=None):
    """造一个计数版 `_get_player`（重建入口）；返回 (fn, box['n'])。"""
    box = {"n": 0}

    def fn(lay, clip_):
        box["n"] += 1
        return ret

    return fn, box


def make_engine(layers, get_player):
    """真 `AutoVJEngine` 实例，但把重活接口 stub 掉，只保留活跃标记 + `_heal_*` 真实实现。"""
    eng = engine.AutoVJEngine({"auto": {}}, FakeAudio(), {})
    eng.freeze = False
    eng.running = True
    eng.pause_auto = False
    eng.locked = True            # 直接跳过 _kv_tick（本测试不测它）
    eng.mode = "normal"
    eng.cfg = {"auto": {}}
    eng.audio = FakeAudio()
    eng.layers = layers
    eng.set_canvas_size(320, 180)          # 画布小一点，加快 _tick_locked 尾部拷贝
    eng._health_tick = lambda: None        # 别写真实 layer_health.log
    eng._get_player = get_player
    for name in ("_update_mode", "_refresh_auto_layers", "_energy_tier_switch",
                 "_auto_switch_tick", "_beat_tick", "_process_warms", "_kv_tick",
                 "_compose", "_apply_color", "_apply_postfx", "_spout_send", "_ndi_send"):
        setattr(eng, name, lambda *a, **k: None)
    return eng


def tick(eng):
    """跑一整帧（真实的 `_tick_locked`）。"""
    engine.AutoVJEngine._tick_locked(eng)


def heal_silent(eng, lay):
    engine.AutoVJEngine._heal_silent(eng, lay)


# ================================================================
print("(a) 根治证据：`lay.player` 脱离缓存后，活跃标记仍必须把它置为活跃")
gp, n_rebuild = gp_counter()
clip = FakeClip("dropped_from_cache.mov")
p = FakeVideoPlayer(frames=500, active=False)
lay = FakeLayer(clip, p, cache={})                      # ★ 缓存里**没有** p（正是冻帧根因）
eng = make_engine([lay], gp)
CLOCK.t = 6000.0
p._last_t = CLOCK.t                                     # 健康（每帧都在产帧）⇒ 看门狗不重建
print("     运行前：缓存里元素数=%d（旧实现只遍历缓存 ⇒ 根本不会碰到 p）"
      % len(lay.cache))
tick(eng)
check("脱离缓存的当前播放器被置为 `_active=True`（根治盲区①）",
      p._active is True, "_active=%s set_active=%s" % (p._active, p.set_active_vals))
check("（旁证）看门狗没被牵连去重建", n_rebuild["n"] == 0, "rebuilds=%d" % n_rebuild["n"])

# ================================================================
print("(b) 反证：缓存里「非当前」的播放器必须被挂起（省 CPU 行为未被破坏）")
gp, n_rebuild = gp_counter()
cur = FakeVideoPlayer(frames=500, active=True)
idle = FakeVideoPlayer(frames=500, active=True)          # 在缓存、但既不是 player 也不是 prev
eng = make_engine([FakeLayer(FakeClip("cur.mov"), cur,
                             cache={"cur.mov": cur, "idle.mov": idle})], gp)
CLOCK.t = 6000.0
cur._last_t = idle._last_t = CLOCK.t
tick(eng)
check("当前播放器仍活跃", cur._active is True, "cur.active=%s" % cur._active)
check("非当前（缓存中）播放器被挂起", idle._active is False, "idle.active=%s" % idle._active)

# ================================================================
print("(c) player is None 超过宽限 ⇒ _heal_silent 兜底重建 + 记日志")
new_p = FakeVideoPlayer(frames=0, active=True)
gp, n_rebuild = gp_counter(ret=new_p)
lay = FakeLayer(FakeClip("none.mov"), None, cache={})
eng = make_engine([lay], gp)
CLOCK.t = 7000.0
LOGGED.clear()
heal_silent(eng, lay)                                   # 第一拍：只记起始时刻
check("首拍不动作（仅记起始）", n_rebuild["n"] == 0 and lay.player is None,
      "n=%d player=%s" % (n_rebuild["n"], lay.player))
CLOCK.t += 1.2                                          # 越过宽限 1.0s
heal_silent(eng, lay)
check("宽限后兜底重建（调用 _get_player 1 次）", n_rebuild["n"] == 1, "n=%d" % n_rebuild["n"])
check("图层拿到新播放器", lay.player is new_p)
check("写了「player缺失」兜底日志", any("player缺失" in m for m in LOGGED),
      str(LOGGED[-1:])[:90])

# ================================================================
print("(d) 有播放器但长期不活跃 ⇒ 补进缓存 + 激活（**不重建**）")
gp, n_rebuild = gp_counter()
stuck = FakeVideoPlayer(frames=100, active=False)
lay = FakeLayer(FakeClip("stuck.mov"), stuck, cache={})  # 缓存里也没有它
eng = make_engine([lay], gp)
CLOCK.t = 8000.0
stuck._last_t = CLOCK.t
LOGGED.clear()
heal_silent(eng, lay)
check("首拍不动作", stuck._active is False and lay.cache == {},
      "active=%s cache=%s" % (stuck._active, lay.cache))
CLOCK.t += 1.2
heal_silent(eng, lay)
check("补进缓存", lay.cache.get("stuck.mov") is stuck)
check("被激活", stuck._active is True, "active=%s" % stuck._active)
check("写了「未激活」兜底日志（且没重建）", any("未激活" in m for m in LOGGED) and n_rebuild["n"] == 0,
      "n=%d %s" % (n_rebuild["n"], str(LOGGED[-1:])[:90]))

# ================================================================
print("(e) 健康图层 ⇒ _heal_silent 0 动作、0 日志（热路径不误伤）")
gp, n_rebuild = gp_counter()
healthy = FakeVideoPlayer(frames=4821, active=True)
lay = FakeLayer(FakeClip("healthy.mov"), healthy, cache={"healthy.mov": healthy})
eng = make_engine([lay], gp)
CLOCK.t = 9000.0
LOGGED.clear()
for _ in range(120):                                    # 模拟 120s @1Hz
    healthy._last_t = CLOCK.t                           # 每帧都在产帧
    tick(eng)
    CLOCK.t += 1.0
silent = [m for m in LOGGED if "静默冻帧兜底" in m]
check("健康图层 ⇒ 无任何静默兜底日志", len(silent) == 0, "日志=%s" % silent[:2])
check("健康图层 ⇒ 播放器一直活跃", healthy._active is True)
check("健康图层 ⇒ 从未重建", n_rebuild["n"] == 0, "n=%d" % n_rebuild["n"])

# ================================================================
print("(f) 「有帧前就 ended」⇒ 不重建，但每 5s 记一行（去重）")
gp, n_rebuild = gp_counter()
ended = FakeVideoPlayer(frames=0, active=True, ended=True)
lay = FakeLayer(FakeClip("ended.mov"), ended, cache={"ended.mov": ended})
eng = make_engine([lay], gp)
CLOCK.t = 15000.0
ended._last_t = CLOCK.t
LOGGED.clear()
for _ in range(16):                                     # 16s @1Hz
    heal_silent(eng, lay)
    CLOCK.t += 1.0
ended_logs = [m for m in LOGGED if "ended无帧" in m]
print("     16s 内 ended 无帧日志 %d 行（预期 ≈4 行：0/5/10/15s）" % len(ended_logs))
check("ended 无帧 ⇒ 不重建（_get_player 未被调用）", n_rebuild["n"] == 0,
      "n=%d" % n_rebuild["n"])
check("ended 无帧 ⇒ 按 ~5s 去重记录（16s 内 3~4 行）", 3 <= len(ended_logs) <= 4,
      "行数=%d %s" % (len(ended_logs), ended_logs[:1]))

# ================================================================
print("(g) 图片/GIF 播放器（无 `_active`）⇒ 不被当成「未激活」反复救")
gp, n_rebuild = gp_counter()
img = FakeImagePlayer()
lay = FakeLayer(FakeClip("pic.png"), img, cache={"pic.png": img})
eng = make_engine([lay], gp)
CLOCK.t = 20000.0
LOGGED.clear()
for _ in range(10):                                     # 10s @1Hz
    heal_silent(eng, lay)
    CLOCK.t += 1.0
silent = [m for m in LOGGED if "静默冻帧兜底" in m]
check("图片图层 ⇒ 无「未激活」误救日志", len(silent) == 0, "日志=%s" % silent[:2])
check("图片播放器 set_active 未被兜底反复调用", img.set_active_calls == 0,
      "calls=%d" % img.set_active_calls)

# ================================================================
print("(h) 无帧重建封顶后 ⇒ 自动跳该图层下一个素材（改 lay.cur + 记日志 + 临时避选）")
gp, n_rebuild = gp_counter()                            # _get_player 返回 None（跳过去即可）
c0, c1 = FakeClip("bad.mov"), FakeClip("good.mov")
p0 = FakeVideoPlayer(frames=0, active=True)             # 从未出帧、未 ended
lay = FakeLayer(c0, p0, cache={"bad.mov": p0})
lay.clips = [c0, c1]
lay.cur = 0
lay._silent_noframe_since = 40000.0
lay._silent_noframe_path = "bad.mov"
lay._silent_noframe_count = engine._SILENT_NOFRAME_MAX  # 已封顶
eng = make_engine([lay], gp)
CLOCK.t = 41000.0
LOGGED.clear()
heal_silent(eng, lay)
check("封顶后自动切换到下一个素材（lay.cur 0→1）", lay.cur == 1, "cur=%d" % lay.cur)
check("记了「无帧跳过」日志", any("无帧跳过" in m for m in LOGGED),
      str([m for m in LOGGED if "无帧跳过" in m][-1:])[:110])
check("坏素材被临时加入 recent（不永久拉黑）", "bad.mov" in lay.recent,
      "recent=%s" % lay.recent)

print("(i) 只跳一次：切换后再 heal 不会因同一素材反复跳")
cur_before = lay.cur
for _ in range(3):
    CLOCK.t += 1.0
    heal_silent(eng, lay)
check("未因同一坏素材反复切换", lay.cur == cur_before, "cur=%d（期望 %d）" % (lay.cur, cur_before))

print("(j) 单素材层 ⇒ 无可跳，只记日志（不切换）")
gp2, _ = gp_counter()
only = FakeVideoPlayer(frames=0, active=True)
lay_single = FakeLayer(FakeClip("only.mov"), only, cache={"only.mov": only})
lay_single._silent_noframe_since = 50000.0
lay_single._silent_noframe_path = "only.mov"
lay_single._silent_noframe_count = engine._SILENT_NOFRAME_MAX
eng2 = make_engine([lay_single], gp2)
CLOCK.t = 51000.0
LOGGED.clear()
heal_silent(eng2, lay_single)
check("单素材层不切换（cur 仍 0）", lay_single.cur == 0, "cur=%d" % lay_single.cur)
check("单素材层记了日志（封顶 / 无法跳过）",
      any("无帧封顶" in m for m in LOGGED), str(LOGGED[-1:])[:110])

print("(k) 过渡进行中（trans_active）⇒ 本 tick 不跳（绝不打断过渡）")
gp3, _ = gp_counter()
pa = FakeVideoPlayer(frames=0, active=True)
mA, mB = FakeClip("a.mov"), FakeClip("b.mov")
lay_tr = FakeLayer(mA, pa, cache={"a.mov": pa})
lay_tr.clips = [mA, mB]
lay_tr.cur = 0
lay_tr.trans_active = True
lay_tr._silent_noframe_since = 60000.0
lay_tr._silent_noframe_path = "a.mov"
lay_tr._silent_noframe_count = engine._SILENT_NOFRAME_MAX
eng3 = make_engine([lay_tr], gp3)
CLOCK.t = 61000.0
LOGGED.clear()
heal_silent(eng3, lay_tr)
check("过渡中不切换（cur 仍 0）", lay_tr.cur == 0, "cur=%d" % lay_tr.cur)

# ================================================================
engine.time = _real_engine_time
print()
print("结果：%s" % ("全部通过 ✓" if not FAILS else "失败 %d 项 ✗ %s" % (len(FAILS), FAILS)))
sys.exit(1 if FAILS else 0)
