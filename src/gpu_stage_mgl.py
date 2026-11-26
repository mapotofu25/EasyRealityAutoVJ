# -*- coding: utf-8 -*-
"""完整 GPU 场景合成（moderngl 独立 GL 上下文）。

为什么不用 PySide6 的 QOpenGLContext：
  · 旧 `gpu_stage.py` 用 PySide6 QOffscreenSurface/QOpenGLFramebufferObject，
    在引擎线程调用时会长时间持 GIL（真机主线程停顿 700ms+、输出 11~20 帧）。
  · moderngl/glcontext 是独立 C 扩展，创建 standalone context 不依赖 Qt，
    纹理上传/FBO/回读都在 GL 层完成；实测 1080p 7 层约 11ms（RTX 3070 Ti）。

接口与旧 `gpu_stage.SceneStage` 保持一致，便于 `engine._gpu_compose_tick`
直接替换：
    stage = SceneStage(log=...)
    stage.ensure(w, h)
    img = stage.compose({"w":1920,"h":1080,"alpha":False,
                         "layers":[descriptor,...], "lut":QImage|None,
                         "blackout":False})
    stage.stats() / stage.close()
"""
from __future__ import annotations

import struct
import threading
import time

import numpy as np
import moderngl
try:
    import glfw
except Exception:                                          # noqa: BLE001
    glfw = None

from PySide6.QtGui import QImage

_LAYER_VERT = """
#version 330 core
in vec2 in_pos;
in vec2 in_uv;
out vec2 v_uv;
void main() {
    v_uv = in_uv;
    gl_Position = vec4(in_pos, 0.0, 1.0);
}
"""

_LAYER_FRAG = """
#version 330 core
in vec2 v_uv;
uniform sampler2D u_tex;
uniform float u_opacity;
uniform int u_bgra;
uniform int u_premul;
out vec4 o_color;
void main() {
    vec4 c = texture(u_tex, v_uv);
    if (u_bgra == 1) c = c.bgra;
    if (u_premul == 1) c.rgb *= c.a;   // 直通 alpha → 预乘
    o_color = c * u_opacity;            // 预乘 alpha：四通道同乘
}
"""

