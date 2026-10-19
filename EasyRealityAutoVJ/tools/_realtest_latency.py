# -*- coding: utf-8 -*-
"""**真机测「画面比音频慢多少」** —— 在输出窗口上量，不读引擎内部时钟。

做法：
  1. 造一个只放「延迟尺子」的测试码表（`tools/_tempo/ruler.ercue`）：
     单层、单片段、全屏不透明，内容是一段**亮度每秒重新递增**的视频
     ⇒ 截一张图就能反解出「画面此刻显示的是曲内第几秒」。
  2. 临时把配置里西行谣的绑定码表指向它、并隐藏其它图层（**跑完必还原**）。
  3. 启动 exe（真渲染）＋ 声卡真放《西行谣》从 5s 起 45s。
  4. 高频截取**输出窗口**一小块区域的亮度，得到 (挂钟, 画面内容时间) 序列。
  5. 滞后 = 画面内容时间 − 音频真实位置（音频位置由我们自己放的，已知）。

⚠ 只改 `%LOCALAPPDATA%\AutoVJ\config.json` 两处（prefer / 图层可见性），`finally` 里还原。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
DIST = os.path.join(ROOT, os.environ.get("AUTOVJ_DIST", "dist_r38"), "EasyRealityAutoVJ")
EXE = os.path.join(DIST, "EasyRealityAutoVJ.exe")
LOCAL = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ")
CFG = os.path.join(LOCAL, "config.json")
TEMPO = os.path.join(ROOT, "tools", "_tempo")
RULER_MP4 = os.path.join(TEMPO, "ruler.mp4")
RULER_PKG = os.path.join(TEMPO, "ruler.ercue")
SRC_PKG = r"Y:/EasyRealityAutoVJ/码表素材/测试.ercue"
XING = r"Y:/EasyRealityAutoVJ/码表素材/西行谣 feat. 凛花 - Sociable State, 凛花, Northad, DJ Tranceair.flac"
FP_ID = "fp2-7f938c866ccaba19"          # 西行谣
TRACK_W, TRACK_H, FPS = 1280, 720, 30
PLAY_FROM = float(os.environ.get('RULER_FROM', 5.0))
PLAY_DUR = 45.0


# ---------------------------------------------------------------- 造测试码表
def build_pkg():
    with zipfile.ZipFile(SRC_PKG) as z:
        d = json.loads(z.read("cuesheet.json").decode("utf-8"))
    tr = None
    for t in d["tracks"]:
        if t.get("identity", {}).get("fp_id") == FP_ID:
            tr = t
            break
    if tr is None:
        raise SystemExit("找不到西行谣曲目")
    tr["composition"]["layers"] = [{
        "lyr_id": "rl", "name": "ruler", "z": 0, "blend": "normal", "opacity": 1.0,
        "items": [{
            "item_id": "ri1", "clip_id": "r1",
            # ★ 把片段放到 at_sec=PLAY_FROM 处：用来检验"码表走到后面是否开始偏"
            "at_beats": PLAY_FROM / (60.0 / 142.0), "at_sec": PLAY_FROM,
            "end_beats": 99999.0,
            "src_in_sec": 0.0, "src_out_sec": 60.0, "speed": 1.0,
            "crop": None, "xform": {"scale": 1.0, "x": 0.0, "y": 0.0},
            "trans_in": "cut", "trans_in_beats": 0.0,
            "trans_out": "cut", "trans_out_beats": 0.0,
            "snap": "off", "note": "", "blend": "", "opacity": None,
        }],
    }]
    d["tracks"] = [tr]
    d["media"] = [{"clip_id": "r1", "name": "ruler.mp4", "sha256": "", "embedded": True,
                   "duration_sec": 60.0, "has_alpha": False}]
    with zipfile.ZipFile(RULER_PKG, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("cuesheet.json", json.dumps(d, ensure_ascii=False))
        z.write(RULER_MP4, "media/ruler.mp4")
    print("测试码表：%s（%.1f MB）" % (RULER_PKG, os.path.getsize(RULER_PKG) / 1e6))


# ---------------------------------------------------------------- 配置改/还原
def apply_cfg():
    shutil.copy2(CFG, CFG + ".lagbak")
    c = json.load(open(CFG, encoding="utf-8"))
    c.setdefault("cue", {}).setdefault("prefer", {})[FP_ID] = RULER_PKG
    hidden = []
    for i, L in enumerate(c.get("layers", [])):
        if str(L.get("kind") or L.get("type") or "").lower() == "cuesheet" or L.get("is_cuesheet"):
            continue
        if L.get("visible", True):
            L["visible"] = False
            hidden.append(i)
    json.dump(c, open(CFG, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("配置已改：prefer→ruler；隐藏图层 %s" % hidden)
    return hidden


def restore_cfg():
    if os.path.isfile(CFG + ".lagbak"):
        shutil.copy2(CFG + ".lagbak", CFG)
        os.remove(CFG + ".lagbak")
        print("配置已还原")


# ---------------------------------------------------------------- 采集
def find_hwnd(title):
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


def gray_of(hwnd):
    """截输出窗口中心一块，返回平均灰度（0~255）。"""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    pm = app.primaryScreen().grabWindow(int(hwnd), TRACK_W // 2 - 20, TRACK_H // 2 - 20, 40, 40)
    img = pm.toImage()
    w, h = img.width(), img.height()
    if w <= 0 or h <= 0:
        return None
    s = n = 0
    for y in range(0, h, 4):
        for x in range(0, w, 4):
            v = img.pixelColor(x, y)
            s += (v.red() + v.green() + v.blue()) / 3.0
            n += 1
    return s / n if n else None


def play(path, stop, dur, start):
    import numpy as np
    import sounddevice as sd
    import soundfile as sf
    sr = sf.info(path).samplerate
    data, _sr = sf.read(path, dtype="float32", always_2d=True,
                        start=int(start * sr), frames=int(dur * sr) + 1)
    sd.play(np.ascontiguousarray(data), _sr)
    t = time.time()
    while not stop.is_set() and time.time() - t < dur:
        time.sleep(0.2)
    sd.stop()


def kill():
    subprocess.call(["taskkill", "/F", "/IM", "EasyRealityAutoVJ.exe"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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
    build_pkg()
    hidden = []
    try:
        hidden = apply_cfg()
        kill()
        time.sleep(1.0)
        print("启动 exe …")
        proc = subprocess.Popen([EXE], cwd=DIST)
        time.sleep(22)
        hwnd = find_hwnd("Easy Reality Output") or find_hwnd("Easy Reality AutoVJ")
        print("窗口 hwnd=%s" % hwnd)
        if not hwnd:
            print("!! 找不到窗口")
            return 1

        stop = threading.Event()
        th = threading.Thread(target=play, args=(XING, stop, PLAY_DUR, PLAY_FROM), daemon=True)
        t_wall0 = time.time()
        th.start()

        samples = []
        t_end = t_wall0 + PLAY_DUR - 2.0
        while time.time() < t_end:
            g = gray_of(hwnd)
            if g is not None:
                samples.append((time.time() - t_wall0, g))
            time.sleep(0.02)
        stop.set()
        time.sleep(1.5)
        try:
            proc.terminate()
        except Exception:                                       # noqa: BLE001
            pass
        time.sleep(1.0)
        kill()

        # ---- 反解画面内容时间 ----
        rows = []
        for dt, g in samples:
            # 2 秒周期：g 对应"这 2 秒内的相位" p∈[0,1)
            p = (g - 20.0) / 215.0 * (59.0 / 60.0)
            if p < -0.05 or p > 1.05:
                continue
            phase = max(0.0, min(1.0, p))
            coarse = dt                               # 片段起点=PLAY_FROM ⇒ 内容时间就是 dt
            n = round((coarse - 2.0 * phase) / 2.0)
            media = 2.0 * n + 2.0 * phase
            # 内容只覆盖 0..60s
            if not (0.0 <= media <= 60.0):
                continue
            rows.append((dt, media, coarse, media - coarse))

        print("\n有效截图 %d / %d" % (len(rows), len(samples)))
        if len(rows) < 20:
            print("!! 有效样本太少，可能画面没被尺子占满")
            return 1
        lags = sorted(r[3] for r in rows)
        n = len(lags)
        # 去掉首尾各 10%（换片段/淡入淡出过渡）
        core = lags[int(n * 0.1):max(1, int(n * 0.9))]
        m = len(core)
        print("滞后（画面 − 音频）：P10 %.3fs  中位 %.3fs  P90 %.3fs  min %.3fs  max %.3fs"
              % (core[int(m * 0.1)], core[m // 2], core[int(m * 0.9)], core[0], core[-1]))
        # 漂移：前后两半的中位差
        h1 = sorted(r[3] for r in rows[:n // 2])
        h2 = sorted(r[3] for r in rows[n // 2:])
        d1, d2 = h1[len(h1) // 2], h2[len(h2) // 2]
        print("漂移：前半中位 %.3fs → 后半中位 %.3fs（差 %+.3fs）" % (d1, d2, d2 - d1))
        med = core[m // 2]
        print("\n结论：画面比音频%s %.0f ms（%s）"
              % ("慢" if med > 0 else "快", abs(med) * 1000,
                 "需要加大 LEAD_SEC" if med > 0.02 else
                 ("偏快，LEAD_SEC 过大" if med < -0.02 else "已同步")))
        return 0
    finally:
        restore_cfg()


if __name__ == "__main__":
    sys.exit(main())
