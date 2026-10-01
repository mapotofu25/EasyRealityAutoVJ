"""离屏 OpenGL 解码器：BC(DXT) 压缩块 → GPU 硬件解压 → 缩放绘制 → 回读 RGBA。

这是"GPU 解码"的核心。设计要点：

1. **不依赖 PyOpenGL**：用 glfw 创建离屏上下文，再用 `glfw.get_proc_address()` 拿函数地址，
   全部经 ctypes 调用。（PyOpenGL 3.1.10 传 `void*` 的转换器有 bug，且发布时多一个依赖。）
   将来若要去掉 glfw，可换成 PySide6 的 `QOpenGLContext.getProcAddress()`，接口一致。

2. **全进程只有一个 GL 上下文**（`GLWorker` 持有的单个 `GLDecoder`）：GL 上下文是
   "当前线程"绑定的，所以这个唯一上下文固定绑在 **GL 工作线程**上，所有解码请求都
   排队交给它**串行**执行。（2026-09-26 起不再 per-player 建/销毁上下文 —— 因为
   `glfwCreateWindow`/`glfwDestroyWindow` 在 NVIDIA 驱动上会偶发永久挂死，见文件末尾
   `GLWorker` 的详细说明。）

3. **回读是唯一的"墙"**：GPU 解压后在 GPU 上完成缩放，只回读目标尺寸的 RGBA，
   所以回读量与源分辨率无关（4K 源缩到 1080p 也只回读 1080p）。
   实测 1080p 回读 1.04ms、上传 0.26ms、整条 CPU 1.56ms/帧（软件解要 9.8ms）。

4. **方向**：顶点 UV 已按"视频首行 = 图像顶部"摆放，`glReadPixels` 从 y=0 读回来
   就是自上而下，和 FFmpeg 的 RGBA 一致（无需 np.flipud）。
"""

import collections
import ctypes
import os
import sys
import threading
import time

# ---- GPU 回读监控（见 decode() 里的详细说明）----
SLOW_READBACK_MS = 250      # 单次回读超过这个毫秒数就算"慢"
SLOW_LIMIT = 5              # 连续这么多次慢 → 判定 GPU 没空，退回软解

_LIB = None
_LIB_LOCK = threading.Lock()
_PROC = {}

# ---- GL 上下文配额守卫（2026-09-26 卡死事件的护栏）----
# 实测：**每个 GL 上下文会给进程增加约 21 个线程**（NVIDIA 驱动的 nvoglv64/nvwgf2umx
# 线程组），且这些线程只有在窗口被 `glfwDestroyWindow` 之后才会由驱动回收。
# 正常使用下活跃上下文 = 活跃 GpuDxvPlayer 数 ≤ 4 图层 × LRU 8 = 32，线程约 700。
# 这里设一道硬上限：一旦超过就**拒绝新建、直接走软解** —— 宁可慢一点，
# 也绝不让线程数无声地爬到几千把整台机器拖死（现场卡死时实测 2386 线程）。
MAX_CTX = 40
_CTX_N = 0                    # 当前存活的上下文数
_CTX_LOCK = threading.Lock()  # 保护 _CTX_N
# ★★ 所有 GL 调用的**全局串行锁**（不只是 create/destroy）—— 2026-09-26 死锁根因修复。
# 原来只有 create/destroy 走这把锁，而 `decode()`（上传压缩纹理 / 建 FBO / 回读）
# **完全没有锁** ⇒ 只要出现「一个线程在 `glfwDestroyWindow`、另一个在 `glReadPixels`」
# 这种组合，**NVIDIA 的 WGL 层就会死锁**。现场实测：11 个解码线程全部冻结
# （6 个卡在 destroy_window、4 个卡在 glReadPixels、1 个在建 FBO），
# **逐次快照位置完全不变、CPU 却持续烧 3.6 核（驱动内部自旋）、永久不恢复**。
# 串行化后「同一时刻只有一个线程碰 GL」，从根上消除该组合。
_CREATE_LOCK = threading.RLock()
GL_LOCK_TIMEOUT = 1.0              # 普通 GL 调用等锁上限：拿不到就**丢这一帧**，绝不跟着一起冻结
GL_CREATE_TIMEOUT = 2.0            # 建上下文等锁上限（比单帧操作宽松，但仍有限）
# ★★ 第二轮死锁修复（2026-09-26 夜场）：**光串行还不够，还得能自愈**。
# 上面这把锁解决了「A 在 destroy、B 在 glReadPixels」的并发；但 19:46 版仍会卡死，
# 现场日志抓到另一种形态：**某个线程持锁卡在 `glfwDestroyWindow` 里永不返回**
# （NVIDIA WGL 已知行为），于是这把锁**永久被占** ——
#   1 个线程卡在 destroy_window（持锁者）
#   3 个线程卡在 destroy()      的 `with _CREATE_LOCK:`（无限等 ⇒ 陪葬）
#   2 个线程卡在 _init()        的 `with _CREATE_LOCK:`（无限等 ⇒ 陪葬）
#   4 个线程卡在 decode()        （有 1s 超时，但仍每帧白等一次）
# 结果：所有解码线程全冻 ⇒ 玩家永不出帧 ⇒ 界面「未响应」，直到重开软件（用户实测就是这个）。
# ⇒ 修复三点：
#   ① 建上下文的**整段** GL 操作都必须持锁（原来 make_current/编译着色器/VAO 在锁外，
#      正是它与 destroy_window 并发才把驱动搞死的）；
#   ② 等锁一律**带超时**，绝不无限等；
#   ③ **污染自愈**：一旦发现锁被某个线程持有超过 `GL_LOCK_POISON_S`（明显是挂在驱动里），
#      就判定「GL 驱动已挂死」，之后**所有 GL 操作直接失败**，逼上层降级软解 ——
#      画面继续跑，只是这一轮没了 GPU 加速，**不会再永久卡死**。
GL_LOCK_POISON_S = 5.0             # 锁被同一线程持有超过这么久 ⇒ 判定驱动挂死
# 工作线程"首次建上下文"抢锁失败时的重试间隔（秒）：只是没抢到锁 ⇒ 稍后再试，**不置死**。
_READY_RETRY_SECS = 0.5
_LOCK_POISON = [False]             # 一旦 True：本次进程内不再碰 GL（全部走软解）
_OWN = {"tid": None, "since": 0.0, "depth": 0}   # 持锁者（只在临界区内改，天然串行）


def gl_poisoned():
    """GL 驱动是否已被判定挂死（一旦为真，本次运行不再使用 GPU 解码）"""
    return _LOCK_POISON[0]


