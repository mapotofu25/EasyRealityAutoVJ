# -*- coding: utf-8 -*-
"""摘除码表之后的自检（离屏）。

覆盖最容易出事的四处：
① 界面只剩两页；老配置里的 page=2 必须被钳回合法值
② NewLayerDialog.result_data() 与 ui_main.layer_add 必须同为 4 元组
③ 图层面板 rebuild / refresh_header / refresh_playing 不能再引用已删的 _btn_cue
④ 引擎 tick 一轮不能再引用已删的码表分支

用法：venv/Scripts/python.exe -u tools/_after_strip_smoke.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))
sys.path.insert(0, _HERE)

from PySide6.QtWidgets import QApplication                    # noqa: E402

app = QApplication([])
import panels                                                 # noqa: E402
import ui_main                                                # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (("   " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


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

print("① 界面页数")
check("三页（演出台 / 素材库 / 曲库·曲风）", w.pages.count() == 3, "pages=%d" % w.pages.count())
check("页签按钮三个", len(getattr(w, "_page_btns", [])) == 3)
w.switch_page(0, save=False)
check("switch_page(0) → 0", w.pages.currentIndex() == 0)
w.switch_page(1, save=False)
check("switch_page(1) → 1", w.pages.currentIndex() == 1)
w.switch_page(2, save=False)
check("switch_page(2) → 曲库页", w.pages.currentIndex() == 2)
w.switch_page(9, save=False)
check("★ 越界页号被钳到 2（不会留空页）", w.pages.currentIndex() == 2,
      "currentIndex=%d" % w.pages.currentIndex())
check("没有 cue_editor 属性（已删）", not hasattr(w, "cue_editor"))
check("没有 cue_sheets 属性（已删）", not hasattr(w, "cue_sheets"))

print("② 新建图层对话框返回值")
dlg = panels.NewLayerDialog(w)
data = dlg.result_data()
check("result_data() 是 4 元组 (name, auto, role, is_kv)", len(data) == 4, repr(data))
check("对话框里没有 rb_cue", not hasattr(dlg, "rb_cue"))
check("NewLayerDialog 不接受 cue_hint 参数",
      "cue_hint" not in panels.NewLayerDialog.__init__.__code__.co_varnames)
check("主界面没有码表相关方法",
      not any(hasattr(w, x) for x in ("open_cuesheet", "cue_layer_menu", "cue_layer_use")))

print("③ 图层面板")
w.layers_panel.rebuild()
app.processEvents()
w.layers_panel.refresh_header()
w.layers_panel.refresh_playing()
check("rebuild / refresh_header / refresh_playing 均不报错", True)
row = w.layers_panel.rows[0][0] if w.layers_panel.rows else None
check("图层头不再有 _btn_cue", row is None or not hasattr(row, "_btn_cue"))

print("④ 引擎跑一轮")
err = ""
try:
    import time
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 1.5:
        app.processEvents()
        time.sleep(0.02)
except Exception as e:                                        # noqa: BLE001
    err = "%s: %s" % (type(e).__name__, e)
check("引擎 tick 1.5 秒无异常", not err, err)
check("引擎没有 _cuesheet_tick（已删）", not hasattr(w.engine, "_cuesheet_tick"))
check("引擎没有 set_cue_sheet（已删）", not hasattr(w.engine, "set_cue_sheet"))
check("Layer 不再有 is_cuesheet 属性",
      not hasattr(w.engine.layers[0], "is_cuesheet"))

print()
print("结果：%s" % ("全部通过 ✓" if not FAILS else "失败 %d 项 ✗ %s" % (len(FAILS), FAILS)))
sys.exit(1 if FAILS else 0)
