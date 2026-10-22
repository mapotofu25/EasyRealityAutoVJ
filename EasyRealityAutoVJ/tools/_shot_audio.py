# -*- coding: utf-8 -*-
"""离屏截图：音源设置对话框（核对 ASIO 灰 / NDI 音频行已删）。

用临时数据目录，不碰真实 config。
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

_TMP = tempfile.mkdtemp(prefix="avj_shot_")
os.environ["LOCALAPPDATA"] = _TMP
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication                       # noqa: E402

app = QApplication.instance() or QApplication([])

import panels                                                     # noqa: E402

out = os.path.join(ROOT, "tools", "_shot_audio.png")
dlg = None
for name in dir(panels):
    obj = getattr(panels, name)
    if isinstance(obj, type) and issubclass(obj, panels.QDialog if hasattr(panels, "QDialog") else object):
        if name == "AudioSourceDialog":
            try:
                dlg = obj(None)
                print("构造", name)
                break
            except Exception as e:                                # noqa: BLE001
                print("构造失败", name, e)
if dlg is None:
    print("!! 没找到音源设置对话框类")
    sys.exit(1)
dlg.resize(520, 430)
dlg.show()
app.processEvents()
dlg.grab().save(out)
print("已保存", out)
sys.exit(0)
