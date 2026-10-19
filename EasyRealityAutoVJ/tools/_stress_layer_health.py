# -*- coding: utf-8 -*-
"""追加 2：**长时真实配置压测** + "静默异常"统计（判决"静默冻帧"到底存不存在）。

做什么：
  · 用用户**真实**配置（只读复制到 `tempfile.mkdtemp()`，**绝不覆盖** `%LOCALAPPDATA%\\AutoVJ\\config.json`）
    离屏把引擎跑起来（真解码真素材，素材在 `Y:\\EasyRealityAutoVJ\\素材库`）。
  · 用合成音频（拍/能量/静音随时间变化）驱动，尽量复现真实切换。
  · 每秒按 `layer_health.log` 的格式记录一层状态，同时**在内存里**逐秒统计 4 类静默异常：
      ① 图层可见 + 有播放器 + `not _active`
      ② 图层可见 + `player is None`
      ③ 图层可见 + 播放器不在 `lay.cache` 里
      ④ 图层可见 + `_ended`
    并统计每类的**连续持续秒数**（episodes）。
  · 顺手统计：切换次数 / 重建看门狗触发次数 / 静默兜底触发次数 / `_tick_locked` 异常次数。

layer_health.log 写到 tempdir（不碰用户目录）；进程结束打印统计，并把一份 JSON 落盘到 tempdir。

用法：
  QT_QPA_PLATFORM=offscreen venv/Scripts/python.exe -u tools/_stress_layer_health.py [--secs 600]
退出码 0 = 跑完；1 = 跑不起来（找不到配置/层）。
"""
import argparse
import json
import math
import os
import shutil
import sys
import atexit
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

from PySide6.QtWidgets import QApplication                    # noqa: E402

app = QApplication.instance() or QApplication([])

import config as cfgmod                                        # noqa: E402
import engine                                                  # noqa: E402
import media_manager as mm                                     # noqa: E402
from match_engine import set_custom_visual_map                 # noqa: E402

# ---------------------------------------------------------------- 只读复制真实 config
TMP = tempfile.mkdtemp(prefix="autovj_stress_")


def _cleanup_stress_tmp():
    """新铁律：mkdtemp 必须清理。主产物 stress_result.json 退出前先拷到项目根，再删 TMP。"""
    try:
        src = os.path.join(TMP, "stress_result.json")
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(_ROOT, "_stress_layer_health_result.json"))
    except Exception:
        pass
    shutil.rmtree(TMP, ignore_errors=True)


atexit.register(_cleanup_stress_tmp)

REAL_CFG = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "config.json")
if not os.path.exists(REAL_CFG):
    print("!! 找不到真实配置：%s" % REAL_CFG)
    sys.exit(1)
COPY_CFG = os.path.join(TMP, "config.json")
shutil.copyfile(REAL_CFG, COPY_CFG)
cfgmod.CONFIG_FILE = COPY_CFG          # 让 Config().load() 读副本；save() 也只会写副本
print("真实配置副本（只读复制，绝不回写）：%s" % COPY_CFG)

# layer_health.log 也写到 tempdir（别碰用户 %LOCALAPPDATA%）
HEALTH_LOG = os.path.join(TMP, "layer_health.log")
engine._health_log_path = lambda: HEALTH_LOG

# 捕获看门狗 / 静默兜底日志（stallwatch.log_line 是它们唯一的出口）
LOG_LINES = []
try:
    import stallwatch
    _orig_log = stallwatch.log_line
    stallwatch.log_line = lambda m: LOG_LINES.append(m)
except Exception:                                              # noqa: BLE001
    _orig_log = None


