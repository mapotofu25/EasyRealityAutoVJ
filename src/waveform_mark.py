# -*- coding: utf-8 -*-
r"""波形标记控件（Qt，共享给 ERLIB）—— 在波形上标 BPM / 八拍起点 / BPM 分段。

⚠ 本文件是 ERAVJ 源码树里的**共享 UI 模块**（ERLIB 打包时经 `pathex` 复用，
   与 `beatgrid`/`fp`/`music_meta` 同一机制），只在 `AutoVJ/src` 维护一份。

## 职责边界（数据层零 Qt，UI 层零算法）
- 本控件**只负责画 + 交互 + 发信号**，不写库、不落盘、不算 BPM。
- 数据模型来自 `beatgrid.effective_grid` / `waveform` dict / `grid_manual` dict。
- 所有标记坐标一律**秒（float，权威）**；波形只是背景（`x = sec/dur*width`）。
- 吸附到拍/小节：用 `effective_grid` 反算拍网（`anchor + n*beat_len`），吸附结果仍存秒。

## 交互（与架构 §4.2 / PRD 一致）
- 工具栏模式：`select`（默认，拖动/删除标记）、`anchor`（点选打八拍头）、
  `segment`（拖选 BPM 分段区间）、`bpm`（点选推算 BPM）。
- 八拍头：点选新增 / 拖动调整 / 右键删除；多个共存，信号 `anchorsChanged`。
- 分段：拖选区间 → 信号 `segmentSelected(t0, t1)`（由详情页弹 BPM 输入、再回填），
  分段以不同底色带显示，`segmentsChanged` 通知更新。
- 手动 BPM：`bpm` 模式点选 → 信号 `bpmPicked(sec)`。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QBrush
from PySide6.QtWidgets import QWidget

MODE_SELECT = "select"
MODE_ANCHOR = "anchor"
MODE_SEGMENT = "segment"
MODE_BPM = "bpm"

_HIT_PX = 6.0            # 命中标记线的像素阈值


class WaveformMarkWidget(QWidget):
    """波形标记控件。用法见模块 docstring。"""

    anchorsChanged = Signal()
    segmentsChanged = Signal()
    bpmPicked = Signal(float)              # 手动 BPM 点选：该点的秒
    segmentSelected = Signal(float, float)  # 分段拖选：t0, t1（秒）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._wf = None                    # waveform dict {v,cps,dur,amp}
        self._grid = None                  # effective grid dict
        self._anchors = []                 # 秒，升序
        self._segments = []                # [{t0,bpm,beat_len,anchor}]
        self._snap = True
        self._mode = MODE_SELECT
        self._drag = None                  # 拖拽状态
        self._rubber = None                # 分段拖选 (x0, x1)
        self._hover = -1                   # 悬停的锚点下标
        self.setMouseTracking(True)
        self.setMinimumHeight(120)

    # ---------------- 数据注入 ----------------
    def set_waveform(self, wf):
        self._wf = wf if isinstance(wf, dict) else None
        self.update()

    def set_grid(self, grid):
        """注入**有效网格**（beatgrid.effective_grid 的产物）。"""
        self._grid = grid if isinstance(grid, dict) else None
        self.update()

    def set_manual(self, manual):
        m = manual if isinstance(manual, dict) else {}
        self._anchors = sorted({float(a) for a in (m.get("anchors") or [])})
        self._segments = [dict(s) for s in (m.get("segments") or []) if isinstance(s, dict)]
        self.update()

    def set_snap(self, on):
        self._snap = bool(on)

    def set_mode(self, mode):
        if mode in (MODE_SELECT, MODE_ANCHOR, MODE_SEGMENT, MODE_BPM):
            self._mode = mode
            self.setCursor(Qt.CursorShape.CrossCursor if mode != MODE_SELECT
                           else Qt.CursorShape.ArrowCursor)
            self.update()

    def mode(self):
        return self._mode

    # ---------------- 读取标记 ----------------
    def anchors(self):
        return list(self._anchors)

    def segments(self):
        return [dict(s) for s in self._segments]

    def set_anchors(self, anchors):
        self._anchors = sorted({float(a) for a in (anchors or [])})
        self.update()

    def set_segments(self, segments):
        self._segments = [dict(s) for s in (segments or []) if isinstance(s, dict)]
        self.update()

    def add_segment(self, t0, t1, bpm, default_bpm=None):
        """新增/归一化一个分段（详情页在 `segmentSelected` 后回填调用）。

        语义：`[t0, t1)` 覆盖为 `bpm`；重叠旧段按 `t0` 落在区间内的删除；
        分段按 `t0` 升序、首段从 0 开始（缺首段用 `default_bpm`/grid BPM 补）。
        段 schema = `{t0, bpm, beat_len, anchor}`（anchor = 该段第一个八拍头）。
        """
        try:
            t0, t1, bpm = float(t0), float(t1), float(bpm)
        except (TypeError, ValueError):
            return
        if t1 <= t0 or bpm <= 0:
            return
        segs = [s for s in self._segments
                if not (t0 <= float(s.get("t0") or 0.0) < t1)]
        segs.append({"t0": t0, "bpm": bpm})
        segs.sort(key=lambda s: float(s.get("t0") or 0.0))
        # 首段必须从 0 开始（保证「首尾相接覆盖全曲」不变量）
        if not segs or float(segs[0].get("t0") or 0.0) > 1e-6:
            db = float(default_bpm) if default_bpm else self._grid_bpm()
            if db <= 0:
                db = 120.0
            segs.insert(0, {"t0": 0.0, "bpm": db})
        out = []
        for s in segs:
            b = float(s.get("bpm") or 120.0)
            bl = 60.0 / b
            anchor = self._snap_to_beat(float(s.get("t0") or 0.0), bl)
            out.append({"t0": float(s.get("t0") or 0.0), "bpm": b,
                        "beat_len": round(bl, 6), "anchor": round(anchor, 4)})
        self._segments = out
        self.update()
        self.segmentsChanged.emit()

    # ---------------- 内部坐标/吸附 ----------------
    def _grid_bpm(self):
        if self._grid:
            try:
                return float(self._grid.get("bpm") or 0.0)
            except (TypeError, ValueError):
                pass
        return 0.0

    def _duration(self):
        d = 0.0
        if self._wf:
            d = max(d, float(self._wf.get("dur") or 0.0))
        if self._grid:
            d = max(d, float(self._grid.get("dur") or 0.0))
        if self._segments:
            for s in self._segments:
                try:
                    d = max(d, float(s.get("t0") or 0.0))
                except (TypeError, ValueError):
                    pass
        if self._anchors:
            d = max(d, max(self._anchors))
        return d if d > 0 else 1.0

    def _beat_len(self):
        if self._grid:
            bl = self._grid.get("beat_len") or 0.0
            if bl > 0:
                return float(bl)
        return 0.0

    def _snap_to_beat(self, sec, bl=None):
        if not self._snap:
            return sec
        bl = bl or self._beat_len()
        if bl and bl > 0:
            a = 0.0
            if self._grid:
                try:
                    import beatgrid as _bg
                    a = _bg.anchor_for(self._grid, sec)   # 多锚时取 offset 前最近一个
                except Exception:                          # noqa: BLE001
                    a = float(self._grid.get("anchor") or 0.0)
            return round(a + round((sec - a) / bl) * bl, 4)
        return round(sec, 3)

    def _sec_of_x(self, x):
        w = max(1, self.width())
        d = self._duration()
        return (float(x) / w) * d

    def _x_of_sec(self, sec):
        w = max(1, self.width())
        d = self._duration()
        return (float(sec) / d) * w

    # ---------------- 绘制 ----------------
    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(22, 24, 28))
        w = self.width()
        h = self.height()
        if w <= 0 or h <= 0:
            return
        d = self._duration()

        # 分段底色带
        band_colors = [QColor(40, 58, 92), QColor(56, 46, 88), QColor(30, 70, 70)]
        for i, s in enumerate(self._segments):
            x0 = self._x_of_sec(float(s.get("t0") or 0.0))
            x1 = w if i == len(self._segments) - 1 else \
                self._x_of_sec(float(self._segments[i + 1].get("t0") or d))
            p.fillRect(QRectF(x0, 0, x1 - x0, h), band_colors[i % len(band_colors)])

        # 拍网（浅灰）与八拍头（更亮）
        bl = self._beat_len()
        if bl and bl > 0 and d > 0:
            a = float(self._grid.get("anchor") or 0.0) if self._grid else 0.0
            t = a
            while t <= d:
                x = self._x_of_sec(t)
                is_phrase = (round((t - a) / bl) % 8 == 0)
                pen = QPen(QColor(120, 120, 120, 90) if not is_phrase
                           else QColor(200, 200, 200, 150), 1)
                p.setPen(pen)
                p.drawLine(int(x), 0, int(x), h)
                t += bl

        # 波形包络
        if self._wf and self._wf.get("amp"):
            amp = self._wf["amp"]
            cps = float(self._wf.get("cps") or 100.0)
            mid = h / 2.0
            pen = QPen(QColor(90, 170, 220), 1)
            p.setPen(pen)
            p.setBrush(QBrush(QColor(60, 130, 180, 90)))
            path = QPainterPath()
            path.moveTo(self._x_of_sec(0.0), mid)
            for i, v in enumerate(amp):
                sec = i / cps
                x = self._x_of_sec(sec)
                y = mid - float(v) * (mid - 4.0)
                path.lineTo(x, y)
            # 镜像回中线，形成闭合包络
            for i in range(len(amp) - 1, -1, -1):
                sec = i / cps
                x = self._x_of_sec(sec)
                y = mid + float(amp[i]) * (mid - 4.0)
                path.lineTo(x, y)
            path.closeSubpath()
            p.drawPath(path)

        # 八拍头标记线（manual anchors）
        for i, sec in enumerate(self._anchors):
            x = self._x_of_sec(sec)
            pen = QPen(QColor(255, 120, 120), 2 if i == self._hover else 1)
            p.setPen(pen)
            p.drawLine(int(x), 0, int(x), h)
            p.setPen(QPen(QColor(255, 120, 120), 1))
            p.drawText(int(x) + 3, 12, "%.2f" % sec)

        # 分段边界线
        p.setPen(QPen(QColor(255, 255, 255, 120), 1))
        for i, s in enumerate(self._segments[:-1]):
            x = self._x_of_sec(float(self._segments[i + 1].get("t0") or 0.0))
            p.drawLine(int(x), 0, int(x), h)

        # 分段拖选框
        if self._rubber is not None:
            x0, x1 = sorted(self._rubber)
            p.setPen(QPen(QColor(255, 220, 90), 1, Qt.PenStyle.DashLine))
            p.setBrush(QBrush(QColor(255, 220, 90, 30)))
            p.drawRect(QRectF(x0, 0, x1 - x0, h))

        # 时间轴
        p.setPen(QPen(QColor(160, 160, 160), 1))
        step = 60.0
        t = 0.0
        while t <= d:
            x = self._x_of_sec(t)
            p.drawLine(int(x), h - 6, int(x), h)
            p.drawText(int(x) + 2, h - 10, "%d:%02d" % (int(t) // 60, int(t) % 60))
            t += step

    # ---------------- 交互 ----------------
    def _anchor_near(self, x):
        sec = self._sec_of_x(x)
        best, bi = _HIT_PX * self._duration() / max(1, self.width()), -1
        for i, a in enumerate(self._anchors):
            if abs(self._x_of_sec(a) - x) <= best:
                best = abs(self._x_of_sec(a) - x)
                bi = i
        return bi

    def mousePressEvent(self, ev):
        x = ev.position().x()
        if ev.button() == Qt.MouseButton.RightButton:
            bi = self._anchor_near(x)
            if bi >= 0:
                self._anchors.pop(bi)
                self.update()
                self.anchorsChanged.emit()
            return
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        if self._mode == MODE_ANCHOR:
            bi = self._anchor_near(x)
            if bi >= 0:
                self._drag = {"type": "anchor", "index": bi}
            else:
                sec = self._snap_to_beat(self._sec_of_x(x))
                self._anchors.append(sec)
                self._anchors.sort()
                self.update()
                self.anchorsChanged.emit()
        elif self._mode == MODE_SEGMENT:
            self._rubber = (x, x)
            self.update()
        elif self._mode == MODE_BPM:
            sec = self._snap_to_beat(self._sec_of_x(x))
            self.bpmPicked.emit(sec)
        else:  # select
            bi = self._anchor_near(x)
            if bi >= 0:
                self._drag = {"type": "anchor", "index": bi}

    def mouseMoveEvent(self, ev):
        x = ev.position().x()
        if self._drag is not None and self._drag["type"] == "anchor":
            sec = self._snap_to_beat(self._sec_of_x(x))
            self._anchors[self._drag["index"]] = sec
            self._anchors.sort()
            self.update()
        elif self._rubber is not None:
            self._rubber = (self._rubber[0], x)
            self.update()
        else:
            bi = self._anchor_near(x)
            if bi != self._hover:
                self._hover = bi
                self.update()

    def mouseReleaseEvent(self, ev):
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        if self._drag is not None:
            if self._drag["type"] == "anchor":
                self._anchors.sort()
                self.anchorsChanged.emit()
            self._drag = None
        elif self._rubber is not None:
            x0, x1 = sorted(self._rubber)
            self._rubber = None
            self.update()
            t0 = self._snap_to_beat(self._sec_of_x(x0))
            t1 = self._snap_to_beat(self._sec_of_x(x1))
            if t1 - t0 > 0.05:
                self.segmentSelected.emit(t0, t1)

    def leaveEvent(self, ev):
        if self._hover != -1:
            self._hover = -1
            self.update()
        super().leaveEvent(ev)
