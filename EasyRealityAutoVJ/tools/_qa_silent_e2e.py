# -*- coding: utf-8 -*-
"""QA 端到端可观测验证（Edward）——真的影响渲染吗？

不建 MainWindow。构造**最小真实 AutoVJEngine**（复用其真实 `_compose` 与真实 `Layer`），
两个图层，其中一个 hide_silent=True，player 用一个返回**纯红**图的替身。
再直接调 `_compose(snap)`，检查 `engine.canvas` 像素：
  · 静音 snap（_silent_now_debounced=True）⇒ hide_silent 图层**不被绘制** ⇒ 画布非红。
  · 有声 snap（_silent_now_debounced=False）⇒ 该图层**被绘制** ⇒ 画布红。
并验证 pause_silent 活跃标记同源（走真实 `_tick_locked` 的加锁路径，不绕过锁）。

用法：venv/Scripts/python.exe -u tools/_qa_silent_e2e.py
"""
import os
import sys
import time
import tempfile
import shutil

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

from PySide6.QtGui import QImage, QColor, QPainter  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
import engine as E  # noqa: E402

HAS_DEBOUNCE = hasattr(E.AutoVJEngine, "_silent_hide_tick")
FAILS = []


def check(name, ok, extra=""):
    print(("  OK  " if ok else "  FAIL") + " " + name + (("   " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


class _StubPlayer:
    """返回一张纯 (255,0,0) 不透明 QImage 的播放器替身。"""

    def __init__(self, color=QColor(255, 0, 0)):
        self._img = QImage(64, 36, QImage.Format_ARGB32)
        self._img.fill(color)
        self.active = True
        self.speed = 1.0

    def current(self):
        return self._img

    def set_active(self, a):
        self.active = a

    def set_speed(self, s):
        self.speed = s


class _FakeAudio:
    def __init__(self):
        self._snap = _full_snap(silent=False)

    def snapshot(self):
        return dict(self._snap)


def _full_snap(silent=False):
    """覆盖 _tick_locked 自身读取的所有键的完整 snap（子调用已被 stub）。"""
    s = {"level": 0.5, "bass": 0.0, "mid": 0.0, "high": 0.0, "energy": 0.5,
         "bpm": 0.0, "beat_count": 0, "bar_count": 0, "is_beat": False,
         "is_bar": False, "drop": False, "silent_sec": 0.0, "lv_db": -20.0}
    if silent:
        s["silent_sec"] = 5.0
        s["level"] = 0.5
    return s


def make_engine():
    eng = E.AutoVJEngine.__new__(E.AutoVJEngine)
    # _compose 需要的最小状态（其余字段 _compose 在同路径上不触碰）
    eng.canvas = QImage(160, 90, QImage.Format_RGB32)
    eng.layers = []
    eng.blackout = False
    eng.freeze = False
    eng.running = False
    eng.locked = False
    eng.pause_auto = False
    eng.mode = "normal"
    eng.cfg = {"auto": {"intensity": 1, "transition": ""}, "mode": {}}
    eng._silent_debounce_on = False
    eng._silent_since = 0.0
    eng._loud_since = 0.0
    eng._silent_last_t = time.perf_counter()
    eng._silent_now_debounced = False
    eng._heal_last_global_t = 0.0
    eng._health_last_t = 0.0
    eng.audio = _FakeAudio()
    eng._tick_lock = __import__("threading").RLock()
    eng._last_beat_count = 0
    eng._last_bar_count = 0
    eng._bar_off_user = None
    eng._next_due = None
    eng._spout = None
    eng._ndi = None
    eng._frame_seq = 0
    eng._latest_frame = None
    return eng


def make_layer(name, hide_silent):
    lay = E.Layer(name)
    lay.clips = []                       # _compose 只读 clips 做 item 尺寸，空列表安全
    lay.player = _StubPlayer()
    lay.prev_player = None
    lay.trans_active = False
    lay.hide_silent = hide_silent
    return lay


def canvas_is_red(eng):
    """检查画布中心像素是否接近纯红（真被绘制）。"""
    img = eng.canvas
    c = img.pixelColor(img.width() // 2, img.height() // 2)
    return c.red() > 200 and c.green() < 60 and c.blue() < 60


def force_silent_state(eng, silent):
    """把防抖结果直接置成目标态（绕过时间，专注验证 _compose 的读取）。"""
    eng._silent_debounce_on = silent
    eng._silent_now_debounced = silent


def main():
    d = tempfile.mkdtemp(prefix="qa_silent_e2e_")
    try:
        print("=== 端到端：_compose 是否真的按 hide_silent 跳过绘制 ===")

        # ---- 1) 有声状态下：hide_silent 图层应被绘制（红）----
        eng = make_engine()
        lay = make_layer("hide_layer", hide_silent=True)
        eng.layers = [lay]
        force_silent_state(eng, False)
        eng._compose({"silent_sec": 0.0, "level": 0.5, "bass": 0.0})
        red_loud = canvas_is_red(eng)
        check("有声时 hide_silent 图层被绘制（画布=红）", red_loud,
              "red=%s" % red_loud)

        # ---- 2) 静音状态下：hide_silent 图层应被跳过（画布非红/黑）----
        eng = make_engine()
        lay = make_layer("hide_layer", hide_silent=True)
        eng.layers = [lay]
        force_silent_state(eng, True)
        eng._compose({"silent_sec": 5.0, "level": 0.5, "bass": 0.0})
        red_silent = canvas_is_red(eng)
        check("静音时 hide_silent 图层不被绘制（画布=黑）", not red_silent,
              "red=%s" % red_silent)

        # ---- 3) 对照：hide_silent=False 的图层在静音时**仍**被绘制 ----
        eng = make_engine()
        lay = make_layer("normal_layer", hide_silent=False)
        eng.layers = [lay]
        force_silent_state(eng, True)
        eng._compose({"silent_sec": 5.0, "level": 0.5, "bass": 0.0})
        red_normal = canvas_is_red(eng)
        check("静音时 hide_silent=False 图层仍被绘制（画布=红）", red_normal,
              "red=%s" % red_normal)

        # ---- 4) 走真实 _tick_locked 加锁路径：防抖结果由 _silent_hide_tick 写入 ----
        #     （仅改动后有此方法；改动前无该步，跳过并如实标注）
        if HAS_DEBOUNCE:
            eng = make_engine()
            lay = make_layer("hide_layer", hide_silent=True)
            eng.layers = [lay]
            eng._last_genres = ()
            # 补 _tick_locked 后续会读到的属性（尽量最小）
            for attr, val in [
                ("_auto_refresh_t", 0.0), ("pause_silent", False), ("_frame_i", 0),
                ("_fx_hold_pick", None), ("_fx_hold_due", 0.0), ("_fx_hold_genres", None),
                ("_postfx_trig", {}), ("_latest_frame", None), ("_frame_seq", 0),
                ("_bar_off_user", None), ("_next_due", None), ("_due_off", 0.0),
                ("_silent_last_t", time.perf_counter()),
            ]:
                setattr(eng, attr, val)
            # monkeypatch 掉 _tick_locked 里重度子调用
            for m in ["_update_mode", "_refresh_auto_layers", "_energy_tier_switch",
                      "_auto_switch_tick", "_beat_tick", "_process_warms", "_kv_tick",
                      "_heal_stalled", "_heal_silent", "_health_tick", "_apply_color",
                      "_apply_postfx", "_spout_send", "_ndi_send"]:
                if hasattr(eng, m):
                    setattr(eng, m, lambda *a, **k: None)
            setattr(eng, "_compose", lambda *a, **k: None)
            eng.audio._snap = _full_snap(silent=False)
            called = {}
            real_tick = E.AutoVJEngine._silent_hide_tick

            def spy(snap):
                called["yes"] = True
                return real_tick(eng, snap)
            eng._silent_hide_tick = spy

            class _NoSig:
                def emit(self, *a):
                    pass
            eng.frame_ready = _NoSig()
            with eng._tick_lock:                    # 走真实加锁路径
                eng._tick_locked()
            check("_tick_locked 走加锁路径且调用了 _silent_hide_tick",
                  called.get("yes") is True, "called=%s" % called)
            check("_tick_locked 后 _silent_now_debounced 已写入且为 False（有声）",
                  eng._silent_now_debounced is False)
        else:
            print("  (skip) 改动前无 _silent_hide_tick，第 4 项不适用")

        # ---- 5) ★ 区分版本的关键 e2e：0.2s 瞬时间隙，跑**真实 tick→compose 流水线** ----
        #      改动前：_compose 用行内判据、且逐帧判定 ⇒ 静音帧立即变黑（闪）。
        #      改动后：_compose 读防抖结果 ⇒ 0.2s 不足 0.5s ⇒ 图层**始终**被绘制。
        #      两版都跑：改动后期望 glitch_blacks==0；改动前期望 glitch_blacks>0（证明能抓到）。
        #      用真实 time.sleep 驱动（不 mock perf_counter）。
        eng = make_engine()
        lay = make_layer("hide_layer", hide_silent=True)
        eng.layers = [lay]

        def per_frame_silent(snap, eng):
            """按当前版本语义求本帧"是否静音"再写进 compose 读取的状态。"""
            if HAS_DEBOUNCE:
                eng._silent_hide_tick(snap)
            else:
                # 改动前：行内判据，逐帧即时反应
                eng._silent_now_debounced = (snap["silent_sec"] > 0.25
                                             or snap["level"] < 0.03)

        def pipeline_frame(snap):
            per_frame_silent(snap, eng)
            eng._compose(snap)
            return canvas_is_red(eng)

        # 先建立稳定"有声"基线（≥0.6s）
        t_end = time.perf_counter() + 0.6
        while time.perf_counter() < t_end:
            pipeline_frame(_full_snap(silent=False))
            time.sleep(0.016)
        # 瞬时间隙：静音仅 0.2s（每 tick 推进防抖 + 跑 compose，观察是否变黑）
        glitch_blacks = 0
        glitch_ticks = 0
        t_end = time.perf_counter() + 0.2
        while time.perf_counter() < t_end:
            if not pipeline_frame(_full_snap(silent=True)):
                glitch_blacks += 1
            glitch_ticks += 1
            time.sleep(0.016)

        if HAS_DEBOUNCE:
            check("0.2s瞬时间隙：真实 tick→compose 流水线中图层始终被绘制（0 次变黑）",
                  glitch_ticks > 0 and glitch_blacks == 0,
                  "共 %d tick，其中变黑 %d 次" % (glitch_ticks, glitch_blacks))
        else:
            check("（对照组·改动前）0.2s瞬时间隙：流水线中出现变黑（证明该测试能抓到 bug）",
                  glitch_blacks > 0,
                  "共 %d tick，其中变黑 %d 次" % (glitch_ticks, glitch_blacks))

        print("")
        print("结论：" + ("ALL PASS" if not FAILS else "FAIL %d 项：%s" % (len(FAILS), FAILS)))
        return 0 if not FAILS else 1
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
