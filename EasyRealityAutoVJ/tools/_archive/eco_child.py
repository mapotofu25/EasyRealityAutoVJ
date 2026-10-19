# -*- coding: utf-8 -*-
"""render_fps 诊断 —— 子进程版（由 tools/eco_diag.py 调用，也可单独运行）。

单独运行：先手动把项目根 config.json 的 perf.render_fps 设成想测的值，再跑本脚本。
它会打印引擎的真实状态（timer 是否在跑、间隔多少、图层有没有 player）。
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

try:
    import main  # noqa: F401
except Exception as e:
    print("import main 失败:", e)

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication([])
import config as cm  # noqa: E402
import theme  # noqa: E402

c = cm.Config()
theme.apply(app, c["theme"])
from ui_main import MainWindow  # noqa: E402

# ---- 关键：在构造 MainWindow 之前包一层 start()，看它到底有没有被调用、在哪个线程 ----
import threading  # noqa: E402
from engine import AutoVJEngine  # noqa: E402

_orig_start = AutoVJEngine.start
_orig_setfps = AutoVJEngine.set_render_fps


def _patched_start(self):
    print(">>> [start] 被调用  线程=%s" % threading.current_thread().name, flush=True)
    try:
        _orig_start(self)
        print(">>> [start] 返回后 timerActive=%s interval=%s 线程=%s"
              % (self.timer.isActive(), self.timer.interval(),
                 threading.current_thread().name), flush=True)
    except Exception as e:
        print(">>> [start] 抛异常: %s %s" % (type(e).__name__, e), flush=True)


def _patched_setfps(self, fps):
    print(">>> [set_render_fps] fps=%s 线程=%s  (调用前 timerActive=%s)"
          % (fps, threading.current_thread().name, self.timer.isActive()), flush=True)
    try:
        _orig_setfps(self, fps)
    except Exception as e:
        print(">>> [set_render_fps] 抛异常: %s %s" % (type(e).__name__, e), flush=True)


AutoVJEngine.start = _patched_start
AutoVJEngine.set_render_fps = _patched_setfps

w = MainWindow()
print("配置 render_fps   =", (c["perf"] or {}).get("render_fps"))
print("下拉 index/文本    =", w.settings.render_fps.currentIndex(),
      "/", w.settings.render_fps.currentText())
print("frame_interval_ms =", w.engine.frame_interval_ms())

def dump_state(tag):
    print("[%s] running=%s thread=%s timerActive=%s interval=%s"
          % (tag, w.engine.running, w.engine_thread.isRunning(),
             w.engine.timer.isActive(), w.engine.timer.interval()))


print("timer 所属线程 == 引擎线程 :", w.engine.timer.thread() is w.engine_thread)
print("engine 所属线程 == 引擎线程:", w.engine.thread() is w.engine_thread)
print("engine.cfg is 同一对象      :", w.engine.cfg is c)
try:
    print("engine.cfg 里有 perf 键     :", "perf" in w.engine.cfg.data,
          w.engine.cfg.data.get("perf"))
except Exception as e:
    print("读 engine.cfg 失败:", e)
print("frame_interval_ms          =", w.engine.frame_interval_ms())
print()

w.toggle_run()
t = time.perf_counter()
while time.perf_counter() - t < 15:
    app.processEvents()
    time.sleep(0.004)
dump_state("跑 15 秒后")

# ---- 手动调一次 start()，看到底是"没被调用"还是"调了没生效" ----
print("--- 手动调用 engine.start() ---")
try:
    w.engine.start()
except Exception as e:
    print("   start() 抛异常:", type(e).__name__, e)
dump_state("手动 start 之后")
t = time.perf_counter()
while time.perf_counter() - t < 6:
    app.processEvents()
    time.sleep(0.004)
dump_state("再等 6 秒")

layers = list(w.engine.layers)
print("图层数            =", len(layers))
nplayer = 0
for i, l in enumerate(layers):
    pl = l.player
    if pl is not None:
        nplayer += 1
    print("   #%d fixed=%s clips=%d player=%s active=%s ready=%s" % (
        i, getattr(l, "fixed", None), len(l.clips),
        type(pl).__name__ if pl is not None else None,
        getattr(pl, "_active", None),
        getattr(pl, "_img", None) is not None))
print("有 player 的图层数 =", nplayer)
try:
    import thread_attrib as ta
    ta.sample(3.0, "子进程采样", top=8)
except Exception as e:
    print("采样失败:", e)
sys.stdout.flush()
