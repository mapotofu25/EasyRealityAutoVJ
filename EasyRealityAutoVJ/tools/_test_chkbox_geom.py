# -*- coding: utf-8 -*-
"""回归测试：复选框/单选框的**指示器矩形**与**标签起点**在勾选前后必须一致。

用 Qt 自己的 `QStyle.subElementRect(SE_CheckBoxIndicator / SE_CheckBoxContents)` 量，
比数像素可靠：勾选态若把内容盒写小了/写大了，这两个矩形会立刻不等。

用法：venv/Scripts/python.exe -u tools/_test_chkbox_geom.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

from PySide6.QtWidgets import (QApplication, QCheckBox,          # noqa: E402
                               QRadioButton, QStyle, QStyleOptionButton)

app = QApplication([])
import theme                                                     # noqa: E402

app.setStyleSheet(theme._qss("dark"))
FAILS = []


def check(name, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (("   " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


def probe(cls, text, checked):
    w = cls(text)
    w.setChecked(checked)
    w.resize(w.sizeHint())
    w.show()
    app.processEvents()
    opt = QStyleOptionButton()
    w.initStyleOption(opt)
    st = w.style()
    ind = st.subElementRect(QStyle.SE_CheckBoxIndicator, opt, w)
    txt = st.subElementRect(QStyle.SE_CheckBoxContents, opt, w)
    w.close()
    return ind, txt


print("量的是【指示器矩形】与【标签内容起点】\n")
for label, cls in (("QCheckBox", QCheckBox), ("QRadioButton", QRadioButton)):
    off_ind, off_txt = probe(cls, "示例文字 ABC", False)
    on_ind, on_txt = probe(cls, "示例文字 ABC", True)
    print("%s：未勾选 指示器 %dx%d 标签x=%d ｜ 勾选 指示器 %dx%d 标签x=%d"
          % (label, off_ind.width(), off_ind.height(), off_txt.x(),
             on_ind.width(), on_ind.height(), on_txt.x()))
    check("%s 指示器宽一致" % label, off_ind.width() == on_ind.width(),
          "off=%d on=%d" % (off_ind.width(), on_ind.width()))
    check("%s 指示器高一致" % label, off_ind.height() == on_ind.height(),
          "off=%d on=%d" % (off_ind.height(), on_ind.height()))
    check("%s 指示器左边界一致" % label, off_ind.x() == on_ind.x(),
          "off=%d on=%d" % (off_ind.x(), on_ind.x()))
    check("%s 标签起点一致（这条就是间距看起来不一致的根因）" % label,
          off_txt.x() == on_txt.x(), "off=%d on=%d" % (off_txt.x(), on_txt.x()))
    print()

print("结果：%s" % ("全部通过 ✓" if not FAILS else "失败 %d 项 ✗ %s" % (len(FAILS), FAILS)))
sys.exit(1 if FAILS else 0)
