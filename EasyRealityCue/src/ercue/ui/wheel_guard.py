# -*- coding: utf-8 -*-
"""滚轮护栏：**鼠标滚轮不许改控件值**。

用户原话（2026-11-03 03:56）：
「把那个鼠标悬停在那个选项上，滚轮滚动可以调整选项功能去掉呢，这个经常误操作啊。」

## 为什么会误操作
Qt 里 `QComboBox` / `QAbstractSpinBox`（`QSpinBox`/`QDoubleSpinBox`） / `QSlider` 都吃滚轮：
鼠标经过这些控件、且它**恰好有焦点**（点过一次就一直有）时，滚一下滚轮就把值改了。
属性面板里这类控件密密麻麻（起点/时长/入点/出点/变速/大小/位移/旋转/裁切/不透明度…），
想滚页面往下看，手一抖就把某个值改掉 —— 而且多半当时看不出来。

## 做法
截到 `QEvent.Wheel` 且目标是这三类控件时：
1. 找**最近的 `QAbstractScrollArea`**，把滚轮"翻译"成它的滚动条位移（页面照常滚）；
2. 事件返回 True（控件拿不到，值不变）。

⚠ **不能直接吞掉**：属性面板/对话框都装在 `QScrollArea` 里，吞掉会让页面滚不动 ——
  那比误改值还烦（用户会以为界面卡了）。
⚠ 也**不能**只靠 `setFocusPolicy(StrongFocus)`：用户点过控件后它一直有焦点，
  之后鼠标滑过照样改值（这正是用户抱怨的场景）。

## 范围
只在主窗口构造时装**一次**（应用级过滤器）⇒ 主窗口、属性面板、各种对话框一起受保护。
时间轴的滚轮（横向平移 / Ctrl 缩放 / Alt 行高）**不受影响**：它自己重写了 `wheelEvent`，
且不是这三类控件。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import (
    QAbstractScrollArea, QAbstractSpinBox, QApplication, QComboBox, QSlider, QTabBar,
)

log = logging.getLogger("ercue.ui.wheel_guard")

#: 受保护的控件类型（滚轮不许改它们的值 / 不许切它们的页）
#:
#: ★ 2026-11-03 03:57 用户追加：「所有的空间都加一个这保护，包括曲目里面那个 BPM 什么的」
#:   ⇒ 覆盖**所有"能被滚轮改值"的控件**，不只是属性面板：
#:   · `QComboBox`        素材 / 入场 / 出场 / 混合 等下拉
#:   · `QAbstractSpinBox` 全部数字框（属性面板十几个、曲目 BPM、时长、时间轴行高…）
#:   · `QSlider`          变速 / 大小滑条
#:   · `QTabBar`          滚轮切标签页（和"改值"一样是误操作，一并保护）
#:   ⚠ 这里挂了应用级过滤器 ⇒ 主窗口、属性面板、曲目面板、所有对话框一次覆盖，不用逐处改。
GUARDED_TYPES = (QComboBox, QAbstractSpinBox, QSlider, QTabBar)

#: 应用级过滤器实例（`install()` 幂等；只为了不被 GC 掉才留引用）
_FILTER = None


class WheelGuard(QObject):
    """应用级事件过滤器：把滚轮从"可调值控件"转给最近的滚动区。"""

    def eventFilter(self, obj, ev):                       # noqa: N802（Qt 命名）
        try:
            if ev.type() != QEvent.Wheel:
                return False
            if not isinstance(obj, GUARDED_TYPES):
                return False
            # 例外：下拉框的**弹层正开着**时，滚轮该滚列表（那是用户明确的意图）。
            #   弹层是独立的 view 窗口，鼠标在它上面时 receiver 不是 combo，走不到这里；
            #   但列表刚弹出、鼠标还停在 combo 上的那一瞬间会走到 ⇒ 这里放行。
            if isinstance(obj, QComboBox):
                try:
                    view = obj.view()
                    if view is not None and view.isVisible():
                        return False
                except Exception:                         # noqa: BLE001
                    pass
            dy = 0
            try:
                dy = int(ev.angleDelta().y())
            except Exception:                             # noqa: BLE001
                dy = 0
            self._scroll_ancestor(obj, dy)
            return True                                   # ★ 吞掉：控件绝对拿不到
        except Exception:                                 # noqa: BLE001
            # 护栏自身出问题绝不能影响交互：放行，让 Qt 按原样处理
            log.exception("滚轮护栏异常（本次放行）")
            return False

    @staticmethod
    def _scroll_ancestor(widget, dy):
        """把滚轮位移交给最近的滚动区（按像素滚，与原生滚轮手感接近）。"""
        w = widget.parentWidget() if widget is not None else None
        while w is not None:
            if isinstance(w, QAbstractScrollArea):
                sb = w.verticalScrollBar()
                if sb is not None and sb.maximum() > sb.minimum():
                    # 一格滚轮（±120）≈ 3 行；用滚动区自己的 singleStep 换算，避免"一滚到底"
                    step = max(1, int(sb.singleStep()))
                    sb.setValue(sb.value() - int(dy / 120.0 * step * 3))
                return
            w = w.parentWidget()


def install():
    """装一次应用级护栏（重复调用无副作用）。"""
    global _FILTER
    app = QApplication.instance()
    if app is None:                                       # 无 GUI（纯模型测试）⇒ 什么都不做
        return None
    if _FILTER is not None:
        return _FILTER
    _FILTER = WheelGuard(app)
    app.installEventFilter(_FILTER)
    return _FILTER
