# -*- coding: utf-8 -*-
"""素材面板：缩略图网格 + 右键菜单（在素材上 vs 在空白处）。

用户 2026-11-03：「素材改成那个图标形式……就改成那种网格的。然后右键能设置预览大小啊、
删除啊等等这些的……别验证一下在素材上右键和在空白地方右键时的这些逻辑，哪些能点哪些不能点。」

跑：venv\\Scripts\\python.exe -u tests\\test_media_menu.py
"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_TMP = tempfile.mkdtemp(prefix="cuemenu_")
os.environ["LOCALAPPDATA"] = _TMP

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

from PySide6.QtCore import Qt                                   # noqa: E402
from PySide6.QtWidgets import QApplication, QListView, QListWidgetItem  # noqa: E402

from ercue.ui.panels import MediaPanel                           # noqa: E402

_APP = QApplication.instance() or QApplication([])
_fail = 0


def check(cond, name, detail=""):
    global _fail
    if not cond:
        _fail += 1
    print("  %s %s%s" % ("✓" if cond else "✗", name, ("  " + str(detail)) if detail else ""))


def actions_of(menu):
    """菜单里所有**动作**（含子菜单里的），返回 {文本: QAction}。"""
    out = {}

    def walk(m):
        for a in m.actions():
            if a.isSeparator():
                continue
            if a.menu() is not None:
                out[a.text() + "▸"] = a
                walk(a.menu())
            else:
                out[a.text()] = a
    walk(menu)
    return out


def main():
    p = MediaPanel()
    p.resize(500, 420)
    p.show()
    _APP.processEvents()

    print("=" * 74)
    print("【A】视图：必须是缩略图网格（不是一行一条）")
    print("=" * 74)
    check(p.lst.viewMode() == QListView.IconMode, "(a1) IconMode 网格",
          p.lst.viewMode())
    check(p.lst.isWrapping() and p.lst.flow() == QListView.LeftToRight,
          "(a2) 自动换行、从左到右")
    check(p.lst.movement() == QListView.Static, "(a3) 图标不能拖动乱挪")
    check(not p.lst.wordWrap(), "(a4) 不自动换行（否则长文件名会把缩略图压扁）")

    # 一件素材
    it = QListWidgetItem("【测试】很长的素材名字用来验证省略号不会换行.mp4")
    it.setData(Qt.UserRole, "c1")
    it.setData(Qt.UserRole + 1, r"C:\tmp\a.mp4")
    p.lst.addItem(it)

    print()
    print("=" * 74)
    print("【B】右键**在素材上**：项相关动作可点")
    print("=" * 74)
    m = p.build_menu(it)
    a = actions_of(m)
    print("     菜单项：%s" % list(a))
    for key in ("在文件管理器里定位", "从这张码表里去掉"):
        check(key in a and a[key].isEnabled(), "(b1) 「%s」可点" % key)
    check("缩略图大小▸" in a, "(b2) 有「缩略图大小」子菜单")
    sub = attrs = [x for x in m.actions() if x.text() == "缩略图大小"][0].menu()
    names = [x.text() for x in sub.actions()]
    check(names == ["小", "中", "大"], "(b3) 三档：小/中/大", names)
    check([x.isChecked() for x in sub.actions()].count(True) == 1,
          "(b4) 当前档位打了勾",
          [x.text() for x in sub.actions() if x.isChecked()])

    print()
    print("=" * 74)
    print("【C】右键**在空白处**：项相关的必须**灰掉**（不是不弹）")
    print("=" * 74)
    m2 = p.build_menu(None)
    a2 = actions_of(m2)
    print("     菜单项：%s" % list(a2))
    check(m2.actions() != [] and len(a2) >= 4, "(c1) 空白处**也要弹菜单**（不是没反应）",
          len(a2))
    for key in ("在文件管理器里定位", "从这张码表里去掉"):
        check(key in a2 and not a2[key].isEnabled(), "(c2) 「%s」灰掉" % key)
    for key in ("加素材…", "指定素材目录…"):
        check(key in a2 and a2[key].isEnabled(), "(c3) 「%s」仍可点（面板级）" % key)
    check("缩略图大小▸" in a2 and a2["缩略图大小▸"].isEnabled(),
          "(c4) 「缩略图大小」在空白处也能用")

    print()
    print("=" * 74)
    print("【D】缩略图三档：切换后网格立刻改尺寸")
    print("=" * 74)
    sizes = {}
    for lvl in (1, 2, 3):
        p._set_icon_size(lvl)
        _APP.processEvents()
        sizes[lvl] = (p.lst.iconSize().width(), p.lst.gridSize().width(),
                      p.lst.gridSize().height())
    print("     小/中/大 = %s" % sizes)
    check(sizes[1][0] < sizes[2][0] < sizes[3][0], "(d1) 图标逐档变大",
          [sizes[i][0] for i in (1, 2, 3)])
    check(sizes[1][1] < sizes[3][1], "(d2) 格子跟着变（不是只有图标变）")
    check(sizes[1][2] < sizes[3][2], "(d3) 格子高度也跟着变（留出文件名那一行）")
    # 高度必须比图标高（否则文字被吃掉）
    p._set_icon_size(2)
    check(p.lst.gridSize().height() > p.lst.iconSize().height() + 8,
          "(d4) 格子比图标高 ≥8px（放得下文件名）",
          (p.lst.gridSize().height(), p.lst.iconSize().height()))

    print()
    print("=" * 74)
    if _fail:
        print("结果：%d 项失败 ✗" % _fail)
        return 1
    print("结果：全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
