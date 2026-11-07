# -*- coding: utf-8 -*-
"""左侧「① 码表包 / ② 曲目 / ③ 素材 / ④ 片段属性」四块的容器。

★★ 2026-11-03（用户：「cue 的 UI 有没有什么其他的设计方向，主要是我现在想把
   那四个折叠起来内容给它展开」）：
   原来是 `QTabWidget` —— **一次只能看一个**，另外三块等于被折叠在标签后面。
   这里换成 **可折叠竖排**：四块**默认全部展开**，标题条点一下可单独收起，
   分隔条可拖，双击恢复默认比例。

设计约束（都踩过）：
  · 折叠后必须只留**标题条高度**（约 26px）——不能按比例缩，否则标题会被压没；
  · 「曲目」「片段属性」是表单（十几行），窗口矮时必须能**滚动**，不能把别人挤死
    ⇒ 这两块包一层 QScrollArea（`widgetResizable`、横向滚动条关掉）；
  · 列表类（码表包 / 素材）自己带滚动条，**不再套一层**，避免双滚动条；
  · 保留 `QTabWidget` 的常用接口（`indexOf / tabText / currentIndex / setCurrentWidget`），
    主窗口里 `self.tabs.setCurrentWidget(self.props_panel)` 之类**不用改**。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QTabWidget,QFrame, QScrollArea, QSizePolicy, QSplitter,
                               QToolButton, QVBoxLayout, QWidget)

from . import theme

COLLAPSED_H = 26          # 折叠后只剩标题条的高度（px）
HEADER_H = 26
MIN_SECTION_H = 64        # 展开状态下最小高度（再小就只能看到标题和一条缝）
#: Qt 的 `QWIDGETSIZE_MAX`（PySide6 没有导出这个常量，直接写值）
_HUGE = 16777215


class _Header(QToolButton):
    """一段的标题条：左边小三角 + 标题。整条可点。"""

    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.setText(text)
        self.setCheckable(True)
        self.setChecked(True)
        self.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.setArrowType(Qt.DownArrow)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(HEADER_H)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setStyleSheet(
            "QToolButton{text-align:left;padding:2px 6px;border:none;"
            "border-top:1px solid %s;color:%s;font-weight:bold;background:%s;}"
            "QToolButton:hover{background:%s;}"
            % (theme.c("border"), theme.c("fg"), theme.c("panel"), theme.c("panel2")))
        self.toggled.connect(self._sync_arrow)

    def _sync_arrow(self, on):
        self.setArrowType(Qt.DownArrow if on else Qt.RightArrow)


class _Section(QWidget):
    """一段 = 标题条 + 内容。内容可包滚动区。"""

    def __init__(self, title, body, scroll=False, parent=None):
        super().__init__(parent)
        self.title = title
        self._body_raw = body
        # ★★ 必须显式压掉"最小高度"：
        #   面板自己的 `minimumSizeHint` 很大（实测 码表包=229 / 曲目=94 / 素材=216 / 属性=94），
        #   QSplitter 不会把子控件压到 minimum 以下 ⇒ 四段永远按这几个数分配，
        #   设 `_pref` 完全无效（且总高不够时直接挤爆）。
        #   这里把**段**的最小高度压到标题条，内容靠自己的滚动条/裁剪消化。
        self.setMinimumHeight(COLLAPSED_H)
        body.setMinimumHeight(0)
        self.header = _Header(title, self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.header)

        if scroll:
            sc = QScrollArea(self)
            sc.setWidgetResizable(True)
            sc.setFrameShape(QFrame.NoFrame)
            sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            sc.setMinimumHeight(0)
            sc.setWidget(body)
            self.body = sc
        else:
            self.body = body
        lay.addWidget(self.body, 1)

    @property
    def expanded(self) -> bool:
        return self.header.isChecked()

    def set_expanded(self, on: bool):
        self.header.blockSignals(True)
        self.header.setChecked(bool(on))
        self.header.blockSignals(False)
        self.header._sync_arrow(bool(on))
        self.body.setVisible(bool(on))
        # ★★ 必须同时改 maximumHeight：
        #   `QSplitter.setSizes([26,26,26,26])` 在**四个值相等**时会被 Qt 归一化成
        #   "四等分"（实测 604/4 = 151）⇒ 折叠后每段仍是 151px，等于没折叠。
        #   用 max 高度把折叠段钉死，剩下的空间就只能给展开的那几段。
        self.setMaximumHeight(COLLAPSED_H if not on else _HUGE)

    def widget(self):
        """真正的面板对象（滚动区里那个）。"""
        return self._body_raw


def wrap_scroll(widget):
    """把控件包进无边框滚动区（段内某一格太高时能滚，不挤别的格）。"""
    sc = QScrollArea()
    sc.setWidgetResizable(True)
    sc.setFrameShape(QFrame.NoFrame)
    sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    sc.setMinimumHeight(0)
    sc.setWidget(widget)
    return sc


def make_tabs(pages):
    """把几个面板做成**顶部标签页**（点标签切换，一次只看一个）。

    `pages` = `[(widget, 标题, 是否包滚动区)]`

    ★ 2026-11-03 02:08（用户）：「把码表包和曲目还是改成一开始那种，点击上方栏目然后
      切换的样子」—— 段内上下并排改成页签。段本身（折叠条）不变，因为 ③素材 / ④属性
      还在用同一套。
    ★ 与 `make_vstack` 的区别只在"同时能看到几个"：这里是 1 个，那里是全部。
    """
    tw = QTabWidget()
    for w, title, scroll in pages:
        page = wrap_scroll(w) if scroll else w
        tw.addTab(page, title)
    tw.setMinimumHeight(0)
    return tw


def make_vstack(widgets, sizes=None):
    """把多个面板**上下**摞成一个段的内容（中间可拖）。

    ★ 用 `QSplitter` 而不是 `QVBoxLayout`：用户想调"码表包列表"和"曲目信息"
      各占多高是常态，拖动比写死比例更实用。
    ★ `setChildrenCollapsible(False)`：避免手一抖把某格拖成 0 高（看着像面板丢了）。
    """
    sp = QSplitter(Qt.Vertical)
    for w in widgets:
        sp.addWidget(w)
    sp.setChildrenCollapsible(False)
    sp.setMinimumHeight(0)
    if sizes:
        sp.setSizes([int(x) for x in sizes])
    return sp


class SectionStack(QWidget):
    """四段竖排容器。见模块开头。"""

    sectionToggled = Signal(int, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._secs: list[_Section] = []
        self._pref: list[float] = []
        self._syncing = False
        self._want: list[int] = []
        self._verify_scheduled = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.split = QSplitter(Qt.Vertical, self)
        self.split.setChildrenCollapsible(False)
        self.split.setHandleWidth(5)
        self.split.splitterMoved.connect(self._remember_sizes)
        lay.addWidget(self.split, 1)

    # ---------------------------------------------------------------- 构建
    def add_section(self, widget, title, scroll=False, default_h=120):
        sec = _Section(title, widget, scroll=scroll, parent=self.split)
        sec.header.toggled.connect(
            lambda on, i=len(self._secs): self._on_toggle(i, on))
        self._secs.append(sec)
        self._pref.append(float(default_h))
        self.split.addWidget(sec)
        return sec

    def set_default_sizes(self, heights):
        for i, h in enumerate(heights[:len(self._pref)]):
            self._pref[i] = float(h)
        # 构造期 splitter 还没有真实高度（sizes() 全是默认值）⇒ 等第一次布局完再做
        self._rebalance_soon()
        self._rebalance()
        for i, h in enumerate(heights[:len(self._secs)]):
            sec = self._secs[i]
            # 默认全展开
            if not sec.expanded:
                sec.set_expanded(True)

    def _remember_sizes(self, *_a):
        # ⚠ 必须挡住自己触发的信号：`setSizes()` 也会让 Qt 回一次 `splitterMoved`
        #   （实测：折叠后展开时 Qt 先把 510px 四等分成 128/127/128/127 再发信号），
        #   不挡就会把 `_pref` 写成"四等分" ⇒ 之后再也回不到按权重分配。
        if getattr(self, "_syncing", False):
            return
        for i, s in enumerate(self.split.sizes()):
            if i < len(self._secs) and self._secs[i].expanded and s > COLLAPSED_H:
                self._pref[i] = float(s)

    def _rebalance(self):
        """按 `_pref` 重新分配：折叠段固定 `COLLAPSED_H`，其余按权重分。"""
        n = len(self._secs)
        if not n:
            return
        # ★★ 可用高度取**容器**的，不能取 splitter 自己的：
        #   四段全折叠时 splitter 会被压到最小（实测 height=119，而容器还是 525），
        #   用它算 ⇒ 展开后每段只分到 20~40px，全被 MIN_SECTION_H 夹成 64。
        #   `QSplitter` 的总高 = Σsizes + handleWidth×(n-1)，故减掉手柄宽度。
        hw = self.split.handleWidth() * max(0, len(self._secs) - 1)
        avail = max(self.height() - hw, sum(self.split.sizes()), 0)
        if avail <= 0:
            avail = 200
        fixed = COLLAPSED_H * sum(1 for s in self._secs if not s.expanded)
        flex = [i for i, s in enumerate(self._secs) if s.expanded]
        rest = max(0, avail - fixed)
        tot = sum(self._pref[i] for i in flex) or 1.0
        sizes = []
        for i, s in enumerate(self._secs):
            if not s.expanded:
                sizes.append(COLLAPSED_H)
            else:
                sizes.append(max(MIN_SECTION_H, int(rest * self._pref[i] / tot)))
        # 让和等于 avail（QSplitter 只有在"和 == 可用空间"时才按像素走）
        diff = avail - sum(sizes)
        if flex:
            sizes[flex[-1]] = max(MIN_SECTION_H, sizes[flex[-1]] + diff)
        self._want = list(sizes)
        self._apply(sizes)
        # ★ 再核一次：折叠→展开时 Qt 会在**紧随的布局**里把多出来的空间四等分
        #   （实测 510px → 128/127/128/127），把我们刚设好的尺寸盖掉。
        #   30ms 后再核对一次，不一致就重设（只补一次，不会打架）。
        if not self._verify_scheduled:
            self._verify_scheduled = True
            QTimer.singleShot(30, self._verify)

    def _apply(self, sizes):
        self._syncing = True
        try:
            self.split.setSizes(list(sizes))
        finally:
            self._syncing = False

    def _verify(self):
        self._verify_scheduled = False
        want = getattr(self, "_want", None)
        if not want:
            return
        cur = self.split.sizes()
        if len(cur) == len(want) and max(abs(a - b) for a, b in zip(cur, want)) > 2:
            self._apply(want)

    # ---------------------------------------------------------------- 交互
    def _rebalance_soon(self):
        """下一轮事件循环再算一次。

        ★★ 必须延迟：`setMaximumHeight(26 → 无上限)` 会让 Qt 在**下一次布局**里
          把多出来的空间**四等分**（实测折叠后展开 = 128/127/128/127），
          而这次布局发生在我们的 `setSizes` **之后** ⇒ 同一帧里设的尺寸被它盖掉。
        """
        QTimer.singleShot(0, self._rebalance)

    def _on_toggle(self, i, on):
        sec = self._secs[i]
        sec.set_expanded(on)
        self._rebalance()
        self._rebalance_soon()
        self.sectionToggled.emit(i, bool(on))

    def expand_all(self):
        for s in self._secs:
            s.set_expanded(True)
        self._rebalance_soon()

    def collapse_all(self):
        for s in self._secs:
            s.set_expanded(False)
        self._rebalance_soon()

    def reset_sizes(self):
        self._pref = [120.0] * len(self._secs)
        self.expand_all()

    # ------------------------------------------------- QTabWidget 兼容接口
    def count(self) -> int:
        return len(self._secs)

    def indexOf(self, w) -> int:
        """这个控件属于第几段（-1 = 不属于）。

        ★ 2026-11-03：一个段里可以塞**多个**面板（①② 合并成一段后，
          `pkg_panel` / `track_panel` 都是第 0 段的内容）⇒ 除了"就是这一段"，
          还要认"是这一段的后代"。否则 `indexOf(pkg_panel)` 返回 -1，
          `setCurrentWidget(pkg_panel)` 会静默什么都不做。
        """
        for i, s in enumerate(self._secs):
            body = s.widget()
            if body is w:
                return i
            if body is not None and w is not None and body.isAncestorOf(w):
                return i
        return -1

    def set_header_tooltip(self, i, text):
        """把说明文字挂到第 i 段的标题条上（收起正文后仍可查）。"""
        if 0 <= i < len(self._secs) and text:
            self._secs[i].header.setToolTip(str(text))

    def tabText(self, i) -> str:
        if 0 <= i < len(self._secs):
            return self._secs[i].title
        return ""

    def widget(self, i):
        return self._secs[i].widget() if 0 <= i < len(self._secs) else None

    def currentIndex(self) -> int:
        for i, s in enumerate(self._secs):
            if s.expanded:
                return i
        return 0

    def setCurrentIndex(self, i):
        if 0 <= i < len(self._secs):
            self._secs[i].set_expanded(True)
            self._rebalance()

    def setCurrentWidget(self, w):
        """兼容 `QTabWidget.setCurrentWidget`：展开那块，并把段内的**标签页**也切过去。

        ⚠ 段里可能套了一层 QTabWidget（①② 就是），只展开段不切页签 ⇒ 用户看到的
          还是「曲目」，点「跳到码表包」等于没反应（2026-11-03 加）。
        """
        for tab in self.findChildren(QTabWidget):
            for k in range(tab.count()):
                page = tab.widget(k)
                if page is w or (page is not None and w is not None
                                 and page.isAncestorOf(w)):
                    tab.setCurrentIndex(k)
                    break
        i = self.indexOf(w)
        if i < 0:
            return
        if not self._secs[i].expanded:
            self._secs[i].set_expanded(True)
            self._rebalance()

    # 兼容 `tabs.tabText(...)` 之外偶发的 `tabs.<QTabWidget 方法>` 调用不必实现；
    # 真要加新用法请先看本文件开头"为什么不用 QTabWidget"。
