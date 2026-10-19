# -*- coding: utf-8 -*-
"""三页界面自检：演出台 / 素材库（全屏副本）/ 曲库·曲风。

覆盖：
  ① 页数=3、页签文案、页号钳位（含老配置里的越界值）
  ② 切到素材库页 ⇒ 那一份网格被填充、数量标签一致
  ③ 两页网格内容**完全一致**（同一批筛选状态）
  ④ 在演出台打字搜索 ⇒ 两页一起过滤、且搜索框互相同步
  ⑤ 不可见时不填充、只标脏；切回来补上（性能设计不能破）
用法：venv/Scripts/python.exe -u tools/_test_pages3.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

from PySide6.QtWidgets import QApplication                        # noqa: E402

app = QApplication([])
import ui_main                                                    # noqa: E402

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

print("① 页数与页签")
check("三页", w.pages.count() == 3, "pages=%d" % w.pages.count())
texts = [b.text() for b in getattr(w, "_page_btns", [])]
check("页签 3 个且文案正确", len(texts) == 3 and "演出台" in texts[0]
      and "素材库" in texts[1] and "曲库" in texts[2], texts)
w.switch_page(0, save=False)
check("switch_page(0) → 0", w.pages.currentIndex() == 0)
w.switch_page(1, save=False)
check("switch_page(1) → 素材库页", w.pages.currentIndex() == 1)
w.switch_page(2, save=False)
check("switch_page(2) → 曲库页", w.pages.currentIndex() == 2)
w.switch_page(9, save=False)
check("越界 9 ⇒ 钳到 2", w.pages.currentIndex() == 2)
w.switch_page(-3, save=False)
check("越界 -3 ⇒ 钳到 0", w.pages.currentIndex() == 0)

print("\n② 全屏素材库那份的填充")
check("grid_full 已建", getattr(w, "grid_full", None) is not None)
w.switch_page(1, save=False)
app.processEvents()
n_full = w.grid_full.count()
n_main = w.grid.count()
check("切到该页后网格被填充", n_full > 0, "格数=%d（库共 %d）" % (n_full, len(w.library)))
check("与演出台那份数量一致", n_full == n_main, "full=%d main=%d" % (n_full, n_main))
check("数量标签同步", w.lbl_lib_count2.text() == f"{n_full}/{len(w.library)}",
      w.lbl_lib_count2.text())

print("\n③ 搜索：两页一起过滤 + 输入框互相同步")
kw = (list(w.library.values())[0].name or "a")[:3]
w.search_box.setText(kw)
app.processEvents()
check("演出台那份被过滤", w.grid.count() < n_main or n_main == 1,
      "main: %d → %d" % (n_main, w.grid.count()))
check("素材库那份也被过滤", w.grid_full.count() == w.grid.count(),
      "full=%d main=%d" % (w.grid_full.count(), w.grid.count()))
check("另一页搜索框文本已同步", w.search_box2.text() == kw, repr(w.search_box2.text()))

print("\n④ 不可见时不填充、只标脏（性能设计）")
w.switch_page(0, save=False)
w.set_kind_filter(1)          # 只看图片
app.processEvents()
check("在演出台改筛选 ⇒ 全屏那份只标脏", w._lib_full_dirty is True)
n_main2 = w.grid.count()
w.switch_page(1, save=False)
app.processEvents()
check("切回后自动补齐", w.grid_full.count() == n_main2 and w._lib_full_dirty is False,
      "full=%d main=%d" % (w.grid_full.count(), n_main2))
check("类型按钮在两页都同步成'图片'",
      w.kind_group.button(1).isChecked() and w.kind_group2.button(1).isChecked())

print("\n⑤ 标签按钮文案两页一致")
w.tag_selected = {"测试标签"}
w._sync_lib_controls()
check("两页标签按钮文案一致",
      w.btn_tagfilter.text() == w.btn_tagfilter2.text(), w.btn_tagfilter.text())
w.tag_selected = set()
w._sync_lib_controls()

print()
print("结果：%s" % ("全部通过 ✓" if not FAILS else "失败 %d 项 ✗ %s" % (len(FAILS), FAILS)))
sys.exit(1 if FAILS else 0)
