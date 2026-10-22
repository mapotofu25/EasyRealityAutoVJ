# -*- coding: utf-8 -*-
"""核对工具条与设置分区（本地检查脚本）"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"P:/AutoVJ/src")
from PySide6.QtWidgets import QApplication          # noqa: E402

app = QApplication([])
import ui_main                                       # noqa: E402

w = ui_main.MainWindow()
w.resize(1500, 900)
w.show()
app.processEvents()

out = []
tb = w.main_view.layout().itemAt(0).layout()
for i in range(tb.count()):
    wd = tb.itemAt(i).widget()
    if wd is None:
        out.append("      [分隔/弹簧]")
        continue
    txt = wd.text() if hasattr(wd, "text") else ""
    tag = "隐藏" if wd.isHidden() else "显示"
    out.append("   %-4s %-16s %s" % (tag, type(wd).__name__, str(txt)[:18]))

lines = ["=== 工具条控件（按顺序）==="] + out
lines.append("")
lines.append("btn_theme 隐藏 = %s" % w.btn_theme.isHidden())
lines.append("⋯ 菜单项 = %s" % [a.text() for a in w._more_menu.actions()])
lines.append("语言子菜单 = %s" % [a.text() for a in w._lang_menu.actions()])
lines.append("")
lines.append("设置分区 = %s" % list(getattr(w.settings, "_sections", {}).keys()))
lines.append("输出面板独立且可见 = %s" % w.output_panel.isVisible())
lines.append("")
lines.append("=== 关键控件存活 ===")
for n in ("chk_beat_grid", "btn_grid_flip", "lbl_grid_flip", "render_fps", "chk_gpu",
          "resolution", "chk_spout", "chk_ndi", "screen_combo", "beat_pick", "energy_scale"):
    o = getattr(w.settings, n, None)
    if o is None:
        o = getattr(w.output_panel, n, None)
    lines.append("   %-16s %s" % (n, "OK" if o is not None else "★ 丢失"))

open(r"P:/AutoVJ/_ui_check.txt", "w", encoding="utf-8").write("\n".join(lines))
print("written")
