# -*- coding: utf-8 -*-
"""完整 GPU 场景合成（引擎线程，离屏 FBO）。

与旧 `gpu_render` 的区别：

* **所有图层**（普通 / Kv / 码表子层 / 转场）都按引擎给出的描述绘制；
* **颜色 LUT 在合成之后**做一次全屏 pass，不再逐层套 LUT（旧实现逐层套色
  导致偏色/花屏）；
* 支持 normal / screen / multiply / lighten / darken / add / subtract 混合；
* 每块屏一个 FBO，读回一张 ARGB32_Premultiplied 图，直接交给
  NDI/Spout/输出窗/预览，不再回退 CPU 合成；
* 纹理按图层槽位缓存并复用，视频帧只做一次上传，不每帧新建 QOpenGLTexture。

调用约定（引擎线程）：

    stage = SceneStage(log)
    img = stage.compose({"w": 1920, "h": 1080, "alpha": False,
                         "layers": [descriptor, ...],   # 从上到下
                         "lut": QImage|None,
                         "blackout": False})
"""

from __future__ import annotations

import math
import os
import struct
import threading
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import (QImage, QOpenGLContext, QOffscreenSurface,
                           QSurfaceFormat)
from PySide6.QtOpenGL import (QOpenGLBuffer, QOpenGLFramebufferObject,
                              QOpenGLFramebufferObjectFormat,
                              QOpenGLShader, QOpenGLShaderProgram,
                              QOpenGLTexture, QOpenGLVertexArrayObject)

_GL_BLEND = 0x0BE2
_GL_DEPTH_TEST = 0x0B71
_GL_SCISSOR_TEST = 0x0C11
_GL_COLOR_BUFFER_BIT = 0x00004000
_GL_TEXTURE_2D = 0x0DE1
_GL_TEXTURE0 = 0x84C0
_GL_TEXTURE1 = 0x84C1
_GL_TRIANGLE_STRIP = 0x0005
_GL_FLOAT = 0x1406
_GL_RGBA8 = 0x8058
_GL_FUNC_ADD = 0x8006
_GL_FUNC_REVERSE_SUBTRACT = 0x800B
_GL_MAX = 0x8008
_GL_MIN = 0x8007
_GL_ONE = 1
_GL_SRC_ALPHA = 0x0302
_GL_ONE_MINUS_SRC_ALPHA = 0x0303
_GL_ONE_MINUS_SRC_COLOR = 0x0301
_GL_DST_COLOR = 0x0306
_GL_CLAMP_TO_EDGE = 0x812F

_VERT = """
#version 330 core
layout(location = 0) in vec2 a_pos;   // NDC
layout(location = 1) in vec2 a_uv;
out vec2 v_uv;
void main() {
    v_uv = a_uv;
    gl_Position = vec4(a_pos, 0.0, 1.0);
}
"""

_LAYER_FRAG = """
#version 330 core
in vec2 v_uv;
uniform sampler2D u_tex;
uniform float u_opacity;
out vec4 o_color;
void main() {
    vec4 c = texture(u_tex, v_uv);
    o_color = c * u_opacity;      // 预乘 alpha：四通道同乘
}
"""

_FINAL_FRAG = """
#version 330 core
in vec2 v_uv;
uniform sampler2D u_tex;
uniform sampler2D u_lut;
uniform float u_use_lut;
out vec4 o_color;
void main() {
    vec4 c = texture(u_tex, v_uv);
    if (u_use_lut > 0.5) {
        float a = max(c.a, 0.0001);
        vec3 s = c.rgb / a;
        s.r = texture(u_lut, vec2(clamp(s.r, 0.0, 1.0), 0.5)).r;
        s.g = texture(u_lut, vec2(clamp(s.g, 0.0, 1.0), 0.5)).g;
        s.b = texture(u_lut, vec2(clamp(s.b, 0.0, 1.0), 0.5)).b;
        c = vec4(s * a, c.a);
    }
    o_color = c;
}
"""

_BLENDS = {"normal", "sourceover", "add", "plus", "screen", "multiply",
           "lighten", "darken", "subtract"}


