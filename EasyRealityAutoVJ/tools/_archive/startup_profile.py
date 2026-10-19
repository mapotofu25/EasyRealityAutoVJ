# -*- coding: utf-8 -*-
r"""启动耗时剖析：主窗口构造到底慢在哪。

用法：venv\Scripts\python.exe tools\startup_profile.py
输出：P:\AutoVJ\_startup_profile.txt
"""
import os
import sys
import io
import time
import cProfile
import pstats
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

out = []


def w(s=""):
    out.append(str(s))
    print(s, flush=True)


def watchdog(sec=180):
    time.sleep(sec)
    w("!! 超时 %ds，强制退出（可能卡在模态框）" % sec)
    open(r"P:\AutoVJ\_startup_profile.txt", "w", encoding="utf-8").write("\n".join(out))
    os._exit(3)


threading.Thread(target=watchdog, daemon=True).start()

from PySide6.QtWidgets import QApplication  # noqa: E402

t = time.perf_counter()
app = QApplication([])
w("QApplication      %6.2fs" % (time.perf_counter() - t))

import config  # noqa: E402
import theme  # noqa: E402

t = time.perf_counter()
cfg = config.Config()
w("Config()          %6.2fs   素材 %d 个 / 音乐 %d 首"
  % (time.perf_counter() - t, len(cfg["library"]), len(cfg["music_library"])))

t = time.perf_counter()
theme.apply(app, cfg["theme"])
w("theme.apply       %6.2fs" % (time.perf_counter() - t))

from ui_main import MainWindow  # noqa: E402

pr = cProfile.Profile()
pr.enable()
t = time.perf_counter()
win = MainWindow()
el = time.perf_counter() - t
pr.disable()
w("MainWindow()      %6.2fs   ← 主窗口构造" % el)
w("")

s = io.StringIO()
ps = pstats.Stats(pr, stream=s)
ps.sort_stats("cumulative").print_stats(32)
w("================ 累计耗时 TOP（cumulative）================")
w(s.getvalue())

s2 = io.StringIO()
ps2 = pstats.Stats(pr, stream=s2)
ps2.sort_stats("tottime").print_stats(22)
w("================ 自身耗时 TOP（tottime）================")
w(s2.getvalue())

open(r"P:\AutoVJ\_startup_profile.txt", "w", encoding="utf-8").write("\n".join(out))
os._exit(0)
