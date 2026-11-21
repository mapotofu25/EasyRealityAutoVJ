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
# 能量段落带（用户 2026-10-29：「标在波形图下方」）。
# 为什么从"叠在波形上的半透明染色"改成"下方独立条带"：
#   · 叠色只能表达「这段属于哪一档」——0.72 和 0.95 都染同一个颜色，看不出差别；
#   · 独立条带可以用**柱高 = 平均能量**，层级一眼可比，且完全不挡读波形。
# ⚠ 高度按"能不能读出层级"定：18px 柱区时 0.48 的段只有 8.6px、0.17 只有 3px，
#   截图里几乎看不出高低差（第一版实拍验证）。26px 柱区下 0.17→4.4px、0.98→25px，
#   一眼可比；再加 12px 文字行，总高 46px 仍然只占 320px 控件的 1/7。
# ⚠ 46 的时候段落文字和底部时间轴标签**贴到一起**了（实拍详情页截图）；
#   加到 54 让出 8px 呼吸位，柱子仍然 26px 不缩水。
ENERGY_H = 54.0          # 段落带总高（26 柱 + 13 文字 + 分隔/上下留白）
ENERGY_BAR_H = 26.0      # 柱区高度（能量 0~1 线性映射到 0~26px）
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


def grid_times(anchor, beat_len, dur):
    """拍网时间序列（秒）——**纯函数，供 paintEvent 与测试共用**。

    产出 `(k, t)`：`k` 是相对 `anchor` 的拍序号（`k % 8 == 0` 即八拍头），`t` 是秒。

    ⚠⚠ 为什么不能从 `anchor` 单向往后画（2026-10-28 用户报「左边空的没有八拍划分」）：
    `anchor` 只是**某一处**八拍头，VDJ 的 `Phase` 常落在曲子中段（实测某曲 66.0s）⇒
    只往后画会让 anchor 之前的整段一条线都没有。拍网是**周期性的**，
    因此必须从 anchor 往回退到 ≤0（`k` 取负）再一路画到曲尾。
    """
    try:
        bl = float(beat_len or 0.0)
        d = float(dur or 0.0)
        a = float(anchor or 0.0)
    except (TypeError, ValueError):
        return
    if bl <= 0.0 or d <= 0.0:
        return
    # 往回退到第一个 ≤ 0 的拍位：k = -(floor(a/bl) + 1)
    k = -(int(a / bl) + 1) if a > 0.0 else 0
    t = a + k * bl
    while t <= d + 1e-9:
        yield k, t
        t += bl
        k += 1


