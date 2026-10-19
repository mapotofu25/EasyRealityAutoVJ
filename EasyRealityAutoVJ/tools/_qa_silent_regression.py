# -*- coding: utf-8 -*-
"""QA 回归验证（Edward）——确认新防抖没伤到 pause_silent / _kv_tick。

1) pause_silent 仍**只在 self.running 时**生效（活跃标记）—— 走真实 `_tick_locked` 加锁路径，
   构造一个 hide_silent=False / pause_silent=True 的图层，喂静音 snap：
     · running=True  ⇒ player.set_active(False)（挂起）
     · running=False ⇒ player.set_active(True) （预览保持实时，不挂起）
2) _kv_tick 用的是自己的 kv_silent_sec 独立计时，**不**读 _silent_now_debounced。
   断言：在防抖状态被强制成 True/False 时，_kv_tick 的行为不受影响（只跟 lv_db 有关）。
3) pause_silent 延迟：确认加了 0.5s 防抖后，其触发用的即是防抖值（≥0.5s 才置静音）。

用法：venv/Scripts/python.exe -u tools/_qa_silent_regression.py
"""
import os
import sys
import time
import tempfile
import shutil

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

import engine as E  # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print(("  OK  " if ok else "  FAIL") + " " + name + (("   " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


class _RecPlayer:
    """记录最后一次 set_active 的播放器替身。"""

    def __init__(self):
        self.last_active = None
        self.n = 0

    def set_active(self, a):
        self.last_active = a
        self.n += 1

    def set_speed(self, s):
        pass

    def current(self):
        return None


class _FakeAudio:
    def __init__(self, snap):
        self._snap = snap

    def snapshot(self):
        return dict(self._snap)


def _full_snap(silent=False, lv_db=-20.0):
    s = {"level": 0.5, "bass": 0.0, "mid": 0.0, "high": 0.0, "energy": 0.5,
         "bpm": 0.0, "beat_count": 0, "bar_count": 0, "is_beat": False,
         "is_bar": False, "drop": False, "silent_sec": 0.0, "lv_db": lv_db}
    if silent:
        s["silent_sec"] = 5.0
    return s


def make_engine(running, snap=None):
    eng = E.AutoVJEngine.__new__(E.AutoVJEngine)
    eng.layers = []
    eng.running = running
    eng.freeze = False
    eng.locked = False
    eng.pause_auto = False
    eng.mode = "normal"
    eng.cfg = {"auto": {"intensity": 1, "bpm_speed_sync": False, "transition": ""},
               "mode": {}, "kv": {"db_threshold": -50.0, "silent_delay": 2.0,
                                  "resume_delay": 2.5, "switch_sec": 0.0,
                                  "in_trans": "fade", "out_trans": "fade",
                                  "in_dur": 0.1, "out_dur": 0.1}}
    eng.blackout = False
    eng._silent_debounce_on = False
    eng._silent_since = 0.0
    eng._loud_since = 0.0
    eng._silent_last_t = time.perf_counter()
    eng._silent_now_debounced = False
    eng.audio = _FakeAudio(snap or _full_snap())
    eng._tick_lock = __import__("threading").RLock()
    eng._last_beat_count = 0
    eng._last_bar_count = 0
    eng._last_genres = ()
    eng._bar_off_user = None
    eng._next_due = None
    eng._due_off = 0.0
    eng._heal_last_global_t = 0.0
    eng._health_last_t = 0.0
    eng._kv_last_t = time.perf_counter()
    eng._auto_refresh_t = 0.0
    eng._frame_i = 0
    eng._postfx_trig = {}
    eng._fx_hold_pick = None
    eng._fx_hold_due = 0.0
    eng._fx_hold_genres = None
    eng._latest_frame = None
    eng._frame_seq = 0
    eng._spout = None
    eng._ndi = None
    from PySide6.QtGui import QImage
    eng.canvas = QImage(64, 36, QImage.Format_RGB32)

    class _NoSig:
        def emit(self, *a):
            pass
    eng.frame_ready = _NoSig()
    return eng


def stub_heavy(eng, keep_pause_path=True):
    """把 _tick_locked 里与本次断言无关的重活 stub 掉（保留活跃标记那段）。"""
    for m in ["_update_mode", "_refresh_auto_layers", "_energy_tier_switch",
              "_auto_switch_tick", "_beat_tick", "_process_warms",
              "_heal_stalled", "_heal_silent", "_health_tick", "_apply_color",
              "_apply_postfx", "_spout_send", "_ndi_send", "_compose"]:
        setattr(eng, m, lambda *a, **k: None)
    # _kv_tick 需要保留用于第 2 项；调用方可控制


def make_layer(pause_silent, hide_silent=False):
    lay = E.Layer("L")
    lay.clips = []
    lay.player = _RecPlayer()
    lay.prev_player = None
    lay.pause_silent = pause_silent
    lay.hide_silent = hide_silent
    lay.trans_active = False
    return lay


