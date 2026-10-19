# -*- coding: utf-8 -*-
"""离屏核对 P4 菜单瘦身：短标题 + 悬停提示 + 分组分隔线 + 危险项红点。

思路：不 monkeypatch（PySide6 类型不可改），而是**先挂一个 QTimer**，
在 QMenu.exec() 的嵌套事件循环里抓图 + 关闭菜单。

用法：venv/Scripts/python.exe -u tools/_ui_menu_check.py
输出：P:/AutoVJ/_ui_shot/3x_*.png + 控制台结构表
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"P:/AutoVJ/src")

from PySide6.QtWidgets import (QApplication, QMenu, QListWidgetItem,   # noqa: E402
                               QWidget)
from PySide6.QtCore import Qt, QPoint, QTimer                          # noqa: E402

app = QApplication([])
import theme            # noqa: E402
import ui_main          # noqa: E402
import panels           # noqa: E402

theme.apply(app, "dark")

OUT = r"P:/AutoVJ/_ui_shot"
os.makedirs(OUT, exist_ok=True)

CAP = []
CUR = {"name": "menu"}


def _dump(menu, name):
    menu.adjustSize()
    app.processEvents()
    pm = menu.grab()
    pm.save(os.path.join(OUT, name + ".png"))
    rows = []
    for a in menu.actions():
        if a.isSeparator():
            rows.append(("SEP", "", "", True))
        else:
            rows.append((a.text(), a.toolTip(),
                         "●" if not a.icon().isNull() else "",
                         a.isEnabled()))
    CAP.append((name, rows, pm.width(), pm.height()))


def _shot():
    wnd = QApplication.activePopupWidget()
    if wnd is None:
        for tl in QApplication.topLevelWidgets():
            if isinstance(tl, QMenu) and tl.isVisible():
                wnd = tl
                break
    if wnd is None:
        print(f"  !! {CUR['name']}: 没抓到弹出菜单")
        return
    _dump(wnd, CUR["name"])
    wnd.close()
    app.processEvents()


def capture(name, fn):
    CUR["name"] = name
    n0 = len(CAP)
    QTimer.singleShot(70, _shot)
    try:
        fn()
    except Exception as e:      # noqa: BLE001
        print(f"  {name} ERR: {type(e).__name__}: {e}")
    if len(CAP) == n0:
        print(f"  {name}: 未捕获")


class _DummyMedia:
    name = "dummy"
    roles = {}
    excluded = False
    tags = []
    path = ""


def main():
    w = ui_main.MainWindow()
    w.show()
    app.processEvents()

    lays = list(w.engine.layers)
    print("layers =", len(lays), [getattr(l, "name", "?") for l in lays])

    normal = next((i for i, l in enumerate(lays)
                   if not getattr(l, "is_kv", False)), 0)

    # 1) 图层右键菜单（普通图层）
    capture("30_layer_menu",
            lambda: w.layer_menu(normal, QPoint(4, 4)))

    # 2) 图层右键菜单（Kv 图层，看禁用项）
    kv = next((i for i, l in enumerate(lays) if getattr(l, "is_kv", False)), None)
    if kv is not None:
        capture("31_layer_menu_kv",
                lambda: w.layer_menu(kv, QPoint(4, 4)))

    # 3) 图层内素材格右键菜单
    capture("32_clip_menu",
            lambda: w.clip_menu(normal, _DummyMedia(), QPoint(4, 4)))

    # 4) 素材库网格右键菜单
    def _grid():
        g = panels.LibraryGrid(w)
        it = QListWidgetItem("dummy")
        it.setData(Qt.UserRole, _DummyMedia())
        g.addItem(it)
        it.setSelected(True)
        g.show()
        app.processEvents()
        g._menu(QPoint(8, 8))
    capture("33_media_grid_menu", _grid)

    # 5) 曲库列表右键菜单
    def _mlib():
        if not w.cfg["music_library"]:
            w.cfg["music_library"] = [r"P:/x.flac"]
        dlg = panels.MusicLibraryDialog(w)
        dlg.show()
        app.processEvents()
        item0 = dlg.list.item(0)
        pos = dlg.list.visualItemRect(item0).center() if item0 else QPoint(8, 8)
        dlg._ctx_menu(pos)
    capture("34_music_list_menu", _mlib)

    # ---- 打印结构 ----
    print("\n" + "=" * 78)
    for name, rows, wpx, hpx in CAP:
        print(f"\n### {name}   ({wpx}x{hpx})")
        for txt, tip, dot, en in rows:
            if txt == "SEP":
                print("   ------------------")
                continue
            mark = "" if en else "   [禁用]"
            t = f"   ⟨{tip}⟩" if tip else ""
            print(f"  {dot:1s} {txt}{t}{mark}")
    print("\n" + "=" * 78)
    print("captured:", [c[0] for c in CAP])
    print("OK")


if __name__ == "__main__":
    main()