def _gl_enter(timeout=GL_LOCK_TIMEOUT):
    """进入 GL 临界区。True = 已持锁（必须配对 `_gl_exit()`）。

    拿不到锁时做一件关键的事：**判断锁是不是被某线程长期占着**。GL 调用进了驱动
    可能**永不返回**，持锁线程若真挂在驱动里，等下去只会让所有解码线程陪葬 ⇒
    判定**污染**，此后所有 GL 操作直接失败，让上层转软解。
    """
    if _LOCK_POISON[0]:
        return False
    if _CREATE_LOCK.acquire(timeout=timeout):
        me = threading.get_ident()
        if _OWN["tid"] != me:                  # 首次获取才计时（重入不刷新）
            _OWN["tid"] = me
            _OWN["since"] = time.perf_counter()
            _OWN["depth"] = 1
        else:
            _OWN["depth"] += 1
        return True
    since = _OWN["since"]
    if since and (time.perf_counter() - since) > GL_LOCK_POISON_S:
        if not _LOCK_POISON[0]:
            _LOCK_POISON[0] = True
            try:
                import stallwatch
                stallwatch.log_line(
                    "!! GL 驱动疑似挂死：线程 %s 持有 GL 锁已超过 %.0f 秒未返回 "
                    "=> 已停用 GPU 解码并转软解（画面不会断；重开软件可恢复 GPU 解码）"
                    % (_OWN["tid"], GL_LOCK_POISON_S))
            except Exception:                                    # noqa: BLE001
                pass
    return False


def _gl_exit():
    d = _OWN["depth"] - 1
    _OWN["depth"] = d if d > 0 else 0
    if d <= 0:
        _OWN["tid"] = None
        _OWN["since"] = 0.0
    _CREATE_LOCK.release()


class GLLockUnavailable(Exception):
    """**抢 GL 全局锁超时**（`_gl_enter` 拿不到锁）。

    与"真正的上下文创建失败"必须区分开：这**只是**"此刻锁被别的线程占用"，
    线程本身好得很、锁一放开就能建成功 ⇒ 上层必须当作"尚未就绪、稍后重试"，
    **绝不可**据此判定"工作线程崩溃"而永久置死 GPU（这正是今天咬了两次的模式）。
    """


def ctx_count():
    """当前存活 GL 上下文数（诊断用）"""
    return _CTX_N


def _ctx_reserve():
    """申请一个上下文配额。返回 False = 已达上限（调用方应回退软解）"""
    global _CTX_N
    with _CTX_LOCK:
        if _CTX_N >= MAX_CTX:
            return False
        _CTX_N += 1
        return True


def _ctx_release():
    global _CTX_N
    with _CTX_LOCK:
        if _CTX_N > 0:
            _CTX_N -= 1

GL_TEXTURE_2D = 0x0DE1
GL_RGBA8 = 0x8058
GL_RGBA = 0x1908
GL_UNSIGNED_BYTE = 0x1401
GL_COMPRESSED_RGB_S3TC_DXT1 = 0x83F0
GL_COMPRESSED_RGBA_S3TC_DXT5 = 0x83F3
GL_FRAMEBUFFER = 0x8D40
GL_COLOR_ATTACHMENT0 = 0x8CE0
GL_FRAMEBUFFER_COMPLETE = 0x8CD5
GL_ARRAY_BUFFER = 0x8892
GL_STATIC_DRAW = 0x88E4
GL_FLOAT = 0x1406
GL_TRIANGLE_STRIP = 0x0005
GL_TEXTURE0 = 0x84C0
GL_COLOR_BUFFER_BIT = 0x00004000
GL_VERTEX_SHADER = 0x8B31
GL_FRAGMENT_SHADER = 0x8B30
GL_LINEAR = 0x2601
GL_CLAMP_TO_EDGE = 0x812F
GL_TEXTURE_MIN_FILTER = 0x2801
GL_TEXTURE_MAG_FILTER = 0x2800
GL_TEXTURE_WRAP_S = 0x2802
GL_TEXTURE_WRAP_T = 0x2803


def _lib():
    """glfw 只初始化一次；返回 (glfw, 是否可用)"""
    global _LIB
    with _LIB_LOCK:
        if _LIB is not None:
            return _LIB
        try:
            # 允许用 _glenv 这种隔离安装目录（开发期）
            for extra in (os.path.join(os.path.dirname(os.path.dirname(
                    os.path.abspath(__file__))), "_glenv"),):
                if os.path.isdir(extra) and extra not in sys.path:
                    sys.path.append(extra)
            import glfw
            if not glfw.init():
                _LIB = False
            else:
                _LIB = glfw
        except Exception:
            _LIB = False
        return _LIB


def gl(name, restype, *argtypes):
    """按名字取 GL 函数（经 glfw.get_proc_address），带缓存"""
    key = (name, restype)
    f = _PROC.get(key)
    if f is None:
        g = _lib()
        addr = g.get_proc_address(name)
        if not addr:
            raise RuntimeError("取不到 GL 函数 %s" % name)
        f = ctypes.WINFUNCTYPE(restype, *argtypes)(addr)
        _PROC[key] = f
    return f


VS = b"""#version 330 core
layout(location=0) in vec2 aPos;
layout(location=1) in vec2 aUV;
out vec2 vUV;
void main(){ vUV = aUV; gl_Position = vec4(aPos, 0.0, 1.0); }
"""

FS = b"""#version 330 core
in vec2 vUV;
out vec4 FragColor;
uniform sampler2D uTex;
uniform int uSwizzle;
void main(){
    vec4 c = texture(uTex, vUV);
    FragColor = (uSwizzle == 1) ? c.bgra : c;
}
"""

# 顶点 (x,y,u,v)：uv 的 v=0 放在 GL 底部 → 回读出来就是自上而下
QUAD = (ctypes.c_float * 16)(-1, -1, 0, 0,
                             1, -1, 1, 0,
                             -1, 1, 0, 1,
                             1, 1, 1, 1)


def preinit():
    """在主线程预先初始化 glfw。

    glfw 要求 `glfwInit` 在**主线程**调用（窗口可以在别的线程创建）。集成到软件后
    唯一那个 `GLDecoder` 是在 **GL 工作线程**（`GLWorker`）里创建的，所以启动时先在
    这里把 init 付掉，工作线程只做 create_window + make_context_current。
    失败返回 False（调用方据此直接回退软解，不要重试）。
    """
    try:
        return _lib() is not False
    except Exception:
        return False