# ---------------------------------------------------------------- 合成音频
class SynthAudio:
    """按墙钟演化的假音频快照：有拍、有能量起伏、每分钟有 ~8s 静音。

    ⚠ **不注入 genre_tags**：引擎的 `_refresh_auto_layers` 一旦收到曲风就会用 `match_clips`
      在真实库上重挑自动图层的素材池——而我们的合成曲风与用户库里 `clip_roles` 未必匹配，
      会把自动图层池子清空（`player=None, cur=-1`）导致它们永不上场。保持空曲风 ⇒ 引擎沿用
      **用户在配置里存好的**自动素材池，切换链路照常被驱动（这才是我们要压测的对象）。"""

    def __init__(self, bpm=128.0):
        self.bpm = float(bpm)
        self.t0 = time.perf_counter()

    def snapshot(self):
        el = time.perf_counter() - self.t0
        beat_len = 60.0 / self.bpm
        bc = int(el / beat_len)
        phase = (el / beat_len) - bc
        # 每分钟有 8s 静音（触发 Kv 待机 / pause_silent 路径）
        silent = 0.0
        if int(el) % 60 < 8:
            silent = float(int(el) % 60) + 0.001
        level = 0.5 + 0.45 * math.sin(el / 7.0)
        if silent > 0.3:
            level = 0.01
        energy = max(0.0, min(1.0, 0.5 + 0.5 * math.sin(el / 11.0)))
        return {"level": level, "bass": level, "mid": level, "high": level,
                "energy": energy, "bpm": self.bpm, "beat_phase": phase,
                "beat_count": bc, "bar_count": bc // 4, "downbeat_off": 0.0,
                "drop": (bc % 32 == 0 and phase < 0.2),
                "is_beat": (phase < 0.1), "is_bar": (bc % 4 == 0 and phase < 0.1),
                "beat_active": True, "onset_density": 0.3, "bpm_stab": 0.9,
                "beat_clarity": 0.8, "spec_complex": 0.4, "energy_trend": 0.0,
                "vocalness": 0.1, "segment": "main", "spectrum": [level] * 16,
                "silent_sec": silent, "running": True, "lv_db": -20.0 + 30 * level,
                "recognized_song_id": -1, "recognized_offset": -1.0,
                "recognized_rate": 1.0, "recognized_t": el, "recognized_votes": None,
                "genre_tags": [], "dbg": {}}


# ---------------------------------------------------------------- 建引擎（真实配置）
def build_library(cfg):
    """按 ui_main 的方式，从 `cfg["library"]` 建**完整素材库**（含 clip_adj/tags/roles/excluded）。
    自动图层切换要 `match_engine.match_clips` 按 `role` 挑素材，缺了 roles 就会挑空 ⇒ 自动图层
    永不上场，压测就白跑。"""
    lib = {}
    for p in cfg["library"]:
        try:
            lib[p] = mm.MediaItem(p)
        except Exception:                                      # noqa: BLE001
            pass
    for p, adj in (cfg["clip_adj"] or {}).items():
        m = lib.get(p)
        if m is not None:
            m.img_scale = float(adj.get("scale", 1.0) or 1.0)
            m.img_x = float(adj.get("x", 0.0) or 0.0)
            m.img_y = float(adj.get("y", 0.0) or 0.0)
            m.img_rot = float(adj.get("rot", 0.0) or 0.0)
    for p, tags in (cfg["clip_tags"] or {}).items():
        m = lib.get(p)
        if m is not None and isinstance(tags, list):
            m.tags = list(tags)
    for p, tags in (cfg["clip_tags_manual"] or {}).items():
        m = lib.get(p)
        if m is not None and isinstance(tags, list):
            m.manual_tags = list(tags)
    for p, tags in (cfg["clip_tags_disabled"] or {}).items():
        m = lib.get(p)
        if m is not None and isinstance(tags, list):
            m.disabled_tags = list(tags)
    for p, roles in (cfg["clip_roles"] or {}).items():
        m = lib.get(p)
        if m is not None and isinstance(roles, dict):
            fg, bg = bool(roles.get("fg")), bool(roles.get("bg"))
            if fg and bg:
                fg = False
            if not fg and not bg and roles.get("mg"):
                bg = True
            m.roles = {"fg": fg, "bg": bg}
    for p, _v in (cfg["clip_excluded"] or {}).items():
        m = lib.get(p)
        if m is not None:
            m.excluded = True
    return lib


cfg = cfgmod.Config()
set_custom_visual_map(cfg["genre_visual_map"] or {})
LIB = build_library(cfg)
eng = engine.AutoVJEngine(cfg, SynthAudio(), LIB)
eng.set_canvas_size(1280, 720)
try:
    eng.rebuild_from_cfg()
except Exception as e:                                         # noqa: BLE001
    print("!! rebuild_from_cfg 失败：%r" % e)
    sys.exit(1)
eng._spout_fail = True       # 离屏压测：不投 Spout / NDI
eng._ndi_fail = True
eng.running = True

N_LAYERS = len(eng.layers)
print("素材库：%d 个；图层数：%d；画布 1280x720  render_fps=%s"
      % (len(LIB), N_LAYERS, eng.frame_interval_ms()))
for i, lay in enumerate(eng.layers):
    print("  L%d %-12s vis=%s auto=%s kv=%s clips=%d auto_clips=%d"
          % (i, str(lay.name)[:12], lay.visible, getattr(lay, "auto_mode", False),
             getattr(lay, "is_kv", False), len(lay.clips), len(lay.auto_clips)))
if N_LAYERS == 0:
    print("!! 真实配置里没有图层，无法压测")
    sys.exit(1)


