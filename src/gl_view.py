# -*- coding: utf-8 -*-
"""GPU 输出视图：把 QImage 作为纹理直接 blit 到 QOpenGLWidget。

为什么不用 QPainter：
  · `_GLView` 旧实现用 `QPainter(self).drawImage()` 画在 QOpenGLWidget 上，
    真机两个输出窗 `isValid()` 为真但实际全黑。
  · 这里完全绕开 QPainter：QOpenGLTexture 上传 + shader 画全屏四边形。
    之前 `_GLView` 的黑屏不会再出现。

这还只是"GPU 显示"，不是 GPU 合成；合成器后续会复用同一个纹理/quad 路径。
"""
from __future__ import annotations

import os
import struct
import time
import math

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QImage, QOpenGLContext, QSurfaceFormat
from PySide6.QtOpenGL import (QOpenGLBuffer, QOpenGLShader,
                              QOpenGLShaderProgram, QOpenGLTexture,
                              QOpenGLVertexArrayObject)
from PySide6.QtOpenGLWidgets import QOpenGLWidget

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

_FRAG = """
#version 330 core
in vec2 v_uv;
uniform sampler2D u_tex;
uniform sampler2D u_lut;
uniform float u_use_lut;
uniform float u_flash;
uniform float u_opacity;
out vec4 o_color;
void main() {
    vec4 c = texture(u_tex, v_uv);
    c *= u_opacity;
    if (u_use_lut > 0.5) {
        c.r = texture(u_lut, vec2(c.r, 0.5)).r;
        c.g = texture(u_lut, vec2(c.g, 0.5)).g;
        c.b = texture(u_lut, vec2(c.b, 0.5)).b;
    }
    c.rgb = mix(c.rgb, vec3(1.0), u_flash);
    o_color = c;
}
"""


