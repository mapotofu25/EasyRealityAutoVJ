"""GPU 可行性验证（gl_probe）—— ctypes 直连 OpenGL，不依赖 PyOpenGL

验证四件事 —— 任何一件不成立，"GPU 解码"方案就不成立：

  1) 能否在**无窗口**环境下创建 OpenGL 上下文（脚本 + 打包后的 exe 都要能）
  2) 能否把 DXV 里那种 **BC1/BC3 压缩块原样上传**成显卡纹理，由 GPU 硬件解压
  3) 「上传 + GPU 缩放绘制 + 回读」每帧要多少**墙钟与 CPU**（对照：CPU 解码 9.8 / 12.9 ms）
  4) 回读能不能用 PBO 异步，避免把 CPU 堵在同步点上

⚠ 之所以不用 PyOpenGL：它 3.1.10 传 `void*` 的转换器有 bug
  （`'NumberHandler' object has no attribute 'arrayByteCount'`），而且发布时多一个依赖。
  这里改用 glfw 拿上下文 + `glfw.get_proc_address()` 取函数地址，全部走 ctypes，
  将来换成 PySide6 的 `QOpenGLContext.getProcAddress()` 即可零额外依赖。

用法：
    venv\\Scripts\\python.exe tools\\gl_probe.py
"""

import ctypes
import os
import sys
import time

sys.path.insert(0, r"P:\AutoVJ\_glenv")

OUT = []
GL = None
_PTR = {}


def w(s=""):
    print(s, flush=True)
    OUT.append(s)


