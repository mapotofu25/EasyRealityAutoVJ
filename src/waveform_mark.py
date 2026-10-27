# -*- coding: utf-8 -*-
r"""波形标记控件（Qt，共享给 ERLIB）—— 在波形上标 BPM / 八拍起点 / BPM 分段。

⚠ 本文件是 ERAVJ 源码树里的**共享 UI 模块**（ERLIB 打包时经 `pathex` 复用，
   与 `beatgrid`/`fp`/`music_meta` 同一机制），只在 `AutoVJ/src` 维护一份。

## 职责边界（数据层零 Qt，UI 层零算法）
- 本控件**只负责画 + 交互 + 发信号**，不写库、不落盘、不算 BPM。
- 数据模型来自 `beatgrid.effective_grid` / `waveform` dict / `grid_manual` dict。
- 所有标记坐标一律**秒（float，权威）**；波形只是背景（`x = (sec-view0)/span*width`）。
- 吸附到拍/小节：用 `effective_grid` 反算拍网（`anchor + n*beat_len`），吸附结果仍存秒。

## 交互（与架构 §4.2 / PRD 一致）
- 工具栏模式：`select`（默认，拖动标记/拖播放头）、`anchor`（点选打八拍头）、
  `segment`（拖选 BPM 分段区间）、`bpm`（点选推算 BPM）。
- 八拍头：点选新增 / 拖动调整 / 右键删除；多个共存，信号 `anchorsChanged`。
- 分段：拖选区间 → 信号 `segmentSelected(t0, t1)`（由详情页弹 BPM 输入、再回填），
  分段以不同底色带显示，`segmentsChanged` 通知更新。
- 手动 BPM：`bpm` 模式点选 → 信号 `bpmPicked(sec)`。

## v2 增强（用户要求，2026-10-27）
1. **彩色波形**：`QLinearGradient` 按振幅映射（中线青蓝 → 中间黄 → 顶端红），
   数据格式不变（仍是峰值数组 `amp`）。
2. **视窗缩放**（`view0` / `view_span`）：滚轮以**鼠标所在秒位为锚**缩放（与 ERCUE
   `timeline_widget.wheelEvent` 同一套锚点公式）；Shift+滚轮左右平移。
   ⚠ 所有标记交互的 `x ↔ 秒` 都走 `_sec_of_x` / `_x_of_sec`，因此**自动跟随视窗**。
3. **可拖动播放头**：`set_playhead(sec)` / `playhead_sec()` / 信号 `playheadMoved(float)`；
   `select` 模式下左键在波形空白处按下/拖动 = 移动竖线并 emit（详情页据此 seek）。
4. **分段 BPM 标签上移**：波形**顶部预留 `LABEL_H` 像素标签带**（默认 20px），
   标签文字只画在带内（`label_items()` 给出几何，测试据此断言）；波形区内只留
   分段底色带，不再写文字。
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QLinearGradient, QPainter,
                           QPainterPath, QPen)
from PySide6.QtWidgets import QWidget

MODE_SELECT = "select"
MODE_ANCHOR = "anchor"
MODE_SEGMENT = "segment"
MODE_BPM = "bpm"

_HIT_PX = 6.0            # 命中标记线 / 播放头的像素阈值
# ---- v2 布局常量（详见模块 docstring）----
LABEL_H = 20             # 顶部标签带高度（分段 BPM / 八拍秒数只画在这里）
_BAND_H = LABEL_H        # 兼容别名（历史 API 名）
AXIS_H = 14.0            # 底部时间轴高度
MIN_SPAN_SEC = 0.5       # 最小可视跨度（秒）—— 再放大会看不清也不用
_VIEW_MIN_SPAN = MIN_SPAN_SEC   # 兼容别名（历史 API 名）
ZOOM_FACTOR = 0.82       # 每档滚轮缩放比例（与 ERCUE 一致）
PAN_FRACTION = 0.1       # Shift+滚轮每次平移的可视跨度比例

# 分段底色带（波形区）
_BAND_COLORS = (QColor(40, 58, 92), QColor(56, 46, 88), QColor(30, 70, 70))
# 标签带底色 / 分段标注文字色 / 八拍标注文字色
_LABEL_BG = QColor(30, 34, 42)
_LABEL_SEG_COLOR = QColor(235, 240, 250)   # 近白：与波形渐变(青/黄/红)不撞色，便于取像素断言
_LABEL_ANCHOR_COLOR = QColor(205, 175, 255)  # 浅紫：同上
# 播放头（醒目实线）
_PLAYHEAD_COLOR = QColor(80, 255, 190)
# 波形描边（叠在渐变填充上）
_WAVE_OUTLINE = QColor(150, 210, 255, 200)


class WaveformMarkWidget(QWidget):
    """波形标记控件。用法见模块 docstring。"""

    anchorsChanged = Signal()
    segmentsChanged = Signal()
    bpmPicked = Signal(float)              # 手动 BPM 点选：该点的秒
    segmentSelected = Signal(float, float)  # 分段拖选：t0, t1（秒）
    playheadMoved = Signal(float)           # 播放头被拖动：新位置（秒）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._wf = None                    # waveform dict {v,cps,dur,amp}
        self._grid = None                  # effective grid dict
        self._anchors = []                 # 秒，升序
        self._segments = []                # [{t0,bpm,beat_len,anchor}]
        self._snap = True
        self._mode = MODE_SELECT
        self._drag = None                  # 拖拽状态 {type: anchor/playhead, ...}
        self._rubber = None                # 分段拖选 (x0, x1)
        self._hover = -1                   # 悬停的锚点下标
        # ---- v2：视窗 + 播放头 ----
        self.view0 = 0.0                   # 可视窗口左端（秒）
        self.view_span = 0.0              # 可视跨度（秒）；<=0 或 >= 全曲 = 全曲可视
        self._playhead_sec = 0.0
        self.setMouseTracking(True)
        self.setMinimumHeight(120)

    # ---------------- 数据注入 ----------------
    def set_waveform(self, wf):
        wf = wf if isinstance(wf, dict) else None
        old_dur = self._duration()
        self._wf = wf
        new_dur = self._duration()
        if abs(new_dur - old_dur) > 1e-6:
            self.reset_view()               # 换歌 → 视窗回全曲
            self.set_playhead(self._playhead_sec)
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

    # ---------------- v2：播放头 ----------------
    def playhead_sec(self) -> float:
        """当前播放头位置（秒）。"""
        return float(self._playhead_sec)

    def playhead(self) -> float:
        """`playhead_sec()` 的别名（历史 API 名）。"""
        return float(self._playhead_sec)

    def set_playhead(self, sec):
        """设置播放头（**不发信号**，供播放中随音频推进刷新）。"""
        try:
            sec = float(sec)
        except (TypeError, ValueError):
            return
        d = self._duration()
        self._playhead_sec = max(0.0, min(sec, d))
        self.update()

    def move_playhead(self, sec):
        """设置播放头并**发 `playheadMoved`**（供鼠标拖动 / 详情页 seek 用）。"""
        try:
            sec = float(sec)
        except (TypeError, ValueError):
            return
        d = self._duration()
        self._playhead_sec = max(0.0, min(sec, d))
        self.update()
        self.playheadMoved.emit(float(self._playhead_sec))

    # ---------------- v2：视窗 ----------------
    def reset_view(self):
        """恢复全曲可视（换歌 / 双击复位时用）。"""
        self.view0 = 0.0
        self.view_span = 0.0
        self.update()

    def view_range(self):
        """当前可视窗口 `(view0, span)`（已按全曲长度钳制）。"""
        return self._view()

    # ---- 兼容别名（历史 API 名，行为完全等价；新代码用上面的名字）----
    def view(self):
        """`(view0, span)`（`view_range` 的别名）。"""
        return self._view()

    def set_view(self, view0, span):
        """直接设定视窗（`span <= 0` 或 >= 全曲 ⇒ 全曲）。"""
        d = self._duration()
        try:
            view0 = float(view0)
            span = float(span)
        except (TypeError, ValueError):
            return
        if span <= 0.0 or span >= d:
            self.reset_view()
            return
        self.view_span = max(MIN_SPAN_SEC, span)
        self.view0 = max(0.0, min(d - self.view_span, view0))
        self.update()

    def zoom_to(self, anchor_sec, span_sec):
        """把视窗设成「以 `anchor_sec` 居中、跨度 `span_sec`」（测试 / 快捷键用）。"""
        d = self._duration()
        span = max(MIN_SPAN_SEC, min(d, float(span_sec)))
        if span >= d:
            self.reset_view()
            return
        self.view_span = span
        self.view0 = max(0.0, min(d - span, float(anchor_sec) - span / 2.0))
        self.update()

    def _view(self):
        """`(view0, span)`：视窗跨度为空 / 超全曲 ⇒ 全曲。"""
        d = self._duration()
        if self.view_span <= 0.0 or self.view_span >= d:
            return 0.0, d
        span = float(self.view_span)
        v0 = max(0.0, min(float(self.view0), d - span))
        return v0, span

    def _pan_sec(self, dsec):
        d = self._duration()
        v0, span = self._view()
        if span >= d:
            return
        self.view0 = max(0.0, min(d - span, v0 + float(dsec)))
        self.update()

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
        """像素 → 秒（**跟随视窗**；所有交互都必须走这里）。"""
        w = max(1, self.width())
        v0, span = self._view()
        return v0 + (float(x) / w) * span

    def _x_of_sec(self, sec):
        """秒 → 像素（**跟随视窗**；所有绘制都必须走这里）。"""
        w = max(1, self.width())
        v0, span = self._view()
        if span <= 0:
            return 0.0
        return (float(sec) - v0) / span * w

    def _sec_per_px(self):
        _v0, span = self._view()
        return span / max(1, self.width())

    def _plot_top(self):
        return float(LABEL_H)

    def _plot_bottom(self):
        return max(float(LABEL_H) + 8.0, float(self.height()) - AXIS_H)

    # ---------------- v2：顶部标签几何（测试据此断言「文字在带内」） ----------------
    def label_items(self):
        """顶部标签带里要画的文字项（几何由本方法给出，`paintEvent` 直接用）。

        返回 `[{text, kind, x, y, w, h, color}]`：
        · `kind="segment"`：`段N xxBPM`（按分段区间，左对齐）；
        · `kind="anchor"`：八拍头秒数（红，短标签）。
        ⚠ 所有 `y` 都落在 `[0, LABEL_H]` 内 —— 即**波形区永不出现这些文字**。
        """
        w = max(1, self.width())
        d = self._duration()
        v0, _span = self._view()
        out = []
        top = 1.0
        hh = max(8.0, LABEL_H - 3.0)
        # 分段标签
        n = len(self._segments)
        for i, s in enumerate(self._segments):
            try:
                t0 = float(s.get("t0") or 0.0)
                bpm = float(s.get("bpm") or 0.0)
            except (TypeError, ValueError):
                continue
            t1 = float(self._segments[i + 1].get("t0") or d) if i + 1 < n else d
            x0 = max(0.0, self._x_of_sec(t0))
            x1 = min(float(w), self._x_of_sec(t1))
            if x1 <= x0 + 1.0:
                continue
            text = "段%d %.0fBPM" % (i + 1, bpm) if abs(bpm - round(bpm)) < 0.05 \
                else "段%d %.1fBPM" % (i + 1, bpm)
            out.append({"text": text, "kind": "segment",
                        "x": x0 + 3.0, "y": top, "w": max(0.0, x1 - x0 - 6.0), "h": hh,
                        "color": _LABEL_SEG_COLOR})
        # 八拍头标签
        for a in self._anchors:
            try:
                a = float(a)
            except (TypeError, ValueError):
                continue
            x = self._x_of_sec(a)
            if x < -40.0 or x > w + 4.0:
                continue
            out.append({"text": "%.2f" % a, "kind": "anchor",
                        "x": x + 3.0, "y": top, "w": 48.0, "h": hh,
                        "color": _LABEL_ANCHOR_COLOR})
        return out

    def label_band_height(self):
        """顶部标签带高度（像素）。"""
        return float(LABEL_H)

    # ---------------- 绘制 ----------------
    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(22, 24, 28))
        w = self.width()
        h = self.height()
        if w <= 0 or h <= 0:
            return
        d = self._duration()
        v0, span = self._view()
        plot_top = self._plot_top()
        plot_bot = self._plot_bottom()
        plot_h = max(1.0, plot_bot - plot_top)

        # ---- 1) 分段底色带（只在波形区，不写文字）----
        for i, s in enumerate(self._segments):
            x0 = self._x_of_sec(float(s.get("t0") or 0.0))
            x1 = w if i == len(self._segments) - 1 else \
                self._x_of_sec(float(self._segments[i + 1].get("t0") or d))
            band = _BAND_COLORS[i % len(_BAND_COLORS)]
            p.fillRect(QRectF(x0, plot_top, x1 - x0, plot_h), band)

        # ---- 3) 彩色波形（按振幅渐变：青蓝 → 黄 → 红）----
        if self._wf and self._wf.get("amp"):
            self._paint_waveform(p, w, plot_top, plot_bot)

        # ---- 3b) 拍网（浅灰）/ 八拍头（更亮）—— 画在波形**之上**，否则会被彩色填充盖住
        bl = self._beat_len()
        if bl and bl > 0 and d > 0:
            # ★ 密度门限：全曲可视时 128BPM 的拍间距只有 ~1.4px，逐拍画会变成一片竖条纹
            #   （实测截图有明显条纹噪声）。拍间距 <4px 时只画八拍头线；八拍也 <4px 就不画。
            bl_px = bl / max(1e-9, span) * w
            draw_beat = bl_px >= 4.0
            draw_phrase = bl_px * 8.0 >= 4.0
            a = float(self._grid.get("anchor") or 0.0) if self._grid else 0.0
            t = a
            while t <= d + 1e-9:
                x = self._x_of_sec(t)
                if -2.0 <= x <= w + 2.0:
                    is_phrase = (round((t - a) / bl) % 8 == 0)
                    if draw_beat or (is_phrase and draw_phrase):
                        pen = QPen(QColor(120, 140, 150, 70) if not is_phrase
                                   else QColor(220, 230, 240, 120), 1)
                        p.setPen(pen)
                        p.drawLine(int(x), int(plot_top), int(x), int(plot_bot))
                t += bl

        # ---- 4) 分段边界线 ----
        p.setPen(QPen(QColor(255, 255, 255, 120), 1))
        for i, s in enumerate(self._segments[:-1]):
            x = self._x_of_sec(float(self._segments[i + 1].get("t0") or 0.0))
            if -2.0 <= x <= w + 2.0:
                p.drawLine(int(x), int(plot_top), int(x), int(plot_bot))

        # ---- 5) 八拍头标记线（文字在顶部标签带）----
        for i, sec in enumerate(self._anchors):
            x = self._x_of_sec(sec)
            if x < -2.0 or x > w + 2.0:
                continue
            pen = QPen(QColor(255, 120, 120), 2 if i == self._hover else 1)
            p.setPen(pen)
            p.drawLine(int(x), int(plot_top), int(x), int(plot_bot))

        # ---- 6) 播放头（醒目实线 + 顶部小三角）----
        ph_x = self._x_of_sec(self._playhead_sec)
        if -4.0 <= ph_x <= w + 4.0:
            p.setPen(QPen(_PLAYHEAD_COLOR, 2))
            p.drawLine(int(ph_x), int(plot_top), int(ph_x), int(plot_bot))
            tri = QPainterPath()
            tri.moveTo(ph_x - 5.0, plot_top)
            tri.lineTo(ph_x + 5.0, plot_top)
            tri.lineTo(ph_x, plot_top + 7.0)
            tri.closeSubpath()
            p.fillPath(tri, QBrush(_PLAYHEAD_COLOR))

        # ---- 7) 分段拖选框 ----
        if self._rubber is not None:
            x0, x1 = sorted(self._rubber)
            p.setPen(QPen(QColor(255, 220, 90), 1, Qt.PenStyle.DashLine))
            p.setBrush(QBrush(QColor(255, 220, 90, 30)))
            p.drawRect(QRectF(x0, plot_top, x1 - x0, plot_h))
            p.setBrush(Qt.BrushStyle.NoBrush)

        # ---- 8) 时间轴 ----
        step = self._axis_step(span, w)
        t = max(0.0, (int(max(0.0, v0) / step)) * step)
        p.setPen(QPen(QColor(160, 160, 160), 1))
        while t <= min(d, v0 + span) + 1e-9:
            x = self._x_of_sec(t)
            if 0.0 <= x <= w:
                p.drawLine(int(x), int(h - 6), int(x), int(h))
                p.drawText(int(x) + 2, int(h - 9), self._fmt_time(t, step))
            t += step

        # ---- 9) 顶部标签带（最后画，保证在最上层）----
        p.fillRect(QRectF(0, 0, w, LABEL_H), _LABEL_BG)
        p.setPen(QPen(QColor(70, 76, 90), 1))
        p.drawLine(0, int(LABEL_H), w, int(LABEL_H))
        f = QFont(p.font())
        f.setPointSizeF(8.0)
        p.setFont(f)
        for item in self.label_items():
            if item["w"] < 12.0:
                continue
            p.setPen(QPen(item["color"], 1))
            p.drawText(QRectF(item["x"], item["y"], item["w"], item["h"]),
                       int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                       item["text"])

    def _paint_waveform(self, p, w, plot_top, plot_bot):
        """把峰值数组画成渐变包络。

        ★ 性能：按**像素列**聚合（每列取该列时间范围内的最大 amplitude），路径顶点数
          ≈ 2×宽度（~1800）而不是 2×样本列数。用 28k 列的 FLAC 实测：直接连 56k 个顶点
          的一次重绘要 ~1.2s（GIL 被占满，播放线程都推不动），按像素列聚合后 < 5ms。
        """
        amp = self._wf["amp"]
        n = len(amp)
        cps = float(self._wf.get("cps") or 100.0)
        if cps <= 0 or n == 0 or w <= 1:
            return
        v0, span = self._view()
        mid = (plot_top + plot_bot) / 2.0
        half = max(2.0, (plot_bot - plot_top) / 2.0 - 3.0)

        x_first = max(0, int(self._x_of_sec(v0)) - 1)
        x_last = min(int(w) - 1, int(self._x_of_sec(v0 + span)) + 1)
        if x_last < x_first:
            return
        cols = []
        for px in range(x_first, x_last + 1):
            t0 = v0 + (px / float(w)) * span
            t1 = v0 + ((px + 1.0) / float(w)) * span
            i0 = int(t0 * cps)
            i1 = int(t1 * cps)
            if i1 <= i0:
                i1 = i0 + 1                      # 放大到 1 像素 < 1 个样本时至少取 1 列
            i0 = max(0, min(i0, n - 1))
            i1 = max(i0 + 1, min(i1 + 1, n))
            seg = amp[i0:i1]
            # ★ 只取 max 会让"母带很响"的曲子整块顶满（实测 colmax 均值 0.86 ⇒ 画面变成
            #   一整面渐变墙）。取 0.75*均值 + 0.25*峰值，既保留瞬态尖峰又看得见包络形状。
            a = 0.75 * (sum(seg) / float(len(seg))) + 0.25 * max(seg)
            cols.append((float(px), float(a)))
        if not cols:
            return

        # 按振幅渐变：中线（低）青蓝 → 中间黄 → 顶端（高）红，上下对称
        grad = QLinearGradient(QPointF(0.0, plot_top), QPointF(0.0, plot_bot))
        grad.setColorAt(0.00, QColor(235, 70, 70))     # 最高振幅 → 红
        grad.setColorAt(0.18, QColor(245, 140, 60))    # 高 → 橙
        grad.setColorAt(0.34, QColor(240, 190, 60))    # 中高 → 黄
        grad.setColorAt(0.50, QColor(80, 205, 235))    # 中线（最低）→ 青蓝
        grad.setColorAt(0.66, QColor(240, 190, 60))
        grad.setColorAt(0.82, QColor(245, 140, 60))
        grad.setColorAt(1.00, QColor(235, 70, 70))

        path = QPainterPath()
        outline = QPainterPath()
        path.moveTo(cols[0][0], mid)
        outline.moveTo(cols[0][0], mid - cols[0][1] * half)
        for x, a in cols:
            y = mid - a * half
            path.lineTo(x, y)
            outline.lineTo(x, y)
        for x, a in reversed(cols):
            path.lineTo(x, mid + a * half)
        path.closeSubpath()
        # ★ 只填充、不描边：包络是逐列折线，用 1px 画笔描边会把整条波形涂成笔色、
        #   盖掉渐变（实测过：整块变成描边色 (145,202,244)，红/黄全丢）。
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(grad))
        p.drawPath(path)
        # 上包络单独描一条淡线做轮廓（这条是干净的曲线，不会盖住填充）
        p.setPen(QPen(_WAVE_OUTLINE, 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(outline)
        p.setBrush(Qt.BrushStyle.NoBrush)

        # 中线（淡）
        p.setPen(QPen(QColor(120, 190, 235, 70), 1))
        p.drawLine(int(self._x_of_sec(v0)), int(mid),
                   int(self._x_of_sec(v0 + span)), int(mid))

    @staticmethod
    def _axis_step(span, w):
        """按可视跨度选时间刻度间隔（目标：每格 ≈ 100 px，最多 12 格）。"""
        ticks = max(2.0, float(w) / 100.0)
        step = max(1e-6, float(span) / ticks)
        for cand in (0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 15.0, 30.0, 60.0,
                     120.0, 300.0, 600.0):
            if cand >= step:
                return cand
        return 600.0

    @staticmethod
    def _fmt_time(t, step):
        if step < 1.0:
            return "%.1f" % t
        return "%d:%02d" % (int(t) // 60, int(t) % 60)

    # ---------------- 交互 ----------------
    def _anchor_near(self, x):
        best, bi = _HIT_PX * self._sec_per_px(), -1
        for i, a in enumerate(self._anchors):
            dx = abs(self._x_of_sec(a) - x)
            if dx <= best:
                best = dx
                bi = i
        return bi

    def _playhead_near(self, x):
        return abs(self._x_of_sec(self._playhead_sec) - float(x)) <= _HIT_PX

    def wheelEvent(self, e):
        """滚轮语义**逐条对齐 ERCUE** `timeline_widget.wheelEvent`（用户点名要 ERCUE 那一个按键）。

        · **Ctrl + 滚轮**：以**鼠标所在秒位为锚**缩放（手不用重新找位置，锚点不漂）；
        · **Shift + 滚轮**：左右平移；
        · **普通滚轮**：ERCUE 里是「上下滚动」（纵向内容），本控件没有纵向内容 ⇒
          退化为**左右平移**（与 Shift 同效），**不改变跨度**，避免误缩放。
        """
        dy = e.angleDelta().y()
        if not dy:
            return
        n = float(dy) / 120.0
        mods = e.modifiers()
        d = self._duration()
        v0, span = self._view()
        if not (mods & Qt.KeyboardModifier.ControlModifier):
            self._pan_sec(-n * span * PAN_FRACTION)
            e.accept()
            return
        anchor = self._sec_of_x(e.position().x())
        frac = (anchor - v0) / max(1e-9, span)
        new_span = max(MIN_SPAN_SEC, min(d, span * (ZOOM_FACTOR ** n)))
        if new_span >= d * 0.9999:
            self.reset_view()
        else:
            self.view_span = new_span
            self.view0 = max(0.0, min(d - new_span, anchor - frac * new_span))
        e.accept()
        self.update()

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
        else:  # select：命中锚点 → 拖锚点；否则 → 拖播放头（并立即 emit）
            bi = self._anchor_near(x)
            if bi >= 0:
                self._drag = {"type": "anchor", "index": bi}
            else:
                self._drag = {"type": "playhead"}
                self.move_playhead(self._sec_of_x(x))

    def mouseMoveEvent(self, ev):
        x = ev.position().x()
        if self._drag is not None and self._drag["type"] == "anchor":
            sec = self._snap_to_beat(self._sec_of_x(x))
            self._anchors[self._drag["index"]] = sec
            self._anchors.sort()
            self.update()
        elif self._drag is not None and self._drag["type"] == "playhead":
            self.move_playhead(self._sec_of_x(x))
        elif self._rubber is not None:
            self._rubber = (self._rubber[0], x)
            self.update()
        else:
            bi = self._anchor_near(x)
            if bi != self._hover:
                self._hover = bi
                self.update()
            if self._mode == MODE_SELECT:
                self.setCursor(Qt.CursorShape.SizeHorCursor
                               if (self._playhead_near(x) or bi >= 0)
                               else Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, ev):
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        if self._drag is not None:
            if self._drag["type"] == "anchor":
                self._anchors.sort()
                self.anchorsChanged.emit()
            elif self._drag["type"] == "playhead":
                self.move_playhead(self._sec_of_x(ev.position().x()))
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
