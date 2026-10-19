# -*- coding: utf-8 -*-
"""核对工具条顺序与「画面效果」子标签（本地检查）"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"P:/AutoVJ/src")
from PySide6.QtWidgets import QApplication          # noqa: E402

app = QApplication([])
import ui_main                                       # noqa: E402

w = ui_main.MainWindow()
w.show()
app.processEvents()

L = ["=== 工具条（按顺序）==="]
tb = w.main_view.layout().itemAt(0).layout()
for i in range(tb.count()):
    wd = tb.itemAt(i).widget()
    if wd is None:
        L.append("      [弹簧/间隔]")
        continue
    txt = wd.text() if hasattr(wd, "text") else ""
    L.append("   %-16s %s" % (type(wd).__name__, str(txt)[:20]))

sp = w.settings
L.append("")
L.append("=== 画面效果 子标签页 ===")
tabs = getattr(sp, "_fx_tabs", None)
L.append("   标签数 = %s" % (tabs.count() if tabs else "★ 没有"))
if tabs:
    for i in range(tabs.count()):
        L.append("     [%d] %s" % (i, tabs.tabText(i)))
L.append("")
L.append("=== 设置分区 ===")
L.append("   " + str(list(sp._sections.keys())))
L.append("")
L.append("=== 关键控件存活 ===")
for n in ("chk_beat_grid", "render_fps", "chk_gpu", "chk_color_on", "chk_postfx_on",
          "energy_scale", "beat_pick", "intensity", "transition"):
    L.append("   %-16s %s" % (n, "OK" if getattr(sp, n, None) is not None else "★ 丢失"))
open(r"P:/AutoVJ/_ui_check.txt", "w", encoding="utf-8").write("\n".join(L))
print("ok")
