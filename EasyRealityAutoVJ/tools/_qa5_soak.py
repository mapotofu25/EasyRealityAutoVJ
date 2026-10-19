# -*- coding: utf-8 -*-
"""QA5 独立长压测（≥6min）：真实配置（只读副本）+ 离屏引擎，专测「从未出帧」兜底。

统计（自己算，不采信工程师）：
  · 每个可见图层、每个真解码器：`frames==0` 的**连续持续**（按 tick 精确计时，>2.2s 计入）
    → 次数 + 最长持续秒数；
  · 被新兜底救回的次数（捕获 `无帧重建` 日志）；
  · `gpu=` 取值分布（离屏可能拿不到 GL —— 如实统计）；
  · 「引擎确实在跑」证据：tick 数 / 切换次数 / 帧数增长。

绝不覆盖用户真实配置：只读复制到 tempfile.mkdtemp()。日志（layer_health / ui_stall）也写 tempdir。

用法：QT_QPA_PLATFORM=offscreen venv/Scripts/python.exe -u tools/_qa5_soak.py --secs 380
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
import stallwatch                                              # noqa: E402
from match_engine import set_custom_visual_map                 # noqa: E402

TMP = tempfile.mkdtemp(prefix="qa5_soak_")


def _cleanup_soak_tmp():
    """新铁律：mkdtemp 必须清理。但本脚本的**主产物**是 TMP/qa5_soak_result.json ——
    退出前先把它拷到项目根（_qa5_soak_result.json），再删 TMP，既不留泄漏也不丢结果。"""
    try:
        src = os.path.join(TMP, "qa5_soak_result.json")
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(_ROOT, "_qa5_soak_result.json"))
    except Exception:
        pass
    shutil.rmtree(TMP, ignore_errors=True)


atexit.register(_cleanup_soak_tmp)

REAL_CFG = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "config.json")
if not os.path.exists(REAL_CFG):
    print("!! 找不到真实配置：%s" % REAL_CFG)
    sys.exit(1)
COPY_CFG = os.path.join(TMP, "config.json")
shutil.copyfile(REAL_CFG, COPY_CFG)                            # 只读复制
cfgmod.CONFIG_FILE = COPY_CFG
engine._health_log_path = lambda: os.path.join(TMP, "layer_health.log")
stallwatch.log_path = lambda: os.path.join(TMP, "ui_stall.log")

LOG_LINES = []
stallwatch.log_line = lambda m: LOG_LINES.append(m)

print("tempdir=%s" % TMP)
print("真实配置副本（只读，绝不回写）=%s" % COPY_CFG)


class SynthAudio:
    def __init__(self, bpm=128.0):
        self.bpm = float(bpm)
        self.t0 = time.perf_counter()

    def snapshot(self):
        el = time.perf_counter() - self.t0
        beat_len = 60.0 / self.bpm
        bc = int(el / beat_len)
        phase = (el / beat_len) - bc
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


def build_library(cfg):
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
eng._spout_fail = True
eng._ndi_fail = True
eng.running = True

N_LAYERS = len(eng.layers)
print("素材库 %d；图层 %d" % (len(LIB), N_LAYERS))
if N_LAYERS == 0:
    print("!! 真实配置无图层，无法压测")
    sys.exit(1)

# ---------------------------------------------------------------- 逐 tick 追踪 frames==0
NOFRAME_MIN = 2.2
open_ep = {}          # layer_idx -> (start_t, player_id)
episodes = []         # [(layer_idx, duration)]
gpu_tally = {}
switch_count = 0
last_cur = {}
tick_errors = 0
first_frames_sum = None
last_frames_sum = 0
distinct_players = set()


def tick_track(now):
    global first_frames_sum, last_frames_sum, switch_count
    fs = 0
    for i, lay in enumerate(eng.layers):
        c = getattr(lay, "cur", -1)
        if i in last_cur and last_cur[i] != c:
            switch_count += 1
        last_cur[i] = c
        if not getattr(lay, "visible", True):
            continue
        p = getattr(lay, "player", None)
        if p is None or not hasattr(p, "stalled"):
            continue
        distinct_players.add(id(p))
        fr = int(getattr(p, "_frames", 0) or 0)
        fs += fr
        clips = getattr(lay, "clips", None) or []
        in_cache = 0
        if 0 <= c < len(clips):
            try:
                in_cache = 1 if lay.cache.get(clips[c].path) is p else 0
            except Exception:                                  # noqa: BLE001
                in_cache = 0
        noframe = (fr == 0 and in_cache and getattr(p, "_active", False)
                   and not getattr(p, "_ended", False))
        # pause_silent 有意不出帧 → 不算
        if noframe and getattr(lay, "pause_silent", False) and not getattr(p, "_active", False):
            noframe = False
        key = i
        if noframe:
            ent = open_ep.get(key)
            if ent is None or ent[0] != id(p):          # 新开一段（换了播放器也算新段）
                if ent is not None:
                    episodes.append((key, now - ent[1]))
                open_ep[key] = (id(p), now)
        else:
            ent = open_ep.pop(key, None)
            if ent is not None:
                episodes.append((key, now - ent[1]))
    if first_frames_sum is None:
        first_frames_sum = fs
    last_frames_sum = fs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=380.0)
    ap.add_argument("--fps", type=float, default=30.0)
    a = ap.parse_args()
    global tick_errors
    dt = 1.0 / max(1.0, a.fps)
    start = time.perf_counter()
    end = start + a.secs
    ticks = 0
    last_report = 0.0
    print("\n开始长压测：%.0f 秒 @ %.0f fps（离屏）" % (a.secs, a.fps))
    while time.perf_counter() < end:
        t0 = time.perf_counter()
        now = t0
        try:
            eng._tick_locked()
        except Exception as e:                                 # noqa: BLE001
            tick_errors += 1
            if tick_errors <= 5:
                import traceback
                print("!! _tick_locked 异常：%r" % e)
                traceback.print_exc()
        tick_track(time.perf_counter())
        # gpu 记号（每 2s 取一次，避免过密）
        if ticks % int(max(1, 2 * a.fps)) == 0:
            try:
                tok = eng._gpu_token()
            except Exception:                                  # noqa: BLE001
                tok = "?"
            gpu_tally[tok] = gpu_tally.get(tok, 0) + 1
        ticks += 1
        if now - last_report >= 60:
            last_report = now
            print("   … %.0fs：ticks=%d 切换=%d 无帧段=%d"
                  % (now - start, ticks, switch_count, len(episodes)))
        el = time.perf_counter() - t0
        if el < dt:
            time.sleep(dt - el)

    for i, ent in list(open_ep.items()):
        episodes.append((i, time.perf_counter() - ent[1]))
    try:
        import glctx
        wk = glctx.GLWorker.get()
        gpu_stats = wk.stats()
        gpu_msg = wk.last_message()
    except Exception as e:                                     # noqa: BLE001
        gpu_stats, gpu_msg = {"err": "import/na: %r" % e}, ""
    over = [d for _i, d in episodes if d > NOFRAME_MIN]
    rescues = [m for m in LOG_LINES if "无帧重建" in m]
    capped = [m for m in LOG_LINES if "无帧封顶" in m]
    silent_any = [m for m in LOG_LINES if "静默冻帧兜底" in m]
    heals = [m for m in LOG_LINES if "卡帧看门狗" in m]

    out = {"secs": a.secs, "ticks": ticks, "tick_errors": tick_errors,
           "switches": switch_count, "layers": N_LAYERS,
           "noframe_episodes_total": len(episodes),
           "noframe_over_2p2s": len(over),
           "noframe_max_secs": round(max([d for _i, d in episodes] or [0.0]), 1),
           "noframe_over_details": [[i, round(d, 1)] for i, d in episodes if d > NOFRAME_MIN][:20],
           "rescues_noframe_rebuild": len(rescues),
           "capped_noframe": len(capped),
           "silent_heal_lines": len(silent_any),
           "watchdog_lines": len(heals),
           "gpu_tally": gpu_tally,
           "gpu_stats": gpu_stats,
           "gpu_last_message": gpu_msg,
           "distinct_players": len(distinct_players),
           "frames_first_sum": first_frames_sum,
           "frames_last_sum": last_frames_sum,
           "tmp": TMP}

    print("\n================ 长压测结果（%.0f 秒） ================" % a.secs)
    print("tick=%d（异常帧 %d） 切换=%d 次 图层=%d 出现过播放器对象=%d"
          % (ticks, tick_errors, switch_count, N_LAYERS, len(distinct_players)))
    print("frames==0 段：总 %d 段；>%.1fs 的 %d 段；最长 %.1fs"
          % (len(episodes), NOFRAME_MIN, len(over), out["noframe_max_secs"]))
    print("   各段(layer,dur)（>2.2s）：%s" % (out["noframe_over_details"],))
    print("被新兜底救回（无帧重建）：%d 次；封顶日志：%d 行；静默兜底日志合计：%d 行；看门狗：%d 行"
          % (len(rescues), len(capped), len(silent_any), len(heals)))
    print("gpu= 分布：%s" % gpu_tally)
    print("GLWorker stats=%s last_message=%r" % (gpu_stats, gpu_msg))
    print("帧数增长：首样 %s → 末样 %s" % (first_frames_sum, last_frames_sum))
    ev = ("引擎确实在跑" if (ticks > 100 and switch_count > 0
                            and (last_frames_sum or 0) > (first_frames_sum or 0))
          else "证据不足")
    print("运行证据：%s" % ev)

    with open(os.path.join(TMP, "qa5_soak_result.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("JSON：%s（退出时另拷一份到 %s，随后删除 TMP）"
          % (os.path.join(TMP, "qa5_soak_result.json"),
             os.path.join(_ROOT, "_qa5_soak_result.json")))
    try:
        eng.close()
    except Exception:                                          # noqa: BLE001
        pass


if __name__ == "__main__":
    main()
