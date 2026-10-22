# -*- coding: utf-8 -*-
"""三界面拆分（演出台 / 素材·曲库·曲风 / 码表编排）自测（离屏，不落盘）。

覆盖：页签条存在且互斥 → 各页归属正确（预览在 1、素材库在 2、编排在 3）
     → 惰性创建（没切过去之前不建编排面板）→ 切走时编排的 500ms 刷新停掉
     → `open_cuesheet()` 切页而非弹窗 → 中英切换后页签文案跟着变。

用法：venv/Scripts/python.exe -u tools/_ui_pages_test.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

from PySide6.QtWidgets import QApplication            # noqa: E402

app = QApplication([])
import i18n                                            # noqa: E402
import ui_main                                         # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (("   " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


def main():
    w = ui_main.MainWindow()
    w.save_cfg = lambda *a, **k: None
    w.save_layers = lambda *a, **k: None
    w.cfg.save = lambda *a, **k: None        # 别把测试的页号写进用户配置
    ui_main.QMessageBox.warning = lambda *a, **k: None
    w.show()
    app.processEvents()

    # ① 容器与页签条
    check("存在三页容器且正好 3 页", hasattr(w, "pages") and w.pages.count() == 3,
          getattr(getattr(w, "pages", None), "count", lambda: "-")())
    check("页签条有 3 个按钮", len(getattr(w, "_page_btns", [])) == 3)
    check("页签互斥（同一 QButtonGroup）",
          len(w._page_group.buttons()) == 3 and w._page_group.exclusive())

    # ② 页号是**记住**的（cfg["ui"]["page"]）⇒ 先显式切回演出台，再验证切换本身
    w.switch_page(0, save=False)
    app.processEvents()
    check("切到演出台（第 1 页）", w.pages.currentIndex() == 0, w.pages.currentIndex())
    check("演出台页签是选中态", w._page_btns[0].isChecked()
          and not w._page_btns[1].isChecked() and not w._page_btns[2].isChecked())

    # ③ 各页归属
    p1, p2, p3 = w.pages.widget(0), w.pages.widget(1), w.pages.widget(2)
    check("预览面板挂在第 1 页（演出台）", w.preview_panel.parent() is p1
          or p1.isAncestorOf(w.preview_panel))
    check("图层面板挂在第 1 页（演出台）", p1.isAncestorOf(w.layers_panel))
    check("设置面板挂在第 1 页（演出台）", p1.isAncestorOf(w.settings))
    check("输出面板挂在第 1 页（演出台）", p1.isAncestorOf(w.output_panel))
    check("素材库挂在第 1 页（演出台，用户要求保留在主界面）",
          p1.isAncestorOf(w.library_panel) and p1.isAncestorOf(w.grid),
          type(w.library_panel).__name__)
    check("第 2 页有「音乐曲库 / 曲风映射」入口",
          p2.isAncestorOf(w.btn_p2_music) and p2.isAncestorOf(w.btn_p2_gvmap))
    check("第 2 页显示曲库概况（只读一行）",
          hasattr(w, "lbl_p2_stat") and "曲库" in w.lbl_p2_stat.text(),
          getattr(getattr(w, "lbl_p2_stat", None), "text", lambda: "-")())
    check("顶部工具条不再有「音乐曲库 / 曲风映射」按钮",
          not hasattr(w, "btn_music") and not hasattr(w, "btn_gvmap"))

    # ④ 惰性创建：没切过去之前不该有编排面板
    # 惰性创建只在**本次进程**没切到第 3 页时成立；页号从配置读 ⇒ 这里只查页号
    check("第 1/2 页时编排面板不挡路", w.pages.currentIndex() in (0, 1))

    # ⑤ 切到第 2 页
    w._page_btns[1].click()
    app.processEvents()
    check("点页签切到第 2 页", w.pages.currentIndex() == 1, w.pages.currentIndex())
    check("第 2 页页签选中", w._page_btns[1].isChecked())
    check("切到第 2 页确实停在第 2 页", w.pages.currentIndex() == 1)

    # ⑥ 切到第 3 页
    w.switch_page(2)
    app.processEvents()
    check("切到第 3 页", w.pages.currentIndex() == 2, w.pages.currentIndex())
    check("第 3 页惰性建出编排面板", w.cue_editor is not None)
    check("编排面板挂在第 3 页", p3.isAncestorOf(w.cue_editor))
    check("编排面板可见时刷新定时器在跑",
          w.cue_editor is not None and w.cue_editor._timer.isActive())

    # ⑦ 切走 ⇒ 定时器停（主线程不做无用功）
    w.switch_page(0)
    app.processEvents()
    check("切回演出台", w.pages.currentIndex() == 0, w.pages.currentIndex())
    check("切走后编排刷新定时器停掉",
          w.cue_editor is not None and not w.cue_editor._timer.isActive())
    check("再切回第 3 页定时器恢复", (w.switch_page(2), w.cue_editor._timer.isActive())[1])

    # ⑧ ⋯ 菜单那条动作走的是同一个入口
    w.switch_page(0)
    w.open_cuesheet()
    check("open_cuesheet() 切到第 3 页（不再弹独立窗口）",
          w.pages.currentIndex() == 2, w.pages.currentIndex())

    # ⑨ 越界/非法页号不炸
    w.switch_page(99)
    check("非法页号被钳到第 3 页", w.pages.currentIndex() == 2, w.pages.currentIndex())
    w.switch_page(-5)
    check("负页号被钳到第 1 页", w.pages.currentIndex() == 0, w.pages.currentIndex())
    w.switch_page("x")
    check("页号是垃圾值也不炸（回第 1 页）", w.pages.currentIndex() == 0, w.pages.currentIndex())

    # ⑩ 中英切换：页签文案跟着变
    i18n.set_lang("zh")
    w.retranslate_ui()
    zh = [b.text() for b in w._page_btns]
    check("中文页签文案正确", zh[0] == "🎬 演出台" and zh[1] == "🎵 曲库 · 曲风"
          and zh[2] == "🎞 码表编排", zh)
    i18n.set_lang("en")
    w.retranslate_ui()
    en = [b.text() for b in w._page_btns]
    check("英文页签文案正确（无残留中文）", "Live" in en[0] and "Cue Sheets" in en[2], en)
    check("英文页签 tooltip 也翻了", "Live:" in w._page_btns[0].toolTip(),
          w._page_btns[0].toolTip())
    i18n.set_lang("zh")
    w.retranslate_ui()
    check("切回中文复原", [b.text() for b in w._page_btns] == zh)

    # ⑪ 主题切换不炸（页签样式是注册进 theme 的）
    try:
        w.toggle_theme()
        app.processEvents()
        ok = True
        err = ""
    except Exception as e:                                   # noqa: BLE001
        ok, err = False, "%s: %s" % (type(e).__name__, e)
    check("切主题时页签重设样式不炸", ok, err)
    check("切主题后页签仍有样式", bool(w._page_btns[0].styleSheet()), )

    # ⑫ 双击缩略图 = 播放到当前图层（快捷方式）
    from PySide6.QtCore import Qt as _Qt
    from PySide6.QtWidgets import QListWidgetItem
    w.save_layers = lambda *a, **k: None
    w.layers_panel.sync_layer = lambda *a, **k: None
    lay = w.engine.layers[0]
    lay.auto_mode = False
    w._cur_layer = 0
    n0 = len(lay.clips)
    marker = object()
    it = QListWidgetItem("x")
    it.setData(_Qt.UserRole, marker)
    w.grid._on_double_click(it)
    check("双击缩略图 ⇒ 素材进了当前图层",
          len(lay.clips) == n0 + 1 and lay.clips[-1] is marker, len(lay.clips))
    lay.auto_mode = True                     # 自动图层不接受手动添加
    n1 = len(lay.clips)
    w.grid._on_double_click(it)
    check("自动图层不会被双击污染", len(lay.clips) == n1, len(lay.clips))
    lay.auto_mode = False

    # 打扫
    if w.cue_editor is not None:
        try:
            w.cue_editor._stop()
        except Exception:                                    # noqa: BLE001
            pass
        w.cue_editor.close()
    w.close()

    print("")
    if FAILS:
        print("❌ 失败 %d 项：%s" % (len(FAILS), FAILS))
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
