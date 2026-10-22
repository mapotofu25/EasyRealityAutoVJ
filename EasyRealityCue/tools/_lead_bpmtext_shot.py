# -*- coding: utf-8 -*-
"""真窗口截图：确认 BPM 控件的新文案真的显示出来（不被截断）+ 说明文字无字面星号。"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
# 真窗口（不用 offscreen：offscreen 缺中文字体会渲染成方框）
from PySide6.QtWidgets import QApplication, QLabel
from ercue.ui.panels import TrackPanel
from ercue.ui import theme

app = QApplication.instance() or QApplication([])
panel = TrackPanel()
panel.resize(360, 420)
panel.show()
app.processEvents()

spin = panel.sp_bpm
print("specialValueText =", repr(spin.text()))
print("specialValueText 源码值 =", repr(spin.specialValueText()))
print("tooltip =", repr(spin.toolTip()))
print("value =", spin.value())

hows = [w for w in panel.findChildren(QLabel) if "曲目" in (w.text() or "")]
for w in hows:
    print("说明文字 raw =", repr(w.text()))
    print("textFormat =", w.textFormat())
    print("有字面星号 =", "**" in w.text())

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_cueshots_ercue")
os.makedirs(out, exist_ok=True)
p1 = os.path.join(out, "bpm_unset.png"); panel.grab().save(p1)
spin.setValue(150.0); app.processEvents()
p2 = os.path.join(out, "bpm_150.png"); panel.grab().save(p2)
print("spin.text() 有值时 =", repr(spin.text()))
print("截图：", p1, os.path.getsize(p1), "B；", p2, os.path.getsize(p2), "B")
print("BPMTEXT_SHOT_OK")
