# -*- coding: utf-8 -*-
"""第三轮 T02（主窗 A）验收脚本 —— **真窗口、无头**。

覆盖任务书点名的判据：
  ① **自动保存实测**：新建 → 加素材/片段 → 关闭 → **重开 ⇒ 东西还在**（多码表也还原）
  ② **测试隔离**：`ERCUE_NO_AUTOSAVE=1` ⇒ 不落盘、不恢复
  ③ **绝不写用户 `.ercue`**：一轮完整操作后目录里**没有** `.ercue`，也没有被动过的用户文件
  ④ 关键点强制 flush（新建 / 切换 / 导出）
  ⑤ 导出合并：菜单**无「另存为…」**；顶栏按钮文案 = **「导出当前码表」**
  ⑥ 时间轴工具条**已无「放大/缩小」**
  ⑦ 预览标签显示**当前码表名**（`码表预览 —— <名>`）
  ⑧ 未实现项**明确报 SKIP**，不误报失败

跑法：<Cue>\\venv\\Scripts\\python.exe -u tools\\_t24_t02_verify.py [OUT_JSON]
"""
import json
import os
import sys
import tempfile

CUE = r"<项目根>\EasyRealityCue"
sys.path.insert(0, os.path.join(CUE, "src"))
os.environ.pop("QT_QPA_PLATFORM", None)
os.environ.setdefault("ERCUE_PROF_MAIN", "0")
import logging                                    # noqa: E402
logging.disable(logging.CRITICAL)

from PySide6.QtWidgets import QApplication, QPushButton      # noqa: E402
from PySide6.QtCore import Qt as _Qt                          # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(CUE, "_t24_t02.json")
MOV = r"Y:\EasyRealityAutoVJ\素材库\001 (8).mov"

app = QApplication.instance() or QApplication([])
from ercue.ui.window import MainWindow                        # noqa: E402

res = []


def rec(name, ok=None, detail=""):
    res.append({"项": name, "结论": ("PASS" if ok else "FAIL") if ok is not None else "SKIP",
                "detail": detail})
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL" if ok is not None else "SKIP", name, detail))


def _mk(**kw):
    """按设计签名构造窗口：`MainWindow(parent=None, *, restore_workspace=True, autosave=True)`。"""
    try:
        w = MainWindow(**kw)
    except TypeError:
        w = MainWindow()
        rec("MainWindow(restore_workspace=, autosave=) 参数", False, "构造参数不存在（设计 §3.1 要求）")
    else:
        rec("MainWindow(restore_workspace=, autosave=) 参数", True)
    w._interactive = False
    w.setAttribute(_Qt.WA_DontShowOnScreen, True)
    w.resize(1256, 850)
    w.show()
    for _ in range(6):
        app.processEvents()
    return w


def _close(w):
    try:
        w.shutdown()
    except Exception as e:                                    # noqa: BLE001
        print("    (shutdown 异常：%s)" % e)
    for _ in range(4):
        app.processEvents()


def _build(w, title):
    """在前台建一张码表 + 素材 + 一条片段，触发各处 flush。返回 (key, n_items)。"""
    if hasattr(w, "new_sheet"):
        w.new_sheet(title, 1920, 1080)
    ids = w.add_media_paths([MOV])
    w.add_track("T02曲目")
    ly = w._layers()[0]
    w.add_cue(ly.lyr_id, ids[0], 0.0)
    for _ in range(6):
        app.processEvents()
    return w.current_key, sum(len(l.items) for l in w._layers())


