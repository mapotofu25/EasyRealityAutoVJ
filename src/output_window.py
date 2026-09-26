# -*- coding: utf-8 -*-
"""独立输出窗口：标题固定 Easy Reality Output，兼容 OBS 窗口捕获
- 记忆窗口尺寸/位置，宽高比锁定，自适应不变形
- 无边框 / 置顶 / 隐藏鼠标 / 一键全屏
"""
import time

from PySide6.QtCore import Qt, QRectF, QSize, QTimer
from PySide6.QtGui import QImage, QPainter, QGuiApplication
from PySide6.QtWidgets import QWidget, QApplication


class OutputWindow(QWidget):
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.cfg = config
        self.setWindowTitle("Easy Reality Output")
        self.setObjectName("AutoVJOutputWindow")
        self._img = None
        self.aspect_lock = bool(self.cfg["output"].get("aspect_lock", True))
        self._mouse_hidden = False
        self._win_geom = None   # 最近一次窗口模式的几何（退出全屏时还原用）
        self._drag_pos = None   # 无边框窗口拖拽用
        self._drag_pause_until = 0.0   # 拖动/缩放期间暂停 60fps 重绘（防卡顿）

        # 尺寸永远按设置里的输出分辨率（用户选 1080p 就按 1080p 建窗口）；
        # 上次记忆的几何只拿来恢复**位置**——以前连尺寸一起套用，
        # 结果「设置里选了 1080p、窗口却是上次的小尺寸」。
        self.resize(int(self.cfg["output"].get("width", 1280)),
                    int(self.cfg["output"].get("height", 720)))
        geo = self.cfg["output"].get("geometry")
        if geo and len(geo) == 4:
            self.move(int(geo[0]), int(geo[1]))
        if self.cfg["output"].get("borderless"):
            self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        if self.cfg["output"].get("always_top"):
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)

    # ---------- 画面 ----------
    def _pause_repaint(self):
        """拖动/缩放期间暂停 60fps 重绘（0.25s 无移动自动恢复），防拖动卡顿"""
        self._drag_pause_until = time.perf_counter() + 0.25

    def moveEvent(self, e):
        self._pause_repaint()
        super().moveEvent(e)

    def resizeEvent(self, e):
        self._pause_repaint()
        super().resizeEvent(e)

    def set_frame(self, img: QImage):
        if time.perf_counter() < self._drag_pause_until:
            return   # 拖动中只丢帧不重绘
        self._img = img
        self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.fillRect(self.rect(), Qt.black)
        if self._img is None or self._img.isNull():
            p.end()
            return
        iw, ih = self._img.width(), self._img.height()
        W, H = self.width(), self.height()
        if iw <= 0 or ih <= 0 or W <= 0 or H <= 0:
            p.end()
            return
        if self.aspect_lock:
            # 按宽高比缩放**铺满窗口**；比例不一致时留黑边并把画面**居中**。
            s = min(W / iw, H / ih)
            tw, th = iw * s, ih * s
            target = QRectF((W - tw) / 2.0, (H - th) / 2.0, tw, th)
        else:
            target = QRectF(0, 0, W, H)      # 拉伸铺满（忽略宽高比）
        # ⚠ 必须用「目标矩形 + QRectF 源矩形」这个重载才会缩放。
        # 旧写法 p.drawImage(x, y, img, sx, sy, sw, sh) 是 Qt 的 **1:1 原尺寸**贴图
        # （那个重载只画子矩形、不做缩放，算出来的 tw/th 只被拿去定位），
        # 所以 720p 画布在大屏上永远是 1280×720 大小、贴在左上角 ——
        # 用户实测「设置 720、屏幕 1080、没拉伸」就是它。
        p.drawImage(target, self._img, QRectF(0, 0, iw, ih))
        p.end()

    # ---------- 窗口行为 ----------
    def mousePressEvent(self, e):
        # 无边框窗口：左键按住可拖拽移动（避免挡住对话框时无法移开）
        if e.button() == Qt.LeftButton and (self.windowFlags() & Qt.FramelessWindowHint):
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag_pos is not None and (e.buttons() & Qt.LeftButton):
            self.move(e.globalPosition().toPoint() - self._drag_pos)
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None
        super().mouseReleaseEvent(e)

    def _remember_window_geom(self):
        """进入全屏前记住窗口位置/尺寸（全屏会把窗口撑成整屏，切回来必须还原）"""
        if not self.isFullScreen():
            g = self.geometry()
            # 全屏时窗口会被撑满，只在正常状态下记录
            scr = self.screen() or QGuiApplication.primaryScreen()
            if scr is not None:
                sg = scr.availableGeometry()
                if g.width() < sg.width() or g.height() < sg.height():
                    self._win_geom = [g.x(), g.y(), g.width(), g.height()]
            else:
                self._win_geom = [g.x(), g.y(), g.width(), g.height()]

    def show_fullscreen_on(self, screen):
        """全屏输出到指定显示器（QScreen）——铺满整屏，画面按宽高比缩放"""
        self._remember_window_geom()
        try:
            wh = self.windowHandle()
            if wh is not None:
                wh.setScreen(screen)
            self.setGeometry(screen.geometry())
        except Exception:
            pass
        self.showFullScreen()
        # 进入全屏后再确认几次几何：Windows 多屏 / DPI 缩放下 showFullScreen 偶尔只占一角，
        # 表现为「全屏了但画面缩在左上角」（用户实测反馈）
        self._reassert_fullscreen_later(screen)

    def _reassert_fullscreen_later(self, screen):
        for delay in (0, 120, 400):
            QTimer.singleShot(delay, lambda s=screen: self._reassert_fullscreen(s))

    def _reassert_fullscreen(self, screen):
        if not self.isFullScreen():
            return
        try:
            g = screen.geometry()
            if self.geometry() != g:
                self.setGeometry(g)
        except Exception:
            pass

    def show_windowed(self):
        """恢复普通窗口模式：每次都按设置的输出分辨率重置窗口大小"""
        self.showNormal()
        self.apply_window_size()
        g = self.geometry()
        self._win_geom = [g.x(), g.y(), g.width(), g.height()]

    def reset_window_size(self):
        """手动重置窗口大小：退出全屏，按输出分辨率强制重置并显示"""
        if self.isFullScreen():
            self.setWindowState(Qt.WindowNoState)
            self.showNormal()
        self.apply_window_size()
        self.show()
        self.raise_()

    def apply_window_size(self):
        """窗口模式：**严格按设置里的输出分辨率**重置窗口大小（不再缩到屏幕 80%）。

        以前会缩到屏幕的 80%，所以在 1080p 屏上选 1080p 反而得到 1536×864——
        用户看到的「设置的分辨率没生效」就是这么来的。
        放不下（分辨率比屏幕还大）时贴屏幕左上角，保证画面开头可见。
        """
        # 解除可能的尺寸约束（否则 resize 可能被 min/max size 卡住不生效）
        self.setMinimumSize(1, 1)
        self.setMaximumSize(16777215, 16777215)
        w = int(self.cfg["output"].get("width", 1280))
        h = int(self.cfg["output"].get("height", 720))
        self.resize(w, h)
        scr = self.screen() or QGuiApplication.primaryScreen()
        sg = scr.availableGeometry() if scr is not None else None
        if sg is None:
            return
        if w > sg.width() or h > sg.height():
            self.move(sg.x(), sg.y())
            return
        geo = getattr(self, "_win_geom", None) or self.cfg["output"].get("geometry")
        x = y = None
        if geo and len(geo) == 4:
            x, y = int(geo[0]), int(geo[1])
        if (x is None or y is None
                or not (sg.x() - 50 <= x <= sg.x() + sg.width())
                or not (sg.y() - 50 <= y <= sg.y() + sg.height())):
            x = sg.x() + (sg.width() - w) // 2
            y = sg.y() + (sg.height() - h) // 2
        x = max(sg.x(), min(x, sg.x() + sg.width() - w))
        y = max(sg.y(), min(y, sg.y() + sg.height() - h))
        self.move(x, y)

    def _restore_window_geom(self):
        scr = self.screen() or QGuiApplication.primaryScreen()
        sg = scr.availableGeometry() if scr is not None else None
        geo = getattr(self, "_win_geom", None)
        ow, oh = self.cfg["output"].get("width", 1280), self.cfg["output"].get("height", 720)
        if not geo or len(geo) != 4:
            # 没有记录过：按输出分辨率给一个不超过屏幕 65% 的窗口
            if sg is not None:
                w = min(int(ow), int(sg.width() * 0.65))
                h = min(int(oh), int(sg.height() * 0.65))
                if ow and oh:
                    k = min(w / ow, h / oh)
                    w, h = max(320, int(ow * k)), max(180, int(oh * k))
                geo = None
                self.resize(w, h)
                if sg is not None:
                    self.move(sg.x() + (sg.width() - w) // 2, sg.y() + (sg.height() - h) // 2)
            return
        w, h = int(geo[2]), int(geo[3])
        # 记录值超出当前屏幕可用范围时按比例缩小
        if sg is not None and (w > sg.width() or h > sg.height()):
            k = min(sg.width() / max(w, 1), sg.height() / max(h, 1)) * 0.9
            w, h = max(320, int(w * k)), max(180, int(h * k))
        self.resize(w, h)
        x = int(geo[0])
        y = int(geo[1])
        if sg is not None:
            x = max(sg.x(), min(x, sg.x() + sg.width() - w))
            y = max(sg.y(), min(y, sg.y() + sg.height() - h))
        self.move(x, y)

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.show_windowed()
        else:
            self._remember_window_geom()
            self.showFullScreen()
            scr = self.screen() or QGuiApplication.primaryScreen()
            if scr is not None:
                self._reassert_fullscreen_later(scr)

    def set_borderless(self, on: bool):
        flags = Qt.FramelessWindowHint | Qt.Window if on else Qt.Window
        visible = self.isVisible()
        self.setWindowFlags(flags)
        if visible:
            self.show()

    def set_always_top(self, on: bool):
        visible = self.isVisible()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, on)
        if visible:
            self.show()

    def set_mouse_hidden(self, on: bool):
        self._mouse_hidden = on
        self.setCursor(Qt.BlankCursor if on else Qt.ArrowCursor)

    # ---------- 记忆几何 ----------
    def closeEvent(self, ev):
        """只记窗口位置/尺寸（geometry）——**不覆盖 width/height**。

        width/height 是「设置里的输出分辨率」，由用户在输出设置里选定；
        以前关闭窗口会把窗口实际大小写回去，于是用户选的分辨率被悄悄改掉
        （选 1080p → 窗口被拖小 → 下次启动变回小尺寸）。
        """
        g = None
        if not self.isFullScreen():
            g = self.geometry()
        elif getattr(self, "_win_geom", None):
            g = self._win_geom
        if g is not None:
            self.cfg["output"]["geometry"] = [g.x(), g.y(), g.width(), g.height()]
        self.cfg.save()
        super().closeEvent(ev)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
