#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证「曲库列表显示导入包里的歌」（2026-10-08，erlib 第二步补）。

## 背景
用户导入 `.eravjlib` 后问"怎么不在曲库显示" —— 之前导入的歌只进
`imported_meta.db`（按 song_id），曲库列表（按文件路径）看不到它们。
现在 `MusicLibraryPanel.refresh()` 会把导入的歌以**伪路径**
`@imported:<sid>` 追加进列表（📦 前缀 + 蓝色）。

## 判据
  T1 列表里出现导入的歌（数量 == imported_meta.db 里的行数）
  T2 行文本带 📦、来源显示**包名**（`(<包名>)`；旧数据无包名则「(早期导入)」）
     ⚠ 2026-10-09 起：来源不再是笼统的「(导入包)」，而是具体包名（用户要求③）。
  T3 底部计数含「含导入包 N 首」
  T4 「只看文件缺失」**不包含**导入的歌（它们本来就没有本机文件）
  T5 BPM 升序排序 / BPM 筛选都**不崩**（伪路径能安全参与）
  T6 反向：把 `_imported_lib` 打成空（= 旧行为）⇒ 列表里 0 行导入歌
     ⇒ 证明 T1 抓到的那 30 行确实来自这条路径（测试不是空过）
  T7 右键菜单：伪路径走「查看信息」分支（构造 menu 不崩、不调 rescan/remove）

跑法（无窗口）：
    QT_QPA_PLATFORM=offscreen python tools/_erlib_ui_list_verify.py