_FINAL_VERT = _LAYER_VERT

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
    // FBO/readback 是 RGBA；QImage 的 RGB32/ARGB32 在内存里是 BGRA。
    o_color = vec4(c.b, c.g, c.r, c.a);
}
"""

_PRESENT_FRAG = """
#version 330 core
in vec2 v_uv;
uniform sampler2D u_tex;
uniform int u_swap;
out vec4 o_color;
void main() {
    vec4 c = texture(u_tex, v_uv);
    if (u_swap == 1) c = c.bgra;   // FBO 里是 BGRA（给 QImage），屏幕要 RGBA
    o_color = c;
}
"""

_BLEND_NORMAL = 0
_BLEND_ADD = 1
_BLEND_SCREEN = 2
_BLEND_MULTIPLY = 3
_BLEND_LIGHTEN = 4
_BLEND_DARKEN = 5
_BLEND_SUBTRACT = 6


def _quad_ndc(W, H, x, y, w, h, rot_deg=0.0):
    """画布像素坐标 → NDC 四角（TL, TR, BL, BR），UV 同序 0/1。"""
    cx = float(x) + float(w) * 0.5
    cy = float(y) + float(h) * 0.5
    hw = float(w) * 0.5
    hh = float(h) * 0.5
    rad = float(rot_deg or 0.0) * 3.141592653589793 / 180.0
    ca, sa = (1.0, 0.0) if abs(rad) < 1e-9 else (
        float(np.cos(rad)), float(np.sin(rad)))
    out = []
    for dx, dy, u, v in ((-hw, -hh, 0.0, 0.0), (hw, -hh, 1.0, 0.0),
                         (-hw, hh, 0.0, 1.0), (hw, hh, 1.0, 1.0)):
        px = cx + dx * ca - dy * sa
        py = cy + dx * sa + dy * ca
        nx = px / float(W) * 2.0 - 1.0
        ny = 1.0 - py / float(H) * 2.0
        out.extend((nx, ny, u, v))
    return out


class SceneStage:
    """moderngl 离屏合成器；一个实例只在一个线程里用（引擎渲染线程）。"""

    def __init__(self, log=None):
        self._log = log or (lambda _m: None)
        self._ctx = None
        self._prog = None
        self._final = None
        self._vbo = None
        self._vao = None
        self._vao_final = None
        self._vbo_final = None
        self._dummy = None
        self._window = None
        self._native = False
        self._monitor = 0
        self._always_top = False
        self._title = "Easy Reality Output"
        self._present = None
        self._present_vao = None
        self._spout_targets = {}
        self._spout_senders = {}
        self._spout_err = ""
        self._fbo_a = None
        self._fbo_b = None
        self._fbo_key = None
        self._fbo_out = None
        self._fbo_out_key = None
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
        self._up_bytes = 0
        self._max_tex = (0, 0)
        self._last_img = None
        self._w = 1920
        self._h = 1080

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

    def ensure(self, w=1920, h=1080, native=False, monitor=0,
               always_top=False, title="Easy Reality Output"):
        if self._failed:
            return False
        if self._ok:
            return True
        try:
            t0 = time.perf_counter()
            self._monitor = max(0, int(monitor or 0))
            self._always_top = bool(always_top)
            self._title = str(title or "Easy Reality Output")
            if native:
                if glfw is None:
                    self._fail("glfw 不可用，无法创建原生输出窗")
                    return False
                if not glfw.init():
                    self._fail("glfw.init() 失败")
                    return False
                glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
                glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
                glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
                glfw.window_hint(glfw.DECORATED, glfw.FALSE)
                glfw.window_hint(glfw.RESIZABLE, glfw.FALSE)
                glfw.window_hint(glfw.VISIBLE, glfw.TRUE)
                if self._always_top:
                    glfw.window_hint(glfw.FLOATING, glfw.TRUE)
                _mw, _mh = int(w), int(h)
                _mon = None
                try:
                    _ms = glfw.get_monitors()
                    if _ms:
                        _mon = _ms[min(self._monitor, len(_ms) - 1)]
                        _vm = glfw.get_video_mode(_mon)
                        _mw, _mh = int(_vm.size.width), int(_vm.size.height)
                except Exception:                          # noqa: BLE001
                    _mon = None
                self._window = glfw.create_window(_mw, _mh, self._title,
                                                  None, None)
                if not self._window:
                    self._fail("glfw.create_window() 失败")
                    return False
                try:
                    if _mon is not None:
                        _px, _py = glfw.get_monitor_pos(_mon)
                        glfw.set_window_pos(self._window, int(_px), int(_py))
                        # 真机 150% DPI：无边框窗只拿到 1280x720 的工作区，
                        # 画面缩在左上角。直接切到显示器全屏模式，强制 1920x1080。
                        _vm2 = glfw.get_video_mode(_mon)
                        glfw.set_window_monitor(
                            self._window, _mon, 0, 0,
                            int(_vm2.size.width), int(_vm2.size.height),
                            int(_vm2.refresh_rate or 0))
                except Exception:                          # noqa: BLE001
                    pass
                glfw.make_context_current(self._window)
                try:
                    glfw.swap_interval(0)
                except Exception:                          # noqa: BLE001
                    pass
                self._native = True
                self._ctx = moderngl.create_context()
            else:
                self._ctx = moderngl.create_standalone_context()
            self._prog = self._ctx.program(
                vertex_shader=_LAYER_VERT, fragment_shader=_LAYER_FRAG)
            self._final = self._ctx.program(
                vertex_shader=_FINAL_VERT, fragment_shader=_FINAL_FRAG)
            self._present = self._ctx.program(
                vertex_shader=_FINAL_VERT, fragment_shader=_PRESENT_FRAG)
            self._vbo = self._ctx.buffer(reserve=1048576)
            self._vao = self._ctx.vertex_array(
                self._prog, [(self._vbo, '2f 2f', 'in_pos', 'in_uv')])
            self._vbo_final = self._ctx.buffer(
                struct.pack('<16f', -1, 1, 0, 0, 1, 1, 1, 0,
                            -1, -1, 0, 1, 1, -1, 1, 1))
            self._vao_final = self._ctx.vertex_array(
                self._final, [(self._vbo_final, '2f 2f', 'in_pos', 'in_uv')])
            self._present_vao = self._ctx.vertex_array(
                self._present, [(self._vbo_final, '2f 2f', 'in_pos', 'in_uv')])
            # 解绑采样器用：不能用 tex.release()（moderngl 里是销毁对象，
            # 会把缓存纹理 / FBO 附件打成 InvalidObject）。
            self._dummy = self._ctx.texture((1, 1), 4)
            self._dummy.write(b'\x00\x00\x00\x00')
            self._tid = threading.get_ident()
            self._ok = True
            try:
                info = self._ctx.info
                self._log("GPU 场景合成已启用（moderngl）：%s %s，初始化 %.0fms"
                          % (info.get('GL_RENDERER'), info.get('GL_VERSION'),
                             (time.perf_counter() - t0) * 1000.0))
            except Exception:                                  # noqa: BLE001
                pass
            return True
        except Exception as e:                                 # noqa: BLE001
            self._fail("初始化异常：%s" % e)
            return False

    def _targets(self, w, h):
        key = (int(w), int(h))
        if self._fbo_key == key and self._fbo_a is not None:
            return self._fbo_a, self._fbo_b
        try:
            if self._fbo_a is not None:
                self._fbo_a.release()
            if self._fbo_b is not None:
                self._fbo_b.release()
            ta = self._ctx.texture((w, h), 4)
            tb = self._ctx.texture((w, h), 4)
            for t in (ta, tb):
                t.filter = (moderngl.LINEAR, moderngl.LINEAR)
                t.repeat_x = False
                t.repeat_y = False
            self._fbo_a = self._ctx.framebuffer(color_attachments=[ta])
            self._fbo_b = self._ctx.framebuffer(color_attachments=[tb])
            self._fbo_key = key
            return self._fbo_a, self._fbo_b
        except Exception as e:                                 # noqa: BLE001
            self._fail("FBO 创建失败 %dx%d：%s" % (w, h, e))
            return None, None

    def _out_targets(self, w, h):
        key = (int(w), int(h))
        if self._fbo_out_key == key and self._fbo_out is not None:
            return self._fbo_out
        try:
            if self._fbo_out is not None:
                self._fbo_out.release()
            t = self._ctx.texture((w, h), 4)
            t.filter = (moderngl.LINEAR, moderngl.LINEAR)
            t.repeat_x = False
            t.repeat_y = False
            self._fbo_out = self._ctx.framebuffer(color_attachments=[t])
            self._fbo_out_key = key
            return self._fbo_out
        except Exception as e:                                 # noqa: BLE001
            self._fail("输出 FBO 创建失败 %dx%d：%s" % (w, h, e))
            return None

    def _upload(self, slot, img, version=None):
        """QImage → GPU 纹理；同 slot 同 version/cacheKey 直接复用。"""
        try:
            ck = int(version) if version is not None else int(img.cacheKey())
        except Exception:                                      # noqa: BLE001
            ck = id(img)
        rec = self._tex_slots.get(slot)
        if (rec is not None and rec.get("key") == ck
                and rec.get("w") == img.width() and rec.get("h") == img.height()
                and rec.get("tex") is not None):
            return rec["tex"], rec.get("bgra", 0), rec.get("premul", 0)
        fmt = img.format()
        bgra = 0
        premul = 0
        if fmt in (QImage.Format_ARGB32_Premultiplied, QImage.Format_RGB32):
            bgra = 1
        elif fmt == QImage.Format_ARGB32:
            bgra = 1
            premul = 1
        elif fmt == QImage.Format_RGBA8888_Premultiplied:
            bgra = 0
        elif fmt == QImage.Format_RGBA8888:
            bgra = 0
            premul = 1
        else:
            img = img.convertToFormat(QImage.Format_ARGB32_Premultiplied)
            bgra = 1
        if int(img.bytesPerLine()) != int(img.width()) * 4:
            img = img.convertToFormat(QImage.Format_ARGB32_Premultiplied)
            bgra = 1
            premul = 0
        w, h = int(img.width()), int(img.height())
        tex = rec.get("tex") if rec is not None else None
        if tex is None or rec.get("w") != w or rec.get("h") != h:
            try:
                if tex is not None:
                    tex.release()
            except Exception:                                  # noqa: BLE001
                pass
            tex = self._ctx.texture((w, h), 4)
            tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            tex.repeat_x = False
            tex.repeat_y = False
        tb = time.perf_counter()
        try:
            tex.write(img.constBits())
        except Exception:
            tex.write(bytes(img.constBits()))
        self._up_ms += (time.perf_counter() - tb) * 1000.0
        self._up_bytes += int(w) * int(h) * 4
        if int(w) * int(h) > int(self._max_tex[0]) * int(self._max_tex[1]):
            self._max_tex = (int(w), int(h))
        self._tex_slots[slot] = {"tex": tex, "key": ck, "w": w, "h": h,
                                 "bgra": bgra, "premul": premul, "img": img}
        self._uploads += 1
        # 静态图/缓存上限：视频帧对象不断换，超过 64 个槽位就回收最老的非当前
        if len(self._tex_slots) > 64:
            for k in list(self._tex_slots.keys())[:24]:
                if k == slot:
                    continue
                old = self._tex_slots.pop(k, None)
                try:
                    if old and old.get("tex") is not None:
                        old["tex"].release()
                except Exception:                              # noqa: BLE001
                    pass
        return tex, bgra, premul

    def _lut_texture(self, img):
        if img is None or img.isNull():
            return None
        ck = int(img.cacheKey())
        if ck == self._lut_key and self._lut_tex is not None:
            return self._lut_tex
        try:
            if (self._lut_tex is None
                    or self._lut_tex.width != img.width()
                    or self._lut_tex.height != img.height()):
                if self._lut_tex is not None:
                    self._lut_tex.release()
                self._lut_tex = self._ctx.texture(
                    (img.width(), img.height()), 4)
                self._lut_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
                self._lut_tex.repeat_x = False
                self._lut_tex.repeat_y = False
            src = img
            if (src.format() not in (QImage.Format_RGBA8888,
                                     QImage.Format_RGBA8888_Premultiplied)
                    or int(src.bytesPerLine()) != int(src.width()) * 4):
                src = src.convertToFormat(QImage.Format_RGBA8888)
            try:
                self._lut_tex.write(src.constBits())
            except Exception:
                self._lut_tex.write(bytes(src.constBits()))
            self._lut_key = ck
            return self._lut_tex
        except Exception as e:                                 # noqa: BLE001
            self._log("LUT 纹理上传失败：%s" % e)
            return None

    def _set_blend(self, blend):
        ctx = self._ctx
        b = str(blend or "normal")
        ctx.blend_equation = moderngl.FUNC_ADD
        if b in ("add", "plus"):
            ctx.blend_func = (moderngl.ONE, moderngl.ONE)
        elif b == "screen":
            ctx.blend_func = (moderngl.ONE, moderngl.ONE_MINUS_SRC_COLOR)
        elif b == "multiply":
            ctx.blend_func = (moderngl.DST_COLOR,
                              moderngl.ONE_MINUS_SRC_ALPHA)
        elif b in ("lighten", "darken"):
            ctx.blend_equation = (moderngl.MAX if b == "lighten"
                                  else moderngl.MIN)
            ctx.blend_func = (moderngl.ONE, moderngl.ONE)
        elif b in ("subtract", "difference"):
            ctx.blend_equation = moderngl.FUNC_REVERSE_SUBTRACT
            ctx.blend_func = (moderngl.ONE, moderngl.ONE)
        else:
            ctx.blend_func = (moderngl.ONE, moderngl.ONE_MINUS_SRC_ALPHA)

    def _layer_quads(self, lay):
        """把一层展开成若干四边形（普通 1 个；glitch 切片 N 个）。

        返回 [(tex, bgra, opacity, blend, 16-float-bytes), ...]。
        VBO 在 compose 里**一次性写入**，避免每层 glBufferSubData 等待
        上一层的 draw 完成（真机 7 层会因此从 5ms 掉到 50ms）。
        """
        img = lay.get("img")
        if img is None or img.isNull():
            return []
        slot = str(lay.get("slot") or ("L:%d" % id(img)))
        tex, bgra, premul = self._upload(slot, img, lay.get("version"))
        if tex is None:
            return []
        W = float(self._w)
        H = float(self._h)
        x0 = float(lay.get("x") or 0.0)
        y0 = float(lay.get("y") or 0.0)
        tw = max(1.0, float(lay.get("w") or W))
        th = max(1.0, float(lay.get("h") or H))
        rot = float(lay.get("rot") or 0.0)
        op = max(0.0, min(1.0, float(lay.get("opacity", 1.0) or 0.0)))
        blend = lay.get("blend")
        out = []
        if lay.get("glitch") and abs(rot) < 1e-3:
            seed = int(lay.get("glitch_seed") or int(time.time() * 1000))
            rng = np.random.default_rng(seed & 0x7FFFFFFF)
            step = max(8.0, th / 24.0)
            yy = 0.0
            while yy < th:
                sh = min(step, th - yy)
                off = float(rng.integers(-40, 41)) if rng.random() < 0.35 else 0.0
                data = []
                for dx, dy, u, v in (
                        (x0 + off, y0 + yy, 0.0, yy / th),
                        (x0 + off + tw, y0 + yy, 1.0, yy / th),
                        (x0 + off, y0 + yy + sh, 0.0, (yy + sh) / th),
                        (x0 + off + tw, y0 + yy + sh, 1.0,
                         (yy + sh) / th)):
                    nx = dx / W * 2.0 - 1.0
                    ny = 1.0 - dy / H * 2.0
                    data.extend((nx, ny, u, v))
                out.append((tex, bgra, premul, op, blend,
                            struct.pack("<16f", *data)))
                yy += step
        else:
            out.append((tex, bgra, premul, op, blend,
                        struct.pack("<16f", *_quad_ndc(
                            W, H, x0, y0, tw, th, rot))))
        return out

    def _draw_layer(self, lay):
        img = lay.get("img")
        if img is None or img.isNull():
            return
        slot = str(lay.get("slot") or ("L:%d" % id(img)))
        tex, bgra, premul = self._upload(slot, img, lay.get("version"))
        if tex is None:
            return
        W = float(self._w)
        H = float(self._h)
        x = float(lay.get("x") or 0.0)
        y = float(lay.get("y") or 0.0)
        w = max(1.0, float(lay.get("w") or W))
        h = max(1.0, float(lay.get("h") or H))
        rot = float(lay.get("rot") or 0.0)
        data = struct.pack("<16f", *_quad_ndc(W, H, x, y, w, h, rot))
        self._vbo.write(data)
        self._prog['u_tex'].value = 0
        self._prog['u_opacity'].value = max(
            0.0, min(1.0, float(lay.get("opacity", 1.0) or 0.0)))
        self._prog['u_bgra'].value = int(bgra)
        tex.use(0)
        self._set_blend(lay.get("blend"))
        self._vao.render(moderngl.TRIANGLE_STRIP)
        self._ctx.blend_equation = moderngl.FUNC_ADD

    def _draw_glitch(self, lay):
        """故障转场：横向切片错位；不支持旋转（collector 已保证）。"""
        img = lay.get("img")
        if img is None or img.isNull():
            return
        slot = str(lay.get("slot") or ("L:%d" % id(img)))
        tex, bgra, premul = self._upload(slot, img, lay.get("version"))
        if tex is None:
            return
        W = float(self._w)
        H = float(self._h)
        x0 = float(lay.get("x") or 0.0)
        y0 = float(lay.get("y") or 0.0)
        tw = max(1.0, float(lay.get("w") or W))
        th = max(1.0, float(lay.get("h") or H))
        seed = int(lay.get("glitch_seed") or int(time.time() * 1000))
        rng = np.random.default_rng(seed & 0x7FFFFFFF)
        step = max(8.0, th / 24.0)
        self._prog['u_tex'].value = 0
        self._prog['u_opacity'].value = max(
            0.0, min(1.0, float(lay.get("opacity", 1.0) or 0.0)))
        self._prog['u_bgra'].value = int(bgra)
        tex.use(0)
        self._set_blend(lay.get("blend"))
        yy = 0.0
        while yy < th:
            sh = min(step, th - yy)
            off = float(rng.integers(-40, 41)) if rng.random() < 0.35 else 0.0
            # 只画这一条：顶点位置用子矩形，UV 保持基图比例
            data = []
            for dx, dy, u, v in ((x0 + off, y0 + yy, 0.0, yy / th),
                                 (x0 + off + tw, y0 + yy, 1.0, yy / th),
                                 (x0 + off, y0 + yy + sh, 0.0,
                                  (yy + sh) / th),
                                 (x0 + off + tw, y0 + yy + sh, 1.0,
                                  (yy + sh) / th)):
                nx = dx / W * 2.0 - 1.0
                ny = 1.0 - dy / H * 2.0
                data.extend((nx, ny, u, v))
            self._vbo.write(struct.pack("<16f", *data))
            self._vao.render(moderngl.TRIANGLE_STRIP)
            yy += step
        self._ctx.blend_equation = moderngl.FUNC_ADD

    def set_spout_targets(self, mapping):
        """sid -> Spout 名字。发送端在合成线程/同一 GL 上下文里创建。"""
        try:
            want = {str(k): str(v) for k, v in (mapping or {}).items()
                    if k and v}
        except Exception:                                      # noqa: BLE001
            want = {}
        for sid, s in list(self._spout_senders.items()):
            if sid not in want:
                try:
                    s.releaseSender()
                except Exception:                              # noqa: BLE001
                    pass
                self._spout_senders.pop(sid, None)
        self._spout_targets = want

    def spout_handles(self, sid):
        return str(sid) in self._spout_targets

    def _ensure_spout(self):
        if not self._spout_targets:
            return
        try:
            from SpoutGL import SpoutSender
        except Exception as e:                                 # noqa: BLE001
            self._spout_err = "SpoutGL 不可导入：%s" % e
            return
        for sid, name in list(self._spout_targets.items()):
            if sid in self._spout_senders:
                continue
            try:
                s = SpoutSender()
                s.setSenderName(str(name))
                if not s.createOpenGL():
                    raise RuntimeError("createOpenGL() 失败")
                self._spout_senders[sid] = s
            except Exception as e:                             # noqa: BLE001
                self._spout_err = "%s: %s" % (name, e)

    def _present_frame(self, fbo, w, h, swap):
        if not self._native or self._window is None or self._present is None:
            return
        try:
            fw, fh = glfw.get_framebuffer_size(self._window)
            if not bool(getattr(self, "_present_logged", False)):
                self._present_logged = True
                self._log("present 窗口 fb=%sx%s fbo=%sx%s"
                          % (fw, fh, fbo.width, fbo.height))
            self._ctx.screen.use()
            self._ctx.viewport = (0, 0, max(1, int(fw)), max(1, int(fh)))
            self._ctx.disable(moderngl.BLEND)
            self._ctx.clear(0.0, 0.0, 0.0, 1.0)
            fbo.color_attachments[0].use(0)
            self._present['u_tex'].value = 0
            self._present['u_swap'].value = 1 if swap else 0
            self._present_vao.render(moderngl.TRIANGLE_STRIP)
            if self._dummy is not None:
                self._dummy.use(0)
            glfw.swap_buffers(self._window)
            glfw.poll_events()
        except Exception as e:                                 # noqa: BLE001
            self._spout_err = "present: %s" % e

    def _send_spout(self, sid, fbo, w, h):
        if not self._spout_targets:
            return
        self._ensure_spout()
        s = self._spout_senders.get(str(sid))
        if s is None:
            return
        try:
            s.sendFbo(int(fbo.glo), int(w), int(h), False)
        except Exception as e:                                 # noqa: BLE001
            self._spout_err = "sendFbo: %s" % e

    def compose(self, scene, readback=True):
        if self._failed:
            return None
        try:
            if not self.ensure():
                return None
            if self._tid is not None and self._tid != threading.get_ident():
                self._fail("GL 上下文跨线程使用")
                return None
            if self._ctx is None:
                return None
            w = max(16, int(scene.get("w") or 1920))
            h = max(16, int(scene.get("h") or 1080))
            self._w, self._h = w, h
            fbo_a, fbo_b = self._targets(w, h)
            if fbo_a is None:
                return None
            t0 = time.perf_counter()
            self._up_ms = 0.0
            alpha = bool(scene.get("alpha"))
            fbo_a.use()
            self._ctx.viewport = (0, 0, w, h)
            self._ctx.disable(moderngl.DEPTH_TEST)
            self._ctx.scissor = None
            self._ctx.enable(moderngl.BLEND)
            self._ctx.clear(0.0, 0.0, 0.0, 0.0 if alpha else 1.0)
            self._ctx.blend_equation = moderngl.FUNC_ADD
            layers = scene.get("layers") or []
            # 与 CPU 一致：描述列表从上到下，绘制从下往上。
            # 先把所有四边形一次性写进 VBO，再逐层绑定纹理 draw —— 避免
            # 每层 glBufferSubData 等待上一层 draw 完成（真机 50ms 的根因）。
            draws = []
            for lay in reversed(layers):
                draws.extend(self._layer_quads(lay))
            if draws:
                blobs = [d[5] for d in draws]
                try:
                    self._vbo.write(b''.join(blobs))
                except Exception:
                    # 超过预留大小（极端多子层）：分块上传，仍只写几次；
                    # 每块必须写到自己的偏移，否则后一块会覆盖前一块。
                    for i in range(0, len(blobs), 128):
                        chunk = b''.join(blobs[i:i + 128])
                        self._vbo.write(chunk, offset=i * 64)
                for i, (tex, bgra, premul, op, blend, _blob) in enumerate(draws):
                    self._prog['u_tex'].value = 0
                    self._prog['u_opacity'].value = op
                    self._prog['u_bgra'].value = int(bgra)
                    self._prog['u_premul'].value = int(premul)
                    tex.use(0)
                    self._set_blend(blend)
                    self._vao.render(moderngl.TRIANGLE_STRIP, 4, i * 4)
                self._ctx.blend_equation = moderngl.FUNC_ADD
            if bool(scene.get("blackout")):
                self._ctx.clear(0.0, 0.0, 0.0, 1.0)
            lut_img = scene.get("lut")
            lut_tex = self._lut_texture(lut_img) if lut_img is not None else None
            # 最终 pass：场景（默认 720p）→ 输出 FBO（1080p）+ LUT + BGRA。
            ow = max(16, int(scene.get("out_w") or w))
            oh = max(16, int(scene.get("out_h") or h))
            out_fbo = self._out_targets(ow, oh)
            if out_fbo is None:
                return None
            out_fbo.use()
            self._ctx.viewport = (0, 0, ow, oh)
            self._ctx.disable(moderngl.BLEND)
            self._ctx.clear(0.0, 0.0, 0.0, 0.0)
            fbo_a.color_attachments[0].use(0)
            self._final['u_tex'].value = 0
            self._final['u_lut'].value = 1
            if lut_tex is not None:
                lut_tex.use(1)
                self._final['u_use_lut'].value = 1.0
            else:
                self._final['u_use_lut'].value = 0.0
            self._vao_final.render(moderngl.TRIANGLE_STRIP)
            # ★ 2026-11-26：Build Up 闪白在**最终输出 FBO**上叠（LUT 之后），
            #   主屏和独立屏都通过这里出图；不再依赖各屏的 layers 列表。
            _fa = int(scene.get("flash") or 0)
            if _fa > 0 and not bool(scene.get("blackout")):
                try:
                    _wi = getattr(self, "_flash_white_img", None)
                    if _wi is None or _wi.isNull():
                        _wi = QImage(2, 2, QImage.Format_ARGB32_Premultiplied)
                        _wi.fill(0xFFFFFFFF)
                        self._flash_white_img = _wi
                    self._ctx.enable(moderngl.BLEND)
                    self._ctx.blend_equation = moderngl.FUNC_ADD
                    self._draw_layer({
                        "img": _wi, "x": 0.0, "y": 0.0,
                        "w": float(w), "h": float(h), "rot": 0.0,
                        "opacity": max(0.0, min(1.0, _fa / 255.0)),
                        "blend": "normal", "slot": "__bu_flash_white"})
                except Exception:                              # noqa: BLE001
                    pass
            # 只解绑，不销毁（release 会销毁纹理）。留下 FBO 附件绑定会在
            # 下一帧 tex.write 时触发驱动同步，造成 80~100ms 慢帧。
            if self._dummy is not None:
                self._dummy.use(0)
                self._dummy.use(1)
            self._ctx.enable(moderngl.BLEND)
            sid = str(scene.get("sid") or "")
            # 原生显示 / Spout 都在**同一个 GL 上下文**里完成：没有第二个
            # Qt GL 上下文，也没有 Spout 线程的另一次 1080p 纹理上传。
            _tp = time.perf_counter()
            if bool(scene.get("display")):
                self._present_frame(out_fbo, ow, oh, True)
            _t_present = (time.perf_counter() - _tp) * 1000.0
            _ts = time.perf_counter()
            if bool(scene.get("spout")):
                self._send_spout(sid, out_fbo, w, h)
            _t_spout = (time.perf_counter() - _ts) * 1000.0
            if not readback:
                self._frames += 1
                self._last_ms = (time.perf_counter() - t0) * 1000.0
                self._parts = {"make": 0.0, "draw": 0.0,
                               "upload": self._up_ms, "final": 0.0,
                               "read": 0.0, "present": _t_present,
                               "spout": _t_spout,
                               "total": (time.perf_counter() - t0) * 1000.0}
                return None
            tr = time.perf_counter()
            data = out_fbo.read(components=4, alignment=1)
            self._readback_ms = (time.perf_counter() - tr) * 1000.0
            fmt = (QImage.Format_ARGB32_Premultiplied if alpha
                   else QImage.Format_RGB32)
            img = QImage(data, ow, oh, ow * 4, fmt).copy()
            self._last_img = img
            self._parts = {"make": 0.0, "draw": 0.0, "upload": self._up_ms,
                           "final": 0.0, "read": self._readback_ms,
                           "present": _t_present, "spout": _t_spout,
                           "total": (time.perf_counter() - t0) * 1000.0}
            self._frames += 1
            self._last_ms = (time.perf_counter() - t0) * 1000.0
            return img
        except Exception as e:                                 # noqa: BLE001
            self._fail("%s: %s" % (type(e).__name__, e))
            return None

    def stats(self):
        return {"frames": self._frames, "last_ms": self._last_ms,
                "avg_ms": self._last_ms, "readback_ms": self._readback_ms,
                "uploads": self._uploads,
                "up_bytes": self._up_bytes,
                "max_tex": self._max_tex,
                "parts": dict(getattr(self, "_parts", {}) or {})}

    def close(self):
        try:
            for s in list(self._spout_senders.values()):
                try:
                    s.releaseSender()
                except Exception:                              # noqa: BLE001
                    pass
            self._spout_senders.clear()
            if self._window is not None:
                try:
                    glfw.destroy_window(self._window)
                except Exception:                              # noqa: BLE001
                    pass
                self._window = None
                try:
                    glfw.terminate()
                except Exception:                              # noqa: BLE001
                    pass
            for rec in list(self._tex_slots.values()):
                try:
                    rec["tex"].release()
                except Exception:                              # noqa: BLE001
                    pass
            self._tex_slots.clear()
            for fbo in (self._fbo_a, self._fbo_b, self._fbo_out):
                try:
                    if fbo is not None:
                        fbo.release()
                except Exception:                              # noqa: BLE001
                    pass
            self._fbo_a = self._fbo_b = None
            if self._lut_tex is not None:
                try:
                    self._lut_tex.release()
                except Exception:                              # noqa: BLE001
                    pass
            if self._ctx is not None:
                self._ctx.release()
        except Exception:                                      # noqa: BLE001
            pass
        self._ok = False


__all__ = ["SceneStage"]
