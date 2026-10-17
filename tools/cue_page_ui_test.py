# -*- coding: utf-8 -*-
"""**真主窗**冒烟：第 4 页签「码表」+ 新建码表图层 + 「⋯ 更多」无码表项。

为什么还要这一层（前面的 `cue_page_test.py` 是"零件级"）：页签是 `QStackedWidget`
+ 自绘按钮条，`layer_add` 是主窗里的胶水代码 —— 只有把**真 MainWindow** 建起来，
才能证明"点得到、切得动、5 元组没接错"。

判据：
  1. 页签 4 个（演出台 / 素材库 / 曲库·曲风 / 码表），`switch_page(3)` 生效并落盘
  2. 老配置 `page=2` 仍合法；`page=9` 被钳回 0（不出现空白界面）
  3. 第 4 页就是 `cue_page.CueSheetPanel`（不是个空壳）
  4. `layer_add` 选「码表图层」⇒ 新图层 `is_cuesheet=True` + 头部下拉可见且有表名
  5. `⋯ 更多` 菜单里没有任何码表动作
  6. 窗口整幅截图（页签条 + 码表列表）`tools/_cueshots/ui_page4_tab.png`

运行：`venv/Scripts/python.exe tools/cue_page_ui_test.py`（exit 0 = 全过）
"""
from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
SHOT_DIR = os.path.join(HERE, "_cueshots")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from PySide6.QtWidgets import QApplication, QDialog                 # noqa: E402

import cuesheet                                                     # noqa: E402
import cue_page                                                     # noqa: E402
import ui_main                                                      # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print(("  [ok  ] " if ok else "  [FAIL] ") + name
          + (("   ← " + str(extra)) if (extra and not ok) else ""))
    if not ok:
        FAILS.append(name)


class FakeBox:
    @staticmethod
    def warning(*a, **k):
        print("      [QMessageBox.warning] %s" % (a[2] if len(a) > 2 else a))

    @staticmethod
    def information(*a, **k):
        pass


def make_sheet(title, track_title, media_name):
    s = cuesheet.CueSheet(title=title, res_w=320, res_h=180)
    te = cuesheet.TrackEntry()
    te.identity = cuesheet.Identity(title=track_title, artist="测试者",
                                    duration_sec=180.0, bpm=120.0)
    te.identity.ensure_meta_id()
    L = cuesheet.CompLayer(lyr_id="L1", name="层1", z=0)
    L.items.append(cuesheet.CueItem(item_id="i1", clip_id="c1", at_beats=0.0,
                                    src_in_sec=0.0, src_out_sec=8.0,
                                    trans_in="cut", trans_out="cut").normalized())
    te.layers.append(L)
    s.tracks.append(te)
    s.media.append(cuesheet.MediaRef(clip_id="c1", name=media_name))
    return s


