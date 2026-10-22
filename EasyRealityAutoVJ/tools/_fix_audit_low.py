# -*- coding: utf-8 -*-
"""092930 清理收尾：残留注释、"latest_frame 故意不加锁"的说明、删死模块。"""
import io
import os
import sys

ROOT = r"P:\AutoVJ"
PAT = []


def rep(rel, old, new, cnt=1):
    PAT.append((rel, old, new, cnt))


# 1) 残留的"三界面"
rep("src/ui_main.py",
    "        # ---- 主区（三界面容器，见 _build_pages）----",
    "        # ---- 主区（两界面容器，见 _build_pages）----")

# 2) latest_frame 说明：为什么它**故意**不加锁
rep("src/engine.py",
    "    def latest_frame(self):",
    """    # ⚠⚠ 这里**故意不加 @_locked**：它是 GUI 每帧都要调的取帧入口，
    #   而引擎线程在整帧渲染期间一直握着 _tick_lock ⇒ 加锁等于让 GUI 每帧都
    #   等在锁上（最坏 2 秒一次），屏幕直接卡成幻灯片。
    #   安全性由"只读一个引用"保证：`self._latest_frame = img` 是**原子赋值**，
    #   GUI 拿到的是上一帧的完整对象（引擎之后只会把它换掉、绝不原地改它）。
    #   —— 这是全项目唯一一个允许无锁的跨线程读，别"顺手修"成带锁的。
    def latest_frame(self):""")

# 3) 删死模块 section_analyze.py（无任何运行时导入，只在 build_exe 的 hidden-import 里）
rep("tools/build_exe.py",
    '    "--hidden-import", "section_analyze",\n', "")


def main():
    for rel, old, new, cnt in PAT:
        p = os.path.join(ROOT, rel)
        s = io.open(p, encoding="utf-8").read()
        if s.count(old) != cnt:
            print("✗ %s 命中 %d 次（应为 %d）" % (rel, s.count(old), cnt))
            return 1
        io.open(p, "w", encoding="utf-8", newline="\n").write(s.replace(old, new))
        print("✓", rel)
    # 删死模块
    p = os.path.join(ROOT, "src", "section_analyze.py")
    if os.path.isfile(p):
        io.open(os.path.join(ROOT, "tools", "_tmp_del.txt"), "w").write("x")
        os.remove(p)
        os.remove(os.path.join(ROOT, "tools", "_tmp_del.txt"))
        print("✓ 已删除死模块 src/section_analyze.py（无任何导入方）")
    import py_compile
    py_compile.compile(os.path.join(ROOT, "src", "engine.py"), doraise=True)
    py_compile.compile(os.path.join(ROOT, "src", "ui_main.py"), doraise=True)
    print("语法 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