# ---------------------------------------------------------------- 逐秒采样 / 统计
ANOM = ("inactive", "none", "not_cache", "ended")
LABEL = {"inactive": "可见+有播放器+not active",
         "none": "可见+player is None",
         "not_cache": "可见+播放器不在 cache",
         "ended": "可见+_ended"}


class Tracker:
    """按"连续秒"计 episode：某(异常类型, 图层)从进入异常到离开算一段。"""

    def __init__(self, names):
        self.names = names
        self.sec_hits = {k: 0 for k in ANOM}          # 累计(层·秒)
        self.episodes = {k: [] for k in ANOM}         # [(layer_idx, duration_s), ...]
        self.open = {k: {} for k in ANOM}             # (k, layer_idx) -> start_sec
        self.per_layer = {k: {} for k in ANOM}        # k -> layer_idx -> sec
        self.detail = {k: {} for k in ANOM}           # k -> layer_idx -> 最近一次详情

    def sample(self, sec, hits, details):
        for k in ANOM:
            layers_now = hits[k]                       # set(layer_idx)
            opened = self.open[k]
            for li in layers_now:
                self.per_layer[k][li] = self.per_layer[k].get(li, 0) + 1
                if li not in opened:
                    opened[li] = sec
                if li in details[k]:
                    self.detail[k][li] = details[k][li]
            for li in list(opened):
                if li not in layers_now:
                    self.episodes[k].append((li, round(sec - opened.pop(li), 1)))
            self.sec_hits[k] += len(layers_now)

    def close_all(self, sec):
        for k in ANOM:
            for li in list(self.open[k]):
                self.episodes[k].append((li, round(sec - self.open[k].pop(li), 1)))

    def dur_list(self, k):
        return [d for _li, d in self.episodes[k]]


