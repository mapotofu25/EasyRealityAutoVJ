# -*- coding: utf-8 -*-
"""看效果：加载真实歌曲 ⇒ 抓时间轴截图（验证波形占比）。

用法：venv\\Scripts\\python.exe tools\\_wave_gain_check.py
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
sys.path.insert(0, _SRC)

from PySide6.QtWidgets import QApplication                          # noqa: E402

from ercue.ui.window import MainWindow                              # noqa: E402

AUDIO = r"Y:\EasyRealityAutoVJ\码表素材\揽月 - Dreamira.flac"
OUT = os.path.join(os.path.dirname(_HERE), "_shots", "wave_gain_check.png")

app = QApplication([])
win = MainWindow()
win.resize(1600, 900)
win.show()

try:
    ok = win.import_track_audio(AUDIO)
    print("导入音频:", ok, "（需要先有曲目；失败不影响下面的直接灌数据）")
except Exception as e:                                               # noqa: BLE001
    print("导入异常:", e)

# ★ 直接灌波形数据（绕过"先加曲目"的前置条件）+ 设一个能看清高度的视口
from ercue.audio import waveform as wf                              # noqa: E402

d = wf.load_or_compute(AUDIO)
print("波形数据:", None if d is None else ("%d 列 / %.1f 秒" % (d.columns, d.duration_sec)))
if d is not None:
    tl = win.timeline
    tl._wav = d
    tl._wav_state = "ready"
    tl.set_view(40.0, 30.0)          # 显示 [40s, 70s]（选一段有起伏的）
    tl.update()

# 等波形算完（后台线程）——这是原有的等待逻辑
state = None
for _ in range(50):
    app.processEvents()
    time.sleep(0.1)
    state = getattr(win.timeline, "_wav_state", None)
    if state != "computing":
        break
print("波形状态:", state)

try:
    img = win.timeline.grab().toImage()
    img.save(OUT)
    print("已存:", OUT, "尺寸:", img.width(), "x", img.height())
except Exception as e:                                               # noqa: BLE001
    print("截图失败:", e)

# 也抓整个窗口（看得更全）
try:
    wimg = win.grab().toImage()
    wout = os.path.join(os.path.dirname(_HERE), "_shots", "wave_gain_window.png")
    wimg.save(wout)
    print("已存:", wout)
except Exception as e:                                               # noqa: BLE001
    print("窗口截图失败:", e)
