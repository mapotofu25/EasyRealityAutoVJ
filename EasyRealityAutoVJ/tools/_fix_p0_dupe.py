# -*- coding: utf-8 -*-
"""P0 修复：重复导入会删掉素材库里的源文件。

审计结论（audit-ui）：
  · `_ask_duplicate_import` 默认选中「替换」，且**没接 `dlg.exec()` 返回值**
    ⇒ Esc / 右上角 X 关闭也会落到 `return "replace"`（函数自己的 docstring 写的是"关掉=不添加"）。
  · 走 replace 时 `_purge_for_replace(p)` 会 `os.remove` 掉素材库里的文件，
    紧接着 ImportWorker 去 copy 这个**已被删掉的源文件** ⇒ 条目消失 + 文件没了 + 导入失败。
  · 触发条件很日常：用户选/拖的重复文件本身就在「素材库」目录里（copy 模式的默认布局）。

修法（最小、可回滚）：
  1. 接住返回值：非 Accepted 一律 "skip"；默认改「不添加」；补「取消」按钮。
  2. **任何情况下都不删文件**（`delete_file` 默认 False）。
  3. 「替换」对"已在库内的同一个文件"= 什么都不做（保留原条目、不重导），
     只对真正的新文件继续导入。不会再有"先删后拷"。
"""

import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "src", "ui_main.py")


def main():
    s = io.open(UI, encoding="utf-8").read()

    # ---- 1) 对话框：接返回值 / 默认不添加 / 加取消 ----
    old = """        rb_replace.setChecked(True)
        for rb in (rb_replace, rb_skip, rb_add):
            lay.addWidget(rb)
        btns = QHBoxLayout()
        ok = QPushButton("确定")
        ok.clicked.connect(dlg.accept)
        btns.addStretch(1)
        btns.addWidget(ok)
        lay.addLayout(btns)
        dlg.exec()
        if rb_skip.isChecked():
            return "skip"
        if rb_add.isChecked():
            return "add"
        return "replace\""""
    new = """        # ★★ P0 审计修复：默认必须是「不添加」——原来默认「替换」，而 Esc/X 关闭
        #   既没接返回值、又落到最后一行 `return "replace"`，会去删素材库里的源文件。
        rb_skip.setChecked(True)
        for rb in (rb_replace, rb_skip, rb_add):
            lay.addWidget(rb)
        btns = QHBoxLayout()
        cancel = QPushButton("取消")
        cancel.clicked.connect(dlg.reject)
        ok = QPushButton("确定")
        ok.clicked.connect(dlg.accept)
        ok.setDefault(True)
        btns.addStretch(1)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        lay.addLayout(btns)
        if dlg.exec() != QDialog.Accepted:      # Esc / X / 取消 ⇒ 不添加
            return "skip"
        if rb_add.isChecked():
            return "add"
        if rb_replace.isChecked():
            return "replace"
        return "skip\""""
    assert old in s, "① 对话框锚点没找到"
    s = s.replace(old, new, 1)

    # ---- 2) _purge_for_replace：默认不删文件 ----
    old2 = """    def _purge_for_replace(self, old_path):
        \"\"\"替换素材：移除旧条目（图层引用一并清理）；旧文件在素材库目录内则删除，
        这样新文件落盘时复用原文件名，按路径记忆的标签/角色/摆位自动延续。\"\"\""""
    new2 = """    def _purge_for_replace(self, old_path, delete_file=False):
        \"\"\"替换素材：移除旧条目（图层引用一并清理）。

        ⚠ 2026-10-18 审计修复：**默认不删文件**。旧实现在这里 `os.remove`，
          而调用方紧接着让 ImportWorker 去 copy 同一个路径 —— 源文件已被删，
          ⇒ 条目消失 + 文件丢失 + 导入失败（数据丢失级 P0）。
          真要删文件必须显式传 `delete_file=True`，目前没有任何调用方这么做。\"\"\""""
    assert old2 in s, "② purge 锚点没找到"
    s = s.replace(old2, new2, 1)

    old3 = """        lib = os.path.abspath(self.library_dir())
        ap = os.path.abspath(old_path)
        if ap.startswith(lib + os.sep) and os.path.exists(ap):
            try:
                os.remove(ap)
            except OSError:
                pass"""
    new3 = """        if not delete_file:                     # ★ 默认不删（见 docstring 说明）
            return
        lib = os.path.abspath(self.library_dir())
        ap = os.path.abspath(old_path)
        if ap.startswith(lib + os.sep) and os.path.exists(ap):
            try:
                os.remove(ap)
            except OSError:
                pass"""
    assert old3 in s, "③ 删文件锚点没找到"
    s = s.replace(old3, new3, 1)

    # ---- 3) 调用方：替换 = 保留原条目，不重导同一个文件 ----
    old4 = """            elif act == "replace":
                for p in dupes:
                    self._purge_for_replace(p)"""
    new4 = """            elif act == "replace":
                # ★ P0 修复：dupes 都是"路径已在库里"的文件 ⇒ 它自己就是库里的那份，
                #   "替换"在这里没有意义（不可能用一个文件替换它自己）。正确做法是：
                #   保留原条目、把它从本次导入里剔除，绝不删文件、也绝不重拷。
                _lib = os.path.abspath(self.library_dir())
                _keep = []
                for p in paths:
                    if p in self.library and os.path.abspath(p).startswith(_lib + os.sep):
                        continue                     # 库内的同一个文件 ⇒ 跳过（原样保留）
                    _keep.append(p)
                paths = _keep
                if not paths:
                    return"""
    assert old4 in s, "④ 调用方锚点没找到"
    s = s.replace(old4, new4, 1)

    io.open(UI, "w", encoding="utf-8").write(s)
    print("P0 已修：对话框返回值 / 默认不添加 / 取消按钮 / 不删文件 / 不重导同一文件")


if __name__ == "__main__":
    sys.exit(main())
