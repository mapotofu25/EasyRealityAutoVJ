# -*- coding: utf-8 -*-
"""独立输出窗口：标题固定 Easy Reality Output，兼容 OBS 窗口捕获
- 记忆窗口尺寸/位置，宽高比锁定，自适应不变形
- 无边框 / 置顶 / 隐藏鼠标 / 一键全屏
"""

from PySide6.QtCore import Qt, QRectF, QSize, QTimer, Signal
from PySide6.QtGui import QImage, QPainter, QGuiApplication
from PySide6.QtWidgets import QWidget, QApplication, QVBoxLayout

try:
    from PySide6.QtOpenGLWidgets import QOpenGLWidget
except Exception:                                              # noqa: BLE001
    QOpenGLWidget = None


def _frame_target(img, W, H, aspect_lock=True):
    iw, ih = img.width(), img.height()
    if iw <= 0 or ih <= 0 or W <= 0 or H <= 0:
        return None
    if aspect_lock:
        s = min(W / iw, H / ih)
        tw, th = iw * s, ih * s
        return QRectF((W - tw) / 2.0, (H - th) / 2.0, tw, th), (iw, ih)
    return QRectF(0, 0, W, H), (iw, ih)


def _paint_image(p, img, W, H, aspect_lock=True):
    p.fillRect(0, 0, W, H, Qt.black)
    if img is None or img.isNull():
        return
    got = _frame_target(img, W, H, aspect_lock)
    if got is None:
        return
    target, (iw, ih) = got
    if (abs(target.x()) < 0.5 and abs(target.y()) < 0.5
            and abs(target.width() - iw) < 0.5
            and abs(target.height() - ih) < 0.5):
        p.drawImage(0, 0, img)                 # 1:1 快路径
    else:
        p.drawImage(target, img, QRectF(0, 0, iw, ih))


if QOpenGLWidget is None:
    # ★ 2026-11-20.23：打包漏掉 Qt6OpenGL.dll 时，`import PySide6.QtOpenGLWidgets`
    #   会抛 ImportError（DLL load failed），上面的 except 把 QOpenGLWidget 置 None。
    #   旧代码紧接着 `class _GLView(QOpenGLWidget)` ⇒ TypeError: NoneType takes no
    #   arguments，整个 App 连启动都起不来（用户朋友实测就是这个）。
    #   这里改为占位类；`OutputWindow.__init__` 有 `QOpenGLWidget is not None`
    #   判断，占位类永远不会被实例化，输出窗口自动走 _CpuView。
    class _GLView(QWidget):
        pass
else:
    class _GLView(QOpenGLWidget):
        """GPU 缩放显示：QPainter 的 OpenGL 后端把 QImage 上传后由显卡缩放。

        输出窗口最常见的卡顿源是 4K 显示器上把 720p/1080p 画布用 CPU
        SmoothPixmapTransform 放大：实测 720p→4K 单帧 13ms wall / 34ms CPU，
        正好顶满 60fps 预算。这里改为 GPU 纹理缩放（同一帧实测约 0.4ms）。
        """

        def __init__(self, parent=None):
            super().__init__(parent)
            self._img = None
            self.aspect_lock = True
            self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            try:
                self.setUpdateBehavior(QOpenGLWidget.NoPartialUpdate)
            except Exception:
                pass

        def set_frame(self, img):
            self._img = img
            self.update()

        def paintGL(self):
            p = QPainter(self)
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            _paint_image(p, self._img, self.width(), self.height(), self.aspect_lock)
            p.end()