def grid_times_range(anchor, beat_len, t_start, t_end):
    """限定区间 `[t_start, t_end]` 内的拍位（多 BPM 段用）。

    与 `grid_times` 的区别：只在**这一段的区间**里画，`k` 以该段自己的 `anchor`
    为基准（与 `beatgrid.segment_at` / `grid_anchor_after_v2` 的语义一致）——
    段与段之间 BPM 变了，共用一条网格会把后半段画错。
    """
    try:
        bl = float(beat_len or 0.0)
        a = float(anchor or 0.0)
        t0 = float(t_start or 0.0)
        t1 = float(t_end or 0.0)
    except (TypeError, ValueError):
        return
    if bl <= 0.0 or t1 < t0:
        return
    k = int((t0 - a) // bl)          # 从段起点前最近的一拍开始
    t = a + k * bl
    while t <= t1 + 1e-9:
        if t >= t0 - 1e-9:
            yield k, t
        t += bl
        k += 1


class WaveformMarkWidget(QWidget):
    """波形标记控件。用法见模块 docstring。"""

    anchorsChanged = Signal()
    segmentsChanged = Signal()
    bpmPicked = Signal(float)              # 手动 BPM 点选：该点的秒
    segmentSelected = Signal(float, float)  # 分段拖选：t0, t1（秒）
    playheadMoved = Signal(float)           # 播放头被拖动：新位置（秒）
    # ★ 2026-10-28（用户要求「多段不同的 bpm 修改改到在波形图上右键选项」）：
    #   右键菜单由**宿主窗口**弹（控件不懂业务）。参数 = (光标秒, 全局坐标 QPoint)。
    contextMenuRequested = Signal(float, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._wf = None                    # waveform dict {v,cps,dur,amp}
        self._grid = None                  # effective grid dict
        self._anchors = []                 # 秒，升序
        self._segments = []                # [{t0,bpm,beat_len,anchor}]
        self._snap = True
        # ★ 2026-10-29 用户要求：「吸附到拍」和「吸附到小节」二选一。
        #   `_snap_beats` 是吸附步长的**拍数**：1 = 每拍，4 = 每小节。
        self._snap_beats = 1
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

    def set_snap_beats(self, n):
        """吸附步长（拍）。1 = 吸附到拍，4 = 吸附到小节。"""
        try:
            v = int(n)
        except (TypeError, ValueError):
            v = 1
        self._snap_beats = 4 if v == 4 else 1
        self._snap = True                    # 选了粒度就一定是"开"
        self.update()

    def snap_beats(self):
        return int(getattr(self, "_snap_beats", 1) or 1)

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

    # ---- 能量段落（用户 2026-10-28）：只读展示，编辑在详情页的对话框里 ----
    def energy_sections(self):
        return [dict(s) for s in (getattr(self, "_energy_sections", None) or [])]

    def set_energy_sections(self, sections):
        """段落色带（按档位着色）。数据与 `sections.normalize()` 同 schema。"""
        self._energy_sections = [dict(s) for s in (sections or [])
                                 if isinstance(s, dict)]
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

    def _flip_beats(self):
        """用户手工的「半句相位翻转」拍数（0 或 4）。

        ★★ 2026-11-07（用户 `Northad - Frosty Dance`「这首歌的 8 拍的线偏移了 4 拍」）：
          `grid_flip` 这个用户产物**一直只有引擎在读**（`ui_main.py` 注入 item 时换算成
          `flip=4.0`），波形这边从来没读过 ⇒ 同一首歌「引擎认定的八拍头」和
          「波形上画的八拍线」相差 4 拍（0.413793 × 4 = **1.655 s**），用户看到的就是
          「线偏了半个乐句」。现在 `beatgrid.effective_grid()` 会把 `flip` 一起下发，
          这里照着用：拍网等间距，整体平移 4 拍后**逐拍线位置不变**，只有八拍头换相位。
        """
        if not self._grid:
            return 0.0
        try:
            v = float(self._grid.get("flip") or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return v % 8.0

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
            # 步长 = 一拍 × 拍数（1=拍 / 4=小节）。原来固定按一拍吸。
            step = bl * float(self.snap_beats())
            return round(a + round((sec - a) / step) * step, 4)
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
        """波形区底边。**有能量段落时让出 `ENERGY_H`**（段落带画在那一段里）。"""
        reserve = AXIS_H
        if getattr(self, "_energy_sections", None):
            reserve = AXIS_H + ENERGY_H
        return max(float(LABEL_H) + 8.0, float(self.height()) - reserve)

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

        # ---- 3a) 能量段落带（用户 2026-10-29：「标在波形图下方」）----
        #   从"叠在波形上的半透明染色"改成**波形下方的独立条带**，因为它能多表达一层：
        #     · 柱高 = 该段平均能量（0~1 线性映射到 ENERGY_BAR_H）
        #     · 柱色 = 高/低档（高=暖橙红、低=青蓝），与详情页色块同一套
        #     · 文字 = 段名 + 能量数值（段够宽才写，窄段自动省略，绝不重叠）
        #     · 竖线 = 段落边界，从带顶一直拉到波形顶（一眼看出"到底切在哪一秒"）
        #   为什么不再染色：0.72（主歌）和 0.96（drop）会染成同一个颜色，
        #   而"这一段有多强"恰恰是要看的东西 —— 柱高才看得出层级差。
        _esec = [x for x in (getattr(self, "_energy_sections", None) or [])
                 if isinstance(x, dict)]
        if _esec and d > 0:
            _bl0 = self._beat_len()
            _tc = {"low": (74, 132, 214), "mid": (222, 190, 90),
                   "high": (232, 96, 76)}
            _by0 = float(plot_bot) + 3.0
            _by1 = _by0 + ENERGY_BAR_H
            # ★ 文字画在**柱区内部**（贴柱底），不额外占一行。
            #   为什么：放到柱区外面会和时间轴标签抢空间 —— 实测 ENERGY_H=46 和 54
            #   两版都出现"段落文字压住 0:00"（详情页实拍）。放柱内后：
            #     高柱 ⇒ 白字压在彩色柱上（可读）
            #     矮柱 ⇒ 白字落在深色底槽上（也可读）
            _ty = _by1 - 1.0
            # 波形区与段落带的分隔线（不然两者糊在一起，看不出条带从哪开始）
            p.setPen(QPen(QColor(70, 78, 92, 200), 1))
            p.drawLine(0, int(float(plot_bot) + 1.0), int(w), int(float(plot_bot) + 1.0))
            # 0.5 能量参考线（很淡，给"柱有多高"一个标尺）
            p.setPen(QPen(QColor(120, 130, 145, 70), 1))
            p.drawLine(0, int(_by0 + ENERGY_BAR_H * 0.5),
                       int(w), int(_by0 + ENERGY_BAR_H * 0.5))
            for _s in _esec:
                try:
                    _a0 = float(_s.get("t0") or 0.0)
                    _a1 = float(_s.get("t1") or d)
                except (TypeError, ValueError):
                    continue
                if _a1 <= _a0:
                    continue
                _x0 = self._x_of_sec(_a0)
                _x1 = self._x_of_sec(_a1)
                if _x1 < -2.0 or _x0 > w + 2.0:
                    continue
                _c = _tc.get(str(_s.get("tier") or "mid"), (150, 150, 150))
                _bw = max(1.0, _x1 - _x0)
                try:
                    _e = max(0.0, min(1.0, float(_s.get("energy") or 0.0)))
                except (TypeError, ValueError):
                    _e = 0.0
                # 柱底槽（很淡，让"空段"也有范围可读）
                p.fillRect(QRectF(_x0, _by0, _bw, ENERGY_BAR_H),
                           QColor(_c[0], _c[1], _c[2], 26))
                # 柱：高度 = 平均能量
                _bh = max(2.0, ENERGY_BAR_H * _e)
                p.fillRect(QRectF(_x0 + 0.5, _by1 - _bh, max(1.0, _bw - 1.0), _bh),
                           QColor(_c[0], _c[1], _c[2], 215))
                # ★ 柱顶高亮线：纯色块之间人眼分不出 14px 和 23px（第一版实拍验证：
                #   柱高明明差 9px，看着却像一条平带）。画一条 1.5px 的亮线，
                #   高度轮廓立刻显现 —— 这是"阶梯图"的读法。
                p.setPen(QPen(QColor(255, 255, 255, 165), 1.5))
                p.drawLine(QPointF(_x0 + 1.0, _by1 - _bh), QPointF(_x1 - 1.0, _by1 - _bh))
                # 边界竖线：带顶 → 波形顶
                p.setPen(QPen(QColor(_c[0], _c[1], _c[2], 190), 1.5))
                p.drawLine(int(_x0), int(_by0), int(_x0), int(plot_top))
                # 文字：段够宽才写。>92px 写「段名 0.96」，>46px 只写数值
                if _bw >= 46.0:
                    _nm = str(_s.get("name") or "")
                    _txt = ("%s %.2f" % (_nm, _e)) if _bw >= 92.0 and _nm else ("%.2f" % _e)
                    p.setPen(QColor(226, 232, 240, 235))
                    _f = p.font()
                    _f.setPointSizeF(7.5)
                    p.setFont(_f)
                    p.drawText(QRectF(_x0 + 3.0, _ty - 11.0, _bw - 5.0, 12.0),
                               int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                               _txt)
                elif _bw >= 9.0 and _bl0 > 0 and (_a1 - _a0) <= 4.6 * _bl0:
                    p.setPen(QColor(255, 255, 255, 235))
                    _f = p.font()
                    _f.setPointSizeF(6.5)
                    p.setFont(_f)
                    p.drawText(QRectF(_x0 - 2.0, _ty - 10.0, _bw + 4.0, 11.0),
                               int(Qt.AlignmentFlag.AlignCenter), "4\u62cd")

        # ---- 3b) 拍网（浅灰）/ 八拍头（更亮）—— 画在波形**之上**，否则会被彩色填充盖住
        bl = self._beat_len()
        # ★ 有分段时用**最密的段**判密度（否则慢段判"可画"、快段糊成条纹）
        _seg_list = [x for x in (self._segments or []) if isinstance(x, dict)]
        if _seg_list:
            _bls = []
            for _x in _seg_list:
                try:
                    v = float(_x.get("beat_len") or 0.0)
                    if v <= 0:
                        _b = float(_x.get("bpm") or 0.0)
                        v = 60.0 / _b if _b > 0 else 0.0
                except (TypeError, ValueError):
                    v = 0.0
                if v > 0:
                    _bls.append(v)
            if _bls:
                bl = min(_bls)
        if bl and bl > 0 and d > 0:
            # ★ 密度门限：全曲可视时 128BPM 的拍间距只有 ~1.4px，逐拍画会变成一片竖条纹
            #   （实测截图有明显条纹噪声）。拍间距 <4px 时只画八拍头线；八拍也 <4px 就不画。
            bl_px = bl / max(1e-9, span) * w
            draw_beat = bl_px >= 4.0
            draw_phrase = bl_px * 8.0 >= 4.0
            a = float(self._grid.get("anchor") or 0.0) if self._grid else 0.0
            # ★★ 2026-11-07：`flip`（用户手工的半句相位翻转）必须一起算进拍网，
            #   否则「引擎的八拍头」和「波形上的八拍线」差 4 拍（见 `_flip_beats`）。
            _fl = self._flip_beats()
            a += _fl * bl
            # ★★ 2026-10-28（用户报「左边空的没有八拍划分」）：
            #   `anchor` 是**某一处八拍头**，VDJ 的 Phase 常在曲子中段
            #   （实测这首歌 anchor=66.0s）⇒ 旧代码 `t = a` 只往后画，
            #   **前 66 秒一条线都没有**。拍网是周期性的，必须从 anchor 往回退到 ≤0 再画。
            #   ⚠ 序列生成抽到模块级 `grid_times()`（纯函数）以便回归测试覆盖。
            # ★★ 2026-10-28（多 BPM 段的歌）：分段时每段用各自 bpm/anchor，
            #   共用一条网格会把变过速的段落全画错（`grid_times_range`）。
            if _seg_list:
                pairs = []
                for _i, _sg in enumerate(_seg_list):
                    try:
                        _s0 = float(_sg.get("t0") or 0.0)
                        _s1 = (float(_seg_list[_i + 1].get("t0") or d)
                               if _i + 1 < len(_seg_list) else d)
                        _sbl = float(_sg.get("beat_len") or 0.0)
                        if _sbl <= 0:
                            _b = float(_sg.get("bpm") or 0.0)
                            _sbl = 60.0 / _b if _b > 0 else 0.0
                        _san = float(_sg.get("anchor")
                                     if _sg.get("anchor") is not None else _s0)
                    except (TypeError, ValueError):
                        continue
                    if _sbl <= 0 or _s1 <= _s0:
                        continue
                    pairs.extend(grid_times_range(_san + _fl * _sbl, _sbl,
                                                  _s0, _s1))
            else:
                _shift_t = None
                if bl > 0:
                    for _s in _esec:
                        try:
                            _t = float(_s.get("t0") or 0.0)
                        except (TypeError, ValueError):
                            continue
                        if _t <= 0.5:
                            continue
                        _q8 = (_t - a) / (8.0 * bl)
                        if abs(_q8 - round(_q8)) <= 0.12:
                            continue
                        _q4 = (_t - a) / (4.0 * bl)
                        if abs(_q4 - round(_q4)) <= 0.12:
                            _shift_t = _t
                            break
                if _shift_t is not None and _shift_t > 0.5:
                    pairs = list(grid_times_range(a, bl, 0.0, _shift_t))
                    pairs += list(grid_times_range(_shift_t, bl, _shift_t, d))
                else:
                    pairs = grid_times(a, bl, d)
            for _k, t in pairs:
                x = self._x_of_sec(t)
                if -2.0 <= x <= w + 2.0:
                    is_phrase = (_k % 8 == 0)
                    if draw_beat or (is_phrase and draw_phrase):
                        pen = QPen(QColor(120, 140, 150, 70) if not is_phrase
                                   else QColor(220, 230, 240, 120), 1)
                        p.setPen(pen)
                        p.drawLine(int(x), int(plot_top), int(x), int(plot_bot))

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
            # ★ 2026-10-28：右键改为「把坐标交给宿主弹菜单」（原来这里是删八拍头，
            #   但用户要求去掉手工八拍功能，右键腾出来做 BPM 分段编辑）。
            ev.accept()
            try:
                _g = ev.globalPosition().toPoint()
            except AttributeError:                               # Qt5 兼容
                _g = ev.globalPos()
            self.contextMenuRequested.emit(float(self._sec_of_x(x)), _g)
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
