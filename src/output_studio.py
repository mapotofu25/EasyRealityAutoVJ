# -*- coding: utf-8 -*-
"""多屏幕输出控制台（输出页）。

这个模块只做 UI + 数据编辑，不直接画输出画面：
  · 上方是棋盘格预览工作区：多路模式显示每块屏幕的独立预览框；单路模式显示整合画布+
    屏幕布局框。滚轮缩放、Ctrl/Shift/Alt+滚轮滚动、拖边框吸附、右键菜单。
  · 下方是每路输出的颜色卡片（列出该路使用的图层）。
  · 右侧是当前屏幕参数；复制/切分屏幕只允许改名称和输出开关，其余跟随源。

数据模型在 `output_model.py`，引擎合成/发送在 `engine.py`。
"""

import json
import os
import time

from PySide6.QtCore import Qt, QPoint, QPointF, QRect, QRectF, QSize, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QImage, QPainter,
                           QPen, QPixmap, QLinearGradient)
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
                               QFormLayout, QLabel, QPushButton, QComboBox,
                               QSpinBox, QDoubleSpinBox, QCheckBox, QLineEdit,
                               QScrollArea, QFrame, QListWidget, QListWidgetItem,
                               QColorDialog, QInputDialog, QFileDialog, QMessageBox,
                               QSizePolicy, QDialog, QDialogButtonBox, QGroupBox,
                               QAbstractItemView, QMenu, QToolButton)

import output_model as OM
import theme


# ---------------------------------------------------------------- 通用小工具
def _color(v, fallback="#7B2FF7"):
    c = QColor(str(v or fallback))
    return c if c.isValid() else QColor(fallback)


def _luma(c):
    return 0.299 * c.redF() + 0.587 * c.greenF() + 0.114 * c.blueF()


def _text_color(bg):
    return QColor("#ffffff") if _luma(bg) < 0.62 else QColor("#111111")


def _dash_pen(color, width=2, dash=Qt.DashLine):
    p = QPen(QColor(color), width)
    p.setStyle(dash)
    return p


class _CheckerTile:
    """棋盘格贴图（32×32，两个 16px 格）——只生成一次，按屏幕空间平铺。

    ★ 2026-11-09（用户第 4 条：「暗色主题下棋盘格底太亮」）：
      原来写死 `#f3f3f3 / #d7d7d7`（接近纯白）⇒ 暗色主题里整块预览区泛白，
      比周围面板亮一大截，很刺眼。现在按**当前主题**取两档：
        · 暗色主题 ⇒ 黑灰（`#2b2b2b / #232323`），只比 #151515 的底色亮一点点，
          既能看出"这是透明格子"，又不抢画面；
        · 亮色主题 ⇒ 保持原来的浅灰白，观感不变。
      ⚠ 主题切换后必须重画（缓存按主题名区分，见 `pixmap()`）。
    """

    _pm = None
    _key = ""
    #: (亮格, 暗格)
    PALETTE = {"dark": ("#2b2b2b", "#232323"),
               "light": ("#f3f3f3", "#d7d7d7")}

    @classmethod
    def pixmap(cls):
        try:
            key = "dark" if str(theme.current()) == "dark" else "light"
        except Exception:                                          # noqa: BLE001
            key = "light"
        if cls._pm is None or cls._key != key:
            a, b = cls.PALETTE.get(key, cls.PALETTE["light"])
            pm = QPixmap(32, 32)
            pm.fill(QColor(a))
            p = QPainter(pm)
            p.fillRect(0, 0, 16, 16, QColor(b))
            p.fillRect(16, 16, 16, 16, QColor(b))
            p.end()
            cls._pm = pm
            cls._key = key
        return cls._pm


