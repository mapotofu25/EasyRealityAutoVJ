# -*- coding: utf-8 -*-
"""界面几何取证：把「曲库·曲风」页真的显示出来，打印每个控件的实际位置/尺寸，
并截图存盘。

为什么要它：用户反馈「整个 UI 的显示区域」用得不对（下面一大片空白）。
凭读代码猜不出来（布局里明明写了 stretch=1），必须看**真实几何**。
⚠ offscreen 平台下 `widget.grab()` 会受布局尺寸影响出假象 ⇒ 这里默认**真显示**。
   若在无桌面环境跑，会退回 offscreen（几何仍然可读，只是截图不可信）。

用法：
  venv\\Scripts\\python.exe -u tools\\_uigeom.py            :: 真窗口 + 截图
  venv\\Scripts\\python.exe -u tools\\_uigeom.py --size 1600 900
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

ARGS = sys.argv[1:]
SIZE = (1280, 800)
WAIT = 0.0
if "--size" in ARGS:
    i = ARGS.index("--size")
    SIZE = (int(ARGS[i + 1]), int(ARGS[i + 2]))
if "--wait" in ARGS:
    WAIT = float(ARGS[ARGS.index("--wait") + 1])

from PySide6.QtWidgets import QApplication, QWidget                # noqa: E402

app = QApplication([])
import ui_main                                                     # noqa: E402


class _MB:
    @staticmethod
    def warning(*a, **k):
        pass

    @staticmethod
    def information(*a, **k):
        pass

    @staticmethod
    def question(*a, **k):
        return 0


ui_main.QMessageBox = _MB

w = ui_main.MainWindow()
w.save_cfg = lambda *a, **k: None
w.save_layers = lambda *a, **k: None
w.cfg.save = lambda *a, **k: None
w.resize(*SIZE)
w.show()
app.processEvents()

for _ in range(3):
    app.processEvents()


def dump(widget, indent=0, maxdepth=4):
    if widget is None or indent > maxdepth:
        return
    g = widget.geometry()
    txt = ""
    try:
        if hasattr(widget, "text"):
            t = widget.text()
            if isinstance(t, str) and t:
                txt = "  text=%r" % t[:26]
    except Exception:
        pass
    pol = widget.sizePolicy()
    print("  " * indent + "%-26s (%4d,%4d) %4dx%-4d  vis=%d  hpol=%s vpol=%s  hint=%dx%d  max=%dx%d%s"
          % (type(widget).__name__, g.x(), g.y(), g.width(), g.height(),
             int(widget.isVisible()), pol.horizontalPolicy().value,
             pol.verticalPolicy().value,
             widget.sizeHint().width(), widget.sizeHint().height(),
             widget.maximumWidth(), widget.maximumHeight(), txt))
    lay = widget.layout()
    if lay is None:
        return
    if hasattr(lay, "stretch") and indent <= 2:
        st = []
        for i in range(lay.count()):
            it = lay.itemAt(i)
            nm = (type(it.widget()).__name__ if it.widget() is not None
                  else ("<布局>" if it.layout() is not None else "<spacer>"))
            st.append("%s:%d" % (nm, lay.stretch(i)))
        print("  " * (indent + 1) + "stretch → " + ", ".join(st))
    for i in range(lay.count()):
        it = lay.itemAt(i)
        ch = it.widget()
        if ch is not None:
            dump(ch, indent + 1, maxdepth)
        elif it.layout() is not None:
            print("  " * (indent + 1) + "<子布局 %s>" % type(it.layout()).__name__)
            for j in range(it.layout().count()):
                c2 = it.layout().itemAt(j).widget()
                if c2 is not None:
                    dump(c2, indent + 2, maxdepth)


for pi, name in ((2, "曲库·曲风"), (1, "素材库"), (0, "演出台")):
    w.switch_page(pi)
    app.processEvents()
    for _ in range(3):
        app.processEvents()
    page = w.pages.currentWidget()
    print()
    print("=" * 92)
    print("第 %d 页 = %s   页面尺寸 %dx%d   窗口 %dx%d"
          % (pi, name, page.width(), page.height(), w.width(), w.height()))
    print("=" * 92)
    dump(page, 0, 5)

# 全窗口截图
if os.environ.get("QT_QPA_PLATFORM", "").startswith("offscreen"):
    print("\n（offscreen 模式，跳过截图）")
else:
    out = os.path.join(_ROOT, "_uigeom_page2.png")
    w.switch_page(2)
    if WAIT > 0:
        import time as _t
        t0 = _t.perf_counter()
        while _t.perf_counter() - t0 < WAIT:
            app.processEvents()
            _t.sleep(0.05)
    for _ in range(4):
        app.processEvents()
    pm = w.grab()
    pm.save(out)
    print("\n截图已存：%s  %dx%d" % (out, pm.width(), pm.height()))
    try:
        lst = w.music_panel.list
        print("列表项数 %d；带图标的 %d"
              % (lst.count(),
                 sum(1 for i in range(lst.count()) if not lst.item(i).icon().isNull())))
    except Exception as e:                                     # noqa: BLE001
        print("（读列表失败：%s）" % e)

print("\n窗口可用屏幕：")
for s in app.screens():
    print("  %s  %dx%d  dpr=%.2f" % (s.name(), s.geometry().width(),
                                     s.geometry().height(), s.devicePixelRatio()))
