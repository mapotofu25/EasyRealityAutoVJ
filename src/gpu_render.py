# -*- coding: utf-8 -*-
"""实验性 GPU 纹理合成器（AutoVJ 1.2，绑定 GPU 解码开关）。

只负责“把已经解出来的 QImage 图层按 cover/缩放/旋转/透明度画到一张 GPU 画布上”。
不支持的部分（码表、转场、特殊混合、颜色后处理）由 engine 自动回退到原 CPU 路径。
"""
from __future__ import annotations

import math
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter, QOpenGLContext, QOffscreenSurface
from PySide6.QtOpenGL import (QOpenGLBuffer, QOpenGLFramebufferObject,
                              QOpenGLShader, QOpenGLShaderProgram, QOpenGLTexture,
                              QOpenGLVertexArrayObject)


_VERT = """
#version 330 core
layout(location = 0) in vec2 a_pos;
layout(location = 1) in vec2 a_uv;
out vec2 v_uv;
void main() {
    gl_Position = vec4(a_pos, 0.0, 1.0);
    v_uv = a_uv;
}
"""

_FRAG = """
#version 330 core
in vec2 v_uv;
uniform sampler2D u_tex;
uniform float u_opacity;
out vec4 o_color;
void main() {
    vec4 c = texture(u_tex, v_uv);
    o_color = vec4(c.rgb, c.a * u_opacity);
}
"""


