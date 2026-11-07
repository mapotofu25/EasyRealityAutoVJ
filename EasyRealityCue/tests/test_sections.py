# -*- coding: utf-8 -*-
"""左列三段（① 码表包 / ② 曲目 / ③ 素材）+ 右块底部 ④ 片段属性的展开与折叠。

★ 2026-11-03 01:42 用户改的布局：**整窗两栏** ——
  「码表包 / 曲目 / 素材 共一列占左边整个左边的位置，
    时间轴 / 片段属性 / 预览 占右边一块」。
  所以这里断言的是 **3 + 1** 两处 SectionStack，而不是原来的 4 段一摞。

背景（用户 2026-11-03）：原来是 QTabWidget —— 一次只看一块，等于"三块被折叠在标签后面"；
改成可折叠竖排后，要保证：
  · **默认四段全展开**；
  · 折叠后只剩标题条（26px），不是"缩成一半"；
  · 再展开能回到原来的比例（Qt 会在折叠→展开的下一帧把空间**四等分**，
    这是本文件最容易被忽略的一条，实测 510px → 128/127/128/127）；
  · 保留 `indexOf / tabText / setCurrentWidget` 这些原来 QTabWidget 的接口。

跑：venv\\Scripts\\python.exe -u tests\\test_sections.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 隔离配置/自动保存，别碰用户真实工作区
_TMP = tempfile.mkdtemp(prefix="cuesec_")
os.environ["LOCALAPPDATA"] = _TMP

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

from PySide6.QtWidgets import QApplication                        # noqa: E402

from ercue.ui.section_stack import COLLAPSED_H, SectionStack      # noqa: E402
from ercue.ui.window import MainWindow                            # noqa: E402

_APP = QApplication.instance() or QApplication([])
_fail = 0


def check(cond, name, detail=""):
    global _fail
    if not cond:
        _fail += 1
    print("  %s %s%s" % ("✓" if cond else "✗", name, ("  " + str(detail)) if detail else ""))


def pump(ms=250):
    """跑事件循环 `ms` 毫秒（`QTimer.singleShot(30, ...)` 需要**真实时间**才会到点）。"""
    t0 = time.time()
    while (time.time() - t0) * 1000 < ms:
        _APP.processEvents()
        time.sleep(0.005)


def main():
    w = MainWindow()
    w.resize(1500, 950)
    w.move(-20000, -20000)
    w.show()
    pump(600)
    tabs = w.tabs
    sp = tabs.split

    print("=" * 76)
    print("【A】结构与默认状态")
    print("=" * 76)
    # ★ 2026-11-03：①② 合并成一段（用户「把曲目和码表包放在一起去吧」）⇒ 左列只有 2 段
    check(tabs.count() == 2, "(a1) 左列两段", tabs.count())
    titles = [tabs.tabText(i) for i in range(tabs.count())]
    check(titles == ["① 码表包 / ② 曲目", "③ 素材"], "(a2) 左列标题", titles)
    # 合并后两块仍必须找得到自己属于第 0 段（否则 setCurrentWidget 会静默失效）
    check(tabs.indexOf(w.pkg_panel) == 0 and tabs.indexOf(w.track_panel) == 0,
          "(a2c) 码表包 / 曲目 都归第 0 段",
          (tabs.indexOf(w.pkg_panel), tabs.indexOf(w.track_panel)))
    # ★ 2026-11-03 02:08（用户「改成一开始那种，点击上方栏目切换」）：段内必须是**页签**
    from PySide6.QtWidgets import QTabWidget
    _body = tabs._secs[0].widget()
    _tw = _body if isinstance(_body, QTabWidget) else _body.findChild(QTabWidget)
    check(_tw is not None and _tw.count() == 2,
          "(a2d) 第 0 段内是 2 页的标签栏（点击切换）",
          (None if _tw is None else [_tw.tabText(k) for k in range(_tw.count())]))
    check(_tw is not None and "码表包" in _tw.tabText(0) and "曲目" in _tw.tabText(1),
          "(a2e) 页签标题：码表包 / 曲目",
          (None if _tw is None else [_tw.tabText(k) for k in range(_tw.count())]))
    # 切到「曲目」页，再让主窗口"跳到码表包" ⇒ 页签必须自己切回来
    if _tw is not None:
        _tw.setCurrentIndex(1)
        pump(120)
        tabs.setCurrentWidget(w.pkg_panel)
        pump(160)
        check(_tw.currentIndex() == 0,
              "(a2f) setCurrentWidget(pkg_panel) 会把页签切回「码表包」",
              _tw.currentIndex())
    pstack = w.props_stack
    check(pstack.count() == 1 and pstack.tabText(0) == "④ 片段属性",
          "(a2b) 右块底部是 ④ 片段属性（独立一段）",
          (pstack.count(), pstack.tabText(0) if pstack.count() else ""))
    check(all(s.expanded for s in tabs._secs), "(a3) **默认全展开**（含右块 ④）",
          [s.expanded for s in tabs._secs] + [pstack._secs[0].expanded])
    sz = sp.sizes()
    check(all(v >= 60 for v in sz) and sum(sz) > 300,
          "(a4) 每段都有实际高度（不是挤成一条缝）", sz)
    check(sum(sz) <= tabs.height() + 2,
          "(a5) 四段总高不超过容器（不溢出）", (sum(sz), tabs.height()))

    print()
    print("=" * 76)
    print("【B】折叠 / 展开")
    print("=" * 76)
    before = list(sp.sizes())
    tabs.collapse_all()
    pump(200)
    check(all(v <= COLLAPSED_H + 1 for v in sp.sizes()),
          "(b1) 全部折叠 ⇒ 只剩标题条 %dpx" % COLLAPSED_H, sp.sizes())

    tabs.expand_all()
    pump(350)
    after = list(sp.sizes())
    check(all(v >= 60 for v in after), "(b2) 再展开 ⇒ 都恢复", after)
    # 关键：不能被"四等分"（Qt 在折叠→展开的下一帧会这么干）
    spread = max(abs(a - b) for a, b in zip(before, after))
    check(spread <= 40, "(b3) 展开后比例与折叠前接近（不是四等分）",
          "差 %dpx  before=%s after=%s" % (spread, before, after))

    # 单独折一段
    tabs._secs[1].header.setChecked(False)
    pump(300)
    s2 = sp.sizes()
    check(s2[1] <= COLLAPSED_H + 1, "(b4) 单独折叠第 2 段 ⇒ 26px", s2)
    # 左列现在只有三段（④ 在右块顶部）
    rest = [v for i, v in enumerate(s2) if i != 1]
    check(all(v >= 60 for v in rest), "(b5) 其余两段把空间吃掉", s2)
    tabs._secs[1].header.setChecked(True)
    pump(300)
    check(all(v >= 60 for v in sp.sizes()), "(b6) 再单独展开也正常", sp.sizes())

    print()
    print("=" * 76)
    print("【C】QTabWidget 兼容接口（主窗口 / 旧脚本还在用）")
    print("=" * 76)
    check(pstack.indexOf(w.props_panel) == 0, "(c1) props_stack.indexOf(props) == 0",
          pstack.indexOf(w.props_panel))
    check(tabs.indexOf(w.pkg_panel) == 0, "(c2) tabs.indexOf(pkg_panel) == 0")
    check(tabs.indexOf(w.props_panel) == -1, "(c2b) ④ 已不在左列")
    pstack.collapse_all()
    pump(200)
    pstack.setCurrentWidget(w.props_panel)     # 主窗口里就这么调
    pump(300)
    check(pstack._secs[0].expanded, "(c3) setCurrentWidget 会把折叠的那段展开",
          [s.expanded for s in pstack._secs])

    print()
    print("=" * 76)
    print("【C2】顶栏「展开全部 / 折叠全部」要同时管两处")
    print("=" * 76)
    tabs.collapse_all()
    pstack.collapse_all()
    pump(200)
    w._expand_all_sections()
    pump(300)
    check(all(s.expanded for s in tabs._secs) and pstack._secs[0].expanded,
          "(c4) 展开全部 ⇒ 左列两段 + ④ 都展开")
    w._collapse_all_sections()
    pump(300)
    check(not any(s.expanded for s in tabs._secs) and not pstack._secs[0].expanded,
          "(c5) 折叠全部 ⇒ 两处都收起")
    w._expand_all_sections()
    pump(300)

    print()
    print("=" * 76)
    print("【D】说明文字：不占版面，但别丢")
    print("=" * 76)
    # ⚠ 合并后 `pkg_panel` / `track_panel` 同属第 0 段 ⇒ tooltip 是**两段说明拼接**，
    #   所以按面板取"归属段"再查，不能拿 enumerate 下标当段号（会越界）。
    for i, p in enumerate((w.pkg_panel, w.track_panel, w.media_panel)):
        lb = getattr(p, "lbl_how", None)
        if lb is None:
            check(False, "(d1) 第 %d 块找不到 lbl_how" % i)
            continue
        check(not lb.isVisible() and len(lb.text()) > 10,
              "(d1) 第 %d 块说明文字已收起但内容还在（%d 字）" % (i, len(lb.text())))
        gi = tabs.indexOf(p)
        tt = tabs._secs[gi].header.toolTip() if gi >= 0 else ""
        check(len(tt) > 10, "(d2) 第 %d 块所在段（第 %d 段）标题条有 tooltip" % (i, gi))

    print()
    print("=" * 76)
    try:
        w.shutdown()
    except Exception:                                          # noqa: BLE001
        pass
    if _fail:
        print("结果：%d 项失败 ✗" % _fail)
        return 1
    print("结果：全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