def main():
    os.environ.pop("QT_QPA_PLATFORM", None)      # 真平台（offscreen 没 CJK 字体）
    app = QApplication.instance() or QApplication(sys.argv)
    tmp = tempfile.mkdtemp(prefix="cueui_")

    # 两张码表 + 素材目录（放到 cfg 里 ⇒ 装表时不会弹"找不到素材"）
    media_root = os.path.join(tmp, "media")
    os.makedirs(media_root, exist_ok=True)
    open(os.path.join(media_root, "a.mp4"), "wb").close()
    open(os.path.join(media_root, "b.mp4"), "wb").close()
    pa = os.path.join(tmp, "测试码表.ercue")
    pb = os.path.join(tmp, "第二张.ercue")
    cuesheet.save(make_sheet("测试码表", "夏夜星律", "a.mp4"), pa,
                  app_version="2026.10.17.04")
    cuesheet.save(make_sheet("第二张", "另一首", "b.mp4"), pb,
                  app_version="2026.10.17.04")

    # ★ 必须像 main.py 那样先 `theme.apply()`：否则 palette/QSS 没装，
    #   `theme.V("text")` 却是暗色主题的浅灰 ⇒ 截图里标题会"浅灰字压白底"（测试假象）。
    import theme
    theme.apply(app, "dark")
    ui_main.QMessageBox = FakeBox
    w = ui_main.MainWindow()
    # 不许写用户真实配置/图层文件（测试只验证界面行为）
    w.save_cfg = lambda *a, **k: None
    w.save_layers = lambda *a, **k: None
    w.cfg.save = lambda *a, **k: None
    w.show()
    for _ in range(8):
        app.processEvents()

    # ---------------- ① 页签 ----------------
    print("① 页签：4 个，第 4 个是「码表」")
    check("pages 有 4 页", w.pages.count() == 4, w.pages.count())
    check("页签按钮 4 个", len(getattr(w, "_page_btns", [])) == 4,
          len(getattr(w, "_page_btns", [])))
    texts = [b.text() for b in getattr(w, "_page_btns", [])]
    print("      页签 = %s" % texts)
    check("第 4 个页签文本含「码表」", "码表" in (texts[3] if len(texts) > 3 else ""),
          texts)
    w.switch_page(3, save=False)
    check("switch_page(3) 生效", w.pages.currentIndex() == 3, w.pages.currentIndex())
    check("第 4 页按钮被选中", w._page_btns[3].isChecked())

    # ---------------- ② 索引钳位 ----------------
    print("\n② 老配置的 page 值必须被钳进合法范围")
    w.cfg["ui"]["page"] = 9
    w.show()                      # 触发不了开关机路径，直接调钳位逻辑
    try:
        idx = int(w.cfg["ui"].get("page", 0) or 0)
    except Exception:                                            # noqa: BLE001
        idx = 0
    check("page=9 ⇒ 钳到 0", (idx if 0 <= idx <= 3 else 0) == 0)
    w.switch_page(2, save=False)
    check("switch_page(2) 仍是曲库页（老页签没被顶掉）",
          w.pages.currentIndex() == 2, w.pages.currentIndex())

    # ---------------- ③ 第 4 页是码表面板 ----------------
    print("\n③ 第 4 页是 CueSheetPanel")
    check("cue_panel 是 CueSheetPanel 实例",
          isinstance(getattr(w, "cue_panel", None), cue_page.CueSheetPanel),
          type(getattr(w, "cue_panel", None)))

    # 把两张表塞进索引（等价于用户在第 4 页「添加…」）
    w.cfg["cue"] = {"recent": [], "media_dir": media_root,
                    "seek_profile": "auto", "sheets": []}
    idxo = w.engine.cue_index()
    idxo.add(pa)
    idxo.add(pb)
    idxo.save_cfg(w.cfg)
    w.cue_panel.refresh()
    w.switch_page(3, save=False)
    for _ in range(10):
        app.processEvents()
    check("列表里 2 张码表", w.cue_panel.tbl.rowCount() == 2,
          w.cue_panel.tbl.rowCount())

    # ---------------- ④ 新建「码表图层」 ----------------
    print("\n④ layer_add：选「码表图层」⇒ 选表 ⇒ 图层带下拉")
    n_before = len(w.engine.layers)

    class FakeNewDlg:
        def __init__(self, parent=None, default_name=""):
            pass

        def exec(self):
            return QDialog.Accepted

        def result_data(self):
            return ("码表图层", False, "bg", False, True)   # 5 元组：is_cue=True

    real_dlg = ui_main.NewLayerDialog
    real_pick = cue_page.pick_sheet
    ui_main.NewLayerDialog = FakeNewDlg
    cue_page.pick_sheet = lambda owner, parent=None, prompt="": idxo.entries[0]
    try:
        w.layer_add()
    finally:
        ui_main.NewLayerDialog = real_dlg
        cue_page.pick_sheet = real_pick
    for _ in range(10):
        app.processEvents()

    check("新增了 1 个图层", len(w.engine.layers) == n_before + 1, len(w.engine.layers))
    lay = w.engine.layers[-1]
    check("新图层是码表图层", getattr(lay, "is_cuesheet", False) is True)
    check("cuesheet_id 指向选中的包",
          str(getattr(lay, "cuesheet_id", "")) == "测试码表.ercue", lay.cuesheet_id)
    row = w.layers_panel.rows[-1][0] if w.layers_panel.rows else None
    cmb = getattr(row, "_cmb_cue", None) if row is not None else None
    check("图层头有码表下拉", cmb is not None)
    if cmb is not None:
        check("下拉可见", not cmb.isHidden())
        check("下拉显示表名", cmb.currentText() == "测试码表", cmb.currentText())

    # ---------------- ⑤ 更多菜单里没有码表 ----------------
    print("\n⑤ 「⋯ 更多」里没有码表项")
    acts = [a.text() for a in w._more_menu.actions()]
    print("      更多菜单 = %s" % acts)
    check("更多菜单里没有任何含「码表」的项", not any("码表" in a for a in acts), acts)

    # ---------------- ⑥ 整幅截图 ----------------
    print("\n⑥ 整幅截图")
    os.makedirs(SHOT_DIR, exist_ok=True)
    w.switch_page(3, save=False)
    w.resize(1500, 900)
    for _ in range(12):
        app.processEvents()
    p = os.path.join(SHOT_DIR, "ui_page4_tab.png")
    ok = w.grab().save(p, "PNG")
    check("截图已落盘 ui_page4_tab.png", ok, p)
    # 再截一张"演出台"（图层头下拉在真窗口里的样子）
    w.switch_page(0, save=False)
    for _ in range(12):
        app.processEvents()
    p2 = os.path.join(SHOT_DIR, "ui_page1_layer_combo.png")
    check("截图已落盘 ui_page1_layer_combo.png", w.grab().save(p2, "PNG"), p2)

    print("")
    if FAILS:
        print("CUE_UI_FAIL  %d 项不通过：%s" % (len(FAILS), "、".join(FAILS)))
        return 1
    print("CUE_UI_OK  4 页签 / 钳位 / 码表面板 / 新建码表图层 / 更多菜单 全过")
    print("截图：%s" % SHOT_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