class _CpuView(QWidget):
    """GPU 不可用时的原 CPU 回退路径（保持旧行为）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._img = None
        self.aspect_lock = True
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def set_frame(self, img):
        self._img = img
        self.update()

    def paintEvent(self, _ev):
        import os
        import time
        _t0 = time.perf_counter()
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        _paint_image(p, self._img, self.width(), self.height(), self.aspect_lock)
        p.end()
        if os.environ.get("AUTOVJ_DRAW_PROBE") == "1":
            try:
                import stallwatch as _sw
                _sw.log_line("[OW] paint=%.1fms size=%dx%d img=%s" %
                             ((time.perf_counter() - _t0) * 1000.0,
                              self.width(), self.height(),
                              "%dx%d" % (self._img.width(), self._img.height())
                              if self._img is not None else "None"))
            except Exception:                                  # noqa: BLE001
                pass


class OutputWindow(QWidget):
    visibility_changed = Signal(bool)

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

        # ---- 画面子控件：按缩放倍率自动选择 CPU / GPU ----
        # 1:1 或小幅缩放时用 CPU（1280x720 输出实测 0.08~0.2ms/帧，比上传
        # GPU 纹理更稳，也能绕开某些虚拟显示器/采集卡驱动的 OpenGL 卡顿）；
        # 只有真正需要放大（例如 720p 画布全屏到 1080p/4K）才启用 GPU。
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._gl_view = None
        self._cpu_view = _CpuView(self)
        self._cpu_view.aspect_lock = self.aspect_lock
        self._layout.addWidget(self._cpu_view)
        self._view = self._cpu_view
        self._gpu_enabled = bool(self.cfg["output"].get("gpu_display", True))
        if QOpenGLWidget is not None and self._gpu_enabled:
            try:
                self._gl_view = _GLView(self)
                self._gl_view.aspect_lock = self.aspect_lock
                self._gl_view.hide()
                self._layout.addWidget(self._gl_view)
            except Exception:
                self._gl_view = None
        self._backend_key = None

    # ---------- 画面 ----------
    def moveEvent(self, e):
        # ★ 2026-11-08：不再因为拖动输出窗口而丢帧/暂停重绘。
        #   旧实现每次 moveEvent 都 `_pause_repaint()`，连续拖动时 set_frame 一直
        #   提前 return ⇒ 输出窗口画面整段冻住（用户报「拖任何窗口都会卡」）。
        super().moveEvent(e)

    def set_frame(self, img: QImage):
        self._img = img
        key = (img.width(), img.height(), self.width(), self.height(), self.aspect_lock)
        if key != self._backend_key:
            self._backend_key = key
            self._sync_view_backend()
        try:
            self._view.set_frame(img)
        except Exception:
            self.update()
        self._frame_n = getattr(self, "_frame_n", 0) + 1
        if self._frame_n % 120 == 0:
            self._check_gl_view()
            self._sync_view_backend()

    def debug_backend(self):
        """诊断用：当前实际在画的视图（CPU / GPU）与是否有效。"""
        try:
            return "%s(valid=%s)" % (type(self._view).__name__,
                                     bool(self._gl_view.isValid())
                                     if self._view is self._gl_view else "-")
        except Exception:
            return type(getattr(self, "_view", None)).__name__

    def _want_gpu(self):
        """只在真正需要放大时用 GPU（1:1 输出 CPU 更稳，且成本可忽略）。"""
        if not self._gpu_enabled or self._gl_view is None or self._img is None:
            return False
        got = _frame_target(self._img, self.width(), self.height(), self.aspect_lock)
        if got is None:
            return False
        target, (iw, ih) = got
        if iw <= 0 or ih <= 0:
            return False
        # 2026-11-08：只有真正的大倍率放大（≥1.8×，例如 1080p→4K）才用 GPU。
        # 720p/1080p 画布全屏到 1080p 时（1.5×）CPU 只要约 3~4ms/帧，改用 CPU
        # 反而更稳：QOpenGLWidget 会和 Spout / GPU 解码抢同一个显卡驱动，
        # 码表演出时容易出现 GUI 线程长时间卡住（[OUT] backlog 飙到几十）。
        return (target.width() >= iw * 1.80) or (target.height() >= ih * 1.80)

    def _sync_view_backend(self):
        """按当前画布/窗口尺寸切换 CPU/GPU 视图；GPU 失效时保持 CPU。"""
        try:
            want = self._want_gpu()
            if want:
                try:
                    # 窗口已显示时先切过去让 QOpenGLWidget 建上下文；
                    # `_check_gl_view` 会在 300ms 后校验，失败自动退回 CPU。
                    ok = bool(self._gl_view.isValid()) or bool(self.isVisible())
                except Exception:
                    ok = False
                if not ok:
                    want = False
            cur = self._view
            if want and cur is not self._gl_view:
                self._cpu_view.hide()
                self._gl_view.aspect_lock = self.aspect_lock
                self._gl_view.show()
                self._view = self._gl_view
                if self._img is not None:
                    self._gl_view.set_frame(self._img)
            elif (not want) and cur is not self._cpu_view:
                self._gl_view.hide()
                self._cpu_view.aspect_lock = self.aspect_lock
                self._cpu_view.show()
                self._view = self._cpu_view
                if self._img is not None:
                    self._cpu_view.set_frame(self._img)
        except Exception:
            pass

    def _check_gl_view(self):
        """GPU 视图创建失败/上下文丢失时自动退回 CPU 视图（绝不黑屏）。"""
        gl = getattr(self, "_gl_view", None)
        if gl is None or self._view is not gl:
            return
        try:
            ok = bool(gl.isValid())
        except Exception:
            ok = False
        if not ok:
            try:
                gl.hide()
                self._cpu_view.aspect_lock = self.aspect_lock
                self._cpu_view.show()
                self._view = self._cpu_view
                if self._img is not None:
                    self._cpu_view.set_frame(self._img)
            except Exception:
                pass

    def paintEvent(self, _ev):
        # 子控件（GPU/CPU 视图）负责画面；这里只保证边缘/暴露区域是黑底。
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.black)
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

    def _dpr(self, scr=None):
        """当前屏幕的缩放比例（Windows 150% ⇒ 1.5；100% ⇒ 1.0）。

        ⚠ 必须用它：Qt6 的 `resize()` / `geometry()` 全是**逻辑像素**，而用户在
          设置里填的"输出分辨率 1600×900"指的是**物理像素**。
          150% 缩放下直接 `resize(1600,900)` ⇒ 物理 2400×1350 ⇒ **超出屏幕**
          （用户 2026-10-01 报的就是这个）。正确做法：1600÷1.5 = 逻辑 1067×600，
          系统再 ×1.5 渲染，物理正好 1600×900。
        """
        try:
            s = scr if scr is not None else (self.screen() or QGuiApplication.primaryScreen())
            return float(s.devicePixelRatio()) if s is not None else 1.0
        except Exception:                                          # noqa: BLE001
            return 1.0

    def apply_window_size(self):
        """窗口模式：**严格按设置里的输出分辨率**重置窗口大小（不再缩到屏幕 80%）。

        以前会缩到屏幕的 80%，所以在 1080p 屏上选 1080p 反而得到 1536×864——
        用户看到的「设置的分辨率没生效」就是这么来的。
        放不下（分辨率比屏幕还大）时贴屏幕左上角，保证画面开头可见。
        """
        # 解除可能的尺寸约束（否则 resize 可能被 min/max size 卡住不生效）
        self.setMinimumSize(1, 1)
        self.setMaximumSize(16777215, 16777215)
        # 用户填的是**物理**分辨率；Qt 用的是**逻辑**像素 ⇒ 先除以缩放比例。
        w = int(self.cfg["output"].get("width", 1280))
        h = int(self.cfg["output"].get("height", 720))
        scr = self.screen() or QGuiApplication.primaryScreen()
        dpr = self._dpr(scr)
        lw = max(1, int(round(w / dpr)))
        lh = max(1, int(round(h / dpr)))
        self.resize(lw, lh)
        sg = scr.availableGeometry() if scr is not None else None
        if sg is None:
            return
        # ⚠ 边界判断要用**物理**尺寸：availableGeometry() 给的是逻辑尺寸
        pw, ph = int(sg.width() * dpr), int(sg.height() * dpr)
        if w > pw or h > ph:
            self.move(sg.x(), sg.y())
            return
        geo = getattr(self, "_win_geom", None) or self.cfg["output"].get("geometry")
        x = y = None
        if geo and len(geo) == 4:
            x, y = int(geo[0]), int(geo[1])
        if (x is None or y is None
                or not (sg.x() - 50 <= x <= sg.x() + sg.width())
                or not (sg.y() - 50 <= y <= sg.y() + sg.height())):
            x = sg.x() + (sg.width() - lw) // 2
            y = sg.y() + (sg.height() - lh) // 2
        x = max(sg.x(), min(x, sg.x() + sg.width() - lw))
        y = max(sg.y(), min(y, sg.y() + sg.height() - lh))
        self.move(x, y)

    def _restore_window_geom(self):
        scr = self.screen() or QGuiApplication.primaryScreen()
        sg = scr.availableGeometry() if scr is not None else None
        geo = getattr(self, "_win_geom", None)
        ow, oh = self.cfg["output"].get("width", 1280), self.cfg["output"].get("height", 720)
        if not geo or len(geo) != 4:
            # 没有记录过：按输出分辨率给一个不超过屏幕 65% 的窗口
            if sg is not None:
                # ⚠ 同上：ow/oh 是**物理**分辨率，屏幕尺寸是**逻辑**的，先换算再比
                dpr = self._dpr(scr)
                lw0 = max(1.0, ow / dpr)
                lh0 = max(1.0, oh / dpr)
                w = min(int(lw0), int(sg.width() * 0.65))
                h = min(int(lh0), int(sg.height() * 0.65))
                if lw0 and lh0:
                    k = min(w / lw0, h / lh0)
                    w, h = max(320, int(lw0 * k)), max(180, int(lh0 * k))
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

    def _set_flags_keep_state(self, apply_flags):
        """改窗口标志时**保持「全屏 / 可见」状态不变**。

        ⚠ Qt 在可见窗口（尤其全屏）上改 window flags 会隐藏并重建原生窗口 ⇒ 全屏会被踢掉
        （表现为「全屏下改边框/置顶，画面缩回窗口」）。这里记下改前状态、改完恢复回去；
        **非全屏行为与改动前逐值一致**（原来就是 `if visible: self.show()`）。
        """
        was_fullscreen = self.isFullScreen()
        visible = self.isVisible()
        apply_flags()
        if was_fullscreen:
            self.showFullScreen()
        elif visible:
            self.show()

    def set_borderless(self, on: bool):
        # ⚠ setWindowFlags 是整体替换：必须自己保留「窗口置顶」，
        #   否则用户勾了置顶后一碰无边框，置顶就丢了。
        flags = Qt.Window
        if on:
            flags |= Qt.FramelessWindowHint
        try:
            if (self.cfg["output"].get("always_top")
                    or bool(self.windowFlags() & Qt.WindowStaysOnTopHint)):
                flags |= Qt.WindowStaysOnTopHint
        except Exception:                                      # noqa: BLE001
            pass
        self._set_flags_keep_state(lambda: self.setWindowFlags(flags))

    def set_always_top(self, on: bool):
        self._set_flags_keep_state(lambda: self.setWindowFlag(Qt.WindowStaysOnTopHint, on))

    def set_mouse_hidden(self, on: bool):
        self._mouse_hidden = on
        cursor = Qt.BlankCursor if on else Qt.ArrowCursor
        self.setCursor(cursor)
        try:
            self._view.setCursor(cursor)
        except Exception:
            pass

    def set_aspect_lock(self, on: bool):
        self.aspect_lock = bool(on)
        for v in (getattr(self, "_view", None), getattr(self, "_cpu_view", None),
                  getattr(self, "_gl_view", None)):
            if v is not None:
                try:
                    v.aspect_lock = bool(on)
                    v.update()
                except Exception:
                    pass

    # ---------- 记忆几何 ----------
    def showEvent(self, ev):
        super().showEvent(ev)
        try:
            self.visibility_changed.emit(True)
        except Exception:
            pass
        # QOpenGLWidget 的上下文要 show 之后才创建；给驱动 300ms，失败则回退 CPU。
        QTimer.singleShot(0, self._sync_view_backend)
        QTimer.singleShot(300, self._check_gl_view)
        QTimer.singleShot(350, self._sync_view_backend)
        QTimer.singleShot(1500, self._check_gl_view)

    def hideEvent(self, ev):
        super().hideEvent(ev)
        try:
            self.visibility_changed.emit(False)
        except Exception:
            pass

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
        # ⚠ 这里曾经有**两份** resizeEvent：先定义的这份带 _pause_repaint()，后定义的
        #   只调 super() —— 后者静默覆盖前者，「移动/缩放窗口期间暂停重绘」就失效了
        #   （表现为拖窗口时预览抢主线程，越拖越卡）。合并成一份。
        # ★ 2026-11-08：现在**不再暂停**输出重绘；拖动/缩放都继续按帧更新。
        #   尺寸变化后重新判断是否需要 GPU 放大；延迟一点，避免拖动过程中反复切换。
        QTimer.singleShot(250, self._sync_view_backend)
        super().resizeEvent(ev)
