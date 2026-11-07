# -*- coding: utf-8 -*-
"""素材悬停显示完整名字 + 「自动吸附」默认开（用户 2026-11-03 的两条口头需求）。

用户原话：
  「这个素材鼠标悬停在素材的那个窗口上面时，悬停在素材上面时显示素材的完整名字，
    然后那个自动对齐的话要默认打开。自动吸附。」

覆盖：
  【A】素材项 tooltip = **完整名字**
       · 网格里文件名只有一行且走 `ElideMiddle`（长名字必然截断）⇒ 全名只能靠 tooltip；
       · 丢失素材的 tooltip 必须能看出问题（含「丢失」+ 它尝试挂载的路径 / 「未指定路径」）；
       · 正常素材补时长 + 素材编号；
       · **列表那一行文本不受影响**（仍单行、仍 ElideMiddle、不含 tooltip 的附加行）。
  【B】「自动吸附」默认**开**：`chk_snap.isChecked()` 与 `timeline.snap_enabled()` **两者都**是 True
       —— 后者尤其重要：只勾 UI 不推给时间轴 = "看着勾上了但实际不吸"。
  【C】尊重用户显式关过：`cfg["snap"] = False` ⇒ 勾选框与时间轴都是 False；
       开关来回拨动时两者始终一致，且状态落进 `cfg["snap"]`。
  【D】关掉后**重启仍是关**（QA 复验暴露的真缺陷）：`config.DEFAULTS` 必须登记 `"snap"`，
       否则 `Config._merge()` 会把 config.json 里的 `"snap": false` 静默丢掉 ⇒
       「关了重启又变开、用户永远关不掉」。（内存态测不出这条，必须重建 Config 读盘。）

跑：venv\\Scripts\\python.exe -u tests\\test_media_tooltip_and_snap.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 隔离配置 / 自动保存，别碰用户真实工作区（否则测试会读到用户自己的 snap 偏好）
_TMP = tempfile.mkdtemp(prefix="cuetip_")
os.environ["LOCALAPPDATA"] = _TMP

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

from PySide6.QtCore import Qt                                     # noqa: E402
from PySide6.QtWidgets import QApplication, QListView              # noqa: E402

from ercue import config as app_config                            # noqa: E402
from ercue.model.cuesheet import CueSheet, MediaRef               # noqa: E402
from ercue.ui.panels import MediaPanel                            # noqa: E402
from ercue.ui.window import MainWindow                            # noqa: E402

_APP = QApplication.instance() or QApplication([])
_fail = 0

# 一个"很长很长的素材名"：正是用户看不到全名的场景（网格里会变成「开头…结尾」）
_LONG_NAME = "【测试】这是一个特别特别长的素材名字用来验证悬停能看到全部字符_final_v3.mp4"


def check(cond, name, detail=""):
    global _fail
    if not cond:
        _fail += 1
    print("  %s %s%s" % ("✓" if cond else "✗", name, ("  " + str(detail)) if detail else ""))


def _new_window():
    """建主窗口。⚠ `_interactive` **必须**立刻置 False（默认 True 时任何对话框都会卡死无头测试）。"""
    w = MainWindow()
    w._interactive = False
    w.move(-20000, -20000)
    w.show()
    _APP.processEvents()
    return w


def _sheet_two(a_path, b_path):
    """一张码表：m1 有（或没有）路径、m2 指向一个不存在的文件。"""
    sheet = CueSheet(title="t")
    sheet.media.append(MediaRef(clip_id="c1", name=_LONG_NAME, duration_sec=12.35))
    sheet.media.append(MediaRef(clip_id="c2", name="丢了的美.mp4", duration_sec=0.0))
    return sheet


# ==========================================================================
# 【A】素材项 tooltip
# ==========================================================================
def test_media_tooltip(w):
    print("=" * 76)
    print("【A】素材缩略图悬停 ⇒ 完整名字")
    print("=" * 76)

    good = os.path.abspath(__file__)                       # 一个真实存在的文件
    gone = os.path.join(_TMP, "不存在的目录", "丢了的美.mp4")
    sheet = _sheet_two(good, gone)
    paths = {"c1": good, "c2": gone}

    p = w.media_panel
    p.fill(sheet, paths)                                   # cache_dir 不传 ⇒ 不起缩略图线程
    _APP.processEvents()

    check(p.lst.count() == 2, "(a1) 列表填了 2 项", p.lst.count())
    it_ok, it_bad = p.lst.item(0), p.lst.item(1)

    tip_ok = it_ok.toolTip()
    print("     正常项 tooltip = %r" % tip_ok)
    check(_LONG_NAME in tip_ok, "(a2) 正常项 tooltip 含**完整**素材名（含扩展名）")
    check(it_ok.text() != tip_ok and "\n" not in it_ok.text(),
          "(a3) 列表那行仍是单行短名（与 tooltip 不同）",
          repr(it_ok.text()))
    check("<" not in tip_ok and "&" not in tip_ok,
          "(a4) tooltip 是**纯文本**（没有富文本标签）")
    check("12.35" in tip_ok, "(a5) 时长 %s 秒出现在 tooltip 里" % "12.35", tip_ok.splitlines()[-2:])
    check("c1" in tip_ok, "(a6) 素材编号（clip_id）也在 tooltip 里")

    tip_bad = it_bad.toolTip()
    print("     丢失项 tooltip = %r" % tip_bad)
    check("丢失" in tip_bad, "(a7) 丢失项 tooltip 含「丢失」")
    check("丢了的美.mp4" in tip_bad, "(a8) 丢失项 tooltip 仍含完整文件名")
    check(gone in tip_bad, "(a9) 丢失项 tooltip 交代**尝试的路径**", gone)
    check("（丢失）" in it_bad.text(), "(a10) 列表文本仍标「（丢失）」（显示逻辑未改）",
          repr(it_bad.text()))

    # 路径为空的丢失项：tooltip 必须说「未指定路径」，而不是空白（否则用户以为软件没反应）
    tip_none = MediaPanel._media_tooltip(sheet.media[1], "", False)
    print("     无路径 tooltip = %r" % tip_none)
    check("未指定路径" in tip_none, "(a11) 路径为空时 tooltip 写「未指定路径」")

    # 显示逻辑没被改动（仍是 IconMode 网格 + 中间省略、不换行）
    check(p.lst.viewMode() == QListView.IconMode
          and p.lst.textElideMode() == Qt.ElideMiddle
          and not p.lst.wordWrap(),
          "(a12) 列表显示逻辑未变（IconMode / ElideMiddle / 不换行）")


# ==========================================================================
# 【B】自动吸附默认开
# ==========================================================================
def test_snap_default_on():
    print()
    print("=" * 76)
    print("【B】「自动吸附」默认开（且 initial 状态真的推给了时间轴）")
    print("=" * 76)
    cfg = app_config.instance()
    cfg.data.pop("snap", None)                             # 回到"从没设过" ⇒ 走默认值
    w = _new_window()
    try:
        check(w.chk_snap.isChecked() is True, "(b1) 勾选框默认**勾上**", w.chk_snap.isChecked())
        check(w.timeline.snap_enabled() is True,
              "(b2) 时间轴 `snap_enabled()` 也是 True（初值已同步，不是只有 UI 显示勾）",
              w.timeline.snap_enabled())

        # 拨动一下：UI 与时间轴必须始终一致，且状态落进 cfg
        w.chk_snap.setChecked(False)
        _APP.processEvents()
        check(w.timeline.snap_enabled() is False, "(b3) 取消勾选 ⇒ 时间轴关闭吸附")
        check(cfg.data.get("snap") is False, "(b4) 关闭状态写进 cfg[\"snap\"]", cfg.data.get("snap"))
        w.chk_snap.setChecked(True)
        _APP.processEvents()
        check(w.timeline.snap_enabled() is True, "(b5) 重新勾选 ⇒ 时间轴重新吸附")
        check(cfg.data.get("snap") is True, "(b6) 打开状态写进 cfg[\"snap\"]", cfg.data.get("snap"))
    finally:
        w.close()
        w.deleteLater()
        _APP.processEvents()


# ==========================================================================
# 【C】尊重用户显式关过
# ==========================================================================
def test_snap_explicitly_off():
    print()
    print("=" * 76)
    print("【C】配置里显式 `snap=false`（用户自己关过）⇒ 仍然关")
    print("=" * 76)
    cfg = app_config.instance()
    cfg.set("snap", False, save=False)                     # 显式关（不落盘，只改内存态）
    w = _new_window()
    try:
        check(w.chk_snap.isChecked() is False, "(c1) 显式关过 ⇒ 勾选框不勾（不被默认值覆盖）",
              w.chk_snap.isChecked())
        check(w.timeline.snap_enabled() is False, "(c2) 时间轴也未开吸附",
              w.timeline.snap_enabled())
    finally:
        w.close()
        w.deleteLater()
        _APP.processEvents()
    # 直接调回调也要能"推给时间轴 + 存配置"（该槽只干这两件事，**不反向动控件** ——
    # 它就是勾选框自己的 toggled 槽，反向写会绕成环；这一点在这里钉住，防以后被"补"错）
    cfg.data.pop("snap", None)
    w2 = _new_window()
    try:
        w2._on_snap_toggled(False)
        _APP.processEvents()
        check(w2.timeline.snap_enabled() is False,
              "(c3) `_on_snap_toggled(False)` ⇒ 时间轴关吸附", w2.timeline.snap_enabled())
        check(cfg.data.get("snap") is False, "(c4) 同时写进 cfg[\"snap\"]", cfg.data.get("snap"))
        # 恢复：走**真正的用户入口**（勾选框），验证两者重新一致
        w2.chk_snap.setChecked(False)
        _APP.processEvents()
        w2.chk_snap.setChecked(True)
        _APP.processEvents()
        check(w2.chk_snap.isChecked() is True and w2.timeline.snap_enabled() is True,
              "(c5) 勾选框来回拨动后 ⇒ 控件与时间轴一致打开",
              (w2.chk_snap.isChecked(), w2.timeline.snap_enabled()))
    finally:
        w2.close()
        w2.deleteLater()
        _APP.processEvents()


# ==========================================================================
# 【D】关掉之后**重启**还是关（QA 2026-11-03 复验暴露的真缺陷）
# ==========================================================================
def test_snap_persist_across_restart():
    """用户关掉「自动吸附」⇒ 落盘 ⇒ **重启**（新建 Config 读盘 + 换掉单例）仍是关。

    ★ 为什么单列一组：`DEFAULTS` 里原本**没有** `"snap"` 键，而 `Config._merge()`
      只合并 DEFAULTS 中存在的键 ⇒ 用户写进 config.json 的 `"snap": false` 在下次
      `load()` 时被**静默丢掉**，`cfg.get("snap", True)` 又落回默认 True。
      症状：**关了重启又变开，用户永远关不掉**（第 5 轮默认是 False 时，丢键恰好也
      得 False，所以一直看不出来；默认改成 True 后才暴露）。
      要覆盖这条必须"跨进程等价"地重建 Config —— 同一个 `cfg` 对象的**内存态**测不出来。
    """
    print()
    print("=" * 76)
    print("【D】关掉后重启仍是关（DEFAULTS 必须含 snap，否则用户关不掉）")
    print("=" * 76)
    from ercue.config import DEFAULTS, Config

    cfg = app_config.instance()
    check("snap" in DEFAULTS, "(d1) `config.DEFAULTS` 里登记了 \"snap\"（否则 load() 会丢键）",
          sorted(DEFAULTS))

    # 用户关掉 ⇒ 走真实入口（勾选框 → _on_snap_toggled → cfg.set 落盘）
    cfg.data.pop("snap", None)
    w = _new_window()
    try:
        w.chk_snap.setChecked(True)          # 先回到开，再关，确保落盘的是"用户主动关"
        _APP.processEvents()
        w.chk_snap.setChecked(False)
        _APP.processEvents()
    finally:
        w.close()
        w.deleteLater()
        _APP.processEvents()

    # ⚠ 用 `cfg.path`（单例的实际落盘位置），不要用 `config_path()`：
    #   本测试的 `main()` 已把单例指到独立 temp 文件，两者不是同一个路径。
    on_disk = _read_json(cfg.path)
    check(on_disk.get("snap") is False, "(d2) config.json 里落了 \"snap\": false",
          {k: v for k, v in on_disk.items() if k == "snap"})

    # 等价于"重启"：全新 Config 直接读盘
    fresh = Config(cfg.path)
    check(fresh.get("snap", True) is False, "(d3) 重启后 `cfg.get(\"snap\", True)` 仍是 False",
          fresh.get("snap", True))

    # 再进一步：把单例换成"刚读盘的那个"（= 一次真正的启动），窗口必须跟着关
    old = cfg
    app_config._INSTANCE = fresh
    try:
        w2 = _new_window()
        try:
            check(w2.chk_snap.isChecked() is False,
                  "(d4) 重启后勾选框仍是**不勾**（不是被默认值顶回开）",
                  w2.chk_snap.isChecked())
            check(w2.timeline.snap_enabled() is False,
                  "(d5) 重启后时间轴也没开吸附", w2.timeline.snap_enabled())
            w2.chk_snap.setChecked(True)     # 复原，别把状态漏给后面的用例
            _APP.processEvents()
        finally:
            w2.close()
            w2.deleteLater()
            _APP.processEvents()
    finally:
        app_config._INSTANCE = old


def _read_json(path):
    """读一个 JSON 文件；读不到就给空 dict（测试里不该因文件缺失而崩）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                          # noqa: BLE001
        return {}


# ==========================================================================
def main() -> int:
    print("=" * 76)
    print("素材 tooltip（完整名字）+ 自动吸附默认开")
    print("=" * 76)
    # 配置隔离：单例指向临时目录（绝不碰用户真实 %LOCALAPPDATA%\EasyRealityCue）
    app_config._INSTANCE = app_config.Config(
        os.path.join(tempfile.mkdtemp(prefix="cuetip_cfg_"), "config.json"))
    w = _new_window()
    try:
        test_media_tooltip(w)
    finally:
        w.close()
        w.deleteLater()
        _APP.processEvents()
    test_snap_default_on()
    test_snap_explicitly_off()
    test_snap_persist_across_restart()

    print()
    print("=" * 76)
    if _fail:
        print("结果：%d 项失败 ✗" % _fail)
        return 1
    print("结果：全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
