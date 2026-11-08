# -*- coding: utf-8 -*-
"""Optional GPU composition backend for the AutoVJ engine.

This is deliberately a **drop-in QPainter target**, not a new renderer.  The
engine already expresses every layer as QPainter draw calls, so moving those
calls from a CPU ``QImage`` to a ``QOpenGLPaintDevice`` gives GPU composition
without re-implementing crop/rotate/cover/alpha/transition geometry.

Important compatibility rules discovered by the A/B pixel tests:

* ``QImage.Format_ARGB32`` sources with alpha must be converted to
  ``Format_ARGB32_Premultiplied`` before being drawn by Qt's OpenGL paint
  engine; otherwise the engine blends them as if they were already
  premultiplied and the result is visibly wrong.
* Qt's OpenGL paint engine only implements SourceOver/Plus.  Screen, Multiply,
  Lighten, Darken and the project's custom Subtract path stay on the CPU
  compositor.  The engine checks the blend set before selecting this backend.
* Readback is one ``QOpenGLFramebufferObject.toImage()`` per composed frame.
  That is still much cheaper than CPU QPainter for 3+ layers at 720p/1080p.

The class never raises into the render loop: any failure disables the backend
for the rest of the process and the caller falls back to the CPU path.
"""
from __future__ import annotations

import time
import threading

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QOpenGLContext, QOffscreenSurface
from PySide6.QtOpenGL import QOpenGLFramebufferObject, QOpenGLPaintDevice


class GpuCompositor:
    def __init__(self, log=None):
        self._log = log or (lambda _m: None)
        self._ctx = None
        self._surf = None
        self._fbo = None
        self._w = 0
        self._h = 0
        self._ok = False
        self._failed = False
        self._last_error = ""
        self._frames = 0
        self._readback_ms = 0.0
        self._last_readback_ms = 0.0
        self._tid = None

    # ------------------------------------------------------------------
    @property
    def available(self):
        return self._ok and not self._failed

    @property
    def last_error(self):
        return self._last_error

    def _fail(self, msg):
        self._failed = True
        self._ok = False
        self._last_error = str(msg)[:300]
        try:
            self._log("GPU 合成已停用：%s" % self._last_error)
        except Exception:
            pass

    def ensure(self, w, h):
        w = max(16, int(w))
        h = max(16, int(h))
        if self._failed:
            return False
        if self._ok and self._fbo is not None and self._w == w and self._h == h:
            return True
        try:
            if self._ctx is None:
                fmt = QOpenGLContext().format()
                ctx = QOpenGLContext()
                ctx.setFormat(fmt)
                if not ctx.create():
                    self._fail("QOpenGLContext.create() 失败")
                    return False
                surf = QOffscreenSurface()
                surf.setFormat(ctx.format())
                surf.create()
                if not surf.isValid():
                    self._fail("QOffscreenSurface 无效")
                    return False
                self._ctx, self._surf = ctx, surf
                self._tid = threading.get_ident()
            if self._tid is not None and self._tid != threading.get_ident():
                self._fail("GL 上下文跨线程使用（owner=%s, now=%s）"
                           % (self._tid, threading.get_ident()))
                return False
            if not self._ctx.makeCurrent(self._surf):
                self._fail("makeCurrent() 失败")
                return False
            if self._fbo is not None:
                self._fbo = None
            fbo = QOpenGLFramebufferObject(w, h)
            if not fbo.isValid():
                self._fail("QOpenGLFramebufferObject 无效")
                return False
            self._fbo = fbo
            self._w, self._h = w, h
            self._ok = True
            self._log("GPU 合成已启用：%dx%d（单上下文 / 引擎线程）" % (w, h))
            return True
        except Exception as e:                                    # noqa: BLE001
            self._fail("初始化异常：%s" % e)
            return False

    # ------------------------------------------------------------------
    def begin(self, w, h):
        """Bind the FBO and return a QPainter targeting an OpenGL paint device."""
        if not self.ensure(w, h):
            return None
        try:
            self._ctx.makeCurrent(self._surf)
            self._fbo.bind()
            dev = QOpenGLPaintDevice(self._w, self._h)
            p = QPainterOnDevice(dev)
            return p
        except Exception as e:                                    # noqa: BLE001
            self._fail("begin 异常：%s" % e)
            return None

    def end(self, painter):
        """Finish a painter, read the FBO back and return a QImage."""
        try:
            if painter is not None:
                try:
                    if painter.isActive():
                        painter.end()
                except Exception:
                    pass
            if self._fbo is None:
                return None
            t0 = time.perf_counter()
            img = self._fbo.toImage()
            self._last_readback_ms = (time.perf_counter() - t0) * 1000.0
            self._readback_ms += self._last_readback_ms
            self._frames += 1
            self._fbo.release()
            return img
        except Exception as e:                                    # noqa: BLE001
            self._fail("end/readback 异常：%s" % e)
            try:
                if self._fbo is not None:
                    self._fbo.release()
            except Exception:
                pass
            return None

    def stats(self):
        n = max(1, self._frames)
        return {"frames": self._frames,
                "readback_ms_avg": self._readback_ms / n,
                "last_readback_ms": self._last_readback_ms,
                "size": (self._w, self._h)}

    def close(self):
        try:
            self._fbo = None
            self._surf = None
            self._ctx = None
        except Exception:
            pass
        self._ok = False


# Imported late so the module stays importable even if QtOpenGL is absent.
from PySide6.QtGui import QPainter


class QPainterOnDevice(QPainter):
    """QPainter subclass that keeps the paint device alive for the frame."""

    def __init__(self, dev):
        super().__init__(dev)
        self._dev = dev