class SceneStage:
    """离屏 GPU 合成器。一个实例只能在一个线程里用（引擎渲染线程）。"""

    def __init__(self, log=None):
        self._log = log or (lambda _m: None)
        self._ctx = None
        self._surf = None
        self._prog = None
        self._final = None
        self._vao = None
        self._vbo = None
        self._fbo_sets = {}
        self._tex_slots = {}
        self._lut_tex = None
        self._lut_key = None
        self._tid = None
        self._ok = False
        self._failed = False
        self._last_error = ""
        self._frames = 0
        self._last_ms = 0.0
        self._readback_ms = 0.0
        self._uploads = 0
        self._parts = {}
        self._up_ms = 0.0

    # ------------------------------------------------------------------ setup
    @property
    def available(self):
        return bool(self._ok and not self._failed)

    @property
    def last_error(self):
        return self._last_error

    @property
    def last_ms(self):
        return self._last_ms

    def _fail(self, msg):
        self._failed = True
        self._ok = False
        self._last_error = str(msg)[:300]
        try:
            self._log("GPU 场景合成不可用：%s" % self._last_error)
        except Exception:                                     # noqa: BLE001
            pass

    def ensure(self, w=1920, h=1080):
        w = max(16, int(w))
        h = max(16, int(h))
        if self._failed:
            return False
        if self._ok:
            return True
        try:
            fmt = QSurfaceFormat.defaultFormat()
            fmt.setVersion(3, 3)
            fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
            self._surf = QOffscreenSurface()
            self._surf.setFormat(fmt)
            self._surf.create()
            if not self._surf.isValid():
                self._fail("QOffscreenSurface 无效")
                return False
            self._ctx = QOpenGLContext()
            self._ctx.setFormat(fmt)
            if not self._ctx.create() or not self._ctx.isValid():
                self._fail("QOpenGLContext.create() 失败")
                return False
            if not self._ctx.makeCurrent(self._surf):
                self._fail("makeCurrent() 失败")
                return False
            self._tid = threading.get_ident()
            self._prog = self._link(_VERT, _LAYER_FRAG)
            self._final = self._link(_VERT, _FINAL_FRAG)
            if self._prog is None or self._final is None:
                return False
            self._vao = QOpenGLVertexArrayObject()
            self._vao.create()
            self._vao.bind()
            self._vbo = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
            self._vbo.create()
            self._vbo.bind()
            self._vao.release()
            self._vbo.release()
            self._ok = True
            try:
                gl = self._ctx.functions()
                name = gl.glGetString(0x1F01)
                self._log("GPU 场景合成已启用：%s %dx%d" % (name, w, h))
            except Exception:                                 # noqa: BLE001
                pass
            return True
        except Exception as e:                                # noqa: BLE001
            self._fail("初始化异常：%s" % e)
            return False

    def _link(self, vs, fs):
        prog = QOpenGLShaderProgram()
        if not prog.addShaderFromSourceCode(
                QOpenGLShader.ShaderTypeBit.Vertex, vs):
            self._fail("顶点 shader：%s" % prog.log())
            return None
        if not prog.addShaderFromSourceCode(
                QOpenGLShader.ShaderTypeBit.Fragment, fs):
            self._fail("片元 shader：%s" % prog.log())
            return None
        if not prog.link():
            self._fail("shader link：%s" % prog.log())
            return None
        return prog

    def _targets(self, w, h):
        key = (int(w), int(h))
        pair = self._fbo_sets.get(key)
        if pair is not None:
            return pair
        fmt = QOpenGLFramebufferObjectFormat()
        fmt.setAttachment(QOpenGLFramebufferObject.Attachment.NoAttachment)
        fmt.setInternalTextureFormat(_GL_RGBA8)
        fmt.setTextureTarget(_GL_TEXTURE_2D)
        fmt.setMipmap(False)
        fmt.setSamples(0)
        a = QOpenGLFramebufferObject(int(w), int(h), fmt)
        b = QOpenGLFramebufferObject(int(w), int(h), fmt)
        if not a.isValid() or not b.isValid():
            self._fail("FBO 创建失败 %dx%d" % (w, h))
            return None
        pair = (a, b)
        self._fbo_sets[key] = pair
        # 尺寸种类很少（1920x1080 / 1080x1920 / 3840x2160…），最多留 4 组
        while len(self._fbo_sets) > 4:
            _k = next(iter(self._fbo_sets))
            if _k == key:
                break
            self._fbo_sets.pop(_k, None)
        return pair

    # ------------------------------------------------------------------ textures
    def _tex_for(self, slot_key, img):
        if img is None or img.isNull():
            return None
        slot = self._tex_slots.get(slot_key)
        if slot is not None and slot.get("img") is img and slot.get("tex") is not None:
            return slot["tex"]
        fmt = img.format()
        if fmt in (QImage.Format_ARGB32_Premultiplied, QImage.Format_RGB32):
            pf = QOpenGLTexture.PixelFormat.BGRA
        elif fmt in (QImage.Format_RGBA8888,
                     QImage.Format_RGBA8888_Premultiplied):
            pf = QOpenGLTexture.PixelFormat.RGBA
        else:
            img = img.convertToFormat(QImage.Format_ARGB32_Premultiplied)
            pf = QOpenGLTexture.PixelFormat.BGRA
        if int(img.bytesPerLine()) != int(img.width()) * 4:
            img = img.convertToFormat(QImage.Format_ARGB32_Premultiplied)
            pf = QOpenGLTexture.PixelFormat.BGRA
        w, h = int(img.width()), int(img.height())
        tex = slot.get("tex") if slot is not None else None
        if tex is None or slot.get("w") != w or slot.get("h") != h:
            try:
                if tex is not None:
                    tex.destroy()
            except Exception:                                 # noqa: BLE001
                pass
            tex = QOpenGLTexture(QOpenGLTexture.Target.Target2D)
            tex.setFormat(QOpenGLTexture.TextureFormat.RGBA8_UNorm)
            tex.setSize(w, h)
            tex.setMipLevels(1)
            tex.allocateStorage()
            tex.setMinificationFilter(QOpenGLTexture.Filter.Linear)
            tex.setMagnificationFilter(QOpenGLTexture.Filter.Linear)
            tex.setWrapMode(QOpenGLTexture.WrapMode.ClampToEdge)
        try:
            self._up_ms = 0.0
            tb = time.perf_counter()
            tex.setData(0, 0, pf, QOpenGLTexture.PixelType.UInt8,
                        img.constBits())
            self._up_ms += (time.perf_counter() - tb) * 1000.0
        except Exception:
            src = img.convertToFormat(QImage.Format_RGBA8888_Premultiplied)
            tex.setData(0, 0, QOpenGLTexture.PixelFormat.RGBA,
                        QOpenGLTexture.PixelType.UInt8, src.constBits())
        self._tex_slots[slot_key] = {"img": img, "tex": tex, "w": w, "h": h}
        self._uploads += 1
        return tex

    def _lut_texture(self, img):
        if img is None or img.isNull():
            return None
        key = int(img.cacheKey())
        if key == self._lut_key and self._lut_tex is not None:
            return self._lut_tex
        try:
            if (self._lut_tex is None
                    or self._lut_tex.width() != img.width()
                    or self._lut_tex.height() != img.height()):
                try:
                    if self._lut_tex is not None:
                        self._lut_tex.destroy()
                except Exception:                             # noqa: BLE001
                    pass
                self._lut_tex = QOpenGLTexture(QOpenGLTexture.Target.Target2D)
                self._lut_tex.setFormat(QOpenGLTexture.TextureFormat.RGBA8_UNorm)
                self._lut_tex.setSize(img.width(), img.height())
                self._lut_tex.setMipLevels(1)
                self._lut_tex.allocateStorage()
                self._lut_tex.setMinificationFilter(QOpenGLTexture.Filter.Linear)
                self._lut_tex.setMagnificationFilter(QOpenGLTexture.Filter.Linear)
                self._lut_tex.setWrapMode(QOpenGLTexture.WrapMode.ClampToEdge)
            src = img
            pf = QOpenGLTexture.PixelFormat.RGBA
            if src.format() != QImage.Format_RGBA8888 or \
                    int(src.bytesPerLine()) != int(src.width()) * 4:
                src = src.convertToFormat(QImage.Format_RGBA8888)
            self._lut_tex.setData(0, 0, pf, QOpenGLTexture.PixelType.UInt8,
                                  src.constBits())
            self._lut_key = key
            return self._lut_tex
        except Exception as e:                                # noqa: BLE001
            self._log("LUT 纹理上传失败：%s" % e)
            return None

    def _trim(self):
        while len(self._tex_slots) > 48:
            _k = next(iter(self._tex_slots))
            slot = self._tex_slots.pop(_k, None)
            try:
                if slot and slot.get("tex") is not None:
                    slot["tex"].destroy()
            except Exception:                                 # noqa: BLE001
                pass

    # ------------------------------------------------------------------ drawing
    def _quad(self, x0, y0, x1, y1, u0=0.0, v0=0.0, u1=1.0, v1=1.0):
        """像素坐标矩形 → NDC 顶点（TL,TR,BL,BR）。"""
        nx0 = x0 / self._w * 2.0 - 1.0
        nx1 = x1 / self._w * 2.0 - 1.0
        ny0 = 1.0 - y0 / self._h * 2.0
        ny1 = 1.0 - y1 / self._h * 2.0
        return [(nx0, ny0, u0, v0), (nx1, ny0, u1, v0),
                (nx0, ny1, u0, v1), (nx1, ny1, u1, v1)]

    def _draw_quad(self, f, tex, verts, opacity, blend):
        data = struct.pack("<%df" % (len(verts) * 4),
                           *[c for v in verts for c in v])
        self._vbo.bind()
        self._vbo.allocate(data, len(data))
        self._prog.bind()
        f.glUniform1f(self._prog.uniformLocation(b"u_opacity"), float(opacity))
        self._prog.enableAttributeArray(0)
        self._prog.setAttributeBuffer(0, _GL_FLOAT, 0, 2, 4 * 4)
        self._prog.enableAttributeArray(1)
        self._prog.setAttributeBuffer(1, _GL_FLOAT, 2 * 4, 2, 4 * 4)
        tex.bind(0)
        f.glUniform1i(self._prog.uniformLocation(b"u_tex"), 0)
        b = str(blend or "normal")
        if b not in _BLENDS:
            b = "normal"
        f.glBlendEquation(_GL_FUNC_ADD)
        if b == "screen":
            f.glBlendFunc(_GL_ONE, _GL_ONE_MINUS_SRC_COLOR)
        elif b in ("add", "plus"):
            f.glBlendFunc(_GL_ONE, _GL_ONE)
        elif b == "multiply":
            f.glBlendFunc(_GL_DST_COLOR, _GL_ONE_MINUS_SRC_ALPHA)
        elif b == "lighten":
            f.glBlendEquation(_GL_MAX)
            f.glBlendFunc(_GL_ONE, _GL_ONE)
        elif b == "darken":
            f.glBlendEquation(_GL_MIN)
            f.glBlendFunc(_GL_ONE, _GL_ONE)
        elif b == "subtract":
            f.glBlendEquation(_GL_FUNC_REVERSE_SUBTRACT)
            f.glBlendFunc(_GL_ONE, _GL_ONE)
        else:
            f.glBlendFunc(_GL_ONE, _GL_ONE_MINUS_SRC_ALPHA)
        f.glDrawArrays(_GL_TRIANGLE_STRIP, 0, 4)
        tex.release()
        self._vbo.release()
        f.glBlendEquation(_GL_FUNC_ADD)

    def _draw_layer(self, f, lay):
        img = lay.get("img")
        slot = str(lay.get("slot") or id(img))
        tex = self._tex_for(slot, img)
        if tex is None:
            return
        cw, ch = float(self._w), float(self._h)
        cx = cw * 0.5 + float(lay.get("x") or 0.0)
        cy = ch * 0.5 + float(lay.get("y") or 0.0)
        tw = max(0.01, float(lay.get("w") or cw))
        th = max(0.01, float(lay.get("h") or ch))
        op = max(0.0, min(1.0, float(lay.get("opacity") or 0.0)))
        if op <= 0.002:
            return
        rot = float(lay.get("rot") or 0.0)
        blend = lay.get("blend")
        if abs(rot) < 1e-3:
            x0, y0 = cx - tw * 0.5, cy - th * 0.5
            if lay.get("glitch"):
                self._draw_glitch(f, tex, x0, y0, tw, th, op, blend,
                                  float(lay.get("glitch_seed") or 0.0))
                return
            self._draw_quad(f, tex, self._quad(x0, y0, x0 + tw, y0 + th),
                            op, blend)
            return
        rad = math.radians(rot)
        ca, sa = math.cos(rad), math.sin(rad)
        hw, hh = tw * 0.5, th * 0.5
        pts = []
        for dx, dy, u, v in ((-hw, -hh, 0.0, 0.0), (hw, -hh, 1.0, 0.0),
                             (-hw, hh, 0.0, 1.0), (hw, hh, 1.0, 1.0)):
            px = cx + dx * ca - dy * sa
            py = cy + dx * sa + dy * ca
            pts.append((px / cw * 2.0 - 1.0, 1.0 - py / ch * 2.0, u, v))
        self._draw_quad(f, tex, pts, op, blend)

    def _draw_glitch(self, f, tex, x0, y0, tw, th, op, blend, seed):
        # CPU 版 glitch 是「约 24 条横带随机左右错位」；这里用同一形态的 GPU 分条绘制。
        n = 24
        step = max(1.0, th / n)
        s = int(seed) & 0xFFFFFFFF
        for i in range(n):
            y = y0 + i * step
            h = min(step, y0 + th - y)
            if h <= 0:
                break
            s = (1103515245 * s + 12345) & 0x7FFFFFFF
            off = ((s >> 8) % 81) - 40 if (s % 100) < 35 else 0
            v0 = (y - y0) / th
            v1 = (y + h - y0) / th
            self._draw_quad(
                f, tex,
                self._quad(x0 + off, y, x0 + off + tw, y + h, 0.0, v0, 1.0, v1),
                op, blend)

    # ------------------------------------------------------------------ compose
    def compose(self, scene):
        if self._failed:
            return None
        t0 = time.perf_counter()
        try:
            if not self.ensure(scene.get("w", 1920), scene.get("h", 1080)):
                return None
            if self._tid is not None and self._tid != threading.get_ident():
                self._fail("GL 上下文跨线程使用")
                return None
            w = max(16, int(scene.get("w") or 1920))
            h = max(16, int(scene.get("h") or 1080))
            self._w, self._h = w, h
            targets = self._targets(w, h)
            if targets is None:
                return None
            fbo_a, fbo_b = targets
            ctx = self._ctx
            _tm = time.perf_counter()
            if not ctx.makeCurrent(self._surf):
                self._fail("makeCurrent() 失败")
                return None
            _parts = {"make": (time.perf_counter() - _tm) * 1000.0}
            f = ctx.functions()
            fbo_a.bind()
            f.glViewport(0, 0, w, h)
            f.glDisable(_GL_DEPTH_TEST)
            f.glDisable(_GL_SCISSOR_TEST)
            f.glEnable(_GL_BLEND)
            if bool(scene.get("alpha")):
                f.glClearColor(0.0, 0.0, 0.0, 0.0)
            else:
                f.glClearColor(0.0, 0.0, 0.0, 1.0)
            f.glClear(_GL_COLOR_BUFFER_BIT)
            self._vao.bind()
            layers = scene.get("layers") or []
            _td = time.perf_counter()
            # 与 CPU 一致：描述列表从上到下，绘制从下往上
            for lay in reversed(layers):
                self._draw_layer(f, lay)
            _parts["draw"] = (time.perf_counter() - _td) * 1000.0
            _parts["upload"] = self._up_ms
            if bool(scene.get("blackout")):
                f.glClearColor(0.0, 0.0, 0.0, 1.0)
                f.glClear(_GL_COLOR_BUFFER_BIT)
            self._vao.release()
            lut_img = scene.get("lut")
            lut_tex = self._lut_texture(lut_img) if lut_img is not None else None
            _tf = time.perf_counter()
            if lut_tex is not None:
                fbo_b.bind()
                f.glViewport(0, 0, w, h)
                f.glDisable(_GL_BLEND)
                f.glClearColor(0.0, 0.0, 0.0, 0.0)
                f.glClear(_GL_COLOR_BUFFER_BIT)
                self._final.bind()
                self._vao.bind()
                f.glUniform1f(self._final.uniformLocation(b"u_use_lut"), 1.0)
                f.glActiveTexture(_GL_TEXTURE1)
                lut_tex.bind(1)
                f.glUniform1i(self._final.uniformLocation(b"u_lut"), 1)
                f.glActiveTexture(_GL_TEXTURE0)
                f.glBindTexture(_GL_TEXTURE_2D, int(fbo_a.texture()))
                f.glUniform1i(self._final.uniformLocation(b"u_tex"), 0)
                data = struct.pack(
                    "<16f",
                    -1.0, 1.0, 0.0, 0.0,
                    1.0, 1.0, 1.0, 0.0,
                    -1.0, -1.0, 0.0, 1.0,
                    1.0, -1.0, 1.0, 1.0)
                self._vbo.bind()
                self._vbo.allocate(data, len(data))
                self._final.enableAttributeArray(0)
                self._final.setAttributeBuffer(0, _GL_FLOAT, 0, 2, 4 * 4)
                self._final.enableAttributeArray(1)
                self._final.setAttributeBuffer(1, _GL_FLOAT, 2 * 4, 2, 4 * 4)
                f.glDrawArrays(_GL_TRIANGLE_STRIP, 0, 4)
                f.glBindTexture(_GL_TEXTURE_2D, 0)
                f.glActiveTexture(_GL_TEXTURE1)
                try:
                    lut_tex.release(1)
                except TypeError:
                    lut_tex.release()
                f.glActiveTexture(_GL_TEXTURE0)
                self._final.release()
                self._vao.release()
                f.glEnable(_GL_BLEND)
                _t = time.perf_counter()
                img = fbo_b.toImage()
                self._readback_ms = (time.perf_counter() - _t) * 1000.0
            else:
                _t = time.perf_counter()
                img = fbo_a.toImage()
                self._readback_ms = (time.perf_counter() - _t) * 1000.0
            _parts["final"] = (_t - _tf) * 1000.0
            _parts["read"] = self._readback_ms
            _parts["total"] = (time.perf_counter() - t0) * 1000.0
            self._parts = _parts
            fbo_a.release()
            self._trim()
            self._frames += 1
            self._last_ms = (time.perf_counter() - t0) * 1000.0
            return img
        except Exception as e:                                # noqa: BLE001
            try:
                for pair in self._fbo_sets.values():
                    for b in pair:
                        b.release()
            except Exception:                                 # noqa: BLE001
                pass
            self._fail("%s: %s" % (type(e).__name__, e))
            return None

    def stats(self):
        n = max(1, self._frames)
        return {"frames": self._frames, "last_ms": self._last_ms,
                "avg_ms": self._last_ms, "readback_ms": self._readback_ms,
                "uploads": self._uploads,
                "parts": dict(getattr(self, "_parts", {}) or {})}

    def close(self):
        try:
            for pair in self._fbo_sets.values():
                for b in pair:
                    b.release()
            self._fbo_sets.clear()
            for slot in self._tex_slots.values():
                try:
                    slot["tex"].destroy()
                except Exception:                             # noqa: BLE001
                    pass
            self._tex_slots.clear()
            if self._lut_tex is not None:
                self._lut_tex.destroy()
            if self._ctx is not None:
                self._ctx.doneCurrent()
        except Exception:                                     # noqa: BLE001
            pass
        self._ok = False


__all__ = ["SceneStage"]
