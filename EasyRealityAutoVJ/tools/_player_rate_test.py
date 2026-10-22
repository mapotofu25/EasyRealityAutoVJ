# -*- coding: utf-8 -*-
"""**播放器速率精度**回归（用户 2026-10-18："视频和音乐时间越久越对不齐"）。

根因：解码线程的节拍写的是
    `wait = interval / self.speed - (perf_counter() - t0)`
而 `t0` 只包住"读一帧 + 转格式"，**循环自身的开销**（seek 检查、活跃判断、属性读取）
落在 `t0` 之外 ⇒ 每轮真实耗时 = interval + 开销 ⇒ 长期速率系统性偏慢。

实测（10 秒窗口，真素材，speed=1.00）：
    · 旧：`VideoPlayer` **0.9833**，`AvAlphaPlayer` **0.9798**
    · 新（绝对时间表 `_Pacer`）：两者 **0.9999**，speed=1.15 ⇒ **1.1500**
1.7~2% 听着不大，但 **5 分钟就差 5~6 秒** —— 这正是"越久越对不齐"。

判据：speed=1.00 实测 0.993~1.007；speed=1.15 实测 1.138~1.162
（±0.7% —— 8 秒窗口的取整抖动约 ±0.4%；旧写法偏慢 1.7~2.2%，仍会被明确抓红）。
运行：`python tools/_player_rate_test.py`（约 40 秒）
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("CUE_PROBE_KEEP_LOGS", "1")

from PySide6.QtWidgets import QApplication                    # noqa: E402

import media_manager as MM                                     # noqa: E402

MP4 = (r"Y:/EasyRealityAutoVJ/码表素材/测试.ercue.media/"
       r"【东方新春宴2026单品】 ℕ𝕚𝕞𝕓𝕦𝕤 𝕊𝕡𝕒𝕣𝕜 𝟙𝟡𝟡𝟡 [BGA] 【Sociable State】.mp4")
MOV = (r"Y:/EasyRealityAutoVJ/码表素材/测试.ercue.media/"
       r"LOGO循环_tranceair_sociablestate_20s_alpha.mov")
DUR = 8.0
FAIL = []


def ck(name, cond, detail=""):
    if cond:
        print("  [ok  ] %s" % name)
    else:
        print("  [FAIL] %s  %s" % (name, detail))
        FAIL.append(name)


def measure(path, alpha, speed, dur=DUR):
    it = MM.MediaItem(path)
    if alpha is not None:
        it._has_alpha = alpha
    pl = MM.make_player(it, cue=True)
    name = type(pl).__name__
    pl.set_active(True)
    try:
        pl.warm_up()
    except Exception:                                          # noqa: BLE001
        pass
    t0 = time.time()
    while time.time() - t0 < 3.0:
        time.sleep(0.03)
        if pl.current() is not None:
            break
    pl.set_speed(speed)
    time.sleep(0.3)
    p0 = pl.position()
    w0 = time.perf_counter()
    time.sleep(dur)
    p1 = pl.position()
    w1 = time.perf_counter()
    try:
        pl.close()
    except Exception:                                          # noqa: BLE001
        pass
    if p0 is None or p1 is None:
        return name, None
    return name, (p1 - p0) / (w1 - w0)


def main():
    app = QApplication.instance() or QApplication([])
    for path, alpha, label in ((MP4, None, "H.264 mp4"),
                               (MOV, True, "带 alpha mov")):
        if not os.path.isfile(path):
            print("!! 缺素材：%s" % path)
            return 1
        for speed, lo, hi in ((1.0, 0.993, 1.007), (1.15, 1.138, 1.162)):
            name, rate = measure(path, alpha, speed)
            ck("%s / %s speed=%.2f ⇒ %.4f（要求 %.3f~%.3f）"
               % (label, name, speed, rate if rate else -1, lo, hi),
               rate is not None and lo <= rate <= hi,
               "实际 %r —— 偏慢会让位置随时间累积漂移" % (rate,))
    # ---- ③ 输入速率必须乘进播放器倍速（不然音乐降速就结构性越差越远）----
    print("三. 输入音乐速率 → 播放器倍速")
    try:
        import cuesheet as CS
        import cuesheet_runtime as crt
        sh = CS.new_sheet("t", res_w=640, res_h=360)
        te = CS.new_track("T", "A", 100.0, 120.0)
        L = CS.CompLayer(lyr_id="L1", z=0)
        L.items.append(CS.CueItem(item_id="i1", clip_id="c1", at_sec=0.0,
                                  trans_in="cut", trans_out="cut").normalized())
        te.layers.append(L); sh.tracks.append(te)
        sh.media.append(CS.MediaRef(clip_id="c1", name="a.mp4"))
        rt = crt.CueRuntime(sh, te, media_paths={"c1": "a.mp4"})
        st1 = rt.update(10.0, beat_len=0.5, rate=1.0)[0]
        st2 = rt.update(11.0, beat_len=0.5, rate=0.86)[0]
        ck("rate=1.00 ⇒ speed 1.000", abs(st1.speed - 1.0) < 1e-9, "%.4f" % st1.speed)
        ck("rate=0.86 ⇒ speed 0.860（播放器跟着音乐慢下来）",
           abs(st2.speed - 0.86) < 1e-6, "%.4f" % st2.speed)
    except Exception as e:                                     # noqa: BLE001
        ck("输入速率倍速检查未抛异常", False, repr(e))

    print()
    if FAIL:
        print("结论：FAIL —— %d 项未通过：%s" % (len(FAIL), "、".join(FAIL)))
        return 1
    print("结论：PASS —— 播放器长期速率 = 名义倍速（±0.5%），长时间播放不再累积漂移。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
