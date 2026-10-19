# -*- coding: utf-8 -*-
"""实测：真实环境下"新建码表"链路（真窗口 + 程序化触发 + 拦截弹窗函数）。

验证三段：
  ① `_interactive` 值（守卫是否会拦）
  ② 「码表 ▾」菜单里到底有没有「新建…」这个 action（以及它连的是谁）
  ③ 调 `_new_sheet_dialog()` ⇒ 是否走到 `dialogs.ask_new_sheet`（把弹窗替换成 spy，避免阻塞）
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from PySide6.QtCore import QTimer                       # noqa: E402
from PySide6.QtWidgets import QApplication, QMenu       # noqa: E402


def main():
    app = QApplication(sys.argv)
    from ercue.ui.window import MainWindow
    w = MainWindow()
    w.show()

    print("① _interactive =", w._interactive)
    print("   QApplication.instance() is not None =", QApplication.instance() is not None)

    print("② 所有菜单及其 action：")
    for m in w.findChildren(QMenu):
        acts = [a.text() for a in m.actions() if a.text()]
        if acts:
            print("   -", acts)

    def do_click():
        print("③ 触发 _new_sheet_dialog …")
        from ercue.ui import dialogs
        orig = dialogs.ask_new_sheet

        def spy(*a, **k):
            print("   !! ask_new_sheet 被调用 ⇒ 会弹窗 ✓  默认名参数:", a[1] if len(a) > 1 else k)
            return ("测试码表", 1920, 1080)          # 不真弹窗，避免阻塞

        dialogs.ask_new_sheet = spy
        try:
            w._new_sheet_dialog()
        finally:
            dialogs.ask_new_sheet = orig
        print("   完成，当前会话数 =", len(w.sessions))

    QTimer.singleShot(800, do_click)
    QTimer.singleShot(2500, app.quit)
    app.exec()
    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