class GpuRenderer:
    def __init__(self, log=None):
        self._log = log or (lambda _m: None)
        self._ctx = None
        self._surf = None
        self._fbo = None
        self._w = 0
        self._h = 0
        self._prog = None
        self._vbo = None
        self._vao = None
        self._ok = False
        self._err = ""
        self._last_ms = 0.0
        self._frames = 0
        self._tex_cache = {}

    @property
    def available(self):
        return bool(self._ok)

    @property
    def last_error(self):
        return self._err

    def _fail(self, msg):
        self._ok = False
        self._err = str(msg)[:300]
        try:
            self._log("GPU 纹理合成不可用：%s" % self._err)
        except Exception:
            pass

    def ensure(self, w, h):
        w = max(16, int(w))
        h = max(16, int(h))
        if self._ok and self._fbo is not None and self._w == w and self._h == h:
            return True
        try:
            from PySide6.QtGui import QSurfaceFormat
            if self._ctx is None:
                fmt = QSurfaceFormat.defaultFormat()
                fmt.setVersion(3, 3)
                fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
                self._surf = QOffscreenSurface()
                self._surf.setFormat(fmt)
                self._surf.create()
                self._ctx = QOpenGLContext()
                self._ctx.setFormat(fmt)
                if not self._ctx.create() or not self._ctx.isValid():
                    self._fail("QOpenGLContext.create() 失败")
                    return False
                self._ctx.makeCurrent(self._surf)
                self._prog = QOpenGLShaderProgram()
                if not self._prog.addShaderFromSourceCode(
                        QOpenGLShader.ShaderTypeBit.Vertex, _VERT):
                    self._fail("顶点 shader：%s" % self._prog.log())
                    return False
                if not self._prog.addShaderFromSourceCode(
                        QOpenGLShader.ShaderTypeBit.Fragment, _FRAG):
                    self._fail("片元 shader：%s" % self._prog.log())
                    return False
                if not self._prog.link():
                    self._fail("shader link：%s" % self._prog.log())
                    return False
                self._vbo = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
                self._vbo.create()
                self._vao = QOpenGLVertexArrayObject()
                self._vao.create()
            self._ctx.makeCurrent(self._surf)
            if self._fbo is not None:
                self._fbo = None
            fbo = QOpenGLFramebufferObject(w, h)
            if not fbo.isValid():
                self._fail("FBO 创建失败 %dx%d" % (w, h))
                return False
            self._fbo = fbo
            self._w, self._h = w, h
            self._ok = True
            return True
        except Exception as e:                                  # noqa: BLE001
            self._fail("%s: %s" % (type(e).__name__, e))
            return False

    def _texture(self, img):
        if img is None or img.isNull():
            return None
        key = int(img.cacheKey())
        tex = self._tex_cache.get(key)
        if tex is not None:
            return tex
        # 控制缓存大小；旧纹理由 Qt 释放
        if len(self._tex_cache) > 24:
            self._tex_cache.clear()
        try:
            src = (img.convertToFormat(QImage.Format_RGBA8888_Premultiplied)
                   if img.hasAlphaChannel() else img.convertToFormat(QImage.Format_RGBA8888))
            tex = QOpenGLTexture(src,
                                 QOpenGLTexture.MipMapGeneration.DontGenerateMipMaps)
        except Exception:
            tex = QOpenGLTexture(src)
        if not tex.isCreated():
            return None
        tex.setMinificationFilter(QOpenGLTexture.Filter.Nearest)
        tex.setMagnificationFilter(QOpenGLTexture.Filter.Linear)
        tex.setWrapMode(QOpenGLTexture.WrapMode.ClampToEdge)
        self._tex_cache[key] = tex
        return tex

    def render(self, layers, w, h):
        """layers: [{img, opacity, x, y, w, h, rot}]，从上到下。返回 QImage 或 None。"""
        t0 = time.perf_counter()
        if not self.ensure(w, h):
            return None
        try:
            self._ctx.makeCurrent(self._surf)
            self._fbo.bind()
            try:
                from PySide6.QtGui import QOpenGLContext as _QC
                f = _QC.currentContext().functions()
                f.glViewport(0, 0, int(w), int(h))
                f.glDisable(f.GL_DEPTH_TEST)
                f.glEnable(f.GL_BLEND)
                f.glBlendFunc(f.GL_SRC_ALPHA, f.GL_ONE_MINUS_SRC_ALPHA)
                f.glClearColor(0.0, 0.0, 0.0, 0.0)
                f.glClear(f.GL_COLOR_BUFFER_BIT)
            except Exception:
                pass
            self._prog.bind()
            self._vao.bind()
            for lay in reversed(layers):
                img = lay.get("img")
                tex = self._texture(img)
                if tex is None:
                    continue
                tw, th = float(lay.get("w") or w), float(lay.get("h") or h)
                cx, cy = float(lay.get("x") or 0.0), float(lay.get("y") or 0.0)
                deg = float(lay.get("rot") or 0.0)
                rad = math.radians(deg)
                ca, sa = math.cos(rad), math.sin(rad)
                pts = [(-tw / 2, -th / 2), (tw / 2, -th / 2),
                       (tw / 2, th / 2), (-tw / 2, th / 2)]
                # 中心点 = 画布中心 + offset；转成 NDC
                cxp, cyp = w / 2.0 + cx, h / 2.0 + cy
                verts = []
                uvs = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
                for i, (px, py) in enumerate(pts):
                    x = cxp + px * ca - py * sa
                    y = cyp + px * sa + py * ca
                    nx = (x / w) * 2.0 - 1.0
                    ny = 1.0 - (y / h) * 2.0
                    verts.extend([nx, ny, uvs[i][0], uvs[i][1]])
                idx = [0, 1, 2, 0, 2, 3]
                data = []
                for i in idx:
                    data.extend(verts[i * 4:i * 4 + 4])
                self._vbo.bind()
                self._vbo.allocate(bytes(bytearray_from_floats(data)), len(data) * 4)
                self._prog.enableAttributeArray(0)
                self._prog.setAttributeBuffer(0, 0x1406, 0, 2, 4 * 4)  # GL_FLOAT
                self._prog.enableAttributeArray(1)
                self._prog.setAttributeBuffer(1, 0x1406, 2 * 4, 2, 4 * 4)
                tex.bind(0)
                from PySide6.QtGui import QOpenGLContext as _QC
                _f = _QC.currentContext().functions()
                _blend = str(lay.get("blend") or "normal")
                _f.glBlendEquation(0x8006)      # GL_FUNC_ADD
                if _blend == "screen":
                    _f.glBlendFunc(1, 0x0301)   # ONE, ONE_MINUS_SRC_COLOR
                elif _blend == "multiply":
                    _f.glBlendFunc(0x0306, 0x0303)  # DST_COLOR, ONE_MINUS_SRC_ALPHA
                elif _blend == "lighten":
                    _f.glBlendEquation(0x8008)  # GL_MAX
                    _f.glBlendFunc(1, 1)
                elif _blend == "darken":
                    _f.glBlendEquation(0x8007)  # GL_MIN
                    _f.glBlendFunc(1, 1)
                elif _blend in ("subtract", "reverse_subtract"):
                    _f.glBlendEquation(0x800B)  # GL_FUNC_REVERSE_SUBTRACT
                    _f.glBlendFunc(1, 1)
                else:
                    # 纹理已预乘 alpha ⇒ 用 ONE, ONE_MINUS_SRC_ALPHA
                    _f.glBlendFunc(1, 0x0303)
                _f.glUniform1i(self._prog.uniformLocation(b"u_tex"), 0)
                _f.glUniform1f(self._prog.uniformLocation(b"u_opacity"),
                               float(lay.get("opacity") or 1.0))
                _f.glDrawArrays(0x0004, 0, 6)  # GL_TRIANGLES
                tex.release()
                self._vbo.release()
                # 让出 GIL：Qt OpenGL 的纹理上传/绘制调用不会自己释放 GIL，
                # 连续 11 层会把 GUI/输出窗口饿住（现场表现为引擎 23ms、显示 150ms）。
                try:
                    import time as _t
                    _t.sleep(0)
                except Exception:
                    pass
            self._prog.release()
            self._fbo.release()
            img = self._fbo.toImage()
            self._frames += 1
            self._last_ms = (time.perf_counter() - t0) * 1000.0
            return img
        except Exception as e:                                  # noqa: BLE001
            try:
                self._fbo.release()
            except Exception:
                pass
            self._fail("%s: %s" % (type(e).__name__, e))
            return None


def bytearray_from_floats(vals):
    import struct
    return struct.pack("<%df" % len(vals), *[float(v) for v in vals])
