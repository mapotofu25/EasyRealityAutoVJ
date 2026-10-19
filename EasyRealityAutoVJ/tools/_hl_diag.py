# -*- coding: utf-8 -*-
"""诊断：演出台图层行的"正在播放"金黄底为什么没出现。

做法：用**用户真实 config** 离屏起一个 MainWindow，让引擎真跑几秒（这样 lay.cur 等
运行时状态才是有意义的），然后逐图层打印：
  · 图层属性（auto/cur/clips 数）
  · `refresh_playing` 认定的"在播路径"
  · 行里每个格子的路径、是否被判为在播
最后统计整行真正被置成金黄底的格子数。

用法：venv/Scripts/python.exe -u tools/_hl_diag.py
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))
sys.path.insert(0, _HERE)

from PySide6.QtCore import Qt                                    # noqa: E402
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
w = ui_main.MainWindow()
w.save_cfg = lambda *a, **k: None
w.save_layers = lambda *a, **k: None
w.cfg.save = lambda *a, **k: None
w.show()
app.processEvents()

# ★ 必须真的**启动引擎**：不然 lay.cur 全是 -1，高亮自然没得亮（这就是看着没修好的关键）
try:
    w.toggle_run()
    print("已调用 toggle_run()（开始自动 VJ）")
except Exception as e:
    print("toggle_run 失败：", e)
print("跑 8 秒让引擎把 lay.cur / 播放器都建起来…")
t0 = time.perf_counter()
while time.perf_counter() - t0 < 8.0:
    app.processEvents()
    time.sleep(0.02)
print("引擎是否在跑：", getattr(w.engine, "running", "?"))

rows = w.layers_panel.rows
print("\n图层数 = %d，行数 = %d" % (len(w.engine.layers), len(rows)))

for i, (hdr, grid) in enumerate(rows):
    if i >= len(w.engine.layers):
        print("\n[行 %d] 没有对应图层（行多于图层）" % i)
        continue
    lay = w.engine.layers[i]
    print("\n[行 %d] %s  auto=%s  clips=%d  cur=%s  prev_idx=%s  格数=%d"
          % (i, lay.name, getattr(lay, "auto_mode", False), len(lay.clips),
             getattr(lay, "cur", None), getattr(lay, "prev_idx", None), grid.count()))
    print("     引擎侧在播路径：", end=" ")
    ps = []
    for idx in (getattr(lay, "cur", -1), getattr(lay, "prev_idx", -1)):
        if isinstance(idx, int) and 0 <= idx < len(lay.clips):
            ps.append(os.path.basename(getattr(lay.clips[idx], "path", "")))
    print(ps or "(无)", "| player:", os.path.basename(
        getattr(getattr(lay, "player", None), "path", "") or ""))
    hit = 0
    for k in range(grid.count()):
        it = grid.item(k)
        m = it.data(Qt.UserRole)
        p = getattr(m, "path", None) or (m if isinstance(m, str) else "")
        is_play = it.background().color().getRgb()[:3] == (255, 205, 0)
        if k < 3 or is_play:
            print("     格%d %-46s 金黄=%s" % (
                k, os.path.basename(str(p))[:46], "★是" if is_play else "否"))
        if is_play:
            hit += 1
    print("     ⇒ 该行金黄格数 = %d" % hit)

print("\n调用一次 refresh_playing() 后再看：")
w.layers_panel.refresh_playing()
app.processEvents()
for i, (hdr, grid) in enumerate(rows):
    n = sum(1 for k in range(grid.count())
            if grid.item(k).background().color().getRgb()[:3] == (255, 205, 0))
    print("   行 %d 金黄格数 = %d" % (i, n))