def main():
    d = tempfile.mkdtemp(prefix="qa_silent_reg_")
    try:
        print("=== 回归1：pause_silent 仍只在 running 时挂起播放器 ===")

        # running=True + 静音 + pause_silent ⇒ player.set_active(False)
        eng = make_engine(running=True, snap=_full_snap(silent=True))
        lay = make_layer(pause_silent=True)
        eng.layers = [lay]
        stub_heavy(eng)
        # 让防抖直接置为"静音"（模拟已持续静音 ≥0.5s）
        eng._silent_hide_tick = lambda s: setattr(eng, "_silent_now_debounced", True)
        with eng._tick_lock:
            eng._tick_locked()
        check("running=True 静音 ⇒ pause_silent 图层 player.set_active(False)",
              lay.player.last_active is False, "last_active=%s" % lay.player.last_active)

        # running=False + 静音 + pause_silent ⇒ 不挂起（set_active(True)）
        eng = make_engine(running=False, snap=_full_snap(silent=True))
        lay = make_layer(pause_silent=True)
        eng.layers = [lay]
        stub_heavy(eng)
        eng._silent_hide_tick = lambda s: setattr(eng, "_silent_now_debounced", True)
        with eng._tick_lock:
            eng._tick_locked()
        check("running=False 静音 ⇒ pause_silent 不挂起（set_active(True)）",
              lay.player.last_active is True, "last_active=%s" % lay.player.last_active)

        # 有声时 always active
        eng = make_engine(running=True, snap=_full_snap(silent=False))
        lay = make_layer(pause_silent=True)
        eng.layers = [lay]
        stub_heavy(eng)
        eng._silent_hide_tick = lambda s: setattr(eng, "_silent_now_debounced", False)
        with eng._tick_lock:
            eng._tick_locked()
        check("running=True 有声 ⇒ pause_silent 不挂起（set_active(True)）",
              lay.player.last_active is True, "last_active=%s" % lay.player.last_active)

        print("")
        print("=== 回归2：_kv_tick 用独立 kv_silent_sec，不受 _silent_now_debounced 影响 ===")
        # 构造一个 Kv 图层；不管防抖状态如何，静音累计只跟 lv_db 有关。
        def kv_progress(debounced, lv_db, secs=0.6):
            eng = make_engine(running=True, snap=_full_snap(lv_db=lv_db))
            kv = E.Layer("Kv")
            kv.is_kv = True
            kv.clips = [object()]          # 非空即可（会走 _kv_show → stub）
            eng.layers = [kv]
            eng._silent_now_debounced = debounced
            eng._silent_debounce_on = debounced
            # stub 掉 _kv_show / _kv_begin 内的重活
            eng._kv_show = lambda *a, **k: None
            eng._kv_begin = lambda *a, **k: None
            t_end = time.perf_counter() + secs
            while time.perf_counter() < t_end:
                eng._kv_tick(eng.audio.snapshot())
                time.sleep(0.05)
            return kv.kv_silent_sec, kv.kv_loud_sec

        # lv_db 低（静音）⇒ kv_silent_sec 增长，与防抖无关
        s_a, l_a = kv_progress(debounced=True, lv_db=-80.0)
        s_b, l_b = kv_progress(debounced=False, lv_db=-80.0)
        check("_kv_tick: 静音(lv_db低) 时 kv_silent_sec 增长，且与 _silent_now_debounced 无关",
              s_a > 0.3 and s_b > 0.3 and abs(s_a - s_b) < 0.3,
              "debounced=T s=%.2f / debounced=F s=%.2f" % (s_a, s_b))
        # lv_db 高（有声）⇒ kv_loud_sec 增长
        s_c, l_c = kv_progress(debounced=True, lv_db=-10.0)
        check("_kv_tick: 有声(lv_db高) 时 kv_loud_sec 增长（不受防抖状态影响）",
              l_c > 0.3, "loud=%.2f" % l_c)

        print("")
        print("=== 回归3：pause_silent 的触发阈值 = 0.5s 防抖值（行为变化已确认） ===")
        # 直接测：防抖在 0.2s 静音下为 False ⇒ pause_silent 不会被触发（仍是"运行中不暂停"）
        eng = make_engine(running=True, snap=_full_snap(silent=True))
        lay = make_layer(pause_silent=True)
        eng.layers = [lay]
        stub_heavy(eng)
        # 用**真实** _silent_hide_tick：先有声基线 0.6s，再静音 0.2s
        t_end = time.perf_counter() + 0.6
        while time.perf_counter() < t_end:
            eng._silent_hide_tick(_full_snap(silent=False))
            time.sleep(0.016)
        state_after_glitch = None
        t_end = time.perf_counter() + 0.2
        while time.perf_counter() < t_end:
            eng._silent_hide_tick(_full_snap(silent=True))
            time.sleep(0.016)
        state_after_glitch = eng._silent_now_debounced
        check("0.2s静音间隙：防抖值仍为 False ⇒ pause_silent 不触发（不闪）",
              state_after_glitch is False, "debounced=%s" % state_after_glitch)

        print("")
        print("结论：" + ("ALL PASS" if not FAILS else "FAIL %d 项：%s" % (len(FAILS), FAILS)))
        return 0 if not FAILS else 1
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
