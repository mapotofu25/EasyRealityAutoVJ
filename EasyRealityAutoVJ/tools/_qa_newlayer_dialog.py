# -*- coding: utf-8 -*-
"""QA 需求 B 真实 UI 路径验证（Edward）——不是 grep，是**真的构造 NewLayerDialog**。

1) 构造真实 `NewLayerDialog`，选「自动」→ `result_data()` ⇒ 断言 auto is True。
2) 手动 / Kv 分支同样走 UI。
3) 复现 `MainWindow.layer_add` 里的**真实赋值序列**（name/auto/role/is_kv + 新分支），
   断言最终 `Layer.hide_silent`：自动=True、手动=False、Kv=False。
   （真建 MainWindow 会触发 NDI/ASIO DLL 卸载 → 退出码 127 的**环境原生 abort**，非失败；
    故用 offscreen Qt + 直接构造 dialog，避开 MainWindow。判据只看 OK/FAIL 行。）

用法：venv/Scripts/python.exe -u tools/_qa_newlayer_dialog.py
"""
import os
import sys
import tempfile
import shutil

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

FAILS = []


def check(name, ok, extra=""):
    print(("  OK  " if ok else "  FAIL") + " " + name + (("   " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


def main():
    d = tempfile.mkdtemp(prefix="qa_newlayer_")
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])

        import panels as P
        import engine as E
        import inspect
        import ui_main as UM

        print("=== 需求 B：真实 NewLayerDialog + layer_add 赋值路径 ===")

        # ---- 1) 真实构造对话框，逐类型走 result_data() ----
        dlg = P.NewLayerDialog(None, "图层 1")
        app.processEvents()

        # 默认 = 手动
        n, auto, role, is_kv = dlg.result_data()
        check("默认(手动) result_data: auto=False, is_kv=False",
              auto is False and is_kv is False, "auto=%s is_kv=%s" % (auto, is_kv))

        # 选「自动」
        dlg.rb_auto.setChecked(True)
        app.processEvents()
        n, auto, role, is_kv = dlg.result_data()
        check("选自动 result_data: auto=True, is_kv=False",
              auto is True and is_kv is False, "auto=%s is_kv=%s" % (auto, is_kv))

        # 选「Kv」
        dlg.rb_kv.setChecked(True)
        app.processEvents()
        n, auto, role, is_kv = dlg.result_data()
        check("选Kv result_data: auto=False, is_kv=True",
              auto is False and is_kv is True, "auto=%s is_kv=%s" % (auto, is_kv))
        check("result_data 仍返回 **4 元组**（未破坏签名）", True, "len=4")

        # ---- 2) 真实 layer_add 赋值序列（**从源码抽取**，不手抄）----
        import re as _re
        import textwrap as _tw
        src = inspect.getsource(UM.MainWindow.layer_add)
        src_lines = _tw.dedent(src).splitlines()
        # 找到新分支那两行，抽出来（可能不存在 ⇒ 改动前版本）
        branch_lines = []
        for i, ln in enumerate(src_lines):
            if "if auto and not is_kv:" in ln:
                branch_lines = [ln]
                if i + 1 < len(src_lines):
                    branch_lines.append(src_lines[i + 1])
                break
        has_branch = bool(branch_lines)
        check("layer_add 源码含新分支 `if auto and not is_kv:` + `lay.hide_silent = True`",
              has_branch and "lay.hide_silent = True" in "\n".join(branch_lines),
              "抽取到: %r" % (branch_lines or None))

        branch_src = _tw.dedent("\n".join(branch_lines)) if has_branch else "pass"

        def resolve_via_real_source(auto, is_kv, role="bg"):
            """执行**从真实 layer_add 源码抽取**的那段赋值（非手抄）。"""
            lay = E.Layer("t")
            lay.auto_mode = auto
            lay.is_kv = is_kv
            lay.role = role
            ns = {"lay": lay, "auto": auto, "is_kv": is_kv,
                  "any": any, "getattr": getattr, "self": None}
            exec(branch_src, ns)
            return lay

        # 注意：本组断言在"改动前"版本会**因无分支而 False**（能区分版本）
        check("layer_add 赋值：自动 ⇒ hide_silent is True",
              resolve_via_real_source(True, False).hide_silent is True)
        check("layer_add 赋值：手动 ⇒ hide_silent is False",
              resolve_via_real_source(False, False).hide_silent is False)
        check("layer_add 赋值：Kv(即使 auto) ⇒ 不叠加 hide_silent",
              resolve_via_real_source(True, True).hide_silent is False)

        # ---- 3) 端到端组合：UI 选自动 → result_data() → 赋值 ⇒ hide_silent True ----
        dlg2 = P.NewLayerDialog(None, "图层 2")
        dlg2.rb_auto.setChecked(True)
        app.processEvents()
        n, auto, role, is_kv = dlg2.result_data()
        lay2 = resolve_via_real_source(auto, is_kv, role)
        check("UI(选自动)→result_data→赋值 组合：hide_silent is True",
              lay2.hide_silent is True and lay2.auto_mode is True,
              "hide_silent=%s auto=%s" % (lay2.hide_silent, lay2.auto_mode))

        print("")
        print("结论：" + ("ALL PASS" if not FAILS else "FAIL %d 项：%s" % (len(FAILS), FAILS)))
        return 0 if not FAILS else 1
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
