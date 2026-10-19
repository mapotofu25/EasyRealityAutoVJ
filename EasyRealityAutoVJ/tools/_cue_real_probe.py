# -*- coding: utf-8 -*-
"""**真实素材探针**：用用户自己的码表包 + 真解码器，按真实时间跑一段，逐帧测量。

为什么必须这个（而不是 `cue_draw_test`）：
  `cue_draw_test` 用 FakePlayer + 固定图，验的是"合成公式对不对"；
  用户报的「一卡一卡 / 淡出先黑屏 / 多层没实现」全是**真解码器 + 真实时间**才有的现象
  （首帧延迟、seek 落地、帧缓存、α 通道、图层叠加）。

用法（默认取 `Y:/EasyRealityAutoVJ/码表素材/测试.ercue`）：
    python tools/_cue_real_probe.py --track 0 --start 76 --dur 12
    python tools/_cue_real_probe.py --track 0 --start 138 --dur 12

输出：`tools/_cueshots/probe_*.png` + 每 0.25s 一行：
    时间 / 拍位 / 各子层(active,alpha,play_sec,clip) / 画布平均亮度 / seek 累计
★ 平均亮度是"黑屏"的唯一硬判据：黑屏 ⇒ 亮度塌到 < 8（有画面时通常 > 40）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
SHOT = os.path.join(HERE, "_cueshots")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# ★ 探针必须**隔离取证日志**：以前直接写 `%LOCALAPPDATA%\AutoVJ\*`，
#   会污染用户真实排障日志（实测把 `boom-1` 之类的测试噪音写进过用户日志）。
if not os.environ.get("CUE_PROBE_KEEP_LOGS"):
    _tmp = os.path.join(__import__("tempfile").gettempdir(), "cue_probe")
    os.makedirs(os.path.join(_tmp, "AutoVJ"), exist_ok=True)
    os.environ["LOCALAPPDATA"] = _tmp

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication

import cuesheet as CS
import cuesheet_runtime as crt
import engine as E

PKG = r"Y:/EasyRealityAutoVJ/码表素材/测试.ercue"
W, H = 960, 540


# --------------------------------------------------------------------------
def load_pkg(pkg, track_idx):
    z = zipfile.ZipFile(pkg)
    d = json.loads(z.read("cuesheet.json").decode("utf-8"))
    sheet = CS.from_dict(d)
    te = sheet.tracks[track_idx]
    mdir = pkg + ".media"
    paths = {}
    for m in sheet.media:
        p = os.path.join(mdir, m.name)
        if os.path.isfile(p):
            paths[m.clip_id] = p
        else:
            print("  ⚠ 缺素材:", m.clip_id, m.name)
    return sheet, te, paths


# --------------------------------------------------------------------------
class _Lay:
    """最小的码表容器层（不需要真 QWidget）。"""

    def __init__(self, name="码表层"):
        self.name = name
        self.is_cuesheet = True
        self.is_kv = False
        self.visible = True
        self.solo = False
        self.hide_silent = False
        self.opacity = 1.0
        self.lock_motion = True
        self.use_alpha = True
        self.cs_states = []
        self.cs_active = False
        self.cs_times = []
        self.cache = {}
        self._cue_players = {}
        self._cue_items = {}
        self._cue_last_frame = {}
        self.trans_active = False
        self.trans_start = 0.0
        self.trans_ms = 0
        self.prev_player = None
        self.prev_idx = -1
        self.clips = []
        self.cur = 0
        self.player = None
        self.kv_on = 0.0
        self.fixed = -1
        self._alpha_key = None
        self._alpha_img = None
        self._last_frame = None


class _SolidPlayer:
    """底衬图层用的假播放器：恒返回一张纯色图（判"透明"还是"黑"的唯一可靠背景）。"""

    def __init__(self, img):
        self._img = img
        self.speed = 1.0

    def current(self):
        return self._img

    def set_active(self, a):
        pass

    def position(self):
        return 0.0

    def seek(self, sec):
        return True

    def set_speed(self, s):
        self.speed = float(s)

    def close(self):
        pass


class _Underlay:
    """普通（非码表）底衬图层：纯红，用来区分"码表层透明"与"码表层画了一块黑"。"""

    def __init__(self, w, h):
        img = QImage(w, h, QImage.Format_ARGB32)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.fillRect(0, 0, w, h, Qt.red)
        p.end()
        self.name = "底衬(红)"
        self.is_cuesheet = False
        self.is_kv = False
        self.visible = True
        self.solo = False
        self.hide_silent = False
        self.opacity = 1.0
        self.lock_motion = True
        self.use_alpha = True
        self.player = _SolidPlayer(img)
        self.prev_player = None
        self.prev_idx = -1
        self.clips = []
        self.cur = 0
        self.trans_active = False
        self.trans_start = 0.0
        self.trans_ms = 0
        self.kv_on = 0.0
        self._alpha_key = None
        self._alpha_img = None
        self._last_frame = None


def new_engine(canvas, layers):
    eng = E.AutoVJEngine.__new__(E.AutoVJEngine)
    eng.canvas = canvas
    eng.layers = layers
    eng.mode = "normal"
    eng.blackout = False
    eng.freeze = False
    eng.cfg = {"auto": {"intensity": 1, "transition": None},
               "mode": {"transition": ""}, "color": {}, "postfx": {}, "cue": {}}
    eng._silent_now_debounced = False
    eng._health_log = None
    eng._cue_tick_fail = 0
    eng._cue_err_at = 0.0
    eng._cue_pos_beats = None
    eng._cue_pos_src = ""
    eng._cue_sheet = None
    eng._cue_track = None
    eng._cue_rt = None
    eng._cue_media = {}
    eng._cue_jumped = False
    eng._cue_closed_last = 0
    eng._images = {}
    eng._cue_policy = E.CueSeekPolicy(cfg={})
    eng._cue_bound_cache = None
    eng._cue_bad_song_n = 0
    eng._cue_bad_song_t = 0.0
    eng._postfx_trig = {}
    eng._last_bar_count = 0
    eng._last_beat_count = 0
    eng._frame_i = 0
    eng._kv_on = 0.0
    eng._cue_diag_t = 0.0
    return eng


def mean_lum(img):
    if img is None or img.isNull():
        return -1.0
    im = img.convertToFormat(QImage.Format_RGB32)
    w, h = im.width(), im.height()
    ptr = im.constBits()
    a = np.frombuffer(memoryview(ptr), dtype=np.uint8, count=w * h * 4).reshape(h, w, 4)
    return float(a[:, :, :3].mean())


def std_lum(img):
    if img is None or img.isNull():
        return -1.0
    im = img.convertToFormat(QImage.Format_RGB32)
    w, h = im.width(), im.height()
    a = np.frombuffer(memoryview(im.constBits()), dtype=np.uint8,
                      count=w * h * 4).reshape(h, w, 4)
    return float(a[:, :, :3].std())


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pkg", default=PKG)
    ap.add_argument("--track", type=int, default=0)
    ap.add_argument("--start", type=float, default=76.0)
    ap.add_argument("--dur", type=float, default=12.0)
    ap.add_argument("--fps", type=float, default=60.0)
    ap.add_argument("--shots", default="", help="逗号分隔的秒数（相对 start）要存图")
    ap.add_argument("--old-policy", action="store_true",
                    help="A/B 对照：退回旧策略（阈值 0.35s + 不做倍速对齐）")
    ap.add_argument("--underlay", action="store_true",
                    help="在码表层下面加一个纯红普通图层：红=码表透明(正确)，黑=码表画了黑")
    a = ap.parse_args()

    app = QApplication.instance() or QApplication([])
    if a.old_policy:
        # A/B：把策略退回改动前（阈值 0.35 + 计速恒 ps.speed），其余一行不变。
        import cue_seek_policy as CSP
        CSP.SEEK_HARD_SEC = CSP.DRIFT_SEC          # 0.35
        CSP.rate_for = lambda ps, pos=None, mode=None: CSP.player_speed(ps)
        print("  [A/B] 已退回旧策略：seek 阈值 %.2fs，倍速恒为 ps.speed" % CSP.SEEK_HARD_SEC)
    sheet, te, paths = load_pkg(a.pkg, a.track)
    bl = 60.0 / (te.identity.bpm or 120.0)
    print("包=%s" % os.path.basename(a.pkg))
    print("曲目=%s / bpm=%.2f / beat_len=%.4fs / 素材 %d 个"
          % (te.identity.title, te.identity.bpm or 0.0, bl, len(paths)))
    for ly in te.sorted_layers():
        print("   层 %-3s z=%-3d items=%d %s"
              % (ly.lyr_id, ly.z, len(ly.items),
                 [(round(it.at_sec, 1), (None if it.end_beats is None
                                         else round(it.end_beats * bl, 1))) for it in ly.items[:4]]))

    canvas = QImage(W, H, QImage.Format_ARGB32)
    lay = _Lay()
    layers = [lay]
    if a.underlay:
        layers = [lay, _Underlay(W, H)]        # index0 = 最上层 ⇒ 底衬在下
    eng = new_engine(canvas, layers)
    # ★★ 必须走**真实的装表 API**（`set_cue_sheet`）：它会建 `_cue_alpha`（模板事实表）、
    #   夹好 `_cue_rt`。手工赋字段会漏掉这些 ⇒ 探针与生产分叉（铁律 24：夹具照抄生产构造）。
    E.AutoVJEngine.set_cue_sheet(eng, sheet, media_paths=paths, track=te)

    # seek 计数：包一层
    seeks = {"n": 0, "by": {}}
    orig_get = eng._cue_get_player

    speeds = {"min": 9.9, "max": -9.9, "n": 0}

    def get_player(l, clip):
        p = orig_get(l, clip)
        if not getattr(p, "_probe_wrapped", False):
            p._probe_wrapped = True
            oseek = p.seek
            oset = p.set_speed

            def setspd(v, _p=p, _o=oset):
                speeds["min"] = min(speeds["min"], float(v))
                speeds["max"] = max(speeds["max"], float(v))
                speeds["n"] += 1
                return _o(v)
            p.set_speed = setspd

            def seek(sec, _p=p, _o=oseek):
                seeks["n"] += 1
                seeks["by"][os.path.basename(getattr(_p, "path", "?"))] = \
                    seeks["by"].get(os.path.basename(getattr(_p, "path", "?")), 0) + 1
                return _o(sec)
            p.seek = seek
        return p

    eng._cue_get_player = get_player

    shot_at = {}
    for s in (a.shots or "").split(","):
        if s.strip():
            shot_at[float(s)] = True

    os.makedirs(SHOT, exist_ok=True)
    tag = "t%d_%d" % (a.track, int(a.start))
    n = int(a.dur * a.fps)
    t0 = time.perf_counter()
    last_report = -1.0
    rows = []
    # ★ 冻结判据：把画布缩到 32x18 后做逐字节比较（便宜且不受噪声干扰）。
    #   用户报的"一卡一卡"= 画面连续多帧完全一样；seek 未落地时 `player.current()`
    #   返回 None ⇒ `_cue_frame` 回落到缓存帧 ⇒ 这里就会连续命中。
    last_sig = None
    frozen_run = 0
    frozen_max = 0
    frozen_total = 0
    frozen_max_at = 0.0
    frozen_max_skip = 0          # 去掉前 2s（解码器预热）后的最长冻结
    print("\n  相对s  拍位    子层(active,alpha,play)                   亮度  std   seek")
    for i in range(n):
        # 真实时间节奏
        due = t0 + i / a.fps
        dt = due - time.perf_counter()
        if dt > 0:
            time.sleep(dt)
        rel = i / a.fps
        pos_sec = a.start + rel
        eng._cue_pos_beats = pos_sec / bl
        eng._cuesheet_tick()
        eng._compose({"bass": 0.2, "mid": 0.2, "high": 0.2, "energy": 0.5, "level": 0.5,
                      "bpm": te.identity.bpm or 120.0, "bpm_display": te.identity.bpm or 120.0})
        if rel - last_report >= 0.25 - 1e-9:
            last_report = rel
            parts = []
            for ps in (lay.cs_states or []):
                _pl = (lay._cue_players or {}).get(ps.clip_path)
                _pp = None
                try:
                    _pp = _pl.position() if _pl is not None else None
                except Exception:                            # noqa: BLE001
                    _pp = None
                _dr = ("%.2f" % (ps.play_sec - _pp)) if _pp is not None else "?"
                parts.append("%s:%s a=%.2f p=%.1f d=%s" %
                             (ps.lyr_id, "1" if ps.active else "0", ps.alpha,
                              ps.play_sec, _dr))
            lum = mean_lum(canvas)
            sd = std_lum(canvas)
            rows.append((rel, pos_sec, lum, sd, seeks["n"]))
            _ns = len(lay.cs_states or [])
            _na = sum(1 for x in (lay.cs_states or []) if x.active)
            print("  %5.2f  %6.1f  [%d层/%d活] %-52s %6.2f %6.2f  %d"
                  % (rel, pos_sec, _ns, _na, " | ".join(parts)[:52], lum, sd,
                     seeks["n"]))
        try:
            _sm = canvas.scaled(32, 18, Qt.IgnoreAspectRatio, Qt.FastTransformation)
            _sig = bytes(_sm.constBits())[: 32 * 18 * 4]
        except Exception:                                    # noqa: BLE001
            _sig = None
        if _sig is not None:
            if _sig == last_sig:
                frozen_run += 1
                frozen_total += 1
                if frozen_run > frozen_max:
                    frozen_max = frozen_run
                    frozen_max_at = rel
                if rel > 2.0 and frozen_run > frozen_max_skip:
                    frozen_max_skip = frozen_run
            else:
                frozen_run = 0
            last_sig = _sig
        if shot_at and any(abs(rel - k) < 0.5 / a.fps for k in list(shot_at)):
            k = min(shot_at, key=lambda x: abs(rel - x))
            shot_at.pop(k, None)
            p = os.path.join(SHOT, "probe_%s_%+.2fs.png" % (tag, rel))
            canvas.save(p, "PNG")

    print("\nseek 合计 %d 次 / %.1f 秒 = %.2f 次/秒  明细=%s"
          % (seeks["n"], a.dur, seeks["n"] / max(1e-9, a.dur), seeks["by"]))
    print("倍速：实现下发 %d 次，范围 %.3f ~ %.3f（名义 1.000）"
          % (speeds["n"], speeds["min"] if speeds["n"] else 1.0,
             speeds["max"] if speeds["n"] else 1.0))
    print("冻结：最长连续 %d 帧（%.0f ms，出现在 +%.2fs）/ 累计 %d 帧（%.1f%%）；"
          "去掉前 2s 预热后最长 %d 帧（%.0f ms）"
          % (frozen_max, frozen_max * 1000.0 / a.fps, frozen_max_at,
             frozen_total, 100.0 * frozen_total / max(1, n),
             frozen_max_skip, frozen_max_skip * 1000.0 / a.fps))
    if rows:
        lums = [r[2] for r in rows]
        print("亮度 min=%.2f max=%.2f；低于 8 的采样点 %d/%d（黑屏判据）"
              % (min(lums), max(lums), sum(1 for x in lums if x < 8.0), len(lums)))
    print("截图目录:", SHOT)
    lay_names = [ly.lyr_id for ly in te.sorted_layers()]
    print("层清单:", lay_names)
    return 0


if __name__ == "__main__":
    sys.exit(main())
