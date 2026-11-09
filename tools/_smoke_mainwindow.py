# -*- coding: utf-8 -*-
"""UI 冒烟：offscreen 真构造一次 MainWindow（抓 `py_compile` 抓不到的错）。

为什么需要它（P0 BUG-2 的教训）：
    2026-11-09 08:20 那会儿 `_build_screen_setup_box` 用了 `QDoubleSpinBox`，但忘了加进
    `PySide6.QtWidgets` 的导入列表 —— 语法完全合法 ⇒ `py_compile` 通过、静态检查通过，
    但 `MainWindow()` 一构造就 `NameError`，软件**根本起不来**。
    这类错只有"真构造一次"才发现。所以：**每次改完 UI 都必须跑这个脚本**。

跑法：
    venv/Scripts/python.exe -u tools/_smoke_mainwindow.py
    （退出码 0 = 全过；1 = 有失败）

它做的事：
    1. offscreen 起 QApplication，配置指到临时文件（不动用户的 config.json）
    2. 构造 MainWindow → 断言关键面板都建出来了
    3. 打开 / 关闭 / 再打开一次「多屏输出」→ 走一遍 `_build_multi_stage`、
       `_rebuild_multi_blocks`、`_auto_size_blocks_split`（BUG-1 这条路径）
    4. 每次 `processEvents()` 之后检查有没有异常被吞进 `ui_stall.log`
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 隔离：配置/日志都写到临时目录，绝不碰用户真实 data
_DATA = tempfile.mkdtemp(prefix="smoke_mw_")
os.environ["LOCALAPPDATA"] = _DATA

import config as CFG                                             # noqa: E402

CFG.CONFIG_FILE = os.path.join(_DATA, "config.json")

from PySide6.QtWidgets import QApplication                        # noqa: E402

app = QApplication([])

import ui_main                                                    # noqa: E402

_results = []


def ck(name, cond, extra=""):
    _results.append(bool(cond))
    print(("  OK   " if cond else "  FAIL ") + name
          + (("  " + str(extra)) if extra and not cond else ""), flush=True)


def pump(n=8):
    for _ in range(n):
        app.processEvents()


def stall_log_path():
    try:
        return os.path.join(str(CFG.app_base_dir()), "ui_stall.log")
    except Exception:                                             # noqa: BLE001
        return os.path.join(_DATA, "ui_stall.log")


def stall_log_text():
    try:
        with open(stall_log_path(), "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def main():
    print("UI 冒烟（offscreen）  %s" % ROOT, flush=True)
    # 清掉上一次的残留日志，只看本次
    try:
        if os.path.exists(stall_log_path()):
            os.remove(stall_log_path())
    except Exception:                                             # noqa: BLE001
        pass

    ck("config 指向临时文件（不碰用户配置）", "smoke_mw_" in CFG.CONFIG_FILE, CFG.CONFIG_FILE)

    # ---- 1) MainWindow 能构造（BUG-2：NameError 会在这里炸） ----
    try:
        w = ui_main.MainWindow()
        ck("MainWindow() 构造成功", True)
    except Exception as e:                                        # noqa: BLE001
        ck("MainWindow() 构造成功", False, "%s: %s" % (type(e).__name__, e))
        import traceback
        traceback.print_exc()
        return 1
    w.resize(1600, 900)
    w.show()
    pump()

    # ---- 2) 关键面板都在 ----
    for name in ("layers_panel", "output_panel", "settings", "library_panel",
                 "grid", "preview_panel", "btn_multi_out", "btn_run"):
        ck("面板/控件已建：%s" % name, getattr(w, name, None) is not None)
    ck("图层栈有行（layers_panel.rows 非空）",
       len(getattr(w.layers_panel, "rows", []) or []) >= 0,
       len(getattr(w.layers_panel, "rows", []) or []))

    # ---- 3) 多屏：开 → 关 → 再开（BUG-1 那条路径） ----
    try:
        w.btn_multi_out.setChecked(True)
        pump(10)
        ck("打开多屏：output_studio 已建", getattr(w, "output_studio", None) is not None)
        ck("打开多屏：块区与两列已建",
           getattr(w, "_multi_blocks", None) is not None
           and getattr(w, "_multi_col_left", None) is not None
           and getattr(w, "_multi_col_right", None) is not None)
        st = getattr(w, "output_studio", None)
        ck("打开多屏：预览工作区已建", st is not None and st.preview is not None)
        # 直接踢一次自动分配（外层吞异常 ⇒ 必须直接调才能验出 BUG-1 这类问题）
        w._auto_size_blocks_split(force=True)
        ck("块区高度自动分配：调用未抛异常", True)
        sa = w._multi_blocks_scroll
        ck("块区高度 > 0", sa.height() > 0, sa.height())
    except Exception as e:                                        # noqa: BLE001
        ck("打开多屏流程无异常", False, "%s: %s" % (type(e).__name__, e))
        import traceback
        traceback.print_exc()

    try:
        w.btn_multi_out.setChecked(False)
        pump(8)
        ck("关闭多屏：回到普通演出台无异常", True)
        ck("关闭多屏：图层栈回到单屏左列",
           w._stage_left_split.indexOf(w.layers_panel) >= 0)
        w.btn_multi_out.setChecked(True)
        pump(10)
        ck("再开多屏：块区重建无异常", bool(w._multi_blocks))
    except Exception as e:                                        # noqa: BLE001
        ck("多屏开→关→再开无异常", False, "%s: %s" % (type(e).__name__, e))
        import traceback
        traceback.print_exc()

    # ---- 4) 有没有被吞进 ui_stall.log 的异常 ----
    pump(6)
    txt = stall_log_text()
    ck("没有新写入 ui_stall.log（无被吞的 UI 异常）", txt.strip() == "",
       txt.strip()[:400])

    w.close()
    pump(4)

    bad = len(_results) - sum(1 for x in _results if x)
    print("\n通过 %d / 失败 %d" % (len(_results) - bad, bad), flush=True)
    print("结论：" + ("✅ UI 冒烟通过" if bad == 0 else "★ UI 冒烟失败"), flush=True)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
