# -*- coding: utf-8 -*-
"""复现并定位"CPU 一点一点上来"：每 10 秒打印一次解码器活跃情况。

判据：
  · 若"活跃解码器"数量随时间**增长** ⇒ 激活逻辑有问题（代码 bug）；
  · 若一直是 4~8 个但 CPU 仍高 ⇒ 是"每路解码太贵/没限速"（fps 或 speed 的问题）。
用法：venv/Scripts/python.exe -u tools/_active_repro.py [秒=90]
"""
import os
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))
sys.path.insert(0, _HERE)

from PySide6.QtWidgets import QApplication                       # noqa: E402

app = QApplication([])
import ui_main                                                   # noqa: E402


class _MB:
    @staticmethod
    def warning(*a, **k):
        pass

    @staticmethod
    def information(*a, **k):
        pass


ui_main.QMessageBox = _MB
SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 90.0

w = ui_main.MainWindow()
w.save_cfg = lambda *a, **k: None
w.save_layers = lambda *a, **k: None
w.cfg.save = lambda *a, **k: None
w.show()
app.processEvents()
w.toggle_run()
print("引擎已启动，观察 %.0f 秒…\n" % SECS)


def dec_threads():
    n = 0
    for t in threading.enumerate():
        if t.name == "autovj-dec":
            n += 1
    return n


t0 = time.perf_counter()
nxt = 0.0
while time.perf_counter() - t0 < SECS:
    app.processEvents()
    time.sleep(0.02)
    el = time.perf_counter() - t0
    if el < nxt:
        continue
    nxt = el + 10.0
    print("── t=%3.0fs  解码线程 %d 个 ──" % (el, dec_threads()))
    tot_active = 0
    tot_cache = 0
    for i, lay in enumerate(w.engine.layers):
        cache = getattr(lay, "cache", None) or {}
        tot_cache += len(cache)
        acts = [p for p in cache.values() if getattr(p, "_active", False)]
        tot_active += len(acts)
        cur = getattr(lay, "cur", -1)
        clips = list(getattr(lay, "clips", []) or [])
        nm = os.path.basename(getattr(clips[cur], "path", "")) if \
            isinstance(cur, int) and 0 <= cur < len(clips) else "(无)"
        info = []
        for p in acts:
            cls = type(p).__name__
            gpu = getattr(p, "gpu", None)
            fps = getattr(p, "fps", 0)
            spd = getattr(p, "speed", 1.0)
            warm = getattr(p, "_warm", 0)
            info.append("%s(gpu=%s,fps≈%.0f,spd=%.2f,warm=%s)"
                        % (cls[:12], gpu, float(fps or 0), float(spd or 1), warm))
        print("   [%d] %-10s 缓存%2d 活跃%d  cur=%s  在播=%s"
              % (i, lay.name[:10], len(cache), len(acts), cur, nm[:28]))
        for x in info:
            print("        · %s" % x)
    print("   合计：缓存 %d 个解码器，**活跃 %d 个**\n" % (tot_cache, tot_active))