class GLDecoder:
    """离屏 GL 解码器。线程不安全 —— 每个解码线程一个实例。"""

    def __init__(self):
        self.ok = False
        self.err = ""
        self._owner_tid = None
        self._win = None
        self._tex = None
        self._tex_key = None
        self._fbo = None
        self._fbo_tex = None
        self._fbo_size = None
        self._prog = None
        self._vao = None
        self._vbo = None
        self._buf = None
        self._buf_size = None
        self._slow_n = 0            # 连续慢回读计数（GPU 被占用时用来触发降级）
        self.last_readback = 0.0    # 最近一次回读耗时（秒），诊断用
        self._u_swizzle = -1      # uSwizzle 的 uniform 位置
        self._swizzle = -1        # 当前 swizzle 状态（-1 = 未设置）
        self.lock_timeout = False  # ★ True = 仅"没抢到 GL 锁"（可重试，非崩溃）
        try:
            self._init()
            self.ok = True
        except GLLockUnavailable as e:                          # ★ 可重试：不是崩溃
            self.err = str(e)[:200]
            self.ok = False
            self.lock_timeout = True
        except Exception as e:                                  # noqa: BLE001
            self.err = str(e)[:200]
            self.ok = False

    # ---------------------------------------------------------------- 初始化
    def _init(self):
        g = _lib()
        if not g:
            raise RuntimeError("glfw 不可用（无法创建 OpenGL 上下文）")
        if _LOCK_POISON[0]:
            raise RuntimeError("GL 驱动已判定挂死（本次运行不再使用 GPU 解码）")
        # 配额守卫：超过 MAX_CTX 就拒绝新建（调用方会自动回退软解）。
        # 正常情况下活跃上下文 ≤ 4 图层 × LRU 8 = 32，碰不到这条线；
        # 一旦碰到，说明有上下文没被释放 —— 宁可让这个素材走软解，也不能把现场拖死。
        if not _ctx_reserve():
            raise RuntimeError("GL 上下文已达上限 %d（可能有上下文未释放），转软解" % MAX_CTX)
        keep = False
        try:
            # ★★ 建上下文**整段**都要在 GL 锁内（第二轮死锁修复第 ① 点，见模块顶部）。
            #    原来只有 `create_window` 在锁内，而 `make_context_current` / 编译着色器 /
            #    建 VAO-VBO 全在**锁外** —— 正是它们与另一线程持锁的 `glfwDestroyWindow`
            #    并发，才把 NVIDIA WGL 拖进死锁。现场日志的三个签名完全吻合。
            if not _gl_enter(GL_CREATE_TIMEOUT):
                # ★ 只是"没抢到锁"（可达：另一线程正在持锁做 GL）——**不是崩溃**。
                #   抛专用异常，让 `_run` 当作"尚未就绪、稍后重试"，绝不永久置死 GPU。
                raise GLLockUnavailable(
                    "GL 锁在 %.1fs 内未取得（另一线程正持锁）；本次未就绪，稍后重试"
                    % GL_CREATE_TIMEOUT)
            try:
                g.window_hint(g.VISIBLE, g.FALSE)
                g.window_hint(g.CONTEXT_VERSION_MAJOR, 3)
                g.window_hint(g.CONTEXT_VERSION_MINOR, 3)
                g.window_hint(g.OPENGL_PROFILE, g.OPENGL_CORE_PROFILE)
                win = g.create_window(32, 32, "autovj-gpu-decode", None, None)
                if not win:
                    g.window_hint(g.OPENGL_PROFILE, g.OPENGL_ANY_PROFILE)
                    win = g.create_window(32, 32, "autovj-gpu-decode", None, None)
                if not win:
                    raise RuntimeError("创建离屏 GL 上下文失败")
                self._win = win
                g.make_context_current(win)
                self._owner_tid = threading.get_ident()
                self._load_fns()
                self._prog = self._build_program()
                # VAO / VBO
                vao = ctypes.c_uint()
                gl("glGenVertexArrays", None, ctypes.c_int, ctypes.POINTER(ctypes.c_uint))(1, ctypes.byref(vao))
                gl("glBindVertexArray", None, ctypes.c_uint)(vao)
                vbo = ctypes.c_uint()
                gl("glGenBuffers", None, ctypes.c_int, ctypes.POINTER(ctypes.c_uint))(1, ctypes.byref(vbo))
                gl("glBindBuffer", None, ctypes.c_uint, ctypes.c_uint)(GL_ARRAY_BUFFER, vbo)
                gl("glBufferData", None, ctypes.c_uint, ctypes.c_ssize_t, ctypes.c_void_p,
                   ctypes.c_uint)(GL_ARRAY_BUFFER, ctypes.sizeof(QUAD), ctypes.addressof(QUAD),
                                  GL_STATIC_DRAW)
                gl("glEnableVertexAttribArray", None, ctypes.c_uint)(0)
                gl("glVertexAttribPointer", None, ctypes.c_uint, ctypes.c_int, ctypes.c_uint,
                   ctypes.c_ubyte, ctypes.c_int, ctypes.c_void_p)(0, 2, GL_FLOAT, False, 16, None)
                gl("glEnableVertexAttribArray", None, ctypes.c_uint)(1)
                gl("glVertexAttribPointer", None, ctypes.c_uint, ctypes.c_int, ctypes.c_uint,
                   ctypes.c_ubyte, ctypes.c_int, ctypes.c_void_p)(1, 2, GL_FLOAT, False, 16,
                                                                  ctypes.c_void_p(8))
                self._vao, self._vbo = vao, vbo
            finally:
                _gl_exit()
            keep = True
        finally:
            if not keep:
                _ctx_release()          # 创建失败，把配额还回去

    def _load_fns(self):
        gl("glGetError", ctypes.c_uint)
        gl("glFinish", None)
        gl("glViewport", None, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int)
        gl("glClearColor", None, ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float)
        gl("glClear", None, ctypes.c_uint)
        gl("glReadPixels", None, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
           ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p)
        gl("glDrawArrays", None, ctypes.c_uint, ctypes.c_int, ctypes.c_int)
        gl("glGenTextures", None, ctypes.c_int, ctypes.POINTER(ctypes.c_uint))
        gl("glDeleteTextures", None, ctypes.c_int, ctypes.POINTER(ctypes.c_uint))
        gl("glBindTexture", None, ctypes.c_uint, ctypes.c_uint)
        gl("glTexParameteri", None, ctypes.c_uint, ctypes.c_uint, ctypes.c_int)
        gl("glCompressedTexImage2D", None, ctypes.c_uint, ctypes.c_int, ctypes.c_uint,
           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p)
        gl("glCompressedTexSubImage2D", None, ctypes.c_uint, ctypes.c_int, ctypes.c_int,
           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_int,
           ctypes.c_void_p)
        gl("glTexImage2D", None, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_int,
           ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p)
        gl("glGenFramebuffers", None, ctypes.c_int, ctypes.POINTER(ctypes.c_uint))
        gl("glDeleteFramebuffers", None, ctypes.c_int, ctypes.POINTER(ctypes.c_uint))
        gl("glBindFramebuffer", None, ctypes.c_uint, ctypes.c_uint)
        gl("glFramebufferTexture2D", None, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
           ctypes.c_uint, ctypes.c_int)
        gl("glCheckFramebufferStatus", ctypes.c_uint, ctypes.c_uint)
        gl("glActiveTexture", None, ctypes.c_uint)
        gl("glUseProgram", None, ctypes.c_uint)
        gl("glUniform1i", None, ctypes.c_int, ctypes.c_int)
        gl("glGetUniformLocation", ctypes.c_int, ctypes.c_uint, ctypes.c_char_p)

    def _build_program(self):
        cs = gl("glCreateShader", ctypes.c_uint, ctypes.c_uint)
        ssrc = gl("glShaderSource", None, ctypes.c_uint, ctypes.c_int,
                  ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_int))
        cp = gl("glCompileShader", None, ctypes.c_uint)
        prog = []

        def sh(t, src):
            s = cs(t)
            a = ctypes.c_char_p(src)
            n = ctypes.c_int(len(src))
            ssrc(s, 1, ctypes.byref(a), ctypes.byref(n))
            cp(s)
            return s

        p = gl("glCreateProgram", ctypes.c_uint)()
        gl("glAttachShader", None, ctypes.c_uint, ctypes.c_uint)(p, sh(GL_VERTEX_SHADER, VS))
        gl("glAttachShader", None, ctypes.c_uint, ctypes.c_uint)(p, sh(GL_FRAGMENT_SHADER, FS))
        gl("glLinkProgram", None, ctypes.c_uint)(p)
        gl("glUseProgram", None, ctypes.c_uint)(p)
        loc = gl("glGetUniformLocation", ctypes.c_int, ctypes.c_uint, ctypes.c_char_p)
        gl("glUniform1i", None, ctypes.c_int, ctypes.c_int)(loc(p, b"uTex"), 0)
        self._u_swizzle = loc(p, b"uSwizzle")
        prog.append(p)
        return p

    # ---------------------------------------------------------------- 使用
    def make_current(self):
        if self._win is None or _LOCK_POISON[0]:
            return
        if self._owner_tid != threading.get_ident():
            if _gl_enter():
                try:
                    _lib().make_context_current(self._win)
                finally:
                    _gl_exit()
                self._owner_tid = threading.get_ident()
            # 拿不到锁就**不更新** _owner_tid —— 否则会误以为已绑定当前线程

    def _ensure_texture(self, w, h, fmt):
        key = (w, h, fmt)
        if self._tex is not None and self._tex_key == key:
            return
        if self._tex is not None:
            t = ctypes.c_uint(self._tex)
            gl("glDeleteTextures", None, ctypes.c_int, ctypes.POINTER(ctypes.c_uint))(1, ctypes.byref(t))
        t = ctypes.c_uint()
        gl("glGenTextures", None, ctypes.c_int, ctypes.POINTER(ctypes.c_uint))(1, ctypes.byref(t))
        gl("glBindTexture", None, ctypes.c_uint, ctypes.c_uint)(GL_TEXTURE_2D, t)
        for k, v in ((GL_TEXTURE_MIN_FILTER, GL_LINEAR), (GL_TEXTURE_MAG_FILTER, GL_LINEAR),
                     (GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE), (GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)):
            gl("glTexParameteri", None, ctypes.c_uint, ctypes.c_uint, ctypes.c_int)(GL_TEXTURE_2D, k, v)
        self._tex, self._tex_key = t.value, key

    def _ensure_fbo(self, w, h):
        if self._fbo is not None and self._fbo_size == (w, h):
            return True
        if self._fbo is not None:
            t = ctypes.c_uint(self._fbo_tex)
            gl("glDeleteTextures", None, ctypes.c_int, ctypes.POINTER(ctypes.c_uint))(1, ctypes.byref(t))
            f = ctypes.c_uint(self._fbo)
            gl("glDeleteFramebuffers", None, ctypes.c_int, ctypes.POINTER(ctypes.c_uint))(1, ctypes.byref(f))
        t = ctypes.c_uint()
        gl("glGenTextures", None, ctypes.c_int, ctypes.POINTER(ctypes.c_uint))(1, ctypes.byref(t))
        gl("glBindTexture", None, ctypes.c_uint, ctypes.c_uint)(GL_TEXTURE_2D, t)
        gl("glTexImage2D", None, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_int,
           ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p)(
            GL_TEXTURE_2D, 0, GL_RGBA8, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, None)
        gl("glTexParameteri", None, ctypes.c_uint, ctypes.c_uint, ctypes.c_int)(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        gl("glTexParameteri", None, ctypes.c_uint, ctypes.c_uint, ctypes.c_int)(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        fb = ctypes.c_uint()
        gl("glGenFramebuffers", None, ctypes.c_int, ctypes.POINTER(ctypes.c_uint))(1, ctypes.byref(fb))
        gl("glBindFramebuffer", None, ctypes.c_uint, ctypes.c_uint)(GL_FRAMEBUFFER, fb)
        gl("glFramebufferTexture2D", None, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
           ctypes.c_uint, ctypes.c_int)(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, t, 0)
        st = gl("glCheckFramebufferStatus", ctypes.c_uint, ctypes.c_uint)(GL_FRAMEBUFFER)
        self._fbo, self._fbo_tex, self._fbo_size = fb.value, t.value, (w, h)
        return st == GL_FRAMEBUFFER_COMPLETE

    def decode(self, blocks, src_w, src_h, fmt, dst_w, dst_h, bgra=False):
        """blocks: BC 块 bytes（长度 =(w/4)*(h/4)*(8|16)）

        bgra=True 时回读为 **BGRA 字节序** —— 引擎的 QImage 按 BGRA 解释
        （`QImage.Format_ARGB32` 小端序），交换在**着色器里**做（零成本），
        绝不能用 numpy 花式索引（1080p 要 1~2ms，正好把收益吃掉）。

        返回 (dst_h, dst_w, 4) uint8（自上而下）；失败返回 None。
        ⚠ 返回的是**内部复用缓冲**，调用方要留存必须先 copy。
        """
        if not self.ok or _LOCK_POISON[0]:
            return None
        # ★★ 所有 GL 调用必须**全局串行**（2026-09-26 死锁根因，见模块顶部 `_CREATE_LOCK` 的说明）。
        # 带超时：万一某个线程仍卡在驱动里持锁不放，这里只是**丢掉这一帧**（下一帧再试），
        # 绝不让所有解码线程跟着一起永久冻结。若已判定驱动挂死 ⇒ 就地转软解。
        if not _gl_enter():
            self._lock_timeouts = getattr(self, "_lock_timeouts", 0) + 1
            if _LOCK_POISON[0]:
                self.ok = False
                self.err = "GL 驱动已判定挂死（详见 ui_stall.log）—— 本素材转软解"
            return None
        try:
            return self._decode_locked(blocks, src_w, src_h, fmt, dst_w, dst_h, bgra)
        finally:
            _gl_exit()

    def _decode_locked(self, blocks, src_w, src_h, fmt, dst_w, dst_h, bgra):
        """真正的解码 —— **调用方必须已持有 `_CREATE_LOCK`**。"""
        try:
            import numpy as np
        except Exception:
            return None
        self.make_current()
        if not self._ensure_fbo(dst_w, dst_h):
            return None
        first = self._tex_key != (src_w, src_h, fmt)
        self._ensure_texture(src_w, src_h, fmt)
        internal = GL_COMPRESSED_RGB_S3TC_DXT1 if int(fmt) == 1 else GL_COMPRESSED_RGBA_S3TC_DXT5
        n = (src_w // 4) * (src_h // 4) * (8 if int(fmt) == 1 else 16)
        if len(blocks) < n:
            return None
        buf = (ctypes.c_char * n).from_buffer_copy(bytes(blocks[:n]))
        gl("glBindTexture", None, ctypes.c_uint, ctypes.c_uint)(GL_TEXTURE_2D, self._tex)
        if first:
            gl("glCompressedTexImage2D", None, ctypes.c_uint, ctypes.c_int, ctypes.c_uint,
               ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
               ctypes.c_void_p)(GL_TEXTURE_2D, 0, internal, src_w, src_h, 0, n,
                                ctypes.addressof(buf))
        else:
            gl("glCompressedTexSubImage2D", None, ctypes.c_uint, ctypes.c_int, ctypes.c_int,
               ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_int,
               ctypes.c_void_p)(GL_TEXTURE_2D, 0, 0, 0, src_w, src_h, internal, n,
                                ctypes.addressof(buf))
        # 画到 FBO（GPU 上完成缩放）
        gl("glBindFramebuffer", None, ctypes.c_uint, ctypes.c_uint)(GL_FRAMEBUFFER, self._fbo)
        gl("glViewport", None, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int)(0, 0, dst_w, dst_h)
        gl("glClearColor", None, ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float)(0.0, 0.0, 0.0, 0.0)
        gl("glClear", None, ctypes.c_uint)(GL_COLOR_BUFFER_BIT)
        gl("glActiveTexture", None, ctypes.c_uint)(GL_TEXTURE0)
        gl("glBindTexture", None, ctypes.c_uint, ctypes.c_uint)(GL_TEXTURE_2D, self._tex)
        sw = 1 if bgra else 0
        if self._swizzle != sw and self._u_swizzle >= 0:
            gl("glUniform1i", None, ctypes.c_int, ctypes.c_int)(self._u_swizzle, sw)
            self._swizzle = sw
        gl("glDrawArrays", None, ctypes.c_uint, ctypes.c_int, ctypes.c_int)(GL_TRIANGLE_STRIP, 0, 4)
        # 回读
        need = dst_w * dst_h * 4
        if self._buf_size != need:
            self._buf = np.empty((dst_h, dst_w, 4), dtype=np.uint8)
            self._buf_size = need
        # ⚠⚠ 回读是**同步**的：GPU 被别的程序占满（浏览器、游戏、Resolume、另一个播放器…）时，
        #    glReadPixels 会在这里**排队等**。实测空闲 1.17ms，忙时可以到几百 ms。
        #    而这里是**解码线程** —— 回读堵住 → 玩家不出帧 → 引擎等帧 → 整个软件像卡死。
        #    所以量一下耗时：连续多次明显变慢就判定"GPU 现在没空"，**退回软解**，
        #    不让现场跟着一起卡。（偶发一次不算，避免误伤；恢复后重启软件即可再用 GPU。）
        t_rb = time.perf_counter()
        gl("glReadPixels", None, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
           ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p)(0, 0, dst_w, dst_h, GL_RGBA,
                                                          GL_UNSIGNED_BYTE,
                                                          self._buf.ctypes.data)
        el = time.perf_counter() - t_rb
        self.last_readback = el
        if el > SLOW_READBACK_MS / 1000.0:
            self._slow_n += 1
            if self._slow_n >= SLOW_LIMIT:
                self.ok = False
                self.err = ("回读连续 %d 次过慢（最后一次 %.0f ms）—— GPU 正被其他程序"
                            "占用，已自动退回软件解码" % (self._slow_n, el * 1000))
                return None
        else:
            self._slow_n = 0
        return self._buf

    def close(self):
        # GL 调用：全局串行（同 decode 的理由）。拿不到锁就跳过删除 ——
        # 上下文配额由 destroy() 归还，漏删纹理只影响显存，不会拖住线程。
        if not _gl_enter():
            self._tex = self._fbo = None
            return
        try:
            if self._tex is not None:
                t = ctypes.c_uint(self._tex)
                gl("glDeleteTextures", None, ctypes.c_int, ctypes.POINTER(ctypes.c_uint))(1, ctypes.byref(t))
            if self._fbo is not None:
                f = ctypes.c_uint(self._fbo)
                gl("glDeleteFramebuffers", None, ctypes.c_int, ctypes.POINTER(ctypes.c_uint))(1, ctypes.byref(f))
        except Exception:
            pass
        finally:
            _gl_exit()
        self._tex = self._fbo = None
        # 注意：这里**不销毁窗口**（窗口要用 destroy()）。只调 close() 会漏上下文。

    def destroy(self):
        """彻底销毁 GL 上下文与窗口。**必须由创建它的线程调用**（glfw/WGL 的线程亲和）。

        这是 2026-09-26「窗口卡死」事件的核心修复。原因：
        `close()` 只删纹理和 FBO，glfw 窗口与它背后的 GL 上下文会一直留到进程退出，
        而**每个 GL 上下文会给进程增加约 21 个驱动线程**（NVIDIA nvoglv64/nvwgf2umx 的
        线程组，实测：10 个上下文泄漏 207 个线程，且 close() 后一个都不归还）。
        解码器是 LRU 缓存淘汰的，每切一次素材就可能新建一个上下文 ⇒ 跑一小时累积上百个
        ⇒ 线程数千、提交内存 GB 级 ⇒ 主线程抢不到 GIL ⇒ **窗口未响应**。
        修复前现场实测：**线程 2386 / 句柄 15442 / 提交内存 8.0 GB**（正常稳态 310~330 线程）。

        返回 True = 已销毁并归还配额；False = 没销毁（原因见 self.err）。
        """
        if self._win is None:
            return False
        if self._owner_tid is not None and self._owner_tid != threading.get_ident():
            # 宁可漏这一个（有 MAX_CTX 兜底），也不冒险跨线程销毁驱动资源
            self.err = ("destroy() 不在拥有上下文的线程（创建线程 %s / 当前 %s），已跳过"
                        % (self._owner_tid, threading.get_ident()))
            return False
        g = _lib()
        win = self._win
        # ★ 等锁**绝不无限等**（第二轮死锁修复第 ② 点）：拿不到（或驱动已判定挂死）
        #   就**放弃销毁**。宁可漏这一个上下文（有 MAX_CTX 兜底），也绝不跟着驱动一起冻结。
        #   —— 原来这里是 `with _CREATE_LOCK:`（无限等），现场 3 个解码线程就是这样陪葬的。
        if not _gl_enter(GL_LOCK_TIMEOUT):
            self.err = "GL 锁不可用（驱动疑似挂死），已跳过窗口销毁"
            return False                 # 不归还配额：避免驱动线程无限制泄漏（见 _ctx_reserve）
        self._win = None                 # 已进入销毁流程，失败也不会重复销毁
        try:
            if g:
                try:
                    g.make_context_current(None)     # 先解绑当前上下文
                except Exception:
                    pass
                try:
                    g.destroy_window(win)
                except Exception:
                    pass
        finally:
            _gl_exit()
            _ctx_release()
        return True


# ============================================================================
# 单 GL 工作线程（GLWorker）—— 2026-09-26「驱动挂死」的架构级修复
# ============================================================================
# 根因回顾：`glfwCreateWindow` / `glfwDestroyWindow` 在 NVIDIA 驱动上**偶发永久挂死**，
# 而且是"独发"的（不需要和别的线程并发就能发生）。旧设计里 `GLDecoder` 是**每个
# GpuDxvPlayer 一个**，而 `GpuDxvPlayer` 是 LRU 缓存（每图层 8 个）⇒ **每次 LRU 淘汰 /
# 图层切换都会 `glfwDestroyWindow` 一次**。调用次数越多，撞上驱动 bug 的概率越高；
# 上一版的「全局串行锁 + 全部带超时 + 5 秒污染自愈」**挡不住**它 —— 挂死发生在驱动内部，
# 持锁线程永不返回，其他解码线程只能陪葬或每帧白等。
#
# 架构修复：**整个进程只创建 1 个 GL 上下文，并永不销毁。**
#   - 唯一上下文由一个**专属工作线程**持有（glfw 允许在非主线程建窗口）；
#   - 所有解码通过「请求队列 + 应答」提交，工作线程**天然串行**，无需任何全局锁；
#   - 请求带超时：工作线程一旦卡住 ⇒ 请求方超时返回 `None` 并进入 `degraded`（临时降级），
#     冷却期内**所有请求零等待立即返回 `None`**（全体走软解、画面继续）；冷却 `RECOVER_AFTER`
#     秒后自动放开一次探测，成功即恢复 GPU —— 既不"永久掉加速"，也不"每帧卡 1.5s"。
#   - 运行期**不再出现任何 `glfwCreateWindow` / `glfwDestroyWindow` 调用**
#     （只在进程内首次 `ensure()` 时 create 一次，之后永不 destroy）。
# `MAX_CTX` / `_ctx_reserve` / `_gl_enter` / `_gl_exit` / `_LOCK_POISON` 这套兜底**保留**，
# 方案落地后基本不会被触及，作为最后一道保险。


class _GLReq:
    """一次解码请求（投给 GL 工作线程，等它把 result 填好并 set event）。"""
    __slots__ = ("blocks", "src_w", "src_h", "fmt", "dst_w", "dst_h", "bgra",
                 "result", "cancelled", "event")

    def __init__(self, blocks, src_w, src_h, fmt, dst_w, dst_h, bgra):
        self.blocks = blocks
        self.src_w = int(src_w)
        self.src_h = int(src_h)
        self.fmt = int(fmt)
        self.dst_w = int(dst_w)
        self.dst_h = int(dst_h)
        self.bgra = bool(bgra)
        self.result = None; self.cancelled = False    # cancelled：被超时清理唤醒（=失败，非探测成功）
        self.event = threading.Event()


def _log_line(msg):
    """尽力写诊断日志（stallwatch 不在时静默）。"""
    try:
        import stallwatch
        stallwatch.log_line(msg)
    except Exception:                                            # noqa: BLE001
        pass


class GLWorker:
    """唯一 GL 上下文的持有者（单例 + 单线程）。见本段顶部说明。

    对外只用两个方法：
      - `GLWorker.get().ensure()` → 确保工作线程 + 唯一上下文已就绪（True=可用）；
      - `GLWorker.get().decode(...)` → 派一次解码，带超时（降级冷却期内零等待返回 `None`）。
    绝不要在这里调用 `GLDecoder.destroy()`：上下文属于整个进程，由 OS 在退出时回收。
    """

    _inst = None
    _inst_lock = threading.Lock()

    # 冷启动同步等待上限（秒）：**只有"首次启动工作线程"的那一次调用**会同步等这么久。
    # 之后所有调用一律**非阻塞探测**（只读一次状态、立刻返回），绝不重复长等主线程。
    # 为什么给到 12s：冷启动要建 glfw 窗口 + WGL 上下文（会在驱动里拉起 ~21 个线程）
    # + 编译/链接着色器 + 建 VAO/VBO，这段 GL 工作**没有任何超时约束**，在冷机 / 驱动
    # 初始化慢 / 系统繁忙时实测可以数秒；旧的 3s 太紧（现场命中 3 次）。
    GL_READY_TIMEOUT = 12.0
    # 若首次调用恰好发生在**主线程(GUI)**：只等这么短 —— 宁可先软解，也绝不冻界面；
    # 非阻塞探测会在工作线程稍后就绪时自动把它接上（`available()` 变 True）。
    GL_READY_TIMEOUT_UI = 3.0

    def __init__(self):
        self._cv = threading.Condition()      # 请求队列条件变量
        self._req = collections.deque()       # 待处理请求
        self._init_lock = threading.Lock()    # 保护启动
        self._ready_evt = threading.Event()   # 唯一上下文就绪
        self._started = False
        self._dead = False                    # ★ 只有工作线程**崩溃**（不可恢复）才会置真
        self._thread = None
        self._dec = None                      # 唯一的 GLDecoder（永不销毁）
        self.last_error = ""
        self.decode_timeout = 1.5             # 单次请求等待上限（秒）
        self.n_ok = 0                         # 诊断：成功解码次数
        self.n_timeout = 0                    # 诊断：超时次数
        # ★ 冷启动等待策略状态（2026-10-02 现场：启动超时被永久置死 `_dead`，整场退回软解）
        self._start_t = 0.0                   # 工作线程启动时刻（用于诊断"启动多久了"）
        self._blocked_once = False            # 是否已做过"首次同步等待"（只有启动者做一次）
        self._last_slow_log = 0.0             # "冷启动慢"诊断日志限频时刻

    @classmethod
    def get(cls):
        with cls._inst_lock:
            if cls._inst is None:
                cls._inst = GLWorker()
            return cls._inst

    # ---------------------------------------------------------------- 生命周期
    def ensure(self, ready_timeout=None):
        """确保工作线程已启动、唯一 GL 上下文已就绪。True=可用。幂等、线程安全。

        glfw 的 `glfwInit` 在主线程（现状：ui_main 启动时已调 `preinit()`）；这里再调一次
        `preinit()` 只是**幂等 no-op**。真正建窗口发生在工作线程内（glfw 允许）。

        ★ 等待策略（2026-10-02 现场修复：「启动超时 ⇒ 永久 `_dead` ⇒ 整场软解」）：
          · **只有"首次启动工作线程"的那一次调用**会**同步等待**冷启动（最多
            `GL_READY_TIMEOUT` 秒；若这次调用发生在主线程则收紧到 `GL_READY_TIMEOUT_UI`，
            绝不冻界面）。这一次是必须的：冷启动的 GL 工作是异步的，起跑瞬间几乎必然没就绪。
          · **之后所有调用一律非阻塞探测**：只读一次 `_ready_evt`、立即返回。
            ⇒ 上层（解码线程 / GUI）**绝不可能被反复同步等十几秒**。
          · **超时绝不置 `_dead`**：`_dead` 只由工作线程**崩溃**（`_run` 异常 / 上下文 `ok=False`）
            置真。启动慢 ≠ 不可恢复 —— 工作线程稍后就绪后，非阻塞探测会立刻返回 True，
            `available()` 变 True，解码线程的周期重探（`_soft_until_recover`）会自动切回 GPU。
        """
        to = self.GL_READY_TIMEOUT if ready_timeout is None else float(ready_timeout)
        with self._init_lock:
            if self._ready_evt.is_set():
                return not self._dead
            if self._dead:                    # 只可能是工作线程崩溃（不可恢复）
                return False
            launcher = not self._started
            if launcher:
                preinit()                     # 幂等；通常主线程已 init 过
                self._started = True
                self._start_t = time.perf_counter()
                self._thread = threading.Thread(target=self._run, name="gl-worker",
                                                daemon=True)
                self._thread.start()
                self._blocked_once = True     # ★ 只有启动者会做这一次同步等待
        if not launcher:
            # ★ 非阻塞探测：只读一次状态、立刻返回（绝不重复同步等待）
            return self._ready_evt.is_set() and not self._dead
        # ---- 启动者：同步等一次冷启动 ----
        if threading.current_thread() is threading.main_thread():
            to = min(to, self.GL_READY_TIMEOUT_UI)   # 主线程：宁先软解也不冻界面
        if self._ready_evt.wait(to):
            return not self._dead
        # ★ 超时**绝不**置 `_dead`：可能只是冷启动慢（驱动初始化 / 系统繁忙），稍后即就绪。
        self._note_slow_start(to)
        return False

    def _note_slow_start(self, to):
        """冷启动超时只记一条**限频诊断**（不置死、不改 `last_error`，避免污染可用性判定）。"""
        now = time.perf_counter()
        if now - self._last_slow_log < 5.0:
            return
        self._last_slow_log = now
        _log_line("GLWorker 冷启动超过 %.0fs 仍未就绪 —— **不置死**，"
                  "稍后由非阻塞探测自动接入（期间该素材先软解，画面不停）" % to)

    def available(self):
        """唯一上下文**就绪、未挂死、未处于降级冷却中、且解码器本身仍健康**。

        `GLDecoder.ok` 若因回读连续过慢变 False（见 `_decode_locked`）也必须为 False，
        否则上层会一直 demux 却拿不到帧（画面冻住）；降级冷却期内同理返回 False（走软解）。
        """
        if self._dead or not self._ready_evt.is_set():
            return False
        dec = self._dec
        return (dec is not None and bool(getattr(dec, "ok", False))
                and not (self._degraded and time.perf_counter() < self._degrade_until))

    def last_message(self):
        """最近一次失败/降级原因（诊断与上层 `.reason` 用）。"""
        if self.last_error:
            return self.last_error
        dec = self._dec
        if dec is not None and not getattr(dec, "ok", False):
            return getattr(dec, "err", "") or "GL 上下文失效"
        if self._started and not self._dead and not self._ready_evt.is_set():
            # ★ 冷启动中（未就绪但**未**判死）：给上层一个可读原因，别显示成"未知停用"
            return "GL 工作线程冷启动中（尚未就绪，稍后自动接入）"
        return ""

    def stats(self):
        return {"ready": self._ready_evt.is_set(), "dead": self._dead,
                "started": self._started,
                "since_start": (time.perf_counter() - self._start_t) if self._start_t else None,
                "n_ok": self.n_ok, "n_timeout": self.n_timeout,
                "err": self.last_error}

    def _run(self):
        """工作线程主体：建唯一上下文 → 循环处理请求。永不返回 `destroy()`。

        ★ 不变式（2026-10-02 收敛）：**只要本线程还活着，`_dead` 就必须保持 False。**
        `_dead` 只表示"工作线程真的退出 / 彻底没救了"。因此：
          · **抢锁超时**（`GLLockUnavailable`）：这只是"此刻锁被别的线程占用"，**不是崩溃**
            —— 当作"尚未就绪"，睡 `_READY_RETRY_SECS` 后**重试**，绝不置 `_dead`；
            锁一放开即建成（可恢复）。若期间驱动被判定挂死（`gl_poisoned()`），
            则按"不可恢复"退出置 `_dead`（与全局污染策略一致）。
          · **真正的创建失败**（`glfwCreateWindow` / 上下文 / 着色器 / `preinit` 抛异常）：
            保持原样置 `_dead`。
        """
        while True:
            try:
                preinit()
                dec = GLDecoder()
            except Exception as e:                              # noqa: BLE001
                self.last_error = "创建 GL 上下文异常：%s" % str(e)[:150]
                self._dead = True
                self._ready_evt.set()
                return
            if dec.ok:
                break
            if getattr(dec, "lock_timeout", False) and not gl_poisoned():
                # ★ 只是没抢到锁 ⇒ "尚未就绪"，稍后重试；**绝不置死**（保持不变式）
                self.last_error = dec.err or "GL 锁暂不可用（未就绪，稍后重试）"
                time.sleep(_READY_RETRY_SECS)
                continue
            # 驱动已判定挂死，或真正的上下文创建失败 ⇒ 不可恢复
            self.last_error = dec.err or "GL 上下文不可用"
            self._dead = True
            self._ready_evt.set()
            return
        self._dec = dec
        self._ready_evt.set()
        while True:
            with self._cv:
                while not self._req and not self._dead:
                    self._cv.wait()
                if self._dead:                 # 已被判挂死：清空队列并退出
                    for r in self._req:
                        r.cancelled = True
                        r.result = None
                        r.event.set()
                    self._req.clear()
                    return
                req = self._req.popleft()
            try:
                arr = dec.decode(req.blocks, req.src_w, req.src_h, req.fmt,
                                 req.dst_w, req.dst_h, req.bgra)
                # 结果 copy 一份交回请求方（多一次 ~8MB ≈1ms 拷贝，换简单安全）
                req.result = arr.copy() if arr is not None else None
            except Exception as e:                              # noqa: BLE001
                req.result = None
                self.last_error = "解码异常：%s" % str(e)[:150]
            req.event.set()

    # ---- R2 降级冷却 + 自恢复状态（类级默认值；实例一旦赋值即成为实例属性）----
    # 目的：单帧偶发超时（系统卡顿 / 大 GC / 驱动抖动）**不该永久失去 GPU 加速**。
    #   · 请求超时 ⇒ `_degraded=True` 且 `_degrade_until=now+RECOVER_AFTER`：冷却期内
    #     `available()` 返回 False、`decode()` **零等待**返回 None（上层正常走软解）。
    #   · 冷却过后放开**一次真实探测**（就是下一次 `decode()` 提交），成功即清除降级
    #     （自动恢复 GPU）、写日志；再超时则重新进入冷却。**绝不"每帧重试卡 1.5s"**。
    #   · worker 线程崩溃 / 上下文 `ok=False` 这类**不可恢复**的情况仍用 `_dead`（永久停用）。
    RECOVER_AFTER = 60.0        # 降级冷却秒数（可被测试临时调小）
    _degraded = False           # 是否处于"临时降级"
    _degrade_until = 0.0        # 冷却截止时刻（perf_counter）

    # ---------------------------------------------------------------- 请求
    def decode(self, blocks, src_w, src_h, fmt, dst_w, dst_h, bgra=True, timeout=None):
        """把一次 GPU 解码派给工作线程，带超时。返回 (dst_h,dst_w,4) uint8 或 `None`。

        R2 降级/自恢复（详见类属性处说明）：
        - 单次请求超时 ⇒ 进入 `degraded`，之后**零等待**立即返回 None（上层切软解）；
          冷却 `RECOVER_AFTER` 秒后放开**一次真实探测**：成功 ⇒ 自动恢复 GPU（写日志）；
          再超时 ⇒ 重新进入冷却。
        - 探测仍受 `to` 超时约束，且**只阻塞发起方这一个解码线程**（最多一次 timeout），
          绝不影响主线程/其他线程。
        - **并发安全**：状态判定（是否授权探测）+ 入队、以及置/清 `_degraded` 全部在
          `self._cv` 临界区内串行完成 ⇒ **同一时刻只有一个线程被授权探测**，杜绝多线程
          同时撞上冷却结束点时的丢失更新（曾被误判为“探测成功”而清掉降级）。
        """
        if self._dead or not self._ready_evt.is_set():
            return None
        dec = self._dec
        if dec is not None and not getattr(dec, "ok", False):
            return None               # 解码器已自降级（如回读连续过慢）→ 立即失败，上层转软解
        to = self.decode_timeout if timeout is None else float(timeout)
        req = _GLReq(blocks, src_w, src_h, fmt, dst_w, dst_h, bgra)
        # ---- 临界区：状态判定 + 入队，一次原子完成（复用请求条件变量的锁）----
        with self._cv:
            if self._dead or not self._ready_evt.is_set():
                return None
            now = time.perf_counter()
            if self._degraded and now < self._degrade_until:
                return None           # 冷却中：零等待，绝不每帧等 1.5s
            was_degraded = self._degraded
            if was_degraded:          # 冷却已过 → 本次即“唯一探测”：立刻重新武装冷却，
                self._degrade_until = now + float(self.RECOVER_AFTER)   # 其他线程随即零等待
            self._req.append(req)
            self._cv.notify()
        if req.event.wait(to):
            if req.cancelled or req.result is None:
                # 被超时线程清理唤醒（cancelled）或 worker 判定失败（result=None）→ **失败**，
                # 绝不是“探测成功”，绝不据此清除降级（这是 R2 并发竞态的根因修复）。
                return None
            self.n_ok += 1
            if was_degraded:          # 真实探测成功 → 自动恢复 GPU 解码
                with self._cv:
                    self._degraded = False
                    self._degrade_until = 0.0
                    self.last_error = ""
                _log_line("GL 解码已自动恢复（降级冷却后的探测请求成功，恢复 GPU 加速）")
            return req.result
        # ---- 超时：进入/维持降级，冷却 RECOVER_AFTER 秒后再允许一次探测 ----
        with self._cv:
            self.n_timeout += 1
            self._degraded = True
            self._degrade_until = time.perf_counter() + float(self.RECOVER_AFTER)
            self.last_error = ("GL 解码超时（>%.1fs，疑似驱动抖动/挂死）——已临时停用 GPU 解码并转软解；"
                               "%.0f 秒后自动重试" % (to, float(self.RECOVER_AFTER)))
            for r in self._req:            # 唤醒其他等待者（标记 cancelled，避免误判成功）
                r.cancelled = True
                r.result = None
                r.event.set()
            self._req.clear()
        _log_line("!! GLWorker 解码超时（>%.1fs）——已临时停用 GPU 解码转软解，"
                  "%.0f 秒后自动重试（画面继续；探测成功会自动恢复 GPU）"
                  % (to, float(self.RECOVER_AFTER)))
        return None
