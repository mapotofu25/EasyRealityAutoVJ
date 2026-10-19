# -*- coding: utf-8 -*-
"""修搜索同步：不要靠 `hasFocus()` 认是哪个搜索框改的。

原写法在两个框都没焦点时（程序化设置文本、失焦、离屏测试）`lib_kw` 根本不更新
⇒ 搜索失效、另一页也不同步。改成**显式把来源控件传进来**，判定不依赖焦点。
"""
import io
import os
import sys

P = r"P:\AutoVJ\src\ui_main.py"
PAT = []


def rep(old, new, cnt=1):
    PAT.append((old, new, cnt))


rep("""    def _on_search_changed(self):
        \"\"\"任一页的搜索框改动 ⇒ 写进共享状态、同步另一页、刷新两份网格。\"\"\"
        for ed in (getattr(self, "search_box", None), getattr(self, "search_box2", None)):
            if ed is not None and ed.hasFocus():
                self.lib_kw = ed.text() or ""
                break
        self.refresh_grid()""",
    """    def _on_search_changed(self, text=None, src=None):
        \"\"\"任一页的搜索框改动 ⇒ 写进共享状态、同步另一页、刷新两份网格。

        ⚠ 别用 `hasFocus()` 判断"是谁改的"：两个框都没焦点时（程序化 setText、失焦、
          离屏测试）就永远不会更新 `lib_kw` ⇒ 搜索失效（实测踩到）。这里由信号**显式
          把来源控件传进来**（`textChanged` 连接处带 `src=`）。
        \"\"\"
        if src is not None:
            self.lib_kw = text or ""
        else:
            for ed in (getattr(self, "search_box", None),
                       getattr(self, "search_box2", None)):
                if ed is not None and ed.text() != getattr(self, "lib_kw", ""):
                    self.lib_kw = ed.text() or ""
                    break
        self.refresh_grid()""")

rep("""        # ⚠ 两页共用一个搜索词（`lib_kw`）；同步逻辑见 _on_search_changed/_sync_lib_controls
        self.search_box.textChanged.connect(lambda _t: self._on_search_changed())""",
    """        # ⚠ 两页共用一个搜索词（`lib_kw`）；同步逻辑见 _on_search_changed/_sync_lib_controls
        self.search_box.textChanged.connect(
            lambda t, s=None: self._on_search_changed(t, self.search_box))""")

rep("""        self.search_box2.textChanged.connect(lambda _t: self._on_search_changed())""",
    """        self.search_box2.textChanged.connect(
            lambda t, s=None: self._on_search_changed(t, self.search_box2))""")


def main():
    s = io.open(P, encoding="utf-8").read()
    for i, (old, new, cnt) in enumerate(PAT, 1):
        n = s.count(old)
        if n != cnt:
            print("✗ 第 %d 处命中 %d 次（应为 %d）" % (i, n, cnt))
            return 1
    for old, new, cnt in PAT:
        s = s.replace(old, new)
    io.open(P, "w", encoding="utf-8", newline="\n").write(s)
    print("✓ 搜索同步改为显式来源（%d 处）" % len(PAT))
    import py_compile
    py_compile.compile(P, doraise=True)
    print("语法 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