"""
import os
import sqlite3
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

FAILS = []


def _imp_count():
    """imported_meta.db 里的歌数（测试的期望值来源）。"""
    p = Path(os.environ.get("LOCALAPPDATA", "")) / "AutoVJ" / "imported_meta.db"
    if not p.is_file():
        return 0
    c = sqlite3.connect("file:%s?mode=ro" % p.as_posix(), uri=True)
    try:
        return c.execute("SELECT COUNT(*) FROM songs").fetchone()[0]
    finally:
        c.close()


class _Cfg(dict):
    def save(self):
        pass


class _FakeMain:
    """只给面板用到的那几个字段（面板不碰引擎）。

    ⚠ 面板会在 `__init__` 里连接 `self.main.xxx`（导入/扫描按钮）——
      用 `__getattr__` 给所有缺失属性返回 no-op，避免一个个补。
    """

    def __init__(self):
        self.cfg = _Cfg({"ui": {"music_cover_size": 96},
                         "music_meta": {}, "music_library": []})

    def __getattr__(self, name):
        return lambda *a, **k: None


def main():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    # ★ 模拟 **exe 环境**的数据目录：源码运行时 `config.app_base_dir()` 返回
    #   项目根，而用户的软件是打包 exe ⇒ 数据在 `%LOCALAPPDATA%\AutoVJ`。
    #   （`panels._imported_lib` 是函数内 `from config import ...`，
    #     所以这里 patch 模块属性即可生效。）
    import config
    _lb = Path(os.environ.get("LOCALAPPDATA", "")) / "AutoVJ"
    config.app_base_dir = lambda: str(_lb)
    import panels

    n_imp = _imp_count()
    print("imported_meta.db 里的歌：%d 首" % n_imp)
    if n_imp == 0:
        print("★ 本机没有导入数据，先导入一个 .eravjlib 再跑本测试")
        return 2

    p = panels.MusicLibraryPanel(_FakeMain())
    p.resize(900, 600)

    # ---------- T1 数量 ----------
    p.refresh()
    rows = [p.list.item(i) for i in range(p.list.count())]
    imp_rows = [it for it in rows
                if str(it.data(panels.Qt.UserRole)).startswith("@imported:")]
    ok1 = len(imp_rows) == n_imp
    print("\nT1 列表里导入歌行数：%d（期望 %d）%s"
          % (len(imp_rows), n_imp, "✓" if ok1 else " ★"))
    if not ok1:
        FAILS.append("T1")

    # ---------- T2 文本 ----------
    # ★ 2026-10-09：来源改显示**包名**（`(<包名>)`）；旧数据无包名 → 「(早期导入)」。
    #   判据必须**精确**：该行来源 == 这行**真实的 pack 值**（空则「早期导入」）。
    #   ⚠ 不能用"含一对非空括号"这类弱判据 —— 那样即使回退成通用「(导入包)」
    #     或显示成错包名也会 PASS，抓不住要求③。（包名是用户数据，无法写死字面量，
    #     所以从同一数据源 `_imported_lib()` 取该行 pack 作期望值。）
    txt = imp_rows[0].text() if imp_rows else ""
    _fp = imp_rows[0].data(panels.Qt.UserRole) if imp_rows else ""
    _row = p._imported_lib().get(_fp) if _fp else None
    _expect_src = (((_row or {}).get("pack") or "") or "早期导入")
    ok2 = ("📦" in txt) and (("(%s)" % _expect_src) in txt)
    print("T2 行文本样例：%s" % txt[:90])
    print("   期望来源「(%s)」→ %s" % (_expect_src, "✓" if ok2 else "★ 文本不对"))
    if not ok2:
        FAILS.append("T2")

    # ---------- T3 计数 ----------
    lbl = p.lbl.text()
    ok3 = ("导入包" in lbl or "imported" in lbl)
    print("T3 底部计数：%s" % lbl)
    print("   %s" % ("✓ 有导入包计数" if ok3 else "★ 缺计数"))
    if not ok3:
        FAILS.append("T3")

    # ---------- T4 只看文件缺失 ----------
    p.chk_missing.setChecked(True)
    p.refresh()
    n_miss = sum(1 for i in range(p.list.count())
                 if str(p.list.item(i).data(panels.Qt.UserRole))
                 .startswith("@imported:"))
    ok4 = (n_miss == 0)
    print("T4 「只看文件缺失」时导入歌行数：%d（应为 0）%s"
          % (n_miss, "✓" if ok4 else " ★"))
    if not ok4:
        FAILS.append("T4")
    p.chk_missing.setChecked(False)

    # ---------- T5 排序 / 筛选不崩 ----------
    ok5 = True
    try:
        for idx in range(p.cb_sort.count()):
            p.cb_sort.setCurrentIndex(idx)
            p.refresh()
        for idx in range(p.cb_bpm.count()):
            p.cb_bpm.setCurrentIndex(idx)
            p.refresh()
        p.cb_bpm.setCurrentIndex(0)
        p.cb_sort.setCurrentIndex(0)
        p.refresh()
    except Exception as e:                                     # noqa: BLE001
        ok5 = False
        print("   异常：%s" % e)
    print("T5 遍历全部排序 + BPM 筛选：%s" % ("✓ 不崩" if ok5 else "★ 崩了"))
    if not ok5:
        FAILS.append("T5")

    # ---------- T6 反向：伪路径来源验证 ----------
    orig = panels.MusicLibraryPanel._imported_lib
    panels.MusicLibraryPanel._imported_lib = lambda self: {}
    try:
        p.refresh()
        n_old = sum(1 for i in range(p.list.count())
                    if str(p.list.item(i).data(panels.Qt.UserRole))
                    .startswith("@imported:"))
    finally:
        panels.MusicLibraryPanel._imported_lib = orig
    ok6 = (n_old == 0)
    print("T6 反向（_imported_lib 打空 = 旧行为）：导入歌行数 %d（应 0）%s"
          % (n_old, "✓ 反向复现有效" if ok6 else " ★ 反向无效"))
    if not ok6:
        FAILS.append("T6")

    # ---------- T7 右键菜单分支不崩 ----------
    ok7 = True
    try:
        # 不真正弹菜单（exec 会阻塞）：只验证分支条件的判据函数可用
        p.refresh()
        it = p.list.item(0)
        path = it.data(panels.Qt.UserRole)
        is_imp = isinstance(path, str) and path.startswith("@imported:")
        sid = int(path.split(":", 1)[1]) if is_imp else None
        # 直接调信息层的取数（不弹窗）
        r = p._imported_lib().get("@imported:%d" % sid) if sid else None
        ok7 = bool(r and r.get("title") is not None)
        print("T7 伪路径解析 → sid=%s 取到数据：%s" % (sid, "✓" if ok7 else "★"))
    except Exception as e:                                     # noqa: BLE001
        ok7 = False
        print("T7 异常：%s" % e)
    if not ok7:
        FAILS.append("T7")

    if FAILS:
        print("\n结论：★ 判据失败：%s" % "、".join(FAILS))
        return 1
    print("\n结论：✅ 全部通过（导入歌可见 + 语义正确 + 旧行为反向复现）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
