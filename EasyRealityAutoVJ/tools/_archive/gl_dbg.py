"""GL 最小定位：清屏 / 实色绘制 / 贴图绘制，各读一个像素看真实值。"""

import ctypes
import sys
import time

sys.path.insert(0, r"P:\AutoVJ\_glenv")
import glfw                                                    # noqa: E402

_P = {}


def gl(name, restype, *argtypes):
    if name not in _P:
        addr = glfw.get_proc_address(name)
        if not addr:
            raise RuntimeError("no proc " + name)
        _P[name] = ctypes.WINFUNCTYPE(restype, *argtypes)(addr)
    return _P[name]


def main():
    import numpy as np
    glfw.init()
    glfw.window_hint(glfw.VISIBLE, glfw.FALSE)
    glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
    glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
    glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
    win = glfw.create_window(64, 64, "dbg", None, None)
    glfw.make_context_current(win)

    gs = gl("glGetString", ctypes.c_char_p, ctypes.c_uint)
    print("GL:", gs(0x1F02).decode(), "|", gs(0x1F01).decode())
    print("窗口 FBO 尺寸:", glfw.get_framebuffer_size(win))

    f_genTex = gl("glGenTextures", None, ctypes.c_int, ctypes.POINTER(ctypes.c_uint))
    f_genFBO = gl("glGenFramebuffers", None, ctypes.c_int, ctypes.POINTER(ctypes.c_uint))
    f_bindFBO = gl("glBindFramebuffer", None, ctypes.c_uint, ctypes.c_uint)
    f_fboTex = gl("glFramebufferTexture2D", None, ctypes.c_uint, ctypes.c_uint,
                  ctypes.c_uint, ctypes.c_uint, ctypes.c_int)
    f_fboStatus = gl("glCheckFramebufferStatus", ctypes.c_uint, ctypes.c_uint)
    f_bindTex = gl("glBindTexture", None, ctypes.c_uint, ctypes.c_uint)
    f_texImage = gl("glTexImage2D", None, ctypes.c_uint, ctypes.c_int, ctypes.c_int,
                    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
                    ctypes.c_uint, ctypes.c_void_p)
    f_texParam = gl("glTexParameteri", None, ctypes.c_uint, ctypes.c_uint, ctypes.c_int)
    f_viewport = gl("glViewport", None, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int)
    f_clearColor = gl("glClearColor", None, ctypes.c_float, ctypes.c_float,
                      ctypes.c_float, ctypes.c_float)
    f_clear = gl("glClear", None, ctypes.c_uint)
    f_read = gl("glReadPixels", None, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p)
    f_finish = gl("glFinish", None)
    f_err = gl("glGetError", ctypes.c_uint)
    f_drawBufs = gl("glDrawBuffers", None, ctypes.c_int, ctypes.POINTER(ctypes.c_uint))
    f_readBuf = gl("glReadBuffer", None, ctypes.c_uint)

    S = 256
    # FBO
    ft = ctypes.c_uint()
    f_genTex(1, ctypes.byref(ft))
    f_bindTex(0x0DE1, ft)
    f_texImage(0x0DE1, 0, 0x8058, S, S, 0, 0x1908, 0x1401, None)
    f_texParam(0x0DE1, 0x2801, 0x2601)
    f_texParam(0x0DE1, 0x2800, 0x2601)
    fbo = ctypes.c_uint()
    f_genFBO(1, ctypes.byref(fbo))
    f_bindFBO(0x8D40, fbo)
    f_fboTex(0x8D40, 0x8CE0, 0x0DE1, ft, 0)
    print("FBO 状态 0x%04X (0x8CD5=完整)  glGetError=0x%04X"
          % (f_fboStatus(0x8D40), f_err()))
    f_viewport(0, 0, S, S)

    def readpx(tag):
        f_finish()
        buf = np.zeros((4, 4, 4), dtype=np.uint8)
        f_read(0, 0, 4, 4, 0x1908, 0x1401, buf.ctypes.data)
        f_finish()
        print("    %-22s 左上 4x4 像素: %s" % (tag, buf[0, 0].tolist()))

    # 1) 纯清屏
    f_clearColor(1.0, 0.0, 0.0, 1.0)
    f_clear(0x00004000)
    print("  清屏红 glGetError=0x%04X" % f_err())
    readpx("清屏红(期望 [255,0,0,255])")

    # ---------- 着色器 ----------
    VS = b"#version 330 core\nlayout(location=0) in vec2 p;\nout vec2 uv;\nvoid main(){uv=p*0.5+0.5;gl_Position=vec4(p,0,1);}\n"
    FS_SOLID = b"#version 330 core\nout vec4 o;\nvoid main(){o=vec4(0,1,0,1);}\n"
    FS_TEX = b"#version 330 core\nin vec2 uv;\nout vec4 o;\nuniform sampler2D T;\nvoid main(){o=texture(T,uv);}\n"
    f_cs = gl("glCreateShader", ctypes.c_uint, ctypes.c_uint)
    f_ss = gl("glShaderSource", None, ctypes.c_uint, ctypes.c_int,
              ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_int))
    f_cp = gl("glCompileShader", None, ctypes.c_uint)
    f_csh = gl("glCreateProgram", ctypes.c_uint)
    f_at = gl("glAttachShader", None, ctypes.c_uint, ctypes.c_uint)
    f_lk = gl("glLinkProgram", None, ctypes.c_uint)
    f_use = gl("glUseProgram", None, ctypes.c_uint)
    f_loc = gl("glGetUniformLocation", ctypes.c_int, ctypes.c_uint, ctypes.c_char_p)
    f_u1i = gl("glUniform1i", None, ctypes.c_int, ctypes.c_int)
    f_genV = gl("glGenVertexArrays", None, ctypes.c_int, ctypes.POINTER(ctypes.c_uint))
    f_bindV = gl("glBindVertexArray", None, ctypes.c_uint)
    f_genB = gl("glGenBuffers", None, ctypes.c_int, ctypes.POINTER(ctypes.c_uint))
    f_bindB = gl("glBindBuffer", None, ctypes.c_uint, ctypes.c_uint)
    f_bufD = gl("glBufferData", None, ctypes.c_uint, ctypes.c_ssize_t,
                ctypes.c_void_p, ctypes.c_uint)
    f_enVA = gl("glEnableVertexAttribArray", None, ctypes.c_uint)
    f_vaP = gl("glVertexAttribPointer", None, ctypes.c_uint, ctypes.c_int, ctypes.c_uint,
               ctypes.c_ubyte, ctypes.c_int, ctypes.c_void_p)
    f_draw = gl("glDrawArrays", None, ctypes.c_uint, ctypes.c_int, ctypes.c_int)
    f_getLog = gl("glGetShaderInfoLog", None, ctypes.c_uint, ctypes.c_int,
                  ctypes.POINTER(ctypes.c_int), ctypes.c_char_p)
    f_getiv = gl("glGetShaderiv", None, ctypes.c_uint, ctypes.c_uint,
                 ctypes.POINTER(ctypes.c_int))

    def sh(t, src):
        s = f_cs(t)
        a = ctypes.c_char_p(src)
        n = ctypes.c_int(len(src))
        f_ss(s, 1, ctypes.byref(a), ctypes.byref(n))
        f_cp(s)
        ok = ctypes.c_int()
        f_getiv(s, 0x8B81, ctypes.byref(ok))
        if not ok.value:
            lg = ctypes.create_string_buffer(1024)
            f_getLog(s, 1024, None, lg)
            print("   着色器失败:", lg.value.decode(errors="replace")[:160])
        return s

    vao = ctypes.c_uint()
    f_genV(1, ctypes.byref(vao))
    f_bindV(vao)
    vb = ctypes.c_uint()
    f_genB(1, ctypes.byref(vb))
    f_bindB(0x8892, vb)
    q = np.array([-1, -1, 1, -1, -1, 1, 1, 1], dtype=np.float32)
    f_bufD(0x8892, q.nbytes, q.ctypes.data, 0x88E4)
    f_enVA(0)
    f_vaP(0, 2, 0x1406, False, 8, None)
    print("  顶点设置 glGetError=0x%04X" % f_err())

    # 2) 实色绿
    p = f_csh()
    f_at(p, sh(0x8B31, VS))
    f_at(p, sh(0x8B30, FS_SOLID))
    f_lk(p)
    f_use(p)
    f_clearColor(0.0, 0.0, 1.0, 1.0)
    f_clear(0x00004000)
    f_draw(0x0005, 0, 4)
    print("  实色绘制 glGetError=0x%04X" % f_err())
    readpx("实色绿(期望 [0,255,0,255])")

    # 3) 贴图：造一小块 BC3（纯红不透明）64x64
    f_genTex = gl("glGenTextures", None, ctypes.c_int, ctypes.POINTER(ctypes.c_uint))
    f_compImg = gl("glCompressedTexImage2D", None, ctypes.c_uint, ctypes.c_int, ctypes.c_uint,
                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p)
    blk = np.zeros((16 * 16, 16), dtype=np.uint8)
    blk[:, 0] = 0x00
    blk[:, 1] = 0xF8          # c0 = 0xF800 纯红
    blk[:, 2] = 0x00
    blk[:, 3] = 0xF8
    blk[:, 8] = 255
    blk[:, 9] = 255
    blk = np.ascontiguousarray(blk)
    ct = ctypes.c_uint()
    f_genTex(1, ctypes.byref(ct))
    f_bindTex(0x0DE1, ct)
    f_texParam(0x0DE1, 0x2801, 0x2601)
    f_texParam(0x0DE1, 0x2800, 0x2601)
    f_compImg(0x0DE1, 0, 0x83F3, 64, 64, 0, blk.nbytes, blk.ctypes.data)
    print("  BC3上传 glGetError=0x%04X" % f_err())
    p2 = f_csh()
    f_at(p2, sh(0x8B31, VS))
    f_at(p2, sh(0x8B30, FS_TEX))
    f_lk(p2)
    f_use(p2)
    f_u1i(f_loc(p2, b"T"), 0)
    f_clearColor(0.0, 0.0, 1.0, 1.0)
    f_clear(0x00004000)
    f_draw(0x0005, 0, 4)
    print("  贴图绘制 glGetError=0x%04X" % f_err())
    readpx("贴BC3红(期望 [255,0,0,255])")

    glfw.terminate()


if __name__ == "__main__":
    main()