class GLTextureView(QOpenGLWidget):
    """输出窗口的 GPU 显示层；`set_frame(QImage)` 后由 GPU 缩放/显示。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._img = None
        self._key = None
        self._tex = None
        self._scene = None
        self._scene_tex = []
        self._lut = None
        self._flash = 0.0
        self._lut_tex = None
        self._lut_key = None
        self._prog = None
        self._vbo = None
        self._vao = None
        self._gl = None
        self._init_ok = False
        self._paint_n = 0
        self._last_paint_ms = 0.0
        self._grab_done = False
        self._err = ""
        self._tex_err = ""
        self.aspect_lock = True
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        try:
            self.setUpdateBehavior(QOpenGLWidget.NoPartialUpdate)
        except Exception:                                      # noqa: BLE001
            pass
        # ★ 2026-11-26（真机：喂帧 30~31，RTSS OGL 只有 27~28）：输出窗的
        #   present 不再跟着 `set_frame` 的 GUI 时序走，而是固定 30Hz 节拍。
        #   引擎来帧就换图，节拍器每 33ms 贴最新一帧；不丢帧时延迟最多 33ms，
        #   但**屏幕刷新恒定 30**，不会因为某个 tick 晚 2ms 就掉到 20/26。
        self._present_timer = QTimer(self)
        self._present_timer.setTimerType(Qt.PreciseTimer)
        try:
            _pms = int(os.environ.get("AUTOVJ_PRESENT_MS", "33") or 33)
        except (TypeError, ValueError):
            _pms = 33
        self._present_timer.setInterval(max(8, min(100, _pms)))
        self._present_timer.timeout.connect(self._present_tick)
        self._present_timer.start()
        # 默认 QOpenGLWidget 可能只有 2.0；显式请求 3.3 core（失败会自动回退）
        try:
            fmt = QSurfaceFormat.defaultFormat()
            if fmt.majorVersion() < 3:
                fmt.setVersion(3, 3)
                fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
            # ★ 2026-11-26（真机：引擎/喂帧 30，RTSS OGL 只有 26）：输出窗
            #   在 60Hz 垂直同步下，33ms 的帧只要错开一个 16.7ms 间隔就会被
            #   量化成 20~26。输出窗不是游戏，**关掉 vsync** 让每次 set_frame
            #   立即 present，计数器与喂帧一致；投影/采集端更怕停顿，不怕撕裂。
            fmt.setSwapInterval(0)
            self.setFormat(fmt)
        except Exception:                                      # noqa: BLE001
            pass

    # ---------- 外部入口 ----------
    def set_frame(self, img, lut=None, flash=0.0):
        self._img = img
        self._lut = lut
        try:
            self._flash = max(0.0, min(1.0, float(flash or 0.0)))
        except (TypeError, ValueError):
            self._flash = 0.0
        # 由 30Hz 节拍器统一 present；这里只换图（见 __init__ 的说明）。

    def _present_tick(self):
        try:
            if self.isVisible() and self._img is not None:
                self.update()
        except Exception:                                      # noqa: BLE001
            pass

    def set_scene(self, scene, lut=None, flash=0.0):
        self._scene = scene
        self._lut = lut
        try:
            self._flash = max(0.0, min(1.0, float(flash or 0.0)))
        except (TypeError, ValueError):
            self._flash = 0.0
        # 同上：由 30Hz 节拍器 present。

    def last_paint_ms(self):
        return float(self._last_paint_ms)

    def is_ready(self):
        """shader/GL 是否初始化成功；供输出窗口 300ms 后校验，失败退回 CPU。"""
        return bool(self._init_ok)

    def grab_frame(self, path):
        try:
            img = self.grabFramebuffer()
            if img is not None and not img.isNull():
                return bool(img.save(str(path)))
        except Exception:                                      # noqa: BLE001
            pass
        return False

    # ---------- GL ----------
    def initializeGL(self):
        try:
            ctx = QOpenGLContext.currentContext()
            self._gl = ctx.functions() if ctx is not None else None
            self._prog = QOpenGLShaderProgram()
            if not self._prog.addShaderFromSourceCode(
                    QOpenGLShader.ShaderTypeBit.Vertex, _VERT):
                raise RuntimeError("vertex: %s" % self._prog.log())
            if not self._prog.addShaderFromSourceCode(
                    QOpenGLShader.ShaderTypeBit.Fragment, _FRAG):
                raise RuntimeError("fragment: %s" % self._prog.log())
            if not self._prog.link():
                raise RuntimeError("link: %s" % self._prog.log())
            self._vao = QOpenGLVertexArrayObject()
            self._vao.create()
            self._vao.bind()
            self._vbo = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
            self._vbo.create()
            self._vbo.bind()
            data = struct.pack("<16f",
                               0.0, 0.0, 0.0, 1.0,
                               1.0, 0.0, 1.0, 1.0,
                               0.0, 1.0, 0.0, 0.0,
                               1.0, 1.0, 1.0, 0.0)
            self._vbo.allocate(data, len(data))
            self._prog.bind()
            self._prog.enableAttributeArray(0)
            self._prog.setAttributeBuffer(0, 0x1406, 0, 2, 4 * 4)
            self._prog.enableAttributeArray(1)
            self._prog.setAttributeBuffer(1, 0x1406, 4 * 2, 2, 4 * 4)
            self._prog.release()
            self._vbo.release()
            self._vao.release()
            self._init_ok = True
        except Exception as e:                                 # noqa: BLE001
            self._init_ok = False
            self._err = "%s: %s" % (type(e).__name__, e)
            try:
                import stallwatch
                stallwatch.log_line("[GLVIEW] 初始化失败：%s" % e)
            except Exception:                                  # noqa: BLE001
                pass
        # 诊断：抓一张 framebuffer（验证不是黑屏）
        try:
            p = os.environ.get("AUTOVJ_GL_GRAB_PATH", "")
            if p and not self._grab_done:
                self._grab_done = True
                QTimer.singleShot(2500, lambda: self.grab_frame(p))
        except Exception:                                      # noqa: BLE001
            pass

    def _make_tex(self, img):
        self._tex = self._make_tex_into(self._tex, img)
        return self._tex

    def _make_tex_into(self, tex, img):
        try:
            w, h = int(img.width()), int(img.height())
            bpl = int(img.bytesPerLine())
            if tex is None or tex.width() != w or tex.height() != h:
                tex = QOpenGLTexture(QOpenGLTexture.Target.Target2D)
                tex.setFormat(QOpenGLTexture.TextureFormat.RGBA8_UNorm)
                tex.setSize(w, h)
                tex.setMipLevels(1)
                tex.allocateStorage()
                tex.setMinificationFilter(QOpenGLTexture.Filter.Linear)
                tex.setMagnificationFilter(QOpenGLTexture.Filter.Linear)
                tex.setWrapMode(QOpenGLTexture.WrapMode.ClampToEdge)
            data = img.constBits()
            if bpl != w * 4:
                src = (img.convertToFormat(QImage.Format_RGBA8888_Premultiplied)
                       if img.hasAlphaChannel() else img.convertToFormat(QImage.Format_RGBA8888))
                tex.setData(src)
            else:
                try:
                    tex.setData(0, 0, QOpenGLTexture.PixelFormat.BGRA,
                                QOpenGLTexture.PixelType.UInt8, data)
                except Exception:
                    tex.setData(0, QOpenGLTexture.PixelFormat.BGRA,
                                QOpenGLTexture.PixelType.UInt8, data)
            self._tex_err = ""
            return tex
        except Exception as e:                                 # noqa: BLE001
            self._tex_err = "%s: %s" % (type(e).__name__, e)
            return tex

    def _quad_bytes(self, corners):
        # corners: TL, TR, BL, BR in NDC
        # QImage 上传后行 0 在 GL 纹理 v=0；当前四边形顶点顺序是
        # TL, TR, BL, BR，因此 UV 只需要翻 V（不要同时翻 U，否则会变成 180°）。
        uv = ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0))
        vals = []
        for (x, y), (u, v) in zip(corners, uv):
            vals.extend((float(x), float(y), float(u), float(v)))
        return struct.pack("<16f", *vals)

    def _draw_tex(self, f, tex, corners, opacity=1.0, blend="normal"):
        data = self._quad_bytes(corners)
        self._vbo.bind()
        self._vbo.allocate(data, len(data))
        self._prog.setAttributeBuffer(0, 0x1406, 0, 2, 4 * 4)
        self._prog.setAttributeBuffer(1, 0x1406, 4 * 2, 2, 4 * 4)
        f.glUniform1f(self._prog.uniformLocation("u_opacity"),
                      max(0.0, min(1.0, float(opacity))))
        f.glEnable(0x0BE2)                                     # GL_BLEND
        b = str(blend or "normal")
        if b in ("screen",):
            f.glBlendFunc(1, 0x0301)                           # ONE, ONE_MINUS_SRC_COLOR
        elif b in ("add", "plus"):
            f.glBlendFunc(1, 1)                                # ONE, ONE
        elif b in ("multiply",):
            f.glBlendFunc(0x0306, 0x0303)                      # DST_COLOR, ONE_MINUS_SRC_ALPHA
        elif b in ("subtract", "difference"):
            f.glBlendEquation(0x800A)                          # FUNC_REVERSE_SUBTRACT
            f.glBlendFunc(1, 1)
        else:
            f.glBlendEquation(0x8006)                          # FUNC_ADD
            f.glBlendFunc(1, 0x0303)                           # ONE, ONE_MINUS_SRC_ALPHA
        f.glActiveTexture(0x84C0)
        tex.bind(0)
        f.glUniform1i(self._prog.uniformLocation("u_tex"), 0)
        f.glDrawArrays(0x0005, 0, 4)                           # TRIANGLE_STRIP
        tex.release()
        f.glBlendEquation(0x8006)

    def _paint_scene(self, f, vw, vh, scene):
        cw = max(1.0, float(scene.get("w") or vw))
        ch = max(1.0, float(scene.get("h") or vh))
        scale = min(vw / cw, vh / ch) if self.aspect_lock else max(vw / cw, vh / ch)
        ox = (vw - cw * scale) * 0.5
        oy = (vh - ch * scale) * 0.5
        layers = scene.get("layers") or []
        for i, lay in enumerate(layers):
            img = lay.get("img")
            if img is None or img.isNull():
                continue
            while len(self._scene_tex) <= i:
                self._scene_tex.append({"tex": None, "key": None})
            slot = self._scene_tex[i]
            key = int(img.cacheKey())
            if slot["key"] != key or slot["tex"] is None:
                slot["tex"] = self._make_tex_into(slot["tex"], img)
                slot["key"] = key
            tex = slot["tex"]
            if tex is None:
                continue
            cx = cw * 0.5 + float(lay.get("x") or 0.0)
            cy = ch * 0.5 + float(lay.get("y") or 0.0)
            hw = max(1.0, float(lay.get("w") or cw)) * 0.5
            hh = max(1.0, float(lay.get("h") or ch)) * 0.5
            rot = float(lay.get("rot") or 0.0) * 3.141592653589793 / 180.0
            ca, sa = math.cos(rot), math.sin(rot)
            pts = []
            for dx, dy in ((-hw, -hh), (hw, -hh), (-hw, hh), (hw, hh)):
                px = cx + dx * ca - dy * sa
                py = cy + dx * sa + dy * ca
                vx = ox + px * scale
                vy = oy + py * scale
                pts.append((vx / vw * 2.0 - 1.0, 1.0 - vy / vh * 2.0))
            self._draw_tex(f, tex, pts, lay.get("opacity", 1.0),
                           lay.get("blend", "normal"))

    def paintGL(self):
        t0 = time.perf_counter()
        self._paint_n += 1
        try:
            if not self._init_ok or self._gl is None:
                return
            f = self._gl
            dpr = float(self.devicePixelRatioF() or 1.0)
            vw = max(1, int(round(self.width() * dpr)))
            vh = max(1, int(round(self.height() * dpr)))
            f.glViewport(0, 0, vw, vh)
            f.glDisable(0x0B71)                                # GL_DEPTH_TEST
            f.glClearColor(0.0, 0.0, 0.0, 1.0)
            f.glClear(0x00004000)                              # GL_COLOR_BUFFER_BIT
            img = self._img
            if self._scene is not None:
                _use_lut = 0.0
                if self._lut is not None and not self._lut.isNull():
                    try:
                        _lk = int(self._lut.cacheKey())
                        if _lk != self._lut_key or self._lut_tex is None:
                            self._lut_tex = QOpenGLTexture(
                                self._lut,
                                QOpenGLTexture.MipMapGeneration.DontGenerateMipMaps)
                            self._lut_key = _lk
                        if self._lut_tex is not None and self._lut_tex.isCreated():
                            _use_lut = 1.0
                    except Exception:                      # noqa: BLE001
                        _use_lut = 0.0
                self._prog.bind()
                self._vao.bind()
                f.glUniform1f(self._prog.uniformLocation("u_flash"),
                              float(self._flash or 0.0))
                f.glUniform1f(self._prog.uniformLocation("u_use_lut"),
                              float(_use_lut))
                if _use_lut > 0.5:
                    f.glActiveTexture(0x84C1)
                    self._lut_tex.bind(1)
                    f.glUniform1i(self._prog.uniformLocation("u_lut"), 1)
                else:
                    f.glUniform1i(self._prog.uniformLocation("u_lut"), 0)
                self._paint_scene(f, vw, vh, self._scene)
                if _use_lut > 0.5 and self._lut_tex is not None:
                    self._lut_tex.release()
                self._vbo.release()
                self._vao.release()
                self._prog.release()
                return
            if img is None or img.isNull() or img.width() <= 0 or img.height() <= 0:
                return
            key = int(img.cacheKey())
            if key != self._key or self._tex is None:
                # 注意：不要 destroy 旧纹理 —— `_make_tex` 会复用同尺寸纹理；
                # 每帧销毁/新建正是 paint 8~25ms 的根因。
                self._tex = self._make_tex(img)
                self._key = key
            if self._tex is None:
                return
            _use_lut = 0.0
            if self._lut is not None and not self._lut.isNull():
                try:
                    _lk = int(self._lut.cacheKey())
                    if _lk != self._lut_key or self._lut_tex is None:
                        self._lut_tex = QOpenGLTexture(
                            self._lut,
                            QOpenGLTexture.MipMapGeneration.DontGenerateMipMaps)
                        self._lut_key = _lk
                    if self._lut_tex is not None and self._lut_tex.isCreated():
                        _use_lut = 1.0
                except Exception:                          # noqa: BLE001
                    _use_lut = 0.0
            iw, ih = float(img.width()), float(img.height())
            scale = min(vw / iw, vh / ih) if self.aspect_lock else max(vw / iw, vh / ih)
            tw, th = iw * scale, ih * scale
            x, y = (vw - tw) * 0.5, (vh - th) * 0.5
            x0 = x / vw * 2.0 - 1.0
            x1 = (x + tw) / vw * 2.0 - 1.0
            y0 = 1.0 - y / vh * 2.0
            y1 = 1.0 - (y + th) / vh * 2.0
            corners = ((x0, y0), (x1, y0), (x0, y1), (x1, y1))
            self._prog.bind()
            self._vao.bind()
            f.glUniform1f(self._prog.uniformLocation("u_flash"),
                          float(getattr(self, "_flash", 0.0) or 0.0))
            f.glUniform1f(self._prog.uniformLocation("u_use_lut"), float(_use_lut))
            if _use_lut > 0.5:
                f.glActiveTexture(0x84C1)                      # GL_TEXTURE1
                self._lut_tex.bind(1)
                f.glUniform1i(self._prog.uniformLocation("u_lut"), 1)
            else:
                f.glUniform1i(self._prog.uniformLocation("u_lut"), 0)
            self._draw_tex(f, self._tex, corners, 1.0, "normal")
            self._tex.release()
            if _use_lut > 0.5 and self._lut_tex is not None:
                self._lut_tex.release()
            self._vbo.release()
            self._vao.release()
            self._prog.release()
        except Exception as e:                                 # noqa: BLE001
            self._err = "paint %s: %s" % (type(e).__name__, e)
            try:
                import stallwatch
                stallwatch.log_line("[GLVIEW] paint 失败：%s" % e)
            except Exception:                                  # noqa: BLE001
                pass
        finally:
            self._last_paint_ms = (time.perf_counter() - t0) * 1000.0
            if os.environ.get("AUTOVJ_DRAW_PROBE") == "1" and self._paint_n % 15 == 0:
                try:
                    import stallwatch
                    stallwatch.log_line(
                        "[OW] glpaint=%.1fms size=%dx%d img=%s"
                        % (self._last_paint_ms, self.width(), self.height(),
                           "%dx%d" % (self._img.width(), self._img.height())
                           if self._img is not None else "None"))
                except Exception:                              # noqa: BLE001
                    pass


__all__ = ["GLTextureView"]
