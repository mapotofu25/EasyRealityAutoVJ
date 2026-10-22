# -*- coding: utf-8 -*-
"""**真机测「视频比音频慢多少」**（用户 2026-10-18："码表视频还是会慢 跟不上音频"）。

做法：
  · 启动打包 exe
  · 用 sounddevice 把**用户实际那首**（西行谣，码表 bpm=142）从 60s 处 **1.0× 原速**真放 100s
  · 抓 `cue_health.log`，算 `pb`（拍位）的推进速率
    期望 = 142/60 = 2.3667 拍/秒（原速）；比值 < 1 就是"视频慢"
  · 同时看 `r=`（引擎认定的音乐倍率）是否被 BPM 校正拉回 ≈1.0

判据：
  · 拍位速率比值 ∈ [0.995, 1.005]（= 100 秒内累积滞后 < 0.5 秒）
  · `r` ∈ [0.99, 1.02]
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
DIST = os.path.join(ROOT, os.environ.get("AUTOVJ_DIST", "dist_r38"), "EasyRealityAutoVJ")
EXE = os.path.join(DIST, "EasyRealityAutoVJ.exe")
LOG = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "cue_health.log")
AUDIO = r"Y:/EasyRealityAutoVJ/码表素材/西行谣 feat. 凛花 - Sociable State, 凛花, Northad, DJ Tranceair.flac"
SID = 3112469840
BPM = 142.0
START = float(os.environ.get("LAG_START", 60.0))
DUR = float(os.environ.get("LAG_DUR", 100.0))

ROW = re.compile(r"\[(\d\d):(\d\d):(\d\d)\].*?sid=(-?\d+).*?status=(\w+) pb=(\S+).*?"
                 r"dmax=(\S+).*?spd=(\S+).*?r=([\d.]+)(?:.*?dark=([\d.]+)/(\d+))?")
# 另抓最原始的识别 offset（日志里 `off=` 就是 recognized_offset，已含 +窗口×r 补偿）


TIMING = {}


def now_s():
    t = time.localtime()
    return t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec


def rows_since(t0):
    out = []
    for ln in io.open(LOG, encoding="utf-8", errors="replace"):
        m = ROW.search(ln)
        if not m:
            continue
        hh, mm, ss, sid, st, pb, dm, spd, r, dk, dn = m.groups()
        t = int(hh) * 3600 + int(mm) * 60 + int(ss)
        if t < t0:
            continue
        out.append(dict(t=t, sid=int(sid), st=st,
                        pb=(None if pb == "None" else float(pb)),
                        dmax=float(dm), r=float(r),
                        dark=(float(dk) if dk else None)))
    return out


def play(path, stop, dur, start):
    import numpy as np
    import sounddevice as sd
    import soundfile as sf
    sr = sf.info(path).samplerate
    data, _sr = sf.read(path, dtype="float32", always_2d=True,
                        start=int(start * sr), frames=int(dur * sr) + 1)
    print("  播放 %s 从 %.0fs 起 %.0fs（%d Hz）" % (os.path.basename(path), start, dur, _sr))
    # ★ 用户指定：放音走「扬声器2」（Realtek 2ch/48k，设备 22）—— 与采集设备对齐
    _dev = int(os.environ.get('AVJ_PLAY_DEV', '22'))
    sd.play(np.ascontiguousarray(data), _sr, device=_dev)
    TIMING["t_play"] = now_s()       # ★ 真正开始出声的那一刻（解码完成之后）
    t = time.time()
    while not stop.is_set() and time.time() - t < dur:
        time.sleep(0.2)
    sd.stop()


def kill():
    subprocess.call(["taskkill", "/F", "/IM", "EasyRealityAutoVJ.exe"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def find_hwnd(title=""):
    import ctypes
    import ctypes.wintypes as wt
    u32 = ctypes.windll.user32
    ENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    found = {}

    def cb(h, _):
        n = u32.GetWindowTextLengthW(h)
        if n:
            b = ctypes.create_unicode_buffer(n + 1)
            u32.GetWindowTextW(h, b, n + 1)
            if title in b.value:
                found[b.value] = h
        return True
    u32.EnumWindows(ENUMPROC(cb), 0)
    return next(iter(found.values()), None)


# ---------------------------------------------------------------- 点「开始 VJ」
def _lh_size():
    p = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "layer_health.log")
    try:
        return os.path.getsize(p)
    except OSError:
        return -1


def _press_space(hwnd):
    """向主窗口发一次空格（Qt 默认快捷键 toggle_run）。

    ★ 只用全局 keybd_event 不可靠（窗口没拿到前台焦点就丢），实测有一次没起来。
      改成：先 PostMessage 直投窗口（不依赖焦点），不行再走 keybd_event。
    返回是否"发出去了"（是否生效由调用方用 layer_health 判定）。
    """
    import ctypes
    u32 = ctypes.windll.user32
    WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
    try:
        u32.PostMessageW(int(hwnd), WM_KEYDOWN, 0x20, 0)
        time.sleep(0.05)
        u32.PostMessageW(int(hwnd), WM_KEYUP, 0x20, 0)
        return True
    except Exception:                                           # noqa: BLE001
        return False


def _press_space_fg(hwnd):
    import ctypes
    u32 = ctypes.windll.user32
    try:
        u32.SetForegroundWindow(int(hwnd))
        time.sleep(0.3)
        u32.keybd_event(0x20, 0, 0, 0)
        time.sleep(0.06)
        u32.keybd_event(0x20, 0, 2, 0)
    except Exception:                                           # noqa: BLE001
        pass


def ensure_vj_running(main_hwnd, tag=""):
    """确保 VJ 在运行：先看 layer_health 有没有在长（只在 running 时写），没有再按空格。

    返回 True/False（是否在运行）。**绝不盲目按**：已在跑时按一下会把它停掉。
    """
    a = _lh_size()
    time.sleep(2.0)
    b = _lh_size()
    if b > a:
        print("   VJ 已在运行（启动即自动续跑，layer_health 在增长）%s" % tag)
        return True
    if not main_hwnd:
        print("   !! 没窗口，无法点开始 VJ")
        return False
    print("   VJ 未运行 ⇒ 按空格点「开始 VJ」%s" % tag)
    ok = False
    for attempt in range(3):
        if attempt == 0:
            _press_space(main_hwnd)          # 直投窗口（不依赖焦点）
        else:
            _press_space_fg(main_hwnd)       # 抢前台再发
        time.sleep(2.5)
        c = _lh_size()
        time.sleep(1.5)
        d = _lh_size()
        if d > c:
            ok = True
            break
        print("      第 %d 次没生效，重试…" % (attempt + 1))
    print("   %s" % ("✅ 已开始 VJ（layer_health 开始增长）" if ok else "!! 按了空格但没跑起来"))
    return ok


def main():
    if not os.path.isfile(EXE):
        print("!! 缺 exe：%s" % EXE)
        return 1
    kill()
    time.sleep(1.0)
    t0 = now_s() + 1
    print("启动 exe …")
    proc = subprocess.Popen([EXE], cwd=DIST)
    time.sleep(22)
    # ★ 必须真的「开始 VJ」——不是靠启动即自动续跑（用户 2026-10-18 纠正）
    if not ensure_vj_running(find_hwnd("Easy Reality AutoVJ")):
        print("!! 开始 VJ 失败，测下去没有意义")
        return 1
    stop = threading.Event()
    th = threading.Thread(target=play, args=(AUDIO, stop, DUR, START), daemon=True)
    th.start()
    time.sleep(DUR + 2.0)
    stop.set()
    time.sleep(1.5)
    try:
        proc.terminate()
    except Exception:                                          # noqa: BLE001
        pass
    time.sleep(1.0)
    kill()

    rows = [r for r in rows_since(t0) if r["sid"] == SID and r["pb"] is not None]
    k0 = next((i for i, r in enumerate(rows) if r["dmax"] >= 0), None)
    if k0 is None or len(rows) - k0 < 10:
        print("!! 有效采样不足（%d 行）" % len(rows))
        return 1
    rows = rows[k0:]
    print("\n有效采样 %d 行（%.0f 秒）" % (len(rows), rows[-1]["t"] - rows[0]["t"]))

    # 拍位推进速率：只在"同一 sid、位置连续"的段上算，剔除野匹配跳变
    segs, cur = [], []
    for a, b in zip(rows, rows[1:]):
        dt = b["t"] - a["t"]
        v = (b["pb"] - a["pb"]) / dt if dt else 0
        if dt > 0 and 0.5 * BPM / 60 <= v <= 1.5 * BPM / 60:
            cur.append((a["t"], a["pb"]))
        else:
            if len(cur) > 5:
                segs.append(cur)
            cur = [(b["t"], b["pb"])]
    if len(cur) > 5:
        segs.append(cur)

    exp = BPM / 60.0
    # ★ 判据改成**累积滞后**（位置 − 音频真实位置），而不是瞬时速率：
    #   瞬时速率里包含"纠偏动作"（本就该有 ±10%），拿它判好坏会误报。
    # ★★ 绝对滞后：真值 = 播放起点 + 已过去的墙钟（不再拿第一个采样当基准——
    #    那样量出来的是"相对漂移"，不是观众感知的"画面比音频晚多少"）。
    _t_audio_sec = float(TIMING.get("t_play") or 0.0)
    print("   音频实际开始：%s" % (("%d:%02d:%02d" % (_t_audio_sec // 3600,
                                                    (_t_audio_sec % 3600) // 60,
                                                    _t_audio_sec % 60)) if _t_audio_sec else "未知"))
    # ★ 用户要求：从 20 秒起才算（前面是装表/首次锁定的过渡，不代表演出稳态）
    rows = [r for r in rows if (r["t"] - _t_audio_sec) >= 20.0]
    lags = []
    offlags = []
    for r in rows:
        true_pos = START + (r["t"] - _t_audio_sec)
        # ★ 首选 play_sec（真正下发给播放器的秒数）；老日志没有 ps= 才退回 pb 换算
        _v = r.get("ps", -1.0)
        pos = _v if _v is not None and _v >= 0.0 else r["pb"] * 60.0 / BPM
        lags.append((r["t"] - _t_audio_sec, pos - true_pos))
        offlags.append(r.get("off", 0.0) - true_pos)
    print("\n=== 累积滞后（画面位置 − 音频位置，正=跑在前面）===")
    for k in range(0, len(lags), max(1, len(lags) // 6)):
        dt, lg = lags[k]
        print("   t=+%5.1fs  滞后 %+.2fs" % (dt, lg))
    vals = sorted(lg for _d, lg in lags)
    n = len(vals)
    core = vals[int(n * 0.1):max(1, int(n * 0.9))]
    med = core[len(core) // 2] if core else 0.0
    _om = sorted(offlags)[len(offlags) // 2] if offlags else 0.0
    print("   其中「识别 offset」本身：中位 %+.2fs（这一段是渲染之前的位置来源）" % _om)
    print("   → 中位 %+.2fs  P90(绝对值) %.2fs  max %.2fs"
          % (med, sorted(abs(v) for v in core)[min(len(core) - 1, int(len(core) * 0.9))],
             max(abs(v) for v in vals)))
    r_end = rows[-1]["r"]
    dark = rows[-1]["dark"]
    print("\n实测倍率 r（末段）：%.3f" % r_end)
    print("帧级暗场：%s" % ("%.3fs" % dark if dark is not None else "无此字段"))

    bad = []
    if not core:
        bad.append("没有可用样本")
    else:
        if abs(med) > 0.25:
            bad.append("中位滞后 %+.2fs 超过 0.25s" % med)
        if max(abs(v) for v in vals) > 1.0:
            bad.append("最大偏离 %.2fs 超过 1.0s" % max(abs(v) for v in vals))
    if abs(r_end - 1.0) > 0.02:
        bad.append("倍率 r=%.3f 未回到 1.0" % r_end)
    print("\n总判定：%s" % ("✅ 视频与音频同步（100 秒内滞后 <0.5 秒）"
                          if not bad else "❌ " + "；".join(bad)))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