def snapshot_hits():
    """返回 (hits, details)：hits = {异常类型: set(图层号)}；details 给一段文字取证。

    ⚠ 只统计**真解码器**（有 `stalled`）的 3 类内部状态：图片/GIF 播放器没有 `_active`/
      `_frames`（静态素材"不活跃/0 帧"是正常的），把它们算进来只会误报。
    ⚠ `player is None` 只在**当前有有效素材**（`0 <= cur < nclips`）时才算异常；
      `cur=-1` 表示该图层尚未上场（自动图层等切换），不是冻帧，另计 `not_selected`。"""
    hits = {k: set() for k in ANOM}
    details = {k: {} for k in ANOM}
    not_selected = 0
    for i, lay in enumerate(eng.layers):
        if not getattr(lay, "visible", True):
            continue
        clips = getattr(lay, "clips", None) or []
        nclips = len(clips)
        cur = getattr(lay, "cur", -1)
        p = getattr(lay, "player", None)
        ps = "pause_silent" if getattr(lay, "pause_silent", False) else ""
        if p is None:
            if 0 <= cur < nclips:              # 有素材却没播放器 ⇒ 真异常（_heal_silent 兜底）
                hits["none"].add(i)
                details["none"][i] = "cur=%d/%d %s（有素材却无播放器）" % (cur, nclips, ps)
            else:                              # 尚未上场，正常
                not_selected += 1
            continue
        if not hasattr(p, "stalled"):          # 图片 / GIF：跳过（无 _active/_frames）
            continue
        in_cache = 0
        if 0 <= cur < nclips:
            try:
                in_cache = 1 if lay.cache.get(clips[cur].path) is p else 0
            except Exception:                                  # noqa: BLE001
                in_cache = 0
        age = "-"
        last = float(getattr(p, "_last_t", 0.0) or 0.0)
        if last:
            age = "%.2f" % (time.perf_counter() - last)
        d = ("id%d %s cur=%d/%d incache=%d frames=%d age=%s"
             % (id(p) % 100000, ps, cur, nclips, in_cache,
                int(getattr(p, "_frames", 0) or 0), age))
        if not getattr(p, "_active", False):
            hits["inactive"].add(i)
            details["inactive"][i] = d
        if not in_cache:
            hits["not_cache"].add(i)
            details["not_cache"][i] = d
        if getattr(p, "_ended", False):
            hits["ended"].add(i)
            details["ended"][i] = d
    return hits, details, not_selected


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=600.0, help="压测时长（秒），默认 600=10 分钟")
    ap.add_argument("--fps", type=float, default=30.0, help="模拟渲染帧率")
    args = ap.parse_args()

    trk = Tracker([str(l.name)[:12] for l in eng.layers])
    dt = 1.0 / max(1.0, args.fps)
    start = time.perf_counter()
    end = start + args.secs
    next_sample = start + 1.0
    sec = 0
    ticks = 0
    tick_errors = 0
    last_switch = {}
    switch_count = 0
    last_progress = 0
    not_sel_sec = 0

    print("\n开始压测：%.0f 秒 @ %.0f fps（离屏，不进 GUI 事件循环）" % (args.secs, args.fps))
    while time.perf_counter() < end:
        t0 = time.perf_counter()
        # 统计切换次数（lay.cur 变化）
        for i, lay in enumerate(eng.layers):
            c = getattr(lay, "cur", -1)
            if i in last_switch and last_switch[i] != c:
                switch_count += 1
            last_switch[i] = c
        try:
            eng._tick_locked()
        except Exception as e:                                  # noqa: BLE001
            tick_errors += 1
            if tick_errors <= 5:
                import traceback
                print("!! _tick_locked 异常：%r" % e)
                traceback.print_exc()
        ticks += 1
        now = time.perf_counter()
        while now >= next_sample:
            sec += 1
            hits, details, nsel = snapshot_hits()
            not_sel_sec += nsel
            trk.sample(sec, hits, details)
            next_sample += 1.0
            if sec - last_progress >= 30:
                last_progress = sec
                print("   … %ds：异常累计(层·秒) inactive=%d none=%d not_cache=%d ended=%d；"
                      "切换=%d 异常帧=%d"
                      % (sec, trk.sec_hits["inactive"], trk.sec_hits["none"],
                         trk.sec_hits["not_cache"], trk.sec_hits["ended"],
                         switch_count, tick_errors))
        el = time.perf_counter() - t0
        if el < dt:
            time.sleep(dt - el)

    trk.close_all(sec)
    heals = [m for m in LOG_LINES if "卡帧看门狗" in m]
    silent = [m for m in LOG_LINES if "静默冻帧兜底" in m]

    out = {
        "seconds": sec, "ticks": ticks, "tick_errors": tick_errors,
        "fps": args.fps, "layers": N_LAYERS, "switches": switch_count,
        "watchdog_rebuilds": len(heals), "silent_heals": len(silent),
        "not_selected_layer_sec": not_sel_sec,
        "anomalies": {k: {"sec_hits": trk.sec_hits[k],
                          "episodes": trk.episodes[k],
                          "n_episodes": len(trk.episodes[k]),
                          "max_episode_s": (max(trk.dur_list(k)) if trk.episodes[k] else 0.0),
                          "total_episode_s": round(sum(trk.dur_list(k)), 1),
                          "per_layer": {trk.names[li] if li < len(trk.names) else li: v
                                        for li, v in trk.per_layer[k].items()},
                          "detail": {trk.names[li] if li < len(trk.names) else li: v
                                     for li, v in trk.detail[k].items()}}
                      for k in ANOM},
        "health_log": HEALTH_LOG,
        "config_copy": COPY_CFG,
    }

    print("\n================ 压测结果（%d 秒） ================" % sec)
    print("tick 次数 %d，异常帧 %d，切换 %d 次" % (ticks, tick_errors, switch_count))
    print("看门狗重建 %d 次；静默兜底 %d 次" % (len(heals), len(silent)))
    print("（参考）图层「未上场」累计 %d 层·秒 —— 正常态，不算异常" % not_sel_sec)
    for k in ANOM:
        a = out["anomalies"][k]
        print("· %-22s 累计 %5d 层·秒 | %d 段 | 最长 %.1fs | 总 %.1fs"
              % (LABEL[k], a["sec_hits"], a["n_episodes"], a["max_episode_s"], a["total_episode_s"]))
        if a["per_layer"]:
            print("     按层(秒)：%s" % a["per_layer"])
            for nm, dd in a["detail"].items():
                print("        %s：%s" % (nm, dd))
        if a["episodes"]:
            print("     各段(layer,dur)：%s" % a["episodes"][:20])
    verdict = ("未观察到任何静默异常（4 类全 0）"
               if all(out["anomalies"][k]["sec_hits"] == 0 for k in ANOM)
               else "观察到静默异常，见上表")
    print("判决：%s" % verdict)

    res_path = os.path.join(TMP, "stress_result.json")
    with open(res_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("JSON 结果：%s（退出时另拷一份到 %s，随后删除 TMP）"
          % (res_path, os.path.join(_ROOT, "_stress_layer_health_result.json")))
    print("layer_health.log：%s（%d 行）"
          % (HEALTH_LOG, sum(1 for _ in open(HEALTH_LOG, encoding="utf-8", errors="replace"))
             if os.path.exists(HEALTH_LOG) else 0))

    try:
        eng.close()
    except Exception:                                          # noqa: BLE001
        pass


if __name__ == "__main__":
    main()