# ---------------------------------------------------------------- 预览工作区
class OutputPreview(QWidget):
    """可缩放/平移/吸附的多屏预览工作区。"""

    action = Signal(str, str)       # (action, screen_id)
    geometry_changed = Signal()     # 拖动/缩放结束（保存配置）
    view_changed = Signal()         # 缩放/平移（保存 UI 状态）

    def __init__(self, studio, parent=None):
        super().__init__(parent)
        self.studio = studio
        self.setMinimumHeight(260)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setStyleSheet("background:#151515;")
        self.mode = "multi"
        self.screens = []
        self.thumbs = {}
        self.selected_id = ""
        self.zoom = 1.0
        self.pan = QPointF(0.0, 0.0)
        self.snap = True
        self._fit_pending = True
        self._drag = None
        self._drag_start = QPoint()
        self._drag_orig = None
        self._guides = []
        self._hover = None

    # ---------- 数据 ----------
    def set_context(self, mode, screens, cfg, selected_id=""):
        """喂数据。**`mode` 只记录、不再改变绘制方式**（S1 #2，用户第 2 条）。

        ★ 用户：「切模式预览区不许变样 —— 一换模式预览就变'整合画布+布局框'，看不懂」。
          所以预览**永远**按多路那套画（每屏一个独立预览框）；`mode` 只影响
          「输出画布」窗口和引擎输出。`cfg_mode` 保留原值以备查询。
        """
        self.cfg_mode = str(mode or "multi")
        self.mode = "multi"          # ⚠ 绘制/交互一律走多路分支（`_paint_multi`）
        self.screens = list(screens or [])
        self.cfg = cfg
        self.selected_id = str(selected_id or "")
        self._ensure_multi_rects()
        self.update()

    def set_thumbs(self, thumbs):
        self.thumbs = dict(thumbs or {})
        self.update()

    def set_selected(self, sid):
        self.selected_id = str(sid or "")
        self.update()

    def set_snap(self, on):
        self.snap = bool(on)
        self.update()

    def set_view_state(self, state):
        if not isinstance(state, dict):
            return
        try:
            z = float(state.get("zoom", 1.0))
            px, py = state.get("pan", [0.0, 0.0])
            if 0.08 <= z <= 8.0:
                self.zoom = z
                self.pan = QPointF(float(px), float(py))
                self._fit_pending = False
        except Exception:
            pass

    def view_state(self):
        return {"zoom": round(float(self.zoom), 4),
                "pan": [round(float(self.pan.x()), 2), round(float(self.pan.y()), 2)]}

    def _screen(self, sid):
        for s in self.screens:
            if s.get("id") == sid:
                return s
        return None

    def _source_frames(self):
        return [s for s in self.screens if s.get("type") in ("main", "independent")]

    def _ensure_multi_rects(self):
        """多路模式：给没有 preview_rect 的屏幕自动排一排（仅 UI，不影响输出）。"""
        if self.mode != "multi":
            return
        x = 30
        y = 30
        maxh = 0
        for s in self.screens:
            w, h = OM.effective_size(self.cfg, s)
            ar = (w / h) if h else 1.6
            pw = 420
            ph = max(120, int(round(pw / ar)))
            r = OM._normalize_preview_rect(s.get("preview_rect"))
            if r is None:
                r = [x, y, pw, ph]
                s["preview_rect"] = r
            x = r[0] + r[2] + 28
            maxh = max(maxh, r[3])
        # 不自动改用户已经摆好的位置；只给新屏幕排开

    def _world_rect(self):
        """预览工作区的世界包围盒。"""
        if self.mode == "single":
            cw, ch = OM.combined_size(self.cfg)
            return QRectF(0, 0, cw, ch)
        rects = []
        for s in self.screens:
            r = OM._normalize_preview_rect(s.get("preview_rect"))
            if r:
                rects.append(QRectF(*r))
        if not rects:
            return QRectF(0, 0, 640, 360)
        x0 = min(r.x() for r in rects)
        y0 = min(r.y() for r in rects)
        x1 = max(r.x() + r.width() for r in rects)
        y1 = max(r.y() + r.height() for r in rects)
        return QRectF(x0, y0, max(1, x1 - x0), max(1, y1 - y0))

    def fit_all(self):
        wr = self._world_rect()
        if wr.width() <= 1 or wr.height() <= 1 or self.width() <= 2 or self.height() <= 2:
            return
        z = min((self.width() - 48) / wr.width(), (self.height() - 48) / wr.height())
        self.zoom = max(0.05, min(8.0, z))
        self.pan = QPointF(self.width() / 2.0 - (wr.x() + wr.width() / 2.0) * self.zoom,
                           self.height() / 2.0 - (wr.y() + wr.height() / 2.0) * self.zoom)
        self._fit_pending = False
        self.view_changed.emit()
        self.update()

    def fit_screen(self, sid):
        if self.mode == "single":
            s = self._screen(sid)
            if s is None:
                return self.fit_all()
            x, y, w, h = OM.screen_rect(self.cfg, s)
            wr = QRectF(x, y, max(1, w), max(1, h))
        else:
            s = self._screen(sid)
            r = OM._normalize_preview_rect((s or {}).get("preview_rect"))
            if not r:
                return self.fit_all()
            wr = QRectF(*r)
        if wr.width() <= 1 or wr.height() <= 1:
            return
        z = min((self.width() - 48) / wr.width(), (self.height() - 48) / wr.height())
        self.zoom = max(0.05, min(8.0, z))
        self.pan = QPointF(self.width() / 2.0 - (wr.x() + wr.width() / 2.0) * self.zoom,
                           self.height() / 2.0 - (wr.y() + wr.height() / 2.0) * self.zoom)
        self._fit_pending = False
        self.view_changed.emit()
        self.update()

    def _to_screen(self, x, y):
        return QPointF(self.pan.x() + x * self.zoom, self.pan.y() + y * self.zoom)

    def _to_world(self, p):
        if self.zoom <= 0:
            return QPointF(0, 0)
        return QPointF((p.x() - self.pan.x()) / self.zoom,
                       (p.y() - self.pan.y()) / self.zoom)

    def _rect_screen(self, x, y, w, h):
        a = self._to_screen(x, y)
        return QRectF(a.x(), a.y(), max(1.0, w * self.zoom), max(1.0, h * self.zoom))

    # ---------- 吸附 ----------
    def _snap_targets(self, exclude_sid):
        rects = []
        if self.mode == "multi":
            for s in self.screens:
                if s.get("id") == exclude_sid:
                    continue
                r = OM._normalize_preview_rect(s.get("preview_rect"))
                if r:
                    rects.append(QRectF(*r))
        else:
            cw, ch = OM.combined_size(self.cfg)
            rects.append(QRectF(0, 0, cw, ch))
            for s in self._source_frames():
                if s.get("id") == exclude_sid:
                    continue
                x, y, w, h = OM.screen_rect(self.cfg, s)
                rects.append(QRectF(x, y, w, h))
        return rects

    def _snap_move(self, sid, x, y, w, h):
        """返回 (x, y, guides)。吸附到其它屏幕/画布边缘和中心线。"""
        self._guides = []
        if not self.snap:
            return x, y
        thr = 8.0 / max(0.05, self.zoom)
        best_x = best_y = None
        x_edges = [("l", x), ("c", x + w / 2.0), ("r", x + w)]
        y_edges = [("t", y), ("c", y + h / 2.0), ("b", y + h)]
        for tr in self._snap_targets(sid):
            tx = [tr.left(), tr.center().x(), tr.right()]
            ty = [tr.top(), tr.center().y(), tr.bottom()]
            for _, ex in x_edges:
                for tv in tx:
                    d = tv - ex
                    if abs(d) <= thr and (best_x is None or abs(d) < abs(best_x[0])):
                        best_x = (d, tv)
            for _, ey in y_edges:
                for tv in ty:
                    d = tv - ey
                    if abs(d) <= thr and (best_y is None or abs(d) < abs(best_y[0])):
                        best_y = (d, tv)
        if best_x is not None:
            x += best_x[0]
            self._guides.append(("v", best_x[1]))
        if best_y is not None:
            y += best_y[0]
            self._guides.append(("h", best_y[1]))
        return x, y

    # ---------- 命中测试 ----------
    def _frame_at(self, pos):
        """返回 (screen, kind) kind = frame / resize / split / split_edge。"""
        if self.mode == "multi":
            for s in reversed(self.screens):
                r = OM._normalize_preview_rect(s.get("preview_rect"))
                if not r:
                    continue
                sr = self._rect_screen(*r)
                # 右下角缩放把手
                hd = QRectF(sr.right() - 10, sr.bottom() - 10, 12, 12)
                if hd.contains(pos):
                    return s, "resize"
                if sr.contains(pos):
                    return s, "frame"
            return None, None
        # single：先判切分框（带边把手），再判源屏幕
        for s in reversed(self.screens):
            if s.get("type") != "split":
                continue
            x, y, w, h = OM.screen_rect(self.cfg, s)
            sr = self._rect_screen(x, y, w, h)
            if sr.adjusted(-5, -5, 5, 5).contains(pos):
                # 八方向边缘
                edge = ""
                if abs(pos.x() - sr.left()) <= 6: edge += "l"
                if abs(pos.x() - sr.right()) <= 6: edge += "r"
                if abs(pos.y() - sr.top()) <= 6: edge += "t"
                if abs(pos.y() - sr.bottom()) <= 6: edge += "b"
                return s, ("split_edge:" + edge) if edge else "split"
        for s in reversed(self._source_frames()):
            x, y, w, h = OM.screen_rect(self.cfg, s)
            sr = self._rect_screen(x, y, w, h)
            if sr.contains(pos):
                return s, "layout"
        return None, None

    # ---------- 交互 ----------
    def wheelEvent(self, ev):
        d = ev.angleDelta().y()
        if d == 0:
            return
        mods = ev.modifiers()
        step = d / 120.0
        if mods & Qt.ControlModifier:
            self.pan.setY(self.pan.y() + step * 48.0)
        elif mods & Qt.ShiftModifier:
            self.pan.setX(self.pan.x() + step * 48.0)
        elif mods & Qt.AltModifier:
            self.pan.setX(self.pan.x() + step * 36.0)
            self.pan.setY(self.pan.y() + step * 36.0)
        else:
            old = self._to_world(ev.position())
            nz = self.zoom * (1.12 ** step)
            nz = max(0.08, min(8.0, nz))
            self.zoom = nz
            self.pan = QPointF(ev.position().x() - old.x() * nz,
                               ev.position().y() - old.y() * nz)
        self._fit_pending = False
        self.view_changed.emit()
        self.update()
        ev.accept()

    def mousePressEvent(self, ev):
        pos = ev.position()
        if ev.button() == Qt.MiddleButton:
            self._drag = {"kind": "pan", "start": pos, "orig": QPointF(self.pan)}
            self.setCursor(Qt.ClosedHandCursor)
            return
        s, kind = self._frame_at(pos)
        if ev.button() == Qt.RightButton:
            if s is not None:
                self.selected_id = s.get("id", "")
                self.studio.select_screen(self.selected_id)
            self._show_menu(ev.globalPosition().toPoint(), s, kind)
            return
        if ev.button() != Qt.LeftButton:
            return
        if s is not None:
            self.selected_id = s.get("id", "")
            self.studio.select_screen(self.selected_id)
            self._drag_start = pos.toPoint()
            if kind == "resize":
                r = OM._normalize_preview_rect(s.get("preview_rect")) or [0, 0, 320, 180]
                self._drag = {"kind": "resize", "sid": s.get("id"),
                              "start": pos, "orig": list(r)}
            elif kind == "layout":
                x, y, w, h = OM.screen_rect(self.cfg, s)
                self._drag = {"kind": "layout", "sid": s.get("id"), "start": pos,
                              "orig": [x, y, w, h]}
            elif kind == "frame":
                r = OM._normalize_preview_rect(s.get("preview_rect")) or [0, 0, 320, 180]
                self._drag = {"kind": "move", "sid": s.get("id"), "start": pos,
                              "orig": list(r)}
            elif kind and kind.startswith("split"):
                x, y, w, h = OM.screen_rect(self.cfg, s)
                self._drag = {"kind": kind, "sid": s.get("id"), "start": pos,
                              "orig": [x, y, w, h],
                              "uv": list(s.get("uv") or [0, 0, 1, 1])}
            self.setCursor(Qt.ClosedHandCursor)
            self.update()
            return
        # 空白处 = 平移视图（用户确认的“前者”）
        self._drag = {"kind": "pan", "start": pos, "orig": QPointF(self.pan)}
        self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, ev):
        pos = ev.position()
        if self._drag is None:
            s, kind = self._frame_at(pos)
            if kind in ("resize",):
                self.setCursor(Qt.SizeFDiagCursor)
            elif kind and str(kind).startswith("split_edge"):
                self.setCursor(Qt.SizeAllCursor)
            elif s is not None:
                self.setCursor(Qt.SizeAllCursor)
            else:
                self.setCursor(Qt.ArrowCursor)
            return
        d = self._drag
        kind = d.get("kind")
        if kind == "pan":
            self.pan = d["orig"] + (pos - d["start"])
            self.view_changed.emit()
            self.update()
            return
        wd = self._to_world(pos) - self._to_world(d["start"])
        s = self._screen(d.get("sid"))
        if s is None:
            return
        if kind == "move":
            x, y, w, h = d["orig"]
            nx, ny = x + wd.x(), y + wd.y()
            if not (ev.modifiers() & Qt.AltModifier):
                nx, ny = self._snap_move(s.get("id"), nx, ny, w, h)
            else:
                self._guides = []
            s["preview_rect"] = [int(round(nx)), int(round(ny)), int(w), int(h)]
        elif kind == "resize":
            # ★ T3 #8（用户第 8 条 BUG）：拖右下角手柄原来会把屏幕拉成**非等比**
            #   （尤其 S6 给缩放加了吸附之后，宽高各吸各的）。现在：
            #   · 默认**等比**：按该屏原始宽高比推算另一轴（拖哪个轴多就以哪个轴为准）
            #   · 按住 **Shift** 才允许非等比
            #   · 吸附：等比模式只吸"右边"（吸完按比例重算高）；自由模式两边都吸
            x, y, w0, h0 = d["orig"]
            _sw, _sh = OM.effective_size(self.cfg, s)
            ar = (_sw / _sh) if _sh else ((w0 / h0) if h0 else 1.7778)
            free = bool(ev.modifiers() & Qt.ShiftModifier)
            if free:
                w = max(80, w0 + wd.x())
                h = max(45, h0 + wd.y())
            else:
                if abs(wd.y()) > abs(wd.x()):
                    h = max(45, h0 + wd.y())
                    w = max(80, h * ar)
                else:
                    w = max(80, w0 + wd.x())
                    h = max(45, w / ar)
            self._guides = []
            if self.snap and not (ev.modifiers() & Qt.AltModifier):
                thr = 8.0 / max(0.05, self.zoom)
                rx = x + w
                best_rx = None
                for tr in self._snap_targets(s.get("id")):
                    for tv in (tr.left(), tr.center().x(), tr.right()):
                        dd = tv - rx
                        if abs(dd) <= thr and (best_rx is None or abs(dd) < abs(best_rx[0])):
                            best_rx = (dd, tv)
                if best_rx is not None:
                    w = max(80, w + best_rx[0])
                    self._guides.append(("v", best_rx[1]))
                    if not free:
                        h = max(45, w / ar)      # 等比：宽变了高跟着变
                if free:
                    by = y + h
                    best_by = None
                    for tr in self._snap_targets(s.get("id")):
                        for tv in (tr.top(), tr.center().y(), tr.bottom()):
                            dd = tv - by
                            if abs(dd) <= thr and (best_by is None or abs(dd) < abs(best_by[0])):
                                best_by = (dd, tv)
                    if best_by is not None:
                        h = max(45, h + best_by[0])
                        self._guides.append(("h", best_by[1]))
            s["preview_rect"] = [int(x), int(y), int(w), int(h)]
        elif kind == "layout":
            x, y, w, h = d["orig"]
            nx, ny = x + wd.x(), y + wd.y()
            if not (ev.modifiers() & Qt.AltModifier):
                nx, ny = self._snap_move(s.get("id"), nx, ny, w, h)
            else:
                self._guides = []
            s["layout"] = [int(round(nx)), int(round(ny))]
        elif kind == "split":
            # 移动裁切窗：保持大小，改 UV 左上
            x, y, w, h = d["orig"]
            src = OM.source_screen(self.cfg, s)
            sx, sy, sw, sh = OM.screen_rect(self.cfg, src)
            uv = d["uv"]
            u0, v0, u1, v1 = uv
            cw, ch = u1 - u0, v1 - v0
            nu0 = max(0.0, min(1.0 - cw, u0 + wd.x() / max(1, sw)))
            nv0 = max(0.0, min(1.0 - ch, v0 + wd.y() / max(1, sh)))
            s["uv"] = [nu0, nv0, nu0 + cw, nv0 + ch]
        elif kind and str(kind).startswith("split_edge"):
            x, y, w, h = d["orig"]
            src = OM.source_screen(self.cfg, s)
            sx, sy, sw, sh = OM.screen_rect(self.cfg, src)
            u0, v0, u1, v1 = d["uv"]
            edge = kind.split(":", 1)[1]
            if "l" in edge: u0 = max(0.0, min(u1 - 0.002, u0 + wd.x() / max(1, sw)))
            if "r" in edge: u1 = min(1.0, max(u0 + 0.002, u1 + wd.x() / max(1, sw)))
            if "t" in edge: v0 = max(0.0, min(v1 - 0.002, v0 + wd.y() / max(1, sh)))
            if "b" in edge: v1 = min(1.0, max(v0 + 0.002, v1 + wd.y() / max(1, sh)))
            s["uv"] = [u0, v0, u1, v1]
        self.update()

    def mouseReleaseEvent(self, ev):
        if self._drag is not None:
            k = self._drag.get("kind")
            self._drag = None
            self._guides = []
            self.setCursor(Qt.ArrowCursor)
            if k != "pan":
                self.geometry_changed.emit()
            self.update()

    def mouseDoubleClickEvent(self, ev):
        s, _kind = self._frame_at(ev.position())
        if s is not None:
            self.fit_screen(s.get("id"))
        else:
            self.fit_all()

    def resizeEvent(self, ev):                                   # noqa: N802
        super().resizeEvent(ev)
        # 左上角那颗「当前屏幕」胶囊是 preview 的子控件，窗口一变就得重新贴角
        fn = getattr(self.studio, "layout_screen_badge", None)
        if callable(fn):
            fn()

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_0:
            self.fit_all()
        elif ev.key() == Qt.Key_1:
            self.zoom = 1.0
            self.view_changed.emit(); self.update()
        elif ev.key() in (Qt.Key_Plus, Qt.Key_Equal):
            self.zoom = min(8.0, self.zoom * 1.15)
            self.view_changed.emit(); self.update()
        elif ev.key() == Qt.Key_Minus:
            self.zoom = max(0.08, self.zoom / 1.15)
            self.view_changed.emit(); self.update()
        else:
            super().keyPressEvent(ev)

    # ---------- 右键 ----------
    def _show_menu(self, gp, s, kind):
        from PySide6.QtWidgets import QMenu
        m = QMenu(self)
        if s is not None:
            act = m.addAction("适应此屏幕")
            act.triggered.connect(lambda _=False, sid=s.get("id"): self.fit_screen(sid))
            sub = m.addMenu("预览大小")
            for label, w in (("小 50%", 220), ("中 100%", 420), ("大 150%", 620),
                             ("超大 200%", 820)):
                a = sub.addAction(label)
                a.triggered.connect(lambda _=False, sid=s.get("id"), ww=w:
                                    self.studio.set_preview_size(sid, ww))
            a = sub.addAction("适应内容")
            a.triggered.connect(lambda _=False, sid=s.get("id"):
                                self.studio.set_preview_size(sid, 0))
            sub2 = m.addMenu("画面适配")
            for key, label in (("fit", "适应"), ("fill", "填充"), ("1:1", "1:1")):
                a = sub2.addAction(label)
                a.triggered.connect(lambda _=False, sid=s.get("id"), k=key:
                                    self.studio.set_preview_fit(sid, k))
            m.addSeparator()
            a = m.addAction("重命名…")
            a.triggered.connect(lambda _=False, sid=s.get("id"): self.studio.rename_screen(sid))
            a = m.addAction("修改颜色…")
            t = s.get("type")
            a.setEnabled(t in ("main", "independent"))
            a.triggered.connect(lambda _=False, sid=s.get("id"): self.studio.change_color(sid))
            # ★ S3 #6：以前这里只 select_screen（什么都不弹）⇒ 用户会觉得"点了没反应"。
            #   现在真的打开屏幕设置对话框（复制屏不可改，会退化成选中并提示）。
            a = m.addAction("屏幕设置…")
            a.triggered.connect(lambda _=False, sid=s.get("id"):
                                self.studio.select_screen(sid) or self.studio.edit_screen(sid))
            m.addSeparator()
            # ★ S3 #6：删掉「复制此屏幕（跟随主屏）」入口（用户要求）
            a = m.addAction("删除屏幕")
            a.setEnabled(s.get("type") != "main")
            a.triggered.connect(lambda _=False, sid=s.get("id"): self.studio.delete_screen(sid))
        else:
            a = m.addAction("适应全部")
            a.triggered.connect(self.fit_all)
            m.addSeparator()
            a = m.addAction("新建屏幕…")
            a.triggered.connect(self.studio.add_screen)
        m.exec(gp)

    # ---------- 绘制 ----------
    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor("#151515"))
        p.fillRect(self.rect(), QBrush(_CheckerTile.pixmap()))
        if self._fit_pending:
            self._fit_pending = False
            QTimer.singleShot(0, self.fit_all)
        if self.mode == "single":
            self._paint_single(p)
        else:
            self._paint_multi(p)
        # 吸附参考线
        for kind, val in self._guides:
            p.setPen(QPen(QColor("#39c6ff"), 1, Qt.DashLine))
            if kind == "v":
                x = self._to_screen(val, 0).x()
                p.drawLine(QPointF(x, 0), QPointF(x, self.height()))
            else:
                y = self._to_screen(0, val).y()
                p.drawLine(QPointF(0, y), QPointF(self.width(), y))
        # 左下角缩放提示
        p.setPen(QColor(220, 220, 220, 180))
        p.setFont(QFont("Segoe UI", 8))
        p.drawText(8, self.height() - 8, "%d%%" % int(round(self.zoom * 100)))
        p.end()

    def _paint_multi(self, p):
        # ★ U6（用户第 6 条 BUG）：单路模式下**预览区是黑的**。根因：预览自 S1 #2 起
        #   一律按"每屏一个框"画（按屏 id 取缩略图），而**单路模式下引擎只产
        #   `__combined__` 一张缩略图** ⇒ 每块屏 `content=None` → `_paint_frame` 填黑。
        #   这里兜底：单路时从 `__combined__` 里按该屏 layout 占比**裁出属于它的那块**。
        comb = self.thumbs.get("__combined__")
        cw = ch = 0
        try:
            if self.cfg:
                cw, ch = OM.combined_size(self.cfg)
        except Exception:                                          # noqa: BLE001
            cw = ch = 0
        for s in self.screens:
            r = OM._normalize_preview_rect(s.get("preview_rect"))
            if not r:
                continue
            sr = self._rect_screen(*r)
            content = self.thumbs.get(s.get("id"))
            if (content is None and comb is not None and not comb.isNull()
                    and int(cw) > 0 and int(ch) > 0):
                try:
                    lx, ly, lw, lh = OM.screen_rect(self.cfg, s)
                    sx = max(0, int(round(lx * comb.width() / float(cw))))
                    sy = max(0, int(round(ly * comb.height() / float(ch))))
                    sw = max(1, int(round(lw * comb.width() / float(cw))))
                    sh = max(1, int(round(lh * comb.height() / float(ch))))
                    sw = max(1, min(sw, comb.width() - sx))
                    sh = max(1, min(sh, comb.height() - sy))
                    content = comb.copy(sx, sy, sw, sh)
                except Exception:                                  # noqa: BLE001
                    content = None
            self._paint_frame(p, s, sr, content=content)
        # 选中框
        s = self._screen(self.selected_id)
        if s is not None:
            r = OM._normalize_preview_rect(s.get("preview_rect"))
            if r:
                sr = self._rect_screen(*r)
                p.setPen(_dash_pen("#ffffff", 2))
                p.setBrush(Qt.NoBrush)
                p.drawRect(sr.adjusted(-2, -2, 2, 2))

    def _paint_single(self, p):
        cw, ch = OM.combined_size(self.cfg)
        canvas = self._rect_screen(0, 0, cw, ch)
        # 整合画布黑底 + 画面
        p.fillRect(canvas, QColor("#000000"))
        p.setPen(QPen(QColor("#555555"), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(canvas)
        thumb = self.thumbs.get("__combined__")
        if thumb is not None and not thumb.isNull():
            p.drawImage(canvas, thumb, QRectF(0, 0, thumb.width(), thumb.height()))
        # 源屏幕布局框
        for s in self._source_frames():
            x, y, w, h = OM.screen_rect(self.cfg, s)
            sr = self._rect_screen(x, y, w, h)
            self._paint_frame(p, s, sr, content=None, transparent_center=True)
        # 复制/切分标记
        for s in self.screens:
            if s.get("type") == "copy":
                x, y, w, h = OM.screen_rect(self.cfg, s)
                sr = self._rect_screen(x, y, w, h)
                p.setPen(_dash_pen(OM.effective_color(self.cfg, s), 2, Qt.DotLine))
                p.drawRect(sr.adjusted(4, 4, -4, -4))
                self._label(p, s, sr.adjusted(6, 6, -6, -6))
            elif s.get("type") == "split":
                x, y, w, h = OM.screen_rect(self.cfg, s)
                sr = self._rect_screen(x, y, w, h)
                p.fillRect(sr, QColor(0, 0, 0, 38))
                p.setPen(_dash_pen(OM.effective_color(self.cfg, s), 2))
                p.drawRect(sr)
                # 四角/四边把手
                p.setBrush(QColor("#ffffff"))
                for q in (sr.topLeft(), sr.topRight(), sr.bottomLeft(), sr.bottomRight(),
                          QPointF(sr.center().x(), sr.top()),
                          QPointF(sr.center().x(), sr.bottom()),
                          QPointF(sr.left(), sr.center().y()),
                          QPointF(sr.right(), sr.center().y())):
                    p.drawRect(QRectF(q.x() - 3, q.y() - 3, 6, 6))
                p.setBrush(Qt.NoBrush)
                self._label(p, s, sr)
        s = self._screen(self.selected_id)
        if s is not None:
            if s.get("type") in ("main", "independent"):
                x, y, w, h = OM.screen_rect(self.cfg, s)
            else:
                x, y, w, h = OM.screen_rect(self.cfg, s)
            sr = self._rect_screen(x, y, w, h)
            p.setPen(_dash_pen("#ffffff", 2))
            p.drawRect(sr.adjusted(-2, -2, 2, 2))

    def _paint_frame(self, p, s, sr, content=None, transparent_center=False):
        col = _color(OM.effective_color(self.cfg, s))
        if not transparent_center:
            p.fillRect(sr, QColor("#000000"))
            if content is not None and not content.isNull():
                mode = str(s.get("preview_fit") or "fit")
                iw, ih = content.width(), content.height()
                if iw > 0 and ih > 0:
                    if mode == "fill":
                        sc = max(sr.width() / iw, sr.height() / ih)
                    elif mode == "1:1":
                        sc = 1.0
                    else:
                        sc = min(sr.width() / iw, sr.height() / ih)
                    tw, th = iw * sc, ih * sc
                    tgt = QRectF(sr.center().x() - tw / 2, sr.center().y() - th / 2, tw, th)
                    p.save()
                    p.setClipRect(sr)
                    p.drawImage(tgt, content, QRectF(0, 0, iw, ih))
                    p.restore()
        if transparent_center:
            p.setPen(QPen(col, 2))
        else:
            p.setPen(QPen(col, 2))
        p.setBrush(Qt.NoBrush)
        p.drawRect(sr)
        # 标题条
        h = 18
        bar = QRectF(sr.x(), sr.y(), min(sr.width(), 260), h)
        bg = QColor(col)
        bg.setAlpha(210)
        p.fillRect(bar, bg)
        p.setPen(_text_color(bg))
        p.setFont(QFont("Microsoft YaHei", 8, QFont.Bold))
        p.drawText(bar.adjusted(5, 0, -4, 0), Qt.AlignVCenter | Qt.AlignLeft,
                   str(s.get("name") or ""))
        # 右下角分辨率/类型
        txt = "%dx%d · %s" % (OM.effective_size(self.cfg, s)[0],
                              OM.effective_size(self.cfg, s)[1],
                              OM.TYPE_LABELS.get(s.get("type"), s.get("type")))
        p.setFont(QFont("Segoe UI", 7))
        p.setPen(QColor(255, 255, 255, 220))
        p.drawText(sr.adjusted(5, 0, -5, -4), Qt.AlignRight | Qt.AlignBottom, txt)
        # 缩放把手（多路预览框）
        if not transparent_center:
            p.fillRect(QRectF(sr.right() - 9, sr.bottom() - 9, 9, 9), col)

    def _label(self, p, s, sr):
        p.setFont(QFont("Microsoft YaHei", 8, QFont.Bold))
        p.setPen(QColor(255, 255, 255, 230))
        p.drawText(sr.adjusted(4, 2, -4, -2), Qt.AlignLeft | Qt.AlignTop,
                   str(s.get("name") or ""))


# ---------------------------------------------------------------- 屏幕卡片
class ScreenCard(QFrame):
    clicked = Signal(str)
    action = Signal(str, str)

    def __init__(self, studio, screen, parent=None):
        super().__init__(parent)
        self.studio = studio
        self.screen = screen
        self.cfg = studio.active_config()
        self.setMinimumSize(190, 150)
        self.setMaximumHeight(210)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)

    def set_selected(self, on):
        self.setProperty("selected", bool(on))
        self.style().unpolish(self); self.style().polish(self); self.update()

    def _menu(self, pos):
        from PySide6.QtWidgets import QMenu
        m = QMenu(self)
        a = m.addAction("选中此屏幕")
        a.triggered.connect(lambda _=False: self.studio.select_screen(self.screen.get("id")))
        a = m.addAction("适应此屏幕")
        a.triggered.connect(lambda _=False: self.studio.preview.fit_screen(self.screen.get("id")))
        sub = m.addMenu("预览大小")
        for label, w in (("小 50%", 220), ("中 100%", 420), ("大 150%", 620),
                         ("超大 200%", 820)):
            a = sub.addAction(label)
            a.triggered.connect(lambda _=False, sid=self.screen.get("id"), ww=w:
                                self.studio.set_preview_size(sid, ww))
        a = sub.addAction("适应内容")
        a.triggered.connect(lambda _=False, sid=self.screen.get("id"):
                            self.studio.set_preview_size(sid, 0))
        m.addSeparator()
        a = m.addAction("重命名…")
        a.triggered.connect(lambda _=False: self.studio.rename_screen(self.screen.get("id")))
        a = m.addAction("修改颜色…")
        a.setEnabled(self.screen.get("type") in ("main", "independent"))
        a.triggered.connect(lambda _=False: self.studio.change_color(self.screen.get("id")))
        m.addSeparator()
        # ★ S3 #6：删掉「复制此屏幕（跟随主屏）」入口（用户要求）
        a = m.addAction("删除屏幕")
        a.setEnabled(self.screen.get("type") != "main")
        a.triggered.connect(lambda _=False: self.studio.delete_screen(self.screen.get("id")))
        m.exec(self.mapToGlobal(pos))

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.studio.select_screen(self.screen.get("id"))
        super().mousePressEvent(ev)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        col = _color(OM.effective_color(self.cfg, self.screen))
        bg = QColor(col); bg.setAlpha(215 if self.property("selected") else 175)
        p.fillRect(self.rect().adjusted(1, 1, -1, -1), bg)
        p.setPen(_dash_pen("#ffffff", 2) if self.property("selected") else QPen(col, 2))
        p.drawRect(self.rect().adjusted(1, 1, -2, -2))
        tcol = _text_color(bg)
        p.setPen(tcol)
        p.setFont(QFont("Microsoft YaHei", 10, QFont.Bold))
        p.drawText(self.rect().adjusted(10, 8, -10, 0), Qt.AlignLeft | Qt.AlignTop,
                   str(self.screen.get("name") or ""))
        p.setFont(QFont("Segoe UI", 8))
        w, h = OM.effective_size(self.cfg, self.screen)
        sub = "%s · %dx%d" % (OM.TYPE_LABELS.get(self.screen.get("type"), ""), w, h)
        p.drawText(self.rect().adjusted(10, 30, -10, 0), Qt.AlignLeft | Qt.AlignTop, sub)
        # 图层 chips
        chips = self.studio.layer_names_for(self.screen)
        x = 10
        y = 54
        p.setFont(QFont("Microsoft YaHei", 8))
        for name in chips[:8]:
            fm = p.fontMetrics()
            tw = fm.horizontalAdvance(name) + 12
            if x + tw > self.width() - 10:
                x = 10; y += 20
            if y > self.height() - 40:
                break
            c2 = QColor(255, 255, 255, 54)
            p.fillRect(QRectF(x, y, tw, 17), c2)
            p.setPen(tcol)
            p.drawText(QRectF(x + 6, y, tw - 8, 17), Qt.AlignVCenter | Qt.AlignLeft, name)
            x += tw + 5
        # 输出标记
        p.setFont(QFont("Segoe UI", 8))
        outs = []
        if self.screen.get("spout"):
            outs.append("Spout")
        if self.screen.get("ndi"):
            outs.append("NDI")
        if self.screen.get("type") == "copy":
            outs.append("跟随主屏")
        elif self.screen.get("type") == "split":
            outs.append("切分源屏")
        p.drawText(self.rect().adjusted(10, 0, -10, -8), Qt.AlignRight | Qt.AlignBottom,
                   " · ".join(outs) if outs else "未启用输出")
        p.end()


# ---------------------------------------------------------------- 新建屏幕对话框
class ScreenDialog(QDialog):
    """新建屏幕。

    ★ 2026-11-09（用户第 9 条）：
      ① **类型只留「独立屏幕 / 复制屏幕」** —— 「切分」删掉（切分语义并入复制：
         复制出来的屏就是"跟着一块源屏走"的屏，不再让用户先选切分再拖 UV）；
      ② 选「独立屏幕」时**不显示「源屏幕」这一行**（独立屏没有源，摆一行只会误导）；
      ③ **分辨率对独立屏可手改**（以前跟着"跟随主屏"联动，看起来像只读）。
    """

    def __init__(self, studio, source_screen=None, forced_type=None, parent=None,
                 edit_screen=None):
        super().__init__(parent or studio)
        self.studio = studio
        self.cfg = studio.active_config()
        self.edit_screen = edit_screen
        self.setWindowTitle("屏幕设置" if edit_screen is not None else "新建屏幕")
        self.setMinimumWidth(420)
        form = QFormLayout(self)
        self.name = QLineEdit()
        form.addRow("名称", self.name)
        self.type = QComboBox()
        # ★ S3 #5（用户第 5 条）：把「切分屏幕」加回来（三种类型都在）。
        # ★ U4（用户第 4 条）：只留 **独立屏幕 / 切分屏幕**——"切分 UV 全幅"就等于复制。
        #   老配置里的 `copy` 屏由 output_model 迁移成 split（UV 全幅、源=主屏），不会变砖。
        items = [("independent", "独立屏幕"), ("split", "切分屏幕（取源屏的一块）")]
        for k, label in items:
            self.type.addItem(label, k)
        form.addRow("类型", self.type)
        self.source = QComboBox()
        self._fill_sources()
        self.lbl_source = QLabel("源屏幕")
        form.addRow(self.lbl_source, self.source)
        self.w = QSpinBox(); self.w.setRange(16, 7680); self.w.setValue(1920)
        self.h = QSpinBox(); self.h.setRange(16, 4320); self.h.setValue(1080)
        try:
            _m = OM.main_screen(self.cfg)
            if _m is not None:
                _mw, _mh = OM.effective_size(self.cfg, _m)
                self.w.setValue(_mw); self.h.setValue(_mh)
        except Exception:
            pass
        row = QHBoxLayout(); row.addWidget(self.w); row.addWidget(QLabel("×")); row.addWidget(self.h)
        form.addRow("分辨率", row)
        self.color = QPushButton("选择颜色…")
        self.color.setStyleSheet("text-align:left;")
        self._color = QColor(OM.SCREEN_PALETTE[1])
        try:
            used = {str(s.get("color") or "") for s in (self.cfg.get("screens") or [])}
            for _c in OM.SCREEN_PALETTE:
                if _c not in used:
                    self._color = QColor(_c)
                    break
        except Exception:
            pass
        self.color.clicked.connect(self._pick_color)
        self._sync_color_btn()
        form.addRow("边框颜色", self.color)
        # ★ 2026-11-09（用户否掉"使用的图层"勾选那套）：「每一个图层、每一个屏幕的图层
        #   自己建」⇒ 建层时按块归属（见 ui_main.layer_add(target_sid=…)），这里不再需要
        #   勾选组。相关控件/回填逻辑整体删除，别留个能改归属的后门。
        self.uv = None
        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color:#888;")
        form.addRow("", self.hint)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        if forced_type:
            idx = self.type.findData(forced_type)
            if idx >= 0: self.type.setCurrentIndex(idx)
        # ★ 用户第 8 条的补偿入口：多屏模式下「⋯」按钮打开本对话框**编辑已有屏幕**
        #   （名称 / 分辨率 / 边框颜色）。类型不给改：改类型会破坏 source/uv/mapping 关系。
        if edit_screen is not None:
            _s = edit_screen
            self.name.setText(str(_s.get("name") or ""))
            _ti = self.type.findData(str(_s.get("type") or "independent"))
            if _ti >= 0:
                self.type.setCurrentIndex(_ti)
            self.type.setEnabled(False)
            try:
                _w, _h = OM.effective_size(self.cfg, _s)
                self.w.setValue(int(_w))
                self.h.setValue(int(_h))
            except Exception:                                      # noqa: BLE001
                pass
            self._color = _color(OM.effective_color(self.cfg, _s))
            self._sync_color_btn()
        self.type.currentIndexChanged.connect(self._sync_fields)
        self.source.currentIndexChanged.connect(self._sync_fields)
        self.w.valueChanged.connect(self._sync_fields)
        self._sync_fields()

    def apply_to(self, screen):
        """把对话框里的值写回**已有**屏幕（`⋯` 编辑模式）。返回该屏字典。"""
        v = self.values()
        t = str(screen.get("type") or "")
        if v["name"]:
            screen["name"] = v["name"]
        if t in ("main", "independent"):
            screen["width"] = max(16, int(v["width"]))
            screen["height"] = max(16, int(v["height"]))
            if t == "independent":
                screen["follow_main"] = False     # 手填过尺寸 ⇒ 不再跟随主屏
        # （原"写回勾选结果"那段已删：图层的屏幕归属由「+ 图层」在建立时决定）
            screen["color"] = v["color"]
        return screen

    def _fill_sources(self):
        self.source.clear()
        cfg = self.cfg
        for s in cfg.get("screens") or []:
            if s.get("type") in ("main", "independent"):
                self.source.addItem(s.get("name") or s.get("id"), s.get("id"))
        main = OM.main_screen(cfg)
        if main is not None:
            self.source.setCurrentIndex(max(0, self.source.findData(main.get("id"))))

    def _pick_color(self):
        c = QColorDialog.getColor(self._color, self, "选择屏幕边框颜色")
        if c.isValid():
            self._color = c
            self._sync_color_btn()

    def _sync_color_btn(self):
        self.color.setText(self._color.name().upper())
        self.color.setStyleSheet(
            "text-align:left;background:%s;color:%s;" %
            (self._color.name(), "#fff" if _luma(self._color) < 0.6 else "#111"))

    def _sync_fields(self):
        t = self.type.currentData()
        src = OM.screen_by_id(self.cfg, self.source.currentData())
        is_ind = t == "independent"
        is_copy = t == "copy"
        is_split = t == "split"
        # ★ S3 #5：**三种类型都能手改分辨率**（用户要求）；复制屏填了也会被主屏尺寸覆盖，
        #   但输入框本身不再置灰（以前灰着让人以为不能改）。
        self.w.setEnabled(True)
        self.h.setEnabled(True)
        # ★ 用户第 9 条：只有「复制屏幕」才需要选源屏幕；独立屏把这一行整行藏掉。
        _need_src = bool(is_copy or is_split)
        try:
            self.lbl_source.setVisible(_need_src)
        except Exception:                                          # noqa: BLE001
            pass
        self.source.setVisible(_need_src)
        self.source.setEnabled(_need_src and src is not None)
        if is_copy:
            self.hint.setText("复制屏幕只跟随主屏幕：没有独立图层，内容/分辨率/颜色/性能全部跟主屏，名称可改。")
        elif is_split:
            self.hint.setText("切分屏幕取源屏幕的一块：创建后在右侧「屏幕设置」里填 UV（左/上/右/下），"
                              "或直接拖动裁切框；图层与分辨率跟随源屏。")
        else:
            self.hint.setText("独立屏幕初始图层为空、不显示任何图层，创建后可单独修改分辨率/颜色/图层。")
        if is_ind:
            main = OM.main_screen(self.cfg)
            # 初始值已在 __init__ 里设成主屏尺寸；这里不再覆盖用户输入
            if main is not None and self.w.value() <= 0:
                self.w.setValue(OM.effective_size(self.cfg, main)[0])
                self.h.setValue(OM.effective_size(self.cfg, main)[1])
        if is_copy:
            self.source.blockSignals(True)
            main = OM.main_screen(self.cfg)
            if main is not None:
                self.source.setCurrentIndex(max(0, self.source.findData(main.get("id"))))
            self.source.blockSignals(False)

    def _ok(self):
        name = self.name.text().strip()
        if not name:
            QMessageBox.warning(self, "名称不能为空", "请给屏幕起一个名字。")
            return
        self.accept()

    def values(self):
        return {
            "name": self.name.text().strip(),
            "type": self.type.currentData(),
            "source_id": self.source.currentData() or "",
            "width": self.w.value(),
            "height": self.h.value(),
            "color": self._color.name(),
        }


# ---------------------------------------------------------------- 右侧设置
class ScreenSettingsPanel(QWidget):
    changed = Signal()

    def __init__(self, studio, parent=None):
        super().__init__(parent)
        self.studio = studio
        self._loading = False
        self.screen = None
        self.setMinimumWidth(300)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(2, 2, 2, 2)
        outer.setSpacing(4)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        body = QWidget(); scroll.setWidget(body)
        lay = QVBoxLayout(body); lay.setContentsMargins(2, 2, 2, 2); lay.setSpacing(6)
        outer.addWidget(scroll, 1)

        g = QGroupBox("屏幕设置")
        f = QFormLayout(g)
        self.name = QLineEdit()
        self.name.editingFinished.connect(self._on_name)
        f.addRow("名称", self.name)
        self.type = QLabel("-")
        f.addRow("类型", self.type)
        self.source = QComboBox()
        self.source.currentIndexChanged.connect(self._on_any)
        f.addRow("源屏幕", self.source)
        self.w = QSpinBox(); self.w.setRange(16, 7680)
        self.h = QSpinBox(); self.h.setRange(16, 4320)
        # ★ 用户第 9 条：分辨率单独走 `_on_size`（手改 = 要独立尺寸 ⇒ 顺带取消「跟随主屏」），
        #   不能再和 spout/ndi 那个 `_on_any` 绑在一起。
        self.w.valueChanged.connect(self._on_size)
        self.h.valueChanged.connect(self._on_size)
        row = QHBoxLayout(); row.addWidget(self.w); row.addWidget(QLabel("×")); row.addWidget(self.h)
        f.addRow("分辨率", row)
        self.color = QPushButton("选择颜色…")
        self.color.clicked.connect(self._on_color)
        f.addRow("边框颜色", self.color)
        lay.addWidget(g)

        g2 = QGroupBox("使用的图层")
        v2 = QVBoxLayout(g2)
        self.layers = QListWidget()
        self.layers.setMaximumHeight(150)
        self.layers.itemChanged.connect(self._on_layers)
        v2.addWidget(self.layers)
        self.layers_hint = QLabel("")
        self.layers_hint.setWordWrap(True)
        self.layers_hint.setStyleSheet("color:#888;")
        v2.addWidget(self.layers_hint)
        lay.addWidget(g2)

        g3 = QGroupBox("输出路由")
        v3 = QVBoxLayout(g3)
        self.chk_spout = QCheckBox("Spout 输出（本地）")
        self.chk_ndi = QCheckBox("NDI 输出（局域网）")
        self.chk_spout.toggled.connect(self._on_any)
        self.chk_ndi.toggled.connect(self._on_any)
        v3.addWidget(self.chk_spout); v3.addWidget(self.chk_ndi)
        self.out_name = QLabel("")
        self.out_name.setWordWrap(True)
        self.out_name.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v3.addWidget(self.out_name)
        lay.addWidget(g3)

        g4 = QGroupBox("性能")
        v4 = QVBoxLayout(g4)
        self.chk_follow = QCheckBox("跟随主屏幕设置")
        self.chk_follow.setToolTip(
            "仅多屏多路模式出现。勾选后本屏分辨率/性能跟随主屏幕；\n"
            "若图层集合也与主屏一致，引擎会直接复用主屏合成结果，最省 CPU。")
        self.chk_follow.toggled.connect(self._on_any)
        v4.addWidget(self.chk_follow)
        self.perf_hint = QLabel("")
        self.perf_hint.setWordWrap(True)
        self.perf_hint.setStyleSheet("color:#888;")
        v4.addWidget(self.perf_hint)
        lay.addWidget(g4)

        g5 = QGroupBox("位置 / mapping")
        self.g5 = g5
        v5 = QGridLayout(g5)
        self.lx = QSpinBox(); self.ly = QSpinBox()
        self.lx.setRange(-20000, 20000); self.ly.setRange(-20000, 20000)
        self.lx.valueChanged.connect(self._on_layout)
        self.ly.valueChanged.connect(self._on_layout)
        v5.addWidget(QLabel("布局 X"), 0, 0); v5.addWidget(self.lx, 0, 1)
        v5.addWidget(QLabel("布局 Y"), 0, 2); v5.addWidget(self.ly, 0, 3)
        self.uv = []
        for i, lab in enumerate(("左 U0", "上 V0", "右 U1", "下 V1")):
            sp = QDoubleSpinBox(); sp.setRange(0.0, 1.0); sp.setDecimals(4); sp.setSingleStep(0.001)
            sp.valueChanged.connect(self._on_uv)
            self.uv.append(sp)
            v5.addWidget(QLabel(lab), 1 + i // 2, (i % 2) * 2)
            v5.addWidget(sp, 1 + i // 2, (i % 2) * 2 + 1)
        self.btn_export = QPushButton("导出 mapping（JSON + PNG）")
        self.btn_export.clicked.connect(self.studio.export_mapping)
        v5.addWidget(self.btn_export, 4, 0, 1, 4)
        self.map_hint = QLabel("")
        self.map_hint.setWordWrap(True)
        self.map_hint.setStyleSheet("color:#888;")
        v5.addWidget(self.map_hint, 5, 0, 1, 4)
        lay.addWidget(g5)
        lay.addStretch(1)

    def set_screen(self, screen, cfg, mode):
        self._loading = True
        self.screen = screen
        self.cfg = cfg
        self.mode = mode
        t = (screen or {}).get("type")
        self.type.setText(OM.TYPE_LABELS.get(t, "-"))
        self.name.setText((screen or {}).get("name") or "")
        self.name.setEnabled(bool(screen) and not (mode == "single" and t == "main"))
        self.source.blockSignals(True)
        self.source.clear()
        for s in cfg.get("screens") or []:
            if s.get("type") in ("main", "independent"):
                self.source.addItem(s.get("name") or s.get("id"), s.get("id"))
        self.source.blockSignals(False)
        if screen:
            idx = self.source.findData(screen.get("source_id"))
            self.source.setCurrentIndex(max(0, idx))
        self.source.setEnabled(t == "split")
        w, h = OM.effective_size(cfg, screen) if screen else (0, 0)
        self.w.setValue(w); self.h.setValue(h)
        # ★ 用户第 9 条（「独立屏幕的分辨率像是只读的」）：独立屏**始终可改**分辨率。
        #   以前"独立屏 + 跟随主屏"会把这两个框置灰 ⇒ 用户以为改不了；现在可改，
        #   一改就自动取消「跟随主屏」（见 `_on_size`）。
        editable_size = bool(screen) and t in ("main", "independent")
        follow = bool((screen or {}).get("follow_main", False))
        self.w.setEnabled(editable_size); self.h.setEnabled(editable_size)
        col = OM.effective_color(cfg, screen) if screen else "#7B2FF7"
        self.color.setText(str(col).upper())
        self.color.setEnabled(t in ("main", "independent"))
        c = _color(col)
        self.color.setStyleSheet(
            "text-align:left;background:%s;color:%s;" %
            (c.name(), "#fff" if _luma(c) < 0.6 else "#111"))
        self.layers.clear()
        all_layers = self.studio.main.engine.layers
        ids = set(OM.effective_layers(cfg, screen, [l.lid for l in all_layers])) if screen else set()
        for l in all_layers:
            it = QListWidgetItem(l.name)
            it.setData(Qt.UserRole, l.lid)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if l.lid in ids else Qt.Unchecked)
            self.layers.addItem(it)
        editable_layers = bool(screen) and t == "independent"
        self.layers.setEnabled(editable_layers)
        self.layers_hint.setText("" if editable_layers else
                                 "复制/切分/主屏不单独选图层：复制跟随主屏，切分跟随源屏。")
        self.chk_spout.setChecked(bool((screen or {}).get("spout")))
        self.chk_ndi.setChecked(bool((screen or {}).get("ndi")))
        self.chk_spout.setEnabled(bool(screen)); self.chk_ndi.setEnabled(bool(screen))
        self.chk_follow.setVisible(mode == "multi" and t == "independent")
        self.chk_follow.setChecked(follow)
        self.chk_follow.setEnabled(t == "independent")
        self.perf_hint.setText("勾选后分辨率/性能跟随主屏幕；图层集合仍可单独选择。"
                               if mode == "multi" and t == "independent" else "")
        # 名称联动输出
        try:
            names = OM.screen_output_names(cfg)
            nm = names.get((screen or {}).get("id"), (screen or {}).get("name") or "")
            self.out_name.setText("发送端名称：%s" % nm)
        except Exception:
            self.out_name.setText("")
        # 位置/UV
        single = mode == "single"
        self.lx.setEnabled(single and t in ("main", "independent"))
        self.ly.setEnabled(single and t in ("main", "independent"))
        lay = (screen or {}).get("layout") or [0, 0]
        self.lx.setValue(int(lay[0])); self.ly.setValue(int(lay[1]))
        uv = (screen or {}).get("uv") or [0, 0, 1, 1]
        for i, sp in enumerate(self.uv):
            sp.setValue(float(uv[i]))
            sp.setEnabled(single and t == "split")
        self.btn_export.setVisible(single)
        try:
            self.g5.setVisible(single)
        except Exception:
            pass
        self.map_hint.setText(
            "单路模式：切分屏幕可直接拖动裁切框或在这里输入 UV；导出 JSON+PNG。"
            if single else "多路模式不显示 mapping；每块屏幕按自身分辨率独立输出。")
        self._loading = False

    def _on_name(self):
        if self._loading or not self.screen:
            return
        t = self.screen.get("type")
        if self.mode == "single" and t == "main":
            return
        nm = self.name.text().strip()
        if nm:
            self.screen["name"] = nm
            self.changed.emit()
            self.studio.refresh_all()
            self.studio.apply()

    def _on_size(self, *_):
        """改了分辨率（用户第 9 条）：独立屏手改尺寸 ⇒ 自动取消「跟随主屏」。

        不取消的话下一次 `apply()` 会强行把宽高镜像回主屏 ⇒ 用户会觉得"改了没反应"。
        """
        if self._loading or not self.screen:
            return
        s = self.screen
        t = s.get("type")
        if t not in ("main", "independent"):
            return
        s["width"] = max(16, self.w.value())
        s["height"] = max(16, self.h.value())
        if t == "independent" and self.mode == "multi":
            s["follow_main"] = False
            try:
                self.chk_follow.blockSignals(True)
                self.chk_follow.setChecked(False)
                self.chk_follow.blockSignals(False)
            except Exception:                                      # noqa: BLE001
                pass
        self.changed.emit()
        self.studio.apply()

    def _on_any(self, *_):
        if self._loading or not self.screen:
            return
        s = self.screen
        t = s.get("type")
        if t in ("main", "independent"):
            s["width"] = self.w.value(); s["height"] = self.h.value()
        s["spout"] = bool(self.chk_spout.isChecked())
        s["ndi"] = bool(self.chk_ndi.isChecked())
        if t == "independent" and self.mode == "multi":
            s["follow_main"] = bool(self.chk_follow.isChecked())
            if s["follow_main"]:
                main = OM.main_screen(self.cfg)
                if main is not None:
                    s["width"], s["height"] = OM.effective_size(self.cfg, main)
        if t == "split":
            s["source_id"] = self.source.currentData() or s.get("source_id")
            s["uv"] = [self.uv[0].value(), self.uv[1].value(),
                       max(self.uv[0].value() + 0.002, self.uv[2].value()),
                       max(self.uv[1].value() + 0.002, self.uv[3].value())]
        self.changed.emit()
        self.studio.apply()

    def _on_layout(self, *_):
        if self._loading or not self.screen:
            return
        if self.mode != "single":
            return
        if self.screen.get("type") in ("main", "independent"):
            self.screen["layout"] = [self.lx.value(), self.ly.value()]
            self.studio.apply()

    def _on_uv(self, *_):
        if self._loading or not self.screen or self.screen.get("type") != "split":
            return
        self.screen["uv"] = [self.uv[0].value(), self.uv[1].value(),
                             max(self.uv[0].value() + 0.002, self.uv[2].value()),
                             max(self.uv[1].value() + 0.002, self.uv[3].value())]
        self.studio.apply()

    def _on_layers(self, item):
        if self._loading or not self.screen or self.screen.get("type") != "independent":
            return
        ids = []
        for i in range(self.layers.count()):
            it = self.layers.item(i)
            if it.checkState() == Qt.Checked:
                ids.append(it.data(Qt.UserRole))
        self.screen["layers"] = ids
        self.studio.apply()

    def _on_color(self):
        if not self.screen or self.screen.get("type") not in ("main", "independent"):
            return
        c = QColorDialog.getColor(_color(self.screen.get("color")), self, "选择屏幕边框颜色")
        if c.isValid():
            self.screen["color"] = c.name()
            self.changed.emit()
            self.studio.refresh_all()
            self.studio.apply()


# ---------------------------------------------------------------- 输出页
class OutputStudio(QWidget):
    """多屏输出页：预览工作区 + 顶部控制条 + 右侧屏幕设置。

    ★ 2026-11-09：顶部控制条被包成 `self.top_bar`（一个 QWidget），多屏模式下由
      `ui_main` 搬进页签行；`open_library` 信号用于「打开素材库」独立窗口
      （多屏模式下不再内嵌素材库，用户第 5 条）。
    """

    open_library = Signal()          # 请求打开独立素材库窗口（ui_main 接）
    selection_changed = Signal(str)  # 选中的屏幕变了（多屏模式下右侧设置要跟着换作用域）

    def __init__(self, main, parent=None, compact=False):
        super().__init__(parent)
        self.main = main
        self.cfg = main.cfg
        self.compact = bool(compact)
        self._loading = False
        self.active_id = ""
        self.selected_id = ""
        self._preview_targets = []
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)

        # ---- 顶部条 ----
        # ★ 2026-11-09（用户第 1 条：「把这条多屏控制条挪到页签行去」）：
        #   整条控制条包进一个 **QWidget**（`self.top_bar`）而不是直接 addLayout。
        #   理由：多屏模式下 `ui_main` 要把这一个**控件**插进页签行（`QHBoxLayout` 只能
        #   addWidget），关掉多屏时再摘回去 —— 布局不能跨父窗口移动，只有控件能
        #   reparent（见 `attach_top_bar` / `detach_top_bar`）。
        self.top_bar = QWidget()
        self.top_bar.setObjectName("studioTopBar")
        bar = QHBoxLayout(self.top_bar)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(6)
        # 配置下拉（当前配置）+ 配置管理菜单
        # ★ 用户第 2 条：原来「＋新建 / 复制 / 重命名 / 删除」四个按钮各占一格，
        #   在页签行里根本放不下 ⇒ 合并成一个「配置 ▾」菜单按钮（原来那个「配置」
        #   文字标签也去掉了：下拉框本身就显示了当前配置名，重复）。
        self.cfg_combo = QComboBox()
        self.cfg_combo.setMinimumWidth(130)
        self.cfg_combo.setToolTip("输出配置：一整套屏幕 + mapping 设置")
        self.cfg_combo.currentIndexChanged.connect(self._on_config_combo)
        bar.addWidget(self.cfg_combo)
        self.btn_cfg_menu = QToolButton()
        self.btn_cfg_menu.setObjectName("btnCfgMenu")
        self.btn_cfg_menu.setText("配置 ▾")
        self.btn_cfg_menu.setToolTip("配置管理：新建 / 复制当前 / 重命名 / 删除当前")
        self.btn_cfg_menu.setPopupMode(QToolButton.InstantPopup)
        self._cfg_menu = QMenu(self.btn_cfg_menu)
        self.act_cfg_new = self._cfg_menu.addAction("新建配置…")
        self.act_cfg_new.triggered.connect(lambda _c=False: self.add_config())
        self.act_cfg_dup = self._cfg_menu.addAction("复制当前配置")
        self.act_cfg_dup.triggered.connect(lambda _c=False: self.duplicate_config())
        self.act_cfg_ren = self._cfg_menu.addAction("重命名…")
        self.act_cfg_ren.triggered.connect(lambda _c=False: self.rename_config())
        self._cfg_menu.addSeparator()
        self.act_cfg_del = self._cfg_menu.addAction("删除当前配置")
        self.act_cfg_del.triggered.connect(lambda _c=False: self.delete_config())
        self.btn_cfg_menu.setMenu(self._cfg_menu)
        bar.addWidget(self.btn_cfg_menu)
        bar.addSpacing(12)
        bar.addWidget(QLabel("模式"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("多屏多路输出", "multi")
        self.mode_combo.addItem("多屏整合单路输出", "single")
        self.mode_combo.currentIndexChanged.connect(self._on_mode)
        bar.addWidget(self.mode_combo)
        self.lbl_combined = QLabel("整合画布")
        bar.addWidget(self.lbl_combined)
        self.comb_w = QSpinBox(); self.comb_w.setRange(16, 16384); self.comb_w.setValue(1920)
        self.comb_h = QSpinBox(); self.comb_h.setRange(16, 16384); self.comb_h.setValue(1080)
        # ★ S2 #3（用户第 3 条）：整合画布这两个数字框**字号小一号**（原来和按钮一样大，
        #   在页签行里显得笨重）。
        for _sp in (self.comb_w, self.comb_h):
            _sp.setStyleSheet("QSpinBox{font-size:11px;padding:0 2px;}")
        self.comb_w.valueChanged.connect(self._on_combined)
        self.comb_h.valueChanged.connect(self._on_combined)
        bar.addWidget(self.comb_w)
        # ★ 用户第 3 条：「多路模式下孤零零剩一个 ×」——这个 × 原来是**匿名局部变量**，
        #   1360 行附近那组 setVisible(single) 里没人管它 ⇒ 多路模式下 comb_w/comb_h
        #   都藏了，只剩它挂在工具条上，谁都不知道是什么。存成 `self.lbl_x` 并一起藏。
        self.lbl_x = QLabel("×")
        bar.addWidget(self.lbl_x)
        bar.addWidget(self.comb_h)
        bar.addSpacing(8)
        self.btn_auto_layout = QPushButton("自动排列")
        self.btn_auto_layout.setToolTip("把主屏/独立屏边对边横向排开")
        self.btn_auto_layout.clicked.connect(self._auto_layout)
        bar.addWidget(self.btn_auto_layout)
        bar.addSpacing(8)
        self.btn_add_screen = QPushButton("＋屏幕")
        self.btn_add_screen.setToolTip("新建屏幕（独立 / 复制）")
        self.btn_add_screen.clicked.connect(self.add_screen)
        bar.addWidget(self.btn_add_screen)
        bar.addSpacing(8)
        self.chk_snap = QCheckBox("吸附")
        self.chk_snap.setChecked(True)
        self.chk_snap.toggled.connect(self._on_snap)
        bar.addWidget(self.chk_snap)
        self.btn_fit = QPushButton("适应窗口")
        self.btn_fit.clicked.connect(lambda: self.preview.fit_all())
        bar.addWidget(self.btn_fit)
        # ★ 用户第 11 条：「输出画布…」独立窗口（分辨率 + 拖放排布 + 导出 mapping）
        self.btn_canvas = QPushButton("输出画布…")
        self.btn_canvas.setToolTip("打开发送用的输出画布：设分辨率、拖动摆放每块屏幕、导出 mapping")
        self.btn_canvas.clicked.connect(lambda: self.open_output_canvas())
        bar.addWidget(self.btn_canvas)
        # ★ 用户第 5 条：多屏模式下不再内嵌素材库 ⇒ 控制条上给一个「打开素材库」，
        #   点开的是**同一个** `library_panel` 实例（reparent 到浮动窗口，不是复制）。
        self.btn_open_lib = QPushButton("📚 打开素材库")
        self.btn_open_lib.setToolTip(
            "打开素材库窗口（和演出台那份是同一个面板）：可以把素材拖进下面任意图层的色块里添加")
        self.btn_open_lib.clicked.connect(lambda: self.open_library.emit())
        bar.addWidget(self.btn_open_lib)
        bar.addStretch(1)
        self.lbl_mode_hint = QLabel("")
        self.lbl_mode_hint.setStyleSheet("color:#888;")
        # ★ S1 #2（用户第 2 条）：这条提示文字很长，`QLabel` 的**最小宽度 = 整行文字宽**
        #   ⇒ 它会把整条页签行（多屏模式下控制条搬在那一行）撑到超出窗口，进而把整个
        #   页面拉宽、预览区跟着改尺寸（"一换模式预览就变样"的真根因之一）。
        #   这里让它**可被压缩**（Ignored）、文字用 tooltip 兜底，宽度不再影响布局。
        self.lbl_mode_hint.setSizePolicy(QSizePolicy.Policy.Ignored,
                                        QSizePolicy.Policy.Preferred)
        self.lbl_mode_hint.setMinimumWidth(0)
        bar.addWidget(self.lbl_mode_hint, 1)
        outer.addWidget(self.top_bar)
        self._style_top_bar()
        theme.register(self, self._style_top_bar)

        # ★ 用户第 12 条：原来工具条里的「当前屏幕」下拉框已挪到预览区左上角，
        #   做成彩色半透明胶囊（见 `_build_screen_badge`）。
        #   ⚠ 控件本体**保留**（只是不放进布局）：`_sync_screen_combo` /
        #     `select_screen` 一直靠它做"程序侧同步"，删了要改一堆调用点。
        self.screen_combo = QComboBox()
        self.screen_combo.currentIndexChanged.connect(self._on_screen_combo)

        # ---- 中间：预览 + 右侧设置 ----
        # ★ S8 #13（用户第 13 条）：预览与右侧设置之间那条缝太宽 ⇒ 收紧
        #   （spacing 6→2；设置列最小宽 300→280，不会把内容挤变形）。
        mid = QHBoxLayout()
        mid.setSpacing(2)
        left = QVBoxLayout()
        self.preview = OutputPreview(self)
        self.preview.action.connect(self._on_preview_action)
        self.preview.geometry_changed.connect(self._on_preview_geometry)
        self.preview.view_changed.connect(self._save_view_state)
        left.addWidget(self.preview, 1)
        self._build_screen_badge()          # ★ 左上角「当前屏幕」胶囊（用户第 12 条）
        # 底部卡片
        cardbox = QWidget()
        cardbox.setMinimumHeight(180)
        self.cards_scroll = QScrollArea()
        self.cards_scroll.setWidgetResizable(True)
        self.cards_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.cards_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.cards_host = QWidget()
        self.cards_layout = QHBoxLayout(self.cards_host)
        self.cards_layout.setContentsMargins(2, 2, 2, 2)
        self.cards_layout.setSpacing(6)
        self.cards_layout.addStretch(1)
        self.cards_scroll.setWidget(self.cards_host)
        cardbox.setLayout(QVBoxLayout(cardbox))
        cardbox.layout().setContentsMargins(0, 0, 0, 0)
        cardbox.layout().addWidget(self.cards_scroll)
        self.cardbox = cardbox
        if self.compact:
            cardbox.hide()
        left.addWidget(cardbox, 0)
        mid.addLayout(left, 1)
        self.settings = ScreenSettingsPanel(self)
        self.settings.setMinimumWidth(280)
        self.settings.changed.connect(self._on_settings_changed)
        mid.addWidget(self.settings, 0)
        outer.addLayout(mid, 1)

        # ---- 配置 ----
        self._configs = []
        self.reload()

    # ---------- 顶部条（可搬进页签行）----------
    def _style_top_bar(self):
        """顶部条的控件级样式（主题感知；`btn_cfg_menu` 是 QToolButton，吃不到
        `QPushButton` 的全局 QSS，必须自己写一遍，否则在页签行里像个裸文字）。"""
        v = theme.V
        try:
            self.btn_cfg_menu.setStyleSheet(
                "QToolButton{background:%s;color:%s;border:1px solid %s;"
                "border-radius:4px;padding:2px 8px;min-height:18px;}"
                "QToolButton:hover{background:%s;}"
                "QToolButton::menu-indicator{image:none;width:0px;}" % (
                    v("btn"), v("text"), v("border"), v("btn_hov")))
        except Exception:                                          # noqa: BLE001
            pass

    def attach_top_bar(self, host_layout):
        """把顶部控制条**搬进**另一个布局（多屏模式下 = 页签行的 stretch 之前）。

        ⚠ 布局不能跨控件移动，只有**控件**能 reparent ⇒ 顶部条必须是一个 QWidget
          （`self.top_bar`），不能是裸 QHBoxLayout。搬之前先从原布局摘掉，
          否则会留下一个指向"已无父控件"的 layout item。
        """
        if host_layout is None:
            return
        try:
            old = self.top_bar.parentWidget()
            if old is not None and old is not self:
                lay = old.layout()
                if lay is not None:
                    lay.removeWidget(self.top_bar)
            self.top_bar.setParent(None)
            host_layout.addWidget(self.top_bar, 0)
            self.top_bar.setVisible(True)
            self.layout_screen_badge()
        except Exception:                                          # noqa: BLE001
            pass

    def detach_top_bar(self):
        """把顶部控制条搬回本控件自己的布局顶部（关掉多屏时用）。"""
        try:
            old = self.top_bar.parentWidget()
            if old is not None and old is not self:
                lay = old.layout()
                if lay is not None:
                    lay.removeWidget(self.top_bar)
            self.top_bar.setParent(None)
            lay = self.layout()
            if lay is not None:
                lay.insertWidget(0, self.top_bar)
            self.top_bar.setVisible(True)
            self.layout_screen_badge()
        except Exception:                                          # noqa: BLE001
            pass

    # ---------- 预览区左上角的「当前屏幕」胶囊 ----------
    def _build_screen_badge(self):
        """★ 用户第 12 条：「当前屏幕」不要放在工具条上，放预览区左上角，
        做成一条**该屏边框色**的半透明胶囊。

        点它是下拉菜单（列出所有屏幕）⇒ 保留了原来那个下拉框的"切屏"能力，
        但不再占工具条宽度（工具条现在要和页签挤在同一行）。
        """
        self.btn_screen_badge = QToolButton(self.preview)
        self.btn_screen_badge.setObjectName("screenBadge")
        self.btn_screen_badge.setPopupMode(QToolButton.InstantPopup)
        self.btn_screen_badge.setCursor(Qt.PointingHandCursor)
        self.btn_screen_badge.setToolTip("当前选中的屏幕（点击切换）")
        self._badge_menu = QMenu(self.btn_screen_badge)
        self.btn_screen_badge.setMenu(self._badge_menu)
        self.btn_screen_badge.hide()

    def layout_screen_badge(self):
        b = getattr(self, "btn_screen_badge", None)
        if b is None or not b.isVisible():
            return
        try:
            b.adjustSize()
            b.move(10, 10)
            b.raise_()
        except Exception:                                          # noqa: BLE001
            pass

    def _refresh_screen_badge(self, cfg):
        b = getattr(self, "btn_screen_badge", None)
        if b is None:
            return
        # ★ S1 #2：这颗胶囊是**预览区自己的浮层**，与"多路/单路"无关
        #   （用户要求切模式时预览逐像素不变；隐藏它会直接改变预览画面）。
        b.setVisible(True)
        if not (cfg or {}).get("screens"):
            b.hide()
            return
        s = OM.screen_by_id(cfg, self.selected_id) or OM.main_screen(cfg)
        name = str((s or {}).get("name") or "-")
        col = _color(OM.effective_color(cfg, s)) if s else _color("#7B2FF7")
        bg = QColor(col)
        bg.setAlpha(165)
        b.setText("当前选择屏幕 · %s  ▾" % name)
        b.setStyleSheet(
            "#screenBadge{background:rgba(%d,%d,%d,%d);color:%s;"
            "border:1px solid %s;border-radius:9px;padding:2px 10px;font-size:11px;}"
            "#screenBadge::menu-indicator{image:none;width:0px;}"
            "#screenBadge:hover{border-color:#ffffff;}" % (
                bg.red(), bg.green(), bg.blue(), bg.alpha(),
                _text_color(bg).name(), col.name()))
        self._badge_menu.clear()
        for s2 in (cfg or {}).get("screens") or []:
            nm = str(s2.get("name") or s2.get("id"))
            if s2.get("type") != "main":
                nm += "  [%s]" % OM.TYPE_LABELS.get(s2.get("type"), s2.get("type"))
            a = self._badge_menu.addAction(nm)
            a.setCheckable(True)
            a.setChecked(s2.get("id") == self.selected_id)
            a.triggered.connect(lambda _c=False, sid=s2.get("id"): self.select_screen(sid))
        self.layout_screen_badge()

    # ---------- 配置/刷新 ----------
    def active_config(self):
        return OM.active_config(self.cfg.data)

    def reload(self):
        cfg = self.active_config()
        if cfg is None:
            OM.ensure_output_configs(self.cfg.data)
            cfg = self.active_config()
        self._configs = (self.cfg.data.get("output") or {}).get("configs") or []
        self.active_id = (cfg or {}).get("id", "")
        self._loading = True
        self.cfg_combo.clear()
        for c in self._configs:
            self.cfg_combo.addItem(c.get("name") or c.get("id"), c.get("id"))
        idx = self.cfg_combo.findData(self.active_id)
        self.cfg_combo.setCurrentIndex(max(0, idx))
        self.mode_combo.setCurrentIndex(0 if (cfg or {}).get("mode") == "multi" else 1)
        self._loading = False
        screens = (cfg or {}).get("screens") or []
        if not self.selected_id or not OM.screen_by_id(cfg, self.selected_id):
            self.selected_id = ((OM.main_screen(cfg) or {}).get("id") or
                                (screens[0].get("id") if screens else ""))
        # 单路模式：给没有位置的源屏幕补一个自动布局
        if cfg and cfg.get("mode") == "single":
            srcs = OM.layout_sources(cfg)
            if len(srcs) > 1 and all((s.get("layout") or [0, 0]) == [0, 0] for s in srcs[1:]):
                OM.auto_layout(cfg)
        self.preview.set_context((cfg or {}).get("mode", "multi"), screens, cfg,
                                 self.selected_id)
        state = ((self.cfg.data.get("ui") or {}).get("output_preview") or {}).get(self.active_id)
        if state:
            self.preview.set_view_state(state)
        self.refresh_all()
        self.sync_engine()
        self._sync_main_output_route()

    def refresh_all(self):
        cfg = self.active_config()
        if not cfg:
            return
        self._loading = True
        self.cfg_combo.blockSignals(True)
        cur = self.cfg_combo.findData(cfg.get("id"))
        if cur >= 0:
            self.cfg_combo.setCurrentIndex(cur)
        self.cfg_combo.blockSignals(False)
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(0 if cfg.get("mode") == "multi" else 1)
        self.mode_combo.blockSignals(False)
        self._rebuild_cards()
        self._sync_screen_combo(cfg)
        s = OM.screen_by_id(cfg, self.selected_id) or OM.main_screen(cfg)
        self.settings.set_screen(s, cfg, cfg.get("mode", "multi"))
        self.preview.set_context(cfg.get("mode", "multi"), cfg.get("screens") or [],
                                 cfg, self.selected_id)
        single = cfg.get("mode") == "single"
        try:
            self.comb_w.blockSignals(True); self.comb_h.blockSignals(True)
            cw, ch = OM.combined_size(cfg)
            self.comb_w.setValue(int(cw)); self.comb_h.setValue(int(ch))
            self.comb_w.blockSignals(False); self.comb_h.blockSignals(False)
            # ★ 用户第 3 条：「整合画布 1920 × 1080」这一组（含中间那个 ×）只在
            #   单路模式有意义；多路模式下必须**整组**藏掉，否则工具条上孤零零剩一个 ×。
            for w in (self.lbl_combined, self.comb_w, self.lbl_x, self.comb_h,
                      self.btn_auto_layout):
                # ★ T4 #6（用户第 6 条）：单路模式下「整合画布 + 自动排列」也**不再放顶部条**，
                #   整组搬进「输出画布…」窗口（那窗口本来就是干这个的）。顶部条单路下只剩「模式」。
                w.setVisible(False)
            # ★ T1 #1（用户第 1 条）：「输出画布…」只属于**单路**模式
            #   （多路每屏各自输出，没有"整合画布"这回事）。
            self.btn_canvas.setVisible(single)
        except Exception:
            pass
        # ★ U2（用户第 2 条）：多路把「输出设置」绑到**当前选中的屏**（读它的分辨率/
        #   输出显示器/Spout/NDI/Alpha），单路解绑 ⇒ 全局一份。
        try:
            if str((cfg or {}).get("mode") or "multi") == "multi":
                self.main.output_panel.set_screen(OM.screen_by_id(cfg, self.selected_id))
            else:
                self.main.output_panel.set_screen(None)
        except Exception:                                          # noqa: BLE001
            pass
        # ★ S2 #3/#4/#14：右下角「输出设置」按模式决定显示哪些项
        try:
            self.main.output_panel.set_mode("single" if single else "multi")
        except Exception:                                          # noqa: BLE001
            pass
        self.lbl_mode_hint.setText(
            "多路：每块屏幕独立 NDI/Spout；上方棋盘格是监看工作区，拖动/缩放不改输出。"
            if not single else
            "单路：多屏拼成一张大画布，只发一路；上方布局会影响 mapping/最终输出。")
        self.lbl_mode_hint.setToolTip(self.lbl_mode_hint.text())
        self._loading = False
        self._refresh_screen_badge(cfg)     # ★ 预览区左上角胶囊（选中屏幕 + 颜色）
        try:
            # ★ 屏幕增删（apply→refresh_all）也要让 ui_main 重排下方图层块（用户第 10 条）
            self.selection_changed.emit(self.selected_id)
        except Exception:                                          # noqa: BLE001
            pass
        self.preview.update()

    def _rebuild_cards(self):
        while self.cards_layout.count():
            it = self.cards_layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        if self.compact:
            return
        cfg = self.active_config()
        if not cfg:
            return
        for s in cfg.get("screens") or []:
            card = ScreenCard(self, s)
            card.clicked.connect(self.select_screen)
            card.action.connect(self._on_preview_action)
            card.set_selected(s.get("id") == self.selected_id)
            self.cards_layout.addWidget(card)
        btn = QPushButton("＋\n新建屏幕")
        btn.setMinimumSize(90, 150)
        btn.clicked.connect(self.add_screen)
        self.cards_layout.addWidget(btn)
        self.cards_layout.addStretch(1)

    # ---------- 引擎/主窗同步 ----------
    def sync_engine(self):
        cfg = self.active_config()
        if not cfg:
            return
        OM.sync_legacy_output(self.cfg.data)
        try:
            self.main.engine.set_output_config(cfg)
        except Exception:
            pass
        self._sync_main_output_route()

    def apply(self):
        cfg = self.active_config()
        if not cfg:
            return
        # 跟随主屏的独立屏幕：分辨率强制镜像
        main = OM.main_screen(cfg)
        if main is not None:
            for s in cfg.get("screens") or []:
                if s.get("type") == "independent" and s.get("follow_main") and cfg.get("mode") == "multi":
                    s["width"], s["height"] = OM.effective_size(cfg, main)
        # ★ 2026-11-09（用户：「这个输出的分辨率要求可以自定义，**随便设置多少**，
        #   只要不超过 2 的 14 次方」）：这里**不再自动改** `combined_size`。
        #   以前"布局超出就自动扩大"会让用户输 1920×1080 后变成 2560×1080，
        #   用户会当成"这个框存不住数"。现在画布尺寸**只由用户输入 / 拖动 /
        #   显式点「适应窗口」**改变；超出只做**提示**（见 OutputCanvasWindow._update_warn）。
        OM.sync_legacy_output(self.cfg.data)
        try:
            self.cfg.save()
        except Exception:
            pass
        self.sync_engine()
        self.refresh_all()
        self._save_view_state()

    def _sync_main_output_route(self):
        cfg = self.active_config()
        if not cfg:
            return
        s = OM.screen_by_id(cfg, self.selected_id) or OM.main_screen(cfg)
        try:
            self.main.engine.set_preview_route(self.selected_id)
        except Exception:
            pass
        name = ""
        try:
            names = OM.screen_output_names(cfg)
            name = names.get((s or {}).get("id"), (s or {}).get("name") or "")
        except Exception:
            pass
        # 旧输出设置面板（演出台右下）始终显示“主屏幕”的输出名；
        # 输出页右侧的 out_name 才跟随当前选中屏幕（多路模式可逐屏改）。
        old_name = name
        try:
            main_s = OM.main_screen(cfg)
            names = OM.screen_output_names(cfg)
            old_name = names.get((main_s or {}).get("id"),
                                 (main_s or {}).get("name") or name)
        except Exception:
            pass
        if cfg.get("mode") == "multi":
            title = "Easy Reality Output - %s" % (name or "主屏幕")
        else:
            title = "Easy Reality Output"
        try:
            self.main.out_win.setWindowTitle(title)
        except Exception:
            pass
        # 旧输出设置面板右下角名称同步
        try:
            op = getattr(self.main, "output_panel", None)
            if op is not None:
                if cfg.get("mode") == "multi":
                    op.lbl_spout_name.setText(old_name or "-")
                    comp = str(os.environ.get("COMPUTERNAME") or "")
                    op.lbl_ndi_name.setText(("%s (%s)" % (comp, old_name)) if comp else old_name)
                else:
                    op.lbl_spout_name.setText(str(cfg.get("spout_name") or "EasyRealityAutoVJ"))
                    _n = str(cfg.get("ndi_name") or "EasyRealityAutoVJ")
                    _comp = str(os.environ.get("COMPUTERNAME") or "")
                    op.lbl_ndi_name.setText(("%s (%s)" % (_comp, _n)) if _comp else _n)
                # 旧输出设置面板的 Spout/NDI 勾选同步到主屏幕（避免两处显示不一致）
                try:
                    main_s = OM.main_screen(cfg)
                    if main_s is not None:
                        op.chk_spout.blockSignals(True)
                        op.chk_spout.setChecked(bool(main_s.get("spout")))
                        op.chk_spout.blockSignals(False)
                        op.chk_ndi.blockSignals(True)
                        op.chk_ndi.setChecked(bool(main_s.get("ndi")))
                        op.chk_ndi.blockSignals(False)
                except Exception:
                    pass
        except Exception:
            pass

    def _sync_screen_combo(self, cfg):
        try:
            self.screen_combo.blockSignals(True)
            self.screen_combo.clear()
            for s in (cfg or {}).get("screens") or []:
                nm = str(s.get("name") or s.get("id"))
                if s.get("type") != "main":
                    nm += "  [%s]" % OM.TYPE_LABELS.get(s.get("type"), s.get("type"))
                self.screen_combo.addItem(nm, s.get("id"))
            idx = self.screen_combo.findData(self.selected_id)
            self.screen_combo.setCurrentIndex(max(0, idx))
            self.screen_combo.blockSignals(False)
        except Exception:
            pass

    def _on_screen_combo(self, idx):
        if self._loading:
            return
        try:
            sid = self.screen_combo.itemData(idx)
            if sid:
                self.select_screen(sid)
        except Exception:
            pass

    def select_screen(self, sid):
        cfg = self.active_config()
        if not cfg:
            return
        self.selected_id = str(sid or "")
        s = OM.screen_by_id(cfg, self.selected_id) or OM.main_screen(cfg)
        if s is not None:
            self.selected_id = s.get("id")
        self.preview.set_selected(self.selected_id)
        try:
            idx = self.screen_combo.findData(self.selected_id)
            if idx >= 0:
                self.screen_combo.blockSignals(True)
                self.screen_combo.setCurrentIndex(idx)
                self.screen_combo.blockSignals(False)
        except Exception:
            pass
        self.settings.set_screen(s, cfg, cfg.get("mode", "multi"))
        self._refresh_screen_badge(cfg)
        try:
            self.selection_changed.emit(self.selected_id)
        except Exception:                                          # noqa: BLE001
            pass
        self._sync_main_output_route()
        for i in range(self.cards_layout.count()):
            w = self.cards_layout.itemAt(i).widget()
            if isinstance(w, ScreenCard):
                w.set_selected(w.screen.get("id") == self.selected_id)
                w.update()

    def on_frame(self):
        if not self.isVisible():
            return
        try:
            th = self.main.engine.latest_route_thumbs()
            if th is getattr(self, "_last_thumbs", None):
                return
            self._last_thumbs = th
            self.preview.set_thumbs(th)
        except Exception:
            pass

    def on_show(self):
        """切到输出页时刷新一次（配置可能被旧输出面板/其它页改过）。"""
        try:
            self.reload()
        except Exception:
            pass

    def layer_names_for(self, screen):
        try:
            ids = OM.effective_layers(self.active_config(), screen,
                                      [l.lid for l in self.main.engine.layers])
            by = {l.lid: l.name for l in self.main.engine.layers}
            return [by.get(x, "?") for x in ids]
        except Exception:
            return []

    # ---------- 模式/配置 ----------
    def _on_config_combo(self, idx):
        if self._loading:
            return
        cid = self.cfg_combo.itemData(idx)
        if not cid:
            return
        self.cfg.data.setdefault("output", {})["active_config"] = cid
        self.selected_id = ""
        self.apply()

    def _on_mode(self, idx):
        if self._loading:
            return
        cfg = self.active_config()
        if not cfg:
            return
        mode = self.mode_combo.itemData(idx) or "multi"
        cfg["mode"] = mode
        if mode == "single":
            srcs = OM.layout_sources(cfg)
            if len(srcs) > 1 and all((s.get("layout") or [0, 0]) == [0, 0] for s in srcs[1:]):
                OM.auto_layout(cfg)
        self.apply()

    def _on_snap(self, on):
        self.preview.set_snap(on)

    def _on_combined(self, *_):
        if self._loading:
            return
        cfg = self.active_config()
        if not cfg or cfg.get("mode") != "single":
            return
        cfg["combined_size"] = [max(16, self.comb_w.value()),
                                max(16, self.comb_h.value())]
        self.apply()

    def _auto_layout(self):
        cfg = self.active_config()
        if not cfg or cfg.get("mode") != "single":
            return
        OM.auto_layout(cfg, "h")
        self.apply()

    def _save_view_state(self):
        try:
            st = self.preview.view_state()
            ui = self.cfg.data.setdefault("ui", {})
            d = ui.setdefault("output_preview", {})
            d[self.active_id] = st
            self.cfg.save()
        except Exception:
            pass

    def _on_preview_geometry(self):
        self.apply()

    def _on_preview_action(self, action, sid):
        if action == "select":
            self.select_screen(sid)

    def _on_settings_changed(self):
        # 面板内部的每个修改动作都已经自己调用了 `apply()`；这里不再重复重建，
        # 否则一次勾选会触发两次引擎计划重建（发送端/画布白重建一遍）。
        return

    def add_config(self):
        name, ok = QInputDialog.getText(self, "新建配置", "配置名称：", text="配置")
        if not ok or not name.strip():
            return
        cfg = OM.new_config(name.strip())
        self.cfg.data.setdefault("output", {})["configs"].append(cfg)
        self.cfg.data["output"]["active_config"] = cfg["id"]
        self.selected_id = OM.main_screen(cfg)["id"]
        self.apply()

    def duplicate_config(self):
        cfg = self.active_config()
        if not cfg:
            return
        import copy as _copy
        nc = _copy.deepcopy(cfg)
        nc["id"] = OM.new_id("cfg")
        nc["name"] = (cfg.get("name") or "配置") + " 副本"
        self.cfg.data.setdefault("output", {})["configs"].append(nc)
        self.cfg.data["output"]["active_config"] = nc["id"]
        self.selected_id = ""
        self.apply()

    def rename_config(self):
        cfg = self.active_config()
        if not cfg:
            return
        name, ok = QInputDialog.getText(self, "重命名配置", "配置名称：",
                                        text=cfg.get("name") or "")
        if ok and name.strip():
            cfg["name"] = name.strip()
            self.apply()

    def delete_config(self):
        cfg = self.active_config()
        if not cfg:
            return
        if len((self.cfg.data.get("output") or {}).get("configs") or []) <= 1:
            QMessageBox.information(self, "不能删除", "至少保留一套配置。")
            return
        if QMessageBox.question(self, "删除配置",
                                "确定删除「%s」？" % cfg.get("name")) != QMessageBox.Yes:
            return
        configs = self.cfg.data["output"].get("configs") or []
        configs = [c for c in configs if c.get("id") != cfg.get("id")]
        self.cfg.data["output"]["configs"] = configs
        self.cfg.data["output"]["active_config"] = configs[0]["id"]
        self.selected_id = ""
        self.apply()

    # ---------- 屏幕 ----------
    def add_screen(self):
        dlg = ScreenDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        v = dlg.values()
        cfg = self.active_config()
        if not cfg:
            return
        main = OM.main_screen(cfg)
        s = OM._new_screen(v["name"], v["type"], v["width"], v["height"], v["color"],
                           source_id=v["source_id"])
        if v["type"] == "copy":
            s["source_id"] = (main or {}).get("id") or ""
            s["layers"] = None
            s["follow_main"] = True
            s["width"], s["height"] = OM.effective_size(cfg, main)
        elif v["type"] == "split":
            s["source_id"] = v["source_id"] or (main or {}).get("id") or ""
            s["uv"] = [0, 0, 1, 1]
            s["layers"] = None
            s["follow_main"] = True
        else:
            # ★ S4 #7（用户第 7 条）：独立屏**初始图层为空**（该屏不显示任何图层），
            #   由用户在块头「⋯ → 屏幕设置」里自己勾。以前复制主屏全部图层，
            #   结果是"新建一块屏，画面上立刻出现主屏所有图层"，与预期不符。
            s["layers"] = []
            s["width"], s["height"] = v["width"], v["height"]
            # 默认勾选“跟随主屏”；但用户在新建对话框里选了不同分辨率时自动取消，
            # 否则刚建出来就被主屏分辨率覆盖，等于白填。
            try:
                mw, mh = OM.effective_size(cfg, main)
                s["follow_main"] = bool((v["width"], v["height"]) == (mw, mh))
            except Exception:
                s["follow_main"] = True
        s["color"] = v["color"]
        cfg["screens"].append(s)
        # 预览框自动放在末尾
        xs = [OM._normalize_preview_rect(x.get("preview_rect")) for x in cfg["screens"][:-1]]
        xs = [r for r in xs if r]
        px = (max([r[0] + r[2] for r in xs]) + 28) if xs else 30
        s["preview_rect"] = [px, 30, 420, max(120, int(420 / max(0.2, (s["width"] / max(1, s["height"])))))]
        # ★ U5-1（用户第 5 条）：新建屏的布局默认放到**已有屏的右边**——多路/单路都一样。
        #   以前只有单路会排（多路全落在 [0,0]）⇒ Mapping 里所有新屏叠在原点，
        #   看着像"只有一块屏"。这是"新建时的合理默认值"，不是事后偷改用户数据。
        #   ⚠ 这里**不动** `combined_size`（画布尺寸只由用户/「适应窗口」改），
        #   超出时画布窗口会显示超界提示。
        try:
            _lx = 0
            for z in cfg.get("screens") or []:
                if z is s:
                    continue
                zx, zy, zw, zh = OM.screen_rect(cfg, z)
                _lx = max(_lx, zx + zw)
            s["layout"] = [int(_lx), 0]
        except Exception:                                          # noqa: BLE001
            s["layout"] = [0, 0]
        self.selected_id = s["id"]
        self.apply()
        self.preview.fit_all()

    def add_copy_screen(self, source_id=""):
        cfg = self.active_config()
        if not cfg:
            return
        main = OM.main_screen(cfg)
        name = "复制屏幕"
        i = 2
        while any(s.get("name") == name for s in cfg["screens"]):
            name = "复制屏幕 %d" % i; i += 1
        s = OM._new_screen(name, "copy", OM.effective_size(cfg, main)[0],
                           OM.effective_size(cfg, main)[1],
                           (main or {}).get("color"), source_id=(main or {}).get("id"))
        s["source_id"] = (main or {}).get("id") or ""
        cfg["screens"].append(s)
        self._place_new_screen(s, cfg)
        self.selected_id = s["id"]
        self.apply()

    def add_split_screen(self, source_id=""):
        cfg = self.active_config()
        if not cfg:
            return
        src = OM.screen_by_id(cfg, source_id) or OM.main_screen(cfg)
        name = "切分屏幕"
        i = 2
        while any(s.get("name") == name for s in cfg["screens"]):
            name = "切分屏幕 %d" % i; i += 1
        s = OM._new_screen(name, "split", 640, 720, (src or {}).get("color"),
                           source_id=(src or {}).get("id"))
        s["uv"] = [0, 0, 0.5, 1]
        cfg["screens"].append(s)
        self._place_new_screen(s, cfg)
        self.selected_id = s["id"]
        self.apply()

    def _place_new_screen(self, s, cfg):
        xs = [OM._normalize_preview_rect(x.get("preview_rect")) for x in cfg["screens"][:-1]]
        xs = [r for r in xs if r]
        px = (max([r[0] + r[2] for r in xs]) + 28) if xs else 30
        w, h = OM.effective_size(cfg, s)
        s["preview_rect"] = [px, 30, 420, max(120, int(420 * h / max(1, w)))]

    def delete_screen(self, sid):
        cfg = self.active_config()
        if not cfg:
            return
        s = OM.screen_by_id(cfg, sid)
        if not s or s.get("type") == "main":
            return
        linked = [x for x in cfg["screens"] if x.get("source_id") == sid
                  and x.get("type") in ("copy", "split")]
        msg = "确定删除「%s」？" % s.get("name")
        if linked:
            msg += "\n\n有 %d 块复制/切分屏幕引用它，会一起删除。" % len(linked)
        if QMessageBox.question(self, "删除屏幕", msg) != QMessageBox.Yes:
            return
        rm = {sid} | {x.get("id") for x in linked}
        cfg["screens"] = [x for x in cfg["screens"] if x.get("id") not in rm]
        if self.selected_id in rm:
            self.selected_id = (OM.main_screen(cfg) or {}).get("id") or ""
        self.apply()

    def rename_screen(self, sid):
        cfg = self.active_config()
        s = OM.screen_by_id(cfg, sid) if cfg else None
        if not s:
            return
        if s.get("type") == "main" and cfg.get("mode") == "single":
            QMessageBox.information(self, "名称锁定",
                                    "多屏整合单路模式使用配置名作为输出名；主屏幕名称在单路模式下不可改。")
            return
        name, ok = QInputDialog.getText(self, "重命名屏幕", "名称：", text=s.get("name") or "")
        if ok and name.strip():
            s["name"] = name.strip()
            self.apply()

    def change_color(self, sid):
        cfg = self.active_config()
        s = OM.screen_by_id(cfg, sid) if cfg else None
        if not s or s.get("type") not in ("main", "independent"):
            return
        c = QColorDialog.getColor(_color(s.get("color")), self, "选择屏幕边框颜色")
        if c.isValid():
            s["color"] = c.name()
            self.apply()

    def set_preview_size(self, sid, width):
        cfg = self.active_config()
        s = OM.screen_by_id(cfg, sid) if cfg else None
        if not s or cfg.get("mode") != "multi":
            return
        r = OM._normalize_preview_rect(s.get("preview_rect")) or [30, 30, 420, 236]
        if width <= 0:
            # 适应内容：按屏幕比例给一个默认宽
            width = 420
        w, h = OM.effective_size(cfg, s)
        ar = w / max(1, h)
        s["preview_rect"] = [int(r[0]), int(r[1]), int(width), max(60, int(width / ar))]
        self.cfg.save()
        self.preview.set_context(cfg.get("mode"), cfg.get("screens") or [], cfg, self.selected_id)
        self.preview.update()

    def set_preview_fit(self, sid, fit):
        cfg = self.active_config()
        s = OM.screen_by_id(cfg, sid) if cfg else None
        if not s:
            return
        s["preview_fit"] = fit if fit in ("fit", "fill", "1:1") else "fit"
        self.cfg.save()
        self.preview.update()

    # ---------- 屏幕编辑（用户第 8 条的补偿入口） ----------
    def edit_screen(self, sid):
        """编辑某块屏幕：名称 / 分辨率 / 边框颜色（复制屏不可单独改）。

        ★ 背景：多屏模式下按用户第 8 条把 `ScreenSettingsPanel`（名称/类型/源屏幕/
          分辨率/边框颜色/使用的图层）藏掉了，改名的入口只剩预览右键菜单。
          这里给图层块头一个显眼的「⋯」按钮，点开就是这个对话框。
        """
        cfg = self.active_config()
        s = OM.screen_by_id(cfg, sid) if cfg else None
        if s is None or s.get("type") == "copy":
            return
        dlg = ScreenDialog(self, parent=self, edit_screen=s)
        if dlg.exec() != QDialog.Accepted:
            return
        dlg.apply_to(s)
        self.apply()
        try:
            self.settings.set_screen(s, cfg, cfg.get("mode", "multi"))
        except Exception:                                          # noqa: BLE001
            pass

    # ---------- 输出画布窗口（用户第 11 条）----------
    def open_output_canvas(self):
        """打开「输出画布」窗口（单例：再点一次只是前置 + 刷新）。"""
        win = getattr(self, "_canvas_win", None)
        if win is None:
            win = OutputCanvasWindow(self)
            self._canvas_win = win
        try:
            win.reload()
            win.show()
            win.raise_()
            win.activateWindow()
        except Exception:                                          # noqa: BLE001
            pass

    # ---------- mapping ----------
    def export_mapping(self):
        cfg = self.active_config()
        if not cfg:
            return
        d = QFileDialog.getExistingDirectory(self, "选择 mapping 导出目录")
        if not d:
            return
        try:
            self._write_mapping(cfg, d)
            QMessageBox.information(self, "导出完成", "mapping JSON + PNG 已导出到：\n%s" % d)
        except Exception as e:
            QMessageBox.warning(self, "导出失败", str(e))

    def _write_mapping(self, cfg, folder):
        cw, ch = OM.combined_size(cfg)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        base = "mapping_%s_%s" % (str(cfg.get("name") or "config").replace(" ", "_"), stamp)
        data = {
            "version": 1,
            "config": cfg.get("name"),
            "mode": str(cfg.get("mode") or "single"),
            "combined_size": [cw, ch],
            "screens": [],
        }
        for s in cfg.get("screens") or []:
            x, y, w, h = OM.screen_rect(cfg, s)
            uv = OM.screen_uv(cfg, s)
            data["screens"].append({
                "id": s.get("id"), "name": s.get("name"), "type": s.get("type"),
                "source_id": s.get("source_id"),
                "pixel_size": [OM.effective_size(cfg, s)[0], OM.effective_size(cfg, s)[1]],
                "pixel_rect": [x, y, w, h],
                "uv": uv, "color": s.get("color"),
            })
        with open(os.path.join(folder, base + ".json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        # PNG 可视化（1:1，太大时按比例缩到 <=4096）
        sc = min(1.0, 4096.0 / max(cw, ch, 1))
        iw, ih = max(1, int(cw * sc)), max(1, int(ch * sc))
        img = QImage(iw, ih, QImage.Format_ARGB32)
        img.fill(QColor("#1a1a1a"))
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.setPen(QPen(QColor("#777"), 1))
        p.drawRect(QRectF(0.5, 0.5, iw - 1, ih - 1))
        for s in cfg.get("screens") or []:
            x, y, w, h = OM.screen_rect(cfg, s)
            r = QRectF(x * sc, y * sc, w * sc, h * sc)
            c = _color(OM.effective_color(cfg, s))
            fill = QColor(c)
            fill.setAlpha(70)
            p.fillRect(r, fill)
            p.setPen(QPen(c, 2))
            p.drawRect(r)
            # ★ 用户第 12 条（I2）：字号**明显放大**，并且把 名字 / 分辨率 / UV 三行
            #   **整体居中**画在格子中间（原来是左上角 11px，缩到 4096 宽后基本看不清）。
            #   ⚠ 两行用两种字号 ⇒ 先量高度再整体居中，否则 UV 行会往下漂、小格里被裁。
            pw, ph = OM.effective_size(cfg, s)
            uv = OM.screen_uv(cfg, s)
            fs = int(max(12, min(40, min(max(24.0, r.width()), max(24.0, r.height())) / 6.5)))
            f_head = QFont("Microsoft YaHei", fs, QFont.Bold)
            f_uv = QFont("Consolas", max(10, int(fs * 0.62)))
            head = "%s\n%d × %d" % (s.get("name") or "", pw, ph)
            uvline = "UV %.4f,%.4f → %.4f,%.4f" % (uv[0], uv[1], uv[2], uv[3])
            fm1 = QFontMetrics(f_head)
            fm2 = QFontMetrics(f_uv)
            h1 = fm1.lineSpacing() * 2
            h2 = fm2.lineSpacing()
            top = r.center().y() - (h1 + h2 + 4) / 2.0
            p.setFont(f_head)
            p.setPen(QColor("#ffffff"))
            p.drawText(QRectF(r.x(), top, r.width(), h1), Qt.AlignCenter, head)
            p.setFont(f_uv)
            p.setPen(QColor(235, 235, 235, 235))
            p.drawText(QRectF(r.x(), top + h1 + 2, r.width(), h2), Qt.AlignCenter, uvline)
        p.end()
        img.save(os.path.join(folder, base + ".png"))


# ---------------------------------------------------------------- 输出画布（mapping）
class _CanvasBoard(QWidget):
    """输出画布：把每块屏幕按 mapping 坐标摆在一张画布上，**拖动即可排布**。

    ★ 用户第 11 条：多屏单路输出要有一个"输出画布"窗口，在里面设分辨率、拖放排布
      每块屏幕、导出 mapping。坐标就是配置里的 `screen["layout"]`（画布像素），
      所以拖完直接写回配置 → 输出/mapping/预览用的是同一份数据。
    ⚠ 这个画布只是"排布工具"，**不参与预览渲染**（左边预览仍走多屏多路那套）。
    """

    moved = Signal()          # 拖动结束（写回配置后由 studio.apply 落盘）

    def __init__(self, studio, parent=None):
        super().__init__(parent)
        self.studio = studio
        self.cfg = None
        self.setMinimumSize(520, 320)
        self.setMouseTracking(True)
        self.zoom = 1.0
        self.pan = QPointF(0.0, 0.0)
        self._drag = None
        self._fit_pending = True

    # ---- 坐标换算 ----
    def set_config(self, cfg):
        self.cfg = cfg
        self._fit_pending = True
        self.update()

    def _to_screen(self, x, y):
        return QPointF(self.pan.x() + x * self.zoom, self.pan.y() + y * self.zoom)

    def _to_world(self, p):
        if self.zoom <= 0:
            return QPointF(0.0, 0.0)
        return QPointF((p.x() - self.pan.x()) / self.zoom,
                       (p.y() - self.pan.y()) / self.zoom)

    def _rect_screen(self, x, y, w, h):
        a = self._to_screen(x, y)
        return QRectF(a.x(), a.y(), max(1.0, w * self.zoom), max(1.0, h * self.zoom))

    def fit(self):
        if not self.cfg or self.width() <= 4 or self.height() <= 4:
            return
        cw, ch = OM.combined_size(self.cfg)
        self.zoom = max(0.02, min(1.0, min((self.width() - 40) / max(1, cw),
                                          (self.height() - 40) / max(1, ch))))
        self.pan = QPointF((self.width() - cw * self.zoom) / 2.0,
                           (self.height() - ch * self.zoom) / 2.0)
        self._fit_pending = False
        self.update()

    def _frames(self):
        """能摆到画布上的屏：主屏 + 独立屏（复制/切分跟随源屏，不单独摆放）。"""
        return OM.layout_sources(self.cfg) if self.cfg else []

    def _hit(self, pos):
        for s in reversed(self._frames()):
            x, y, w, h = OM.screen_rect(self.cfg, s)
            r = self._rect_screen(x, y, w, h)
            if r.contains(pos):
                return s, r
        return None, None

    # ---- 交互 ----
    def wheelEvent(self, ev):
        d = ev.angleDelta().y()
        if d == 0:
            return
        old = self._to_world(ev.position())
        self.zoom = max(0.02, min(4.0, self.zoom * (1.12 ** (d / 120.0))))
        self.pan = QPointF(ev.position().x() - old.x() * self.zoom,
                           ev.position().y() - old.y() * self.zoom)
        self._fit_pending = False
        self.update()
        ev.accept()

    def mousePressEvent(self, ev):
        if ev.button() != Qt.LeftButton or not self.cfg:
            return
        s, r = self._hit(ev.position())
        if s is not None:
            x, y, _w, _h = OM.screen_rect(self.cfg, s)
            self._drag = {"sid": s.get("id"), "start": ev.position(),
                          "orig": [x, y]}
            self.setCursor(Qt.ClosedHandCursor)
        else:
            self._drag = {"sid": None, "start": ev.position(),
                          "orig": [self.pan.x(), self.pan.y()]}
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, ev):
        d = self._drag
        if d is None:
            s, _r = self._hit(ev.position())
            self.setCursor(Qt.SizeAllCursor if s is not None else Qt.ArrowCursor)
            return
        dx = (ev.position().x() - d["start"].x()) / max(0.001, self.zoom)
        dy = (ev.position().y() - d["start"].y()) / max(0.001, self.zoom)
        if d["sid"] is None:
            self.pan = QPointF(d["orig"][0] + dx, d["orig"][1] + dy)
        else:
            s = OM.screen_by_id(self.cfg, d["sid"])
            if s is not None:
                nx, ny = d["orig"][0] + dx, d["orig"][1] + dy
                # ★ S6 #11（后半）：输出画布窗口里拖屏幕**也要吸附**（画布边 + 其它屏幕边/中线）
                nx, ny = self._snap(s.get("id"), nx, ny)
                # 写回的是**画布像素坐标**（和单路模式的 layout 同一套坐标系）
                s["layout"] = [int(round(nx)), int(round(ny))]
        self.update()

    def _snap(self, sid, x, y):
        """把要落下的 (x, y) 吸到画布边 / 其它屏幕的边与中线（阈值 8 画布像素）。"""
        try:
            if not self.cfg:
                return x, y
            s = OM.screen_by_id(self.cfg, sid)
            if s is None:
                return x, y
            w, h = OM.effective_size(self.cfg, s)
            cw, ch = OM.combined_size(self.cfg)
            thr = 8.0
            tx, ty = [0.0, cw / 2.0, float(cw)], [0.0, ch / 2.0, float(ch)]
            for o in self._frames():
                if o.get("id") == sid:
                    continue
                ox, oy, ow, oh = OM.screen_rect(self.cfg, o)
                tx += [ox, ox + ow / 2.0, ox + ow]
                ty += [oy, oy + oh / 2.0, oy + oh]
            best_x = best_y = None
            for e in (x, x + w / 2.0, x + w):
                for tv in tx:
                    dd = tv - e
                    if abs(dd) <= thr and (best_x is None or abs(dd) < abs(best_x[0])):
                        best_x = (dd, tv)
            for e in (y, y + h / 2.0, y + h):
                for tv in ty:
                    dd = tv - e
                    if abs(dd) <= thr and (best_y is None or abs(dd) < abs(best_y[0])):
                        best_y = (dd, tv)
            if best_x is not None:
                x += best_x[0]
            if best_y is not None:
                y += best_y[0]
        except Exception:                                          # noqa: BLE001
            return x, y
        return x, y

    def mouseReleaseEvent(self, ev):
        d = self._drag
        self._drag = None
        self.setCursor(Qt.ArrowCursor)
        if d is not None and d.get("sid") is not None:
            self.moved.emit()

    def mouseDoubleClickEvent(self, ev):
        self.fit()

    # ---- 绘制 ----
    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor("#151515"))
        if self._fit_pending:
            self._fit_pending = False
            QTimer.singleShot(0, self.fit)
        if not self.cfg:
            p.end()
            return
        cw, ch = OM.combined_size(self.cfg)
        canvas = self._rect_screen(0, 0, cw, ch)
        p.fillRect(canvas, QColor("#000000"))
        p.setPen(QPen(QColor("#666666"), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(canvas)
        for s in self._frames():
            x, y, w, h = OM.screen_rect(self.cfg, s)
            r = self._rect_screen(x, y, w, h)
            col = _color(OM.effective_color(self.cfg, s))
            fill = QColor(col)
            fill.setAlpha(200)
            p.fillRect(r, fill)
            p.setPen(QPen(col.lighter(140), 2))
            p.drawRect(r)
            pw, ph = OM.effective_size(self.cfg, s)
            uv = OM.screen_uv(self.cfg, s)
            fs = int(max(9, min(22, min(r.width(), r.height()) / 8.0)))
            p.setFont(QFont("Microsoft YaHei", fs, QFont.Bold))
            p.setPen(_text_color(col))
            p.drawText(r, Qt.AlignCenter, "%s\n%d × %d\nUV %.4f,%.4f → %.4f,%.4f"
                       % (s.get("name") or "", pw, ph, uv[0], uv[1], uv[2], uv[3]))
            # ★ 超界的屏描红框（只提示，不改数据）：右/下边超出画布就是会被裁掉的那块
            if x + w > cw or y + h > ch:
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(QColor("#ff3b30"), 3))
                p.drawRect(r)
            p.setFont(QFont("Segoe UI", 8))
            p.setPen(QColor(255, 255, 255, 200))
            p.drawText(r.adjusted(4, 2, -4, -2), Qt.AlignLeft | Qt.AlignTop,
                       "%d,%d" % (x, y))
        p.setPen(QColor(220, 220, 220, 180))
        p.setFont(QFont("Segoe UI", 8))
        p.drawText(8, self.height() - 8,
                   "画布 %d×%d · %d%%" % (cw, ch, int(round(self.zoom * 100))))
        p.end()


class _CropCanvas(QWidget):
    """裁切示意画布：显示源屏画面 + 可拖动/可拖角缩放的裁切框（归一化 UV）。"""

    changed = Signal(list)          # [u0, v0, u1, v1]

    HANDLE = 12

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(420, 260)
        self.setMouseTracking(True)
        self._img = None            # 源屏缩略图（可为 None）
        self._fallback = QColor("#2FA8A8")
        self.uv = [0.0, 0.0, 1.0, 1.0]
        self._drag = None           # (mode, start_uv, start_pos)

    def set_image(self, img, color=None):
        self._img = img
        if color is not None:
            self._fallback = QColor(color)
        self.update()

    def set_uv(self, uv):
        try:
            u0, v0, u1, v1 = [float(x) for x in (uv or [0, 0, 1, 1])]
        except Exception:                                          # noqa: BLE001
            u0, v0, u1, v1 = 0.0, 0.0, 1.0, 1.0
        self.uv = [min(u0, u1 - 0.02), min(v0, v1 - 0.02),
                   max(u1, u0 + 0.02), max(v1, v0 + 0.02)]
        self.update()

    # ---- 几何 ----
    def _content_rect(self):
        """源画面在窗口里的显示矩形（等比、留 8px 边距）。"""
        r = self.rect().adjusted(8, 8, -8, -8)
        if self._img is not None and not self._img.isNull():
            iw, ih = self._img.width(), self._img.height()
        else:
            iw, ih = 16, 9
        if iw <= 0 or ih <= 0:
            return r
        sc = min(r.width() / float(iw), r.height() / float(ih))
        w, h = iw * sc, ih * sc
        return QRectF(r.center().x() - w / 2, r.center().y() - h / 2, w, h)

    def _uv_rect(self):
        c = self._content_rect()
        u0, v0, u1, v1 = self.uv
        return QRectF(c.left() + c.width() * u0, c.top() + c.height() * v0,
                      c.width() * (u1 - u0), c.height() * (v1 - v0))

    def _hit(self, pos):
        r = self._uv_rect()
        h = self.HANDLE
        corners = {
            "tl": QRectF(r.left() - h, r.top() - h, 2 * h, 2 * h),
            "tr": QRectF(r.right() - h, r.top() - h, 2 * h, 2 * h),
            "bl": QRectF(r.left() - h, r.bottom() - h, 2 * h, 2 * h),
            "br": QRectF(r.right() - h, r.bottom() - h, 2 * h, 2 * h),
        }
        for k, q in corners.items():
            if q.contains(pos):
                return k
        return "move" if r.contains(pos) else None

    def mousePressEvent(self, ev):
        self._drag = (self._hit(ev.position()), list(self.uv), ev.position())
        if self._drag[0] is None:
            self._drag = None        # 点框外：不动（避免误改）

    def mouseMoveEvent(self, ev):
        if self._drag is None:
            k = self._hit(ev.position())
            self.setCursor(Qt.SizeFDiagCursor if k in ("tl", "tr", "bl", "br")
                           else (Qt.SizeAllCursor if k == "move" else Qt.ArrowCursor))
            return
        mode, orig, start = self._drag
        c = self._content_rect()
        if c.width() <= 0 or c.height() <= 0:
            return
        du = (ev.position().x() - start.x()) / c.width()
        dv = (ev.position().y() - start.y()) / c.height()
        u0, v0, u1, v1 = orig
        if mode == "move":
            u0 += du; u1 += du; v0 += dv; v1 += dv
            if u0 < 0:
                u1 -= u0; u0 = 0.0
            if u1 > 1:
                u0 -= (u1 - 1); u1 = 1.0
            if v0 < 0:
                v1 -= v0; v0 = 0.0
            if v1 > 1:
                v0 -= (v1 - 1); v1 = 1.0
        else:
            if "l" in mode:
                u0 = min(max(0.0, u0 + du), u1 - 0.02)
            if "r" in mode:
                u1 = max(min(1.0, u1 + du), u0 + 0.02)
            if "t" in mode:
                v0 = min(max(0.0, v0 + dv), v1 - 0.02)
            if "b" in mode:
                v1 = max(min(1.0, v1 + dv), v0 + 0.02)
        self.uv = [u0, v0, u1, v1]
        self.update()
        self.changed.emit(list(self.uv))

    def mouseReleaseEvent(self, ev):
        if self._drag is not None:
            self.changed.emit(list(self.uv))
        self._drag = None

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#151515"))
        c = self._content_rect()
        if self._img is not None and not self._img.isNull():
            p.drawImage(c, self._img, QRectF(0, 0, self._img.width(), self._img.height()))
        else:
            p.fillRect(c, self._fallback)
        # 框外压暗
        r = self._uv_rect()
        dim = QColor(0, 0, 0, 130)
        p.fillRect(QRectF(c.left(), c.top(), c.width(), r.top() - c.top()), dim)
        p.fillRect(QRectF(c.left(), r.bottom(), c.width(), c.bottom() - r.bottom()), dim)
        p.fillRect(QRectF(c.left(), r.top(), r.left() - c.left(), r.height()), dim)
        p.fillRect(QRectF(r.right(), r.top(), c.right() - r.right(), r.height()), dim)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor("#ffffff"), 2))
        p.drawRect(r)
        p.setBrush(QColor("#ffffff"))
        for q in (r.topLeft(), r.topRight(), r.bottomLeft(), r.bottomRight()):
            p.drawRect(QRectF(q.x() - 4, q.y() - 4, 8, 8))
        p.setPen(QColor(230, 230, 230))
        p.setFont(QFont("Segoe UI", 8))
        p.drawText(8, self.height() - 8,
                   "UV %.3f,%.3f → %.3f,%.3f" % (self.uv[0], self.uv[1], self.uv[2], self.uv[3]))
        p.end()


class CropDialog(QDialog):
    """「裁切…」窗口（用户第 3 条）：给切分屏拖出一个裁切框，实时写回 UV + 分辨率。

    · 框 = 归一化 UV（0..1），拖动框体移动、拖四个角缩放
    · 写回：`screen["uv"]` + `screen["width"/"height"] = 源分辨率 × 框占比`
    · 外部改 UV（右侧输入框）时调 `set_uv()`，框会跟着动（双向同步）
    """

    def __init__(self, studio, screen, parent=None):
        super().__init__(parent or studio)
        self.studio = studio
        self.screen = screen
        self.setWindowTitle("裁切（拖框选择源屏上的区域）")
        self.resize(720, 480)
        cfg = studio.active_config()
        src = OM.source_screen(cfg, screen) or OM.main_screen(cfg) or {}
        self.src = src
        self.src_w, self.src_h = OM.effective_size(cfg, src)
        lay = QVBoxLayout(self)
        self.canvas = _CropCanvas(self)
        self.canvas.changed.connect(self._on_rect)
        lay.addWidget(self.canvas, 1)
        self.lbl = QLabel("")
        self.lbl.setStyleSheet("color:#aaa;")
        lay.addWidget(self.lbl)
        btn = QHBoxLayout()
        btn.addStretch(1)
        b = QPushButton("完成")
        b.clicked.connect(self.accept)
        btn.addWidget(b)
        lay.addLayout(btn)
        thumb = None
        try:
            thumb = studio.preview.thumbs.get(str(src.get("id") or ""))
        except Exception:                                          # noqa: BLE001
            thumb = None
        self.canvas.set_image(thumb, OM.effective_color(cfg, src))
        self.set_uv(screen.get("uv") or [0, 0, 1, 1])

    def set_uv(self, uv):
        """外部（右侧 UV 输入框）改了 UV ⇒ 框跟着动。"""
        self.canvas.set_uv(uv)
        self._update_label()

    def _update_label(self):
        u0, v0, u1, v1 = self.canvas.uv
        self.lbl.setText("源屏 %d×%d　裁出 ≈ %d×%d"
                         % (self.src_w, self.src_h,
                            int(round(self.src_w * (u1 - u0))),
                            int(round(self.src_h * (v1 - v0)))))

    def _on_rect(self, uv):
        u0, v0, u1, v1 = [round(float(x), 4) for x in uv]
        self.screen["uv"] = [u0, v0, u1, v1]
        self.screen["width"] = max(16, int(round(self.src_w * (u1 - u0))))
        self.screen["height"] = max(16, int(round(self.src_h * (v1 - v0))))
        self._update_label()
        try:
            self.studio.apply()
        except Exception:                                          # noqa: BLE001
            pass


class OutputCanvasWindow(QDialog):
    """用户第 11 条：「输出画布…」独立窗口（多屏**单路**输出的 mapping 工具）。

    · 输出分辨率：自定义，上限 **16384（2^14）**
    · 在画布里**拖动**排布每块屏幕（不要求所有屏幕都摆上去，重叠也行）
    · 「导出 Mapping」也在这里（工具条上原来那个按钮已删除）
    ⚠ 只改 mapping / 布局数据，**不影响左边的多屏预览**。
    """

    MAX_SIDE = 16384          # 2^14

    def __init__(self, studio, parent=None):
        super().__init__(parent or studio)
        self.studio = studio
        self.setWindowTitle("输出画布（Mapping）")
        self.resize(940, 640)
        lay = QVBoxLayout(self)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("输出分辨率"))
        self.sp_w = QSpinBox()
        self.sp_h = QSpinBox()
        for sp in (self.sp_w, self.sp_h):
            sp.setRange(16, self.MAX_SIDE)
            sp.setToolTip("输出画布尺寸，最大 %d（2^14）" % self.MAX_SIDE)
            sp.valueChanged.connect(self._on_size)
        bar.addWidget(self.sp_w)
        bar.addWidget(QLabel("×"))
        bar.addWidget(self.sp_h)
        bar.addSpacing(10)
        self.btn_auto = QPushButton("自动排列")
        self.btn_auto.setToolTip("把主屏/独立屏边对边横向排开")
        self.btn_auto.clicked.connect(self._auto)
        bar.addWidget(self.btn_auto)
        self.btn_fit = QPushButton("适应窗口")
        self.btn_fit.clicked.connect(self._fit)
        bar.addWidget(self.btn_fit)
        bar.addStretch(1)
        self.btn_export = QPushButton("导出 Mapping…")
        self.btn_export.setToolTip("导出 mapping JSON + PNG 到指定目录")
        self.btn_export.clicked.connect(self._export)
        bar.addWidget(self.btn_export)
        lay.addLayout(bar)
        # ★ 超界提示（只提示，不改数据）：画布小于布局包围盒时显示一行黄字
        self.warn = QLabel("")
        self.warn.setWordWrap(True)
        self.warn.setStyleSheet(
            "color:#ffb300;font-size:12px;padding:2px 4px;"
            "border:1px solid #6b4e00;border-radius:4px;")
        self.warn.hide()
        lay.addWidget(self.warn)
        self.board = _CanvasBoard(studio)
        self.board.moved.connect(self._on_moved)
        lay.addWidget(self.board, 1)
        self.hint = QLabel(
            "拖动屏幕摆位置；显示的是**输出坐标**（写回配置的 layout），"
            "画布尺寸不够时会自动放大，绝不裁屏。不要求每块屏幕都摆上来。")
        self.hint.setStyleSheet("color:#888;")
        self.hint.setWordWrap(True)
        lay.addWidget(self.hint)

    def reload(self):
        cfg = self.studio.active_config()
        # ★ U5-1（用户第 5 条）：纯提示，**不改任何数据**（用户对"没动手却变了"很敏感）。
        #   新屏现在在**创建时**就排到已有屏右边（见 add_screen），重叠加只可能是老配置遗留。
        try:
            if cfg:
                src = OM.layout_sources(cfg)
                pos = [(int(x), int(y)) for x, y, _w, _h in
                       (OM.screen_rect(cfg, s) for s in src)]
                if len(src) > 1 and len(set(pos)) < len(pos):
                    self.hint.setText(
                        "⚠ 检测到 %d 块屏幕位置完全重叠，Mapping 里会互相遮挡 —— "
                        "点「自动排列」把它们横向排开（本窗口不会自己改你的布局）。"
                        % (len(pos) - len(set(pos)) + 1))
                else:
                    self.hint.setText(
                        "拖动屏幕摆位置；显示的是**输出坐标**（写回配置的 layout）。"
                        "拖动不会改动画布尺寸，要改就点「适应窗口」。")
        except Exception:                                          # noqa: BLE001
            pass
        self.board.set_config(cfg)
        if cfg:
            cw, ch = OM.combined_size(cfg)
            for sp, v in ((self.sp_w, cw), (self.sp_h, ch)):
                sp.blockSignals(True)
                sp.setValue(int(max(16, min(self.MAX_SIDE, v))))
                sp.blockSignals(False)
        self._update_warn()
        # ★ U5（用户第 5 条）：拖动屏幕**不再**自动"适应窗口"（画布/视图被自动改）。
        #   只在第一次打开窗口时自适应一次，之后完全交给用户点「适应窗口」。
        if not getattr(self, "_fitted_once", False):
            self._fitted_once = True
            self.board.fit()

    def _fit(self):
        """★「适应窗口」= **显式**把画布改成"刚好装下当前布局"（包围盒），再自适应视图。

        这是画布窗口里**唯一**会改 `combined_size` 的动作（用户主动点的）；
        输入框改尺寸时系统绝不偷偷改（见 `apply()` 里那段说明）。
        """
        cfg = self.studio.active_config()
        if cfg:
            bw = bh = 0
            for s in OM.layout_sources(cfg):
                x, y, w, h = OM.screen_rect(cfg, s)
                bw, bh = max(bw, x + w), max(bh, y + h)
            if bw > 0 and bh > 0:
                cfg["combined_size"] = [max(16, min(self.MAX_SIDE, int(bw))),
                                        max(16, min(self.MAX_SIDE, int(bh)))]
                self.studio.apply()
                for sp, v in ((self.sp_w, bw), (self.sp_h, bh)):
                    sp.blockSignals(True)
                    sp.setValue(int(max(16, min(self.MAX_SIDE, v))))
                    sp.blockSignals(False)
                self.board.set_config(cfg)
        self._update_warn()
        self.board.fit()

    def _update_warn(self):
        """画布 < 布局包围盒 ⇒ 显示一行黄字（**只提示，不改数据**）。"""
        try:
            cfg = self.studio.active_config()
            if not cfg:
                self.warn.hide()
                return
            cw, ch = OM.combined_size(cfg)
            bw = bh = 0
            for s in OM.layout_sources(cfg):
                x, y, w, h = OM.screen_rect(cfg, s)
                bw, bh = max(bw, x + w), max(bh, y + h)
            if bw > int(cw) or bh > int(ch):
                over = []
                if bw > int(cw):
                    over.append("宽 %d→%d" % (int(cw), int(bw)))
                if bh > int(ch):
                    over.append("高 %d→%d" % (int(ch), int(bh)))
                self.warn.setText(
                    "⚠ 画布 %d×%d 小于布局 %d×%d（%s），超出部分输出时会被裁掉。"
                    "不想裁就点「适应窗口」把画布扩到刚好装下。"
                    % (int(cw), int(ch), int(bw), int(bh), "、".join(over)))
                self.warn.show()
            else:
                self.warn.hide()
        except Exception:                                          # noqa: BLE001
            self.warn.hide()

    def _on_size(self, *_):
        cfg = self.studio.active_config()
        if not cfg:
            return
        cfg["combined_size"] = [max(16, min(self.MAX_SIDE, int(self.sp_w.value()))),
                                max(16, min(self.MAX_SIDE, int(self.sp_h.value())))]
        self.studio.apply()          # ⚠ apply 现在**不会**再改 combined_size
        self.board.set_config(cfg)
        self.board.fit()
        self._update_warn()          # 超界只提示，不改数据

    def _auto(self):
        cfg = self.studio.active_config()
        if not cfg:
            return
        OM.auto_layout(cfg, "h")
        self.studio.apply()
        self.board.set_config(cfg)
        self.board.fit()
        self._update_warn()          # 布局变了 ⇒ 超界提示跟着变

    def _on_moved(self):
        self.studio.apply()
        self.board.set_config(self.studio.active_config())
        self.board.update()

    def _export(self):
        self.studio.export_mapping()


# ---------------------------------------------------------------- 浮动素材库占位
class FloatingLibraryWindow(QWidget):
    """把演出台的素材库面板搬进一个可拖动浮动窗口（真正的关/开由 MainWindow 管）。"""

    def __init__(self, main, panel, parent=None):
        super().__init__(parent, Qt.Tool | Qt.Window)
        self.main = main
        self.setWindowTitle("素材库")
        self.setMinimumSize(420, 260)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.addWidget(panel)
        self._panel = panel

    def closeEvent(self, ev):
        ev.ignore()
        self.hide()