def save():
    with open(r"P:\AutoVJ\_gl_probe.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(OUT))


# ------------------------------------------------------------------ GL 绑定
def gl(name, restype, *argtypes):
    f = _PTR.get(name)
    if f is None:
        import glfw
        addr = glfw.get_proc_address(name)
        if not addr:
            raise RuntimeError("取不到 GL 函数: %s" % name)
        f = ctypes.WINFUNCTYPE(restype, *argtypes)(addr)
        _PTR[name] = f
    return f


def err(tag):
    e = gl("glGetError", ctypes.c_uint)()
    if e:
        w("      !! %s 之后 GL 错误 0x%04X" % (tag, e))
    return e


# ------------------------------------------------------------------ BC 数据
def make_bc3(w_px, h_px, mode="red"):
    """造 BC3(DXT5) 压缩块。

    ⚠ BC3 的 16 字节布局**不是**颜色在前：
        [0..1] alpha0, alpha1     [2..7] 16 个 3-bit alpha 索引
        [8..9] color0(RGB565 LE)  [10..11] color1   [12..15] 16 个 2-bit 颜色索引
    （第一版把颜色写在 0..3、alpha 写在 8..9 → 采样出来是全白，排查了半天）
    """
    import numpy as np
    bw, bh = w_px // 4, h_px // 4
    out = np.zeros((bw * bh, 16), dtype=np.uint8)
    for by in range(bh):
        for bx in range(bw):
            i = by * bw + bx
            if mode == "red":
                c0 = 0xF800                      # RGB565 纯红
            else:
                c0 = (int(31 * bx / max(1, bw - 1)) << 11)   # 横向渐变，验块顺序
            out[i, 0] = 255                      # alpha0
            out[i, 1] = 255                      # alpha1
            out[i, 2:8] = 0                      # alpha 索引全 0 → 取 alpha0 = 255
            out[i, 8:10] = np.frombuffer(np.uint16([c0]).tobytes(), dtype=np.uint8)
            out[i, 10:12] = np.frombuffer(np.uint16([c0]).tobytes(), dtype=np.uint8)
            out[i, 12:16] = 0                    # 颜色索引全 0 → 取 c0
    return np.ascontiguousarray(out)


def make_bc1(w_px, h_px):
    """BC1(DXT1) 块：8 字节 = [c0(2), c1(2), 4 字节颜色索引]"""
    import numpy as np
    bw, bh = w_px // 4, h_px // 4
    out = np.zeros((bw * bh, 8), dtype=np.uint8)
    red = np.frombuffer(np.uint16([0xF800]).tobytes(), dtype=np.uint8)
    out[:, 0:2] = red
    out[:, 2:4] = red
    return np.ascontiguousarray(out)


GLSL_VS = b"""#version 330 core
layout(location=0) in vec2 aPos;
layout(location=1) in vec2 aUV;
out vec2 vUV;
void main(){ vUV = aUV; gl_Position = vec4(aPos, 0.0, 1.0); }
"""

GLSL_FS = b"""#version 330 core
in vec2 vUV;
out vec4 FragColor;
uniform sampler2D uTex;
void main(){ FragColor = texture(uTex, vUV); }
"""


def main():
    import numpy as np

    w("=" * 78)
    w("GPU 可行性验证（ctypes 直连）   %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    w("=" * 78)

    # ---------- 1) 离屏上下文 ----------
    import glfw
    glfw.init()
    glfw.window_hint(glfw.VISIBLE, glfw.FALSE)
    glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
    glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
    glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
    t0 = time.perf_counter()
    win = glfw.create_window(64, 64, "autovj-glprobe", None, None)
    if not win:
        glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_ANY_PROFILE)
        win = glfw.create_window(64, 64, "autovj-glprobe", None, None)
    if not win:
        w("[1] ★ 创建离屏上下文失败 —— 无 GL 环境，方案不成立")
        glfw.terminate()
        save()
        return
    glfw.make_context_current(win)
    glfw.swap_interval(0)
    w("[1] 离屏 OpenGL 上下文 OK（%.0f ms）" % ((time.perf_counter() - t0) * 1000))
    gs = gl("glGetString", ctypes.c_char_p, ctypes.c_uint)
    for nm, k in (("版本", 0x1F02), ("渲染器", 0x1F01), ("厂商", 0x1F00)):
        v = gs(k)
        w("    %s = %s" % (nm, v.decode() if v else "?"))

    # ---------- 2) 压缩纹理上传 ----------
    GL_COMPRESSED_RGBA_S3TC_DXT5 = 0x83F3
    GL_COMPRESSED_RGB_S3TC_DXT1 = 0x83F0
    f_genTex = gl("glGenTextures", None, ctypes.c_int, ctypes.POINTER(ctypes.c_uint))
    f_bindTex = gl("glBindTexture", None, ctypes.c_uint, ctypes.c_uint)
    f_texParam = gl("glTexParameteri", None, ctypes.c_uint, ctypes.c_uint, ctypes.c_int)
    f_compImg = gl("glCompressedTexImage2D", None, ctypes.c_uint, ctypes.c_int, ctypes.c_uint,
                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p)
    f_compSub = gl("glCompressedTexSubImage2D", None, ctypes.c_uint, ctypes.c_int,
                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
                   ctypes.c_int, ctypes.c_void_p)

    blocks = make_bc3(1920, 1080, "red")
    tid = ctypes.c_uint()
    f_genTex(1, ctypes.byref(tid))
    f_bindTex(0x0DE1, tid)
    for k, v in ((0x2801, 0x2601), (0x2800, 0x2601), (0x2802, 0x812F), (0x2803, 0x812F)):
        f_texParam(0x0DE1, k, v)
    f_compImg(0x0DE1, 0, GL_COMPRESSED_RGBA_S3TC_DXT5, 1920, 1080, 0,
              blocks.nbytes, blocks.ctypes.data)
    e = err("上传 BC3 1080p")
    w("[2] 上传 BC3/DXT5 1920x1080（%.2f MB 压缩数据）→ %s"
      % (blocks.nbytes / 1e6, "OK" if e == 0 else "★ 失败"))
    d1 = make_bc1(1920, 1080)
    f_genTex(1, ctypes.byref(tid))
    f_bindTex(0x0DE1, tid)
    f_compImg(0x0DE1, 0, GL_COMPRESSED_RGB_S3TC_DXT1, 1920, 1080, 0, d1.nbytes, d1.ctypes.data)
    e = err("上传 BC1 1080p")
    w("[2] 上传 BC1/DXT1 1920x1080（%.2f MB）→ %s"
      % (d1.nbytes / 1e6, "OK" if e == 0 else "★ 失败"))

    # ---------- 3) 着色器 / VAO / FBO ----------
    f_createShader = gl("glCreateShader", ctypes.c_uint, ctypes.c_uint)
    f_shaderSource = gl("glShaderSource", None, ctypes.c_uint, ctypes.c_int,
                        ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_int))
    f_compile = gl("glCompileShader", None, ctypes.c_uint)
    f_getShaderiv = gl("glGetShaderiv", None, ctypes.c_uint, ctypes.c_uint,
                       ctypes.POINTER(ctypes.c_int))
    f_getShaderLog = gl("glGetShaderInfoLog", None, ctypes.c_uint, ctypes.c_int,
                        ctypes.POINTER(ctypes.c_int), ctypes.c_char_p)
    f_createProg = gl("glCreateProgram", ctypes.c_uint)
    f_attach = gl("glAttachShader", None, ctypes.c_uint, ctypes.c_uint)
    f_link = gl("glLinkProgram", None, ctypes.c_uint)
    f_use = gl("glUseProgram", None, ctypes.c_uint)
    f_getProgv = gl("glGetProgramiv", None, ctypes.c_uint, ctypes.c_uint,
                    ctypes.POINTER(ctypes.c_int))
    f_getProgLog = gl("glGetProgramInfoLog", None, ctypes.c_uint, ctypes.c_int,
                      ctypes.POINTER(ctypes.c_int), ctypes.c_char_p)
    f_clearColor = gl("glClearColor", None, ctypes.c_float, ctypes.c_float,
                      ctypes.c_float, ctypes.c_float)
    f_clear = gl("glClear", None, ctypes.c_uint)
    f_uniLoc = gl("glGetUniformLocation", ctypes.c_int, ctypes.c_uint, ctypes.c_char_p)
    f_uni1i = gl("glUniform1i", None, ctypes.c_int, ctypes.c_int)
    f_genVAO = gl("glGenVertexArrays", None, ctypes.c_int, ctypes.POINTER(ctypes.c_uint))
    f_bindVAO = gl("glBindVertexArray", None, ctypes.c_uint)
    f_genBuf = gl("glGenBuffers", None, ctypes.c_int, ctypes.POINTER(ctypes.c_uint))
    f_bindBuf = gl("glBindBuffer", None, ctypes.c_uint, ctypes.c_uint)
    f_bufData = gl("glBufferData", None, ctypes.c_uint, ctypes.c_ssize_t,
                   ctypes.c_void_p, ctypes.c_uint)
    f_enableVA = gl("glEnableVertexAttribArray", None, ctypes.c_uint)
    f_vaPtr = gl("glVertexAttribPointer", None, ctypes.c_uint, ctypes.c_int, ctypes.c_uint,
                 ctypes.c_ubyte, ctypes.c_int, ctypes.c_void_p)
    f_genFBO = gl("glGenFramebuffers", None, ctypes.c_int, ctypes.POINTER(ctypes.c_uint))
    f_bindFBO = gl("glBindFramebuffer", None, ctypes.c_uint, ctypes.c_uint)
    f_fboTex = gl("glFramebufferTexture2D", None, ctypes.c_uint, ctypes.c_uint,
                  ctypes.c_uint, ctypes.c_uint, ctypes.c_int)
    f_fboStatus = gl("glCheckFramebufferStatus", ctypes.c_uint, ctypes.c_uint)
    f_viewport = gl("glViewport", None, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int)
    f_texImage2D = gl("glTexImage2D", None, ctypes.c_uint, ctypes.c_int, ctypes.c_int,
                      ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint,
                      ctypes.c_void_p)
    f_draw = gl("glDrawArrays", None, ctypes.c_uint, ctypes.c_int, ctypes.c_int)
    f_finish = gl("glFinish", None)
    f_readPix = gl("glReadPixels", None, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                   ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p)
    f_activeTex = gl("glActiveTexture", None, ctypes.c_uint)

    def compile_sh(stype, src):
        sh = f_createShader(stype)
        arr = ctypes.c_char_p(src)
        ln = ctypes.c_int(len(src))
        f_shaderSource(sh, 1, ctypes.byref(arr), ctypes.byref(ln))
        f_compile(sh)
        ok = ctypes.c_int()
        f_getShaderiv(sh, 0x8B81, ctypes.byref(ok))
        if not ok.value:
            log = ctypes.create_string_buffer(2048)
            f_getShaderLog(sh, 2048, None, log)
            w("    ★ 着色器编译失败: %s" % log.value.decode(errors="replace")[:200])
        return sh

    vs, fs = compile_sh(0x8B31, GLSL_VS), compile_sh(0x8B30, GLSL_FS)
    prog = f_createProg()
    f_attach(prog, vs)
    f_attach(prog, fs)
    f_link(prog)
    lok = ctypes.c_int()
    f_getProgv(prog, 0x8B82, ctypes.byref(lok))
    if not lok.value:
        log = ctypes.create_string_buffer(2048)
        f_getProgLog(prog, 2048, None, log)
        w("    ★ 着色器程序链接失败: %s" % log.value.decode(errors="replace")[:300])
    else:
        w("    着色器程序链接 OK（GL 3.3 core）")
    f_use(prog)
    f_uni1i(f_uniLoc(prog, b"uTex"), 0)

    vao = ctypes.c_uint()
    f_genVAO(1, ctypes.byref(vao))
    f_bindVAO(vao)
    vbo = ctypes.c_uint()
    f_genBuf(1, ctypes.byref(vbo))
    f_bindBuf(0x8892, vbo)
    quad = np.array([-1, -1, 0, 0, 1, -1, 1, 0, -1, 1, 0, 1, 1, 1, 1, 1], dtype=np.float32)
    f_bufData(0x8892, quad.nbytes, quad.ctypes.data, 0x88E4)
    f_enableVA(0)
    f_vaPtr(0, 2, 0x1406, False, 16, None)
    f_enableVA(1)
    f_vaPtr(1, 2, 0x1406, False, 16, ctypes.c_void_p(8))
    err("初始化")

    def build_fbo(tw, th):
        t = ctypes.c_uint()
        f_genTex(1, ctypes.byref(t))
        f_bindTex(0x0DE1, t)
        f_texImage2D(0x0DE1, 0, 0x8058, tw, th, 0, 0x1908, 0x1401, None)
        for k, v in ((0x2801, 0x2601), (0x2800, 0x2601)):
            f_texParam(0x0DE1, k, v)
        fb = ctypes.c_uint()
        f_genFBO(1, ctypes.byref(fb))
        f_bindFBO(0x8D40, fb)
        f_fboTex(0x8D40, 0x8CE0, 0x0DE1, t, 0)
        return fb, f_fboStatus(0x8D40)

    def bench(fn, n=30, warm=3):
        for _ in range(warm):
            fn()
        f_finish()
        p0 = time.process_time()
        t0 = time.perf_counter()
        for _ in range(n):
            fn()
        f_finish()
        return ((time.perf_counter() - t0) / n * 1000,
                (time.process_time() - p0) / n * 1000)

    # ---------- 4) 端到端计时 ----------
    w("")
    w("[3] 端到端：BC3 压缩块 → GPU 硬件解压 → 缩放绘制 → 回读 RGBA")
    w("     %-22s %9s %11s %11s" % ("源 → 目标", "压缩数据", "墙钟ms", "CPU ms"))
    w("     " + "-" * 60)

    cases = (((1920, 1080), (1920, 1080)),
             ((3840, 2160), (1920, 1080)),
             ((3840, 2160), (2880, 1620)),
             ((5376, 1232), (1920, 1080)))
    for (sw, sh), (tw, th) in cases:
        blk = make_bc3(sw, sh, "red")
        t = ctypes.c_uint()
        f_genTex(1, ctypes.byref(t))
        f_bindTex(0x0DE1, t)
        for k, v in ((0x2801, 0x2601), (0x2800, 0x2601), (0x2802, 0x812F), (0x2803, 0x812F)):
            f_texParam(0x0DE1, k, v)
        f_compImg(0x0DE1, 0, GL_COMPRESSED_RGBA_S3TC_DXT5, sw, sh, 0, blk.nbytes, blk.ctypes.data)
        if err("上传 %dx%d" % (sw, sh)):
            continue
        fb, st = build_fbo(tw, th)
        if st != 0x8CD5:
            w("     FBO 不完整 0x%04X" % st)
            continue
        f_bindFBO(0x8D40, fb)
        f_viewport(0, 0, tw, th)
        buf = np.empty((th, tw, 4), dtype=np.uint8)

        def up():
            f_bindTex(0x0DE1, t)
            f_compSub(0x0DE1, 0, 0, 0, sw, sh, GL_COMPRESSED_RGBA_S3TC_DXT5,
                      blk.nbytes, blk.ctypes.data)

        def dr():
            f_clearColor(0.0, 0.0, 1.0, 1.0)          # 清成蓝色：若结果还是蓝 = 没画上去
            f_clear(0x00004000)
            f_activeTex(0x84C0)
            f_bindTex(0x0DE1, t)
            f_draw(0x0005, 0, 4)

        def rd():
            f_readPix(0, 0, tw, th, 0x1908, 0x1401, buf.ctypes.data)

        def full():
            up()
            dr()
            rd()

        uw, uc = bench(up)
        rw, rc = bench(rd)
        fw, fc = bench(full)
        tag = "%dx%d → %dx%d" % (sw, sh, tw, th)
        w("     %-22s %8.1fMB %11.2f %11.2f   ← 仅上传" % (tag, blk.nbytes / 1e6, uw, uc))
        w("     %-22s %8.1fMB %11.2f %11.2f   ← 仅回读" % ("", tw * th * 4 / 1e6, rw, rc))
        w("     %-22s %8s %11.2f %11.2f   ← 上传+绘制+回读" % ("", "", fw, fc))
        ok = bool((buf[:, :, 0] > 240).all() and (buf[:, :, 3] > 250).all()
                  and (buf[:, :, 1] < 20).all())
        w("     %-22s 像素校验 %s (R%.0f G%.0f B%.0f A%.0f)"
          % ("", "OK 纯红不透明" if ok else "★ 不符",
             buf[:, :, 0].mean(), buf[:, :, 1].mean(), buf[:, :, 2].mean(), buf[:, :, 3].mean()))
        err("回读")

    w("")
    w("=" * 78)
    w("对照：CPU 解码 1080p DXT5 = 9.80 ms / 帧，4K DXT5 = 12.89 ms / 帧（各含 LZ + DXT 解压）")
    w("=" * 78)
    glfw.terminate()
    save()


if __name__ == "__main__":
    main()