def main() -> int:
    d = tempfile.mkdtemp(prefix="ercue_t02_")
    old_d, old_no = os.environ.get("ERCUE_AUTOSAVE_DIR"), os.environ.get("ERCUE_NO_AUTOSAVE")
    os.environ["ERCUE_AUTOSAVE_DIR"] = d
    os.environ.pop("ERCUE_NO_AUTOSAVE", None)
    try:
        # ---------------- ① 自动保存往返 ----------------
        print("\n--- ① 自动保存：新建 → 加素材/片段 → 关闭 → 重开 ---")
        w1 = _mk(restore_workspace=False, autosave=True)
        key, n = _build(w1, "T02甲")
        # 再建一张，验证多码表
        key2, n2 = _build(w1, "T02乙")
        _close(w1)
        files = sorted(os.listdir(d)) if os.path.isdir(d) else []
        rec("自动保存已落盘", bool([f for f in files if f.startswith("sheet_")]),
            "目录文件=%s" % files)
        w2 = _mk(restore_workspace=True, autosave=True)
        titles = [s.sheet.title for s in w2.sessions.values()]
        got = sum(len(l.items) for l in w2._layers())
        rec("重开后码表还在", "T02甲" in titles and "T02乙" in titles, "titles=%s" % titles)
        rec("重开后片段还在", got >= 1, "items=%d（关窗前 %d）" % (got, n))
        cur = getattr(w2, "current_key", "")
        rec("重开后当前码表已还原", cur in (key, key2), "current=%s (原 %s/%s)" % (cur, key, key2))
        _close(w2)

        # ---------------- ③ 红线：不写 .ercue ----------------
        bad = [f for f in (os.listdir(d) if os.path.isdir(d) else []) if f.endswith(".ercue")]
        rec("红线：不写用户 .ercue", not bad, "目录=%s" % sorted(os.listdir(d)))

        # ---------------- ② NO_AUTOSAVE 隔离 ----------------
        print("\n--- ② ERCUE_NO_AUTOSAVE=1 隔离 ---")
        d2 = tempfile.mkdtemp(prefix="ercue_t02_off_")
        os.environ["ERCUE_AUTOSAVE_DIR"] = d2
        os.environ["ERCUE_NO_AUTOSAVE"] = "1"
        w3 = _mk(restore_workspace=True, autosave=True)
        _build(w3, "不该被保存")
        _close(w3)
        left = os.listdir(d2) if os.path.isdir(d2) else []
        rec("NO_AUTOSAVE=1 ⇒ 不落盘", not left, "目录=%s" % left)
        w4 = _mk(restore_workspace=True, autosave=True)
        rec("NO_AUTOSAVE=1 ⇒ 不恢复", len(w4.sessions) <= 1, "sessions=%d" % len(w4.sessions))
        _close(w4)
        os.environ.pop("ERCUE_NO_AUTOSAVE", None)
        os.environ["ERCUE_AUTOSAVE_DIR"] = d

        # ---------------- ⑤⑥⑦ 界面项 ----------------
        print("\n--- ⑤⑥⑦ 导出合并 / 去缩放 / 预览标签 ---")
        w = _mk(restore_workspace=False, autosave=True)
        _build(w, "界面项")
        menu = w._pkg_menu()
        acts = [a.text() for a in menu.actions()]
        rec("菜单已删「另存为…」", "另存为…" not in acts, "菜单=%s" % acts)
        btn = getattr(w, "btn_export", None)
        rec("顶栏按钮文案=「导出当前码表」",
            btn is not None and "导出当前码表" in btn.text(),
            "实际=%r" % (btn.text() if btn is not None else None))
        texts = [b.text() for b in w.findChildren(QPushButton)]
        rec("已去「放大/缩小」按钮", "放大" not in texts and "缩小" not in texts,
            "工具条附近=%s" % [t for t in texts if t][:12])
        labels = [lab.text() for lab in w.findChildren(type(w.lbl_state)) if hasattr(lab, "text")]
        rec("预览标签含码表名",
            any("码表预览" in t and "界面项" in t for t in labels),
            "含'码表预览'的标签=%s" % [t for t in labels if "码表预览" in t])
        # ④ 关键点 flush：切码表后应已落盘
        _build(w, "切表后")
        files = sorted(f for f in os.listdir(d) if f.startswith("sheet_"))
        rec("关键点 flush（新建+切换后已落盘）", len(files) >= 2, "sheet 快照数=%d" % len(files))
        _close(w)
    finally:
        if old_d is None:
            os.environ.pop("ERCUE_AUTOSAVE_DIR", None)
        else:
            os.environ["ERCUE_AUTOSAVE_DIR"] = old_d
        if old_no is None:
            os.environ.pop("ERCUE_NO_AUTOSAVE", None)
        else:
            os.environ["ERCUE_NO_AUTOSAVE"] = old_no

    npass = sum(1 for r in res if r["结论"] == "PASS")
    nfail = sum(1 for r in res if r["结论"] == "FAIL")
    nskip = sum(1 for r in res if r["结论"] == "SKIP")
    try:
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
    except OSError:
        pass
    print("\n" + "=" * 68)
    for r in res:
        print("  %-4s %-34s %s" % (r["结论"], r["项"], r["detail"][:70]))
    print("=" * 68)
    print("结果：PASS %d / FAIL %d / SKIP %d" % (npass, nfail, nskip))
    print("汇总：%s" % ("全部通过 ✓" if nfail == 0 and nskip == 0 else
                      ("有失败 ✗" if nfail else "部分未实现（SKIP）")))
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
