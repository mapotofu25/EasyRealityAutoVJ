# -*- coding: utf-8 -*-
"""「输出交给 GPU」的可行性基准：CPU 画布 → GL 纹理 / Spout 各条路的单帧成本。

背景：`tools/_output_bench.py` 已量出 `Spout sendImage` 1080p ≈ 2.55 ms CPU/帧。
SpoutGL 另有 `sendTexture(texID, ...)`（零拷贝，直接把 GPU 纹理交给 Spout 的
GL/DX 共享纹理）—— 但它**要求画面已经是 GPU 纹理**，而我们的画布是 CPU 上的
QImage。所以真正要回答的是：

    把 1080p 画布「上传进 GPU 纹理」要花多少 CPU？这个数字决定了
    「合成留在 CPU + 输出走 GPU」这条路值不值。

再顺带量两条对照：
  · GL 回读（glReadPixels）—— GPU 化之后往回拿的成本（NDI 必须用）
  · Spout sendImage 现状 —— 对照

⚠ `time.process_time()` 只在时钟中断（15.625ms）时更新 → 必须多帧累加后再除。

用法：venv\\Scripts\\python.exe tools\\_gpu_upload_bench.py [帧数=200]
"""
import ctypes
import sys
import time

import numpy as np

sys.path.insert(0, "src")
import glctx                                                      # noqa: E402

W, H = 1920, 1080
GL_TEXTURE_2D = glctx.GL_TEXTURE_2D
GL_RGBA8 = glctx.GL_RGBA8
GL_RGBA = glctx.GL_RGBA
GL_UNSIGNED_BYTE = glctx.GL_UNSIGNED_BYTE
GL_PIXEL_UNPACK_BUFFER = 0x88EC
GL_PIXEL_PACK_BUFFER = 0x88EB
GL_STREAM_DRAW = 0x88E0


def proc_cpu():
    """进程级 CPU（100ns 单位）—— 单帧太小，必须累加"""
    import ctypes
    from ctypes import wintypes as wt

    class FT(ctypes.Structure):
        _fields_ = [("lo", wt.DWORD), ("hi", wt.DWORD)]

    k = ctypes.windll.kernel32
    # ⚠ 不设 restype 会被当作 C int 截断成 32 位 → 传给 GetProcessTimes 报 OverflowError
    k.GetCurrentProcess.restype = wt.HANDLE
    k.GetProcessTimes.argtypes = [wt.HANDLE, ctypes.POINTER(FT), ctypes.POINTER(FT),
                                  ctypes.POINTER(FT), ctypes.POINTER(FT)]
    c, e, kt, u = FT(), FT(), FT(), FT()
    k.GetProcessTimes(k.GetCurrentProcess(), ctypes.byref(c), ctypes.byref(e),
                      ctypes.byref(kt), ctypes.byref(u))
    return ((kt.hi << 32) | kt.lo) + ((u.hi << 32) | u.lo)


def main(n=200):
    log = []

    def w(s):
        print(s, flush=True)
        log.append(s)

    w("=" * 84)
    w("「输出交给 GPU」可行性基准   1080p   %s" % time.strftime("%H:%M:%S"))
    w("=" * 84)

    glfw = glctx._lib()
    w("glfw 可用：%s" % bool(glfw))
    if not glfw:
        return
    glfw.window_hint(glfw.VISIBLE, glfw.FALSE)
    glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)      # ★ 与 GLDecoder 一致
    glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
    glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
    win = glfw.create_window(32, 32, "gpu-bench", None, None)
    if not win:
        glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_ANY_PROFILE)
        win = glfw.create_window(32, 32, "gpu-bench", None, None)
    if not win:
        w("★ 建不出 GL 窗口")
        return
    glfw.make_context_current(win)
    w("GL 上下文已建立")

    gl = glctx.gl
    arr = np.zeros((H, W, 4), np.uint8)
    arr[:, :, 2] = 200
    flat = arr.reshape(-1)
    nbytes = arr.nbytes

    # ---------------- 1) 上传：CPU → GPU 纹理
    tex = ctypes.c_uint()
    gl("glGenTextures", None, ctypes.c_int, ctypes.POINTER(ctypes.c_uint))(1, ctypes.byref(tex))
    gl("glBindTexture", None, ctypes.c_uint, ctypes.c_uint)(GL_TEXTURE_2D, tex.value)
    gl("glTexParameteri", None, ctypes.c_uint, ctypes.c_uint, ctypes.c_int)(
        GL_TEXTURE_2D, glctx.GL_TEXTURE_MIN_FILTER, glctx.GL_LINEAR)
    gl("glTexParameteri", None, ctypes.c_uint, ctypes.c_uint, ctypes.c_int)(
        GL_TEXTURE_2D, glctx.GL_TEXTURE_MAG_FILTER, glctx.GL_LINEAR)
    gl("glTexImage2D", None, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_int,
       ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p)(
        GL_TEXTURE_2D, 0, GL_RGBA8, W, H, 0, GL_RGBA, GL_UNSIGNED_BYTE, None)

    sub = gl("glTexSubImage2D", None, ctypes.c_uint, ctypes.c_int, ctypes.c_int,
             ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint,
             ctypes.c_void_p)

    def upload_plain():
        sub(GL_TEXTURE_2D, 0, 0, 0, W, H, GL_RGBA, GL_UNSIGNED_BYTE,
            ctypes.c_void_p(arr.ctypes.data))

    for _ in range(20):
        upload_plain()
    gl("glFinish", None)()
    c0, t0 = proc_cpu(), time.perf_counter()
    for _ in range(n):
        upload_plain()
    gl("glFinish", None)()
    wall = time.perf_counter() - t0
    cpu = (proc_cpu() - c0) / 1e7
    w("")
    w("[1] 上传 CPU→GPU 纹理（glTexSubImage2D，同步）")
    w("    %.3f ms/帧 墙钟 / %.3f ms/帧 CPU   → 60fps 核当量 %.3f"
      % (wall / n * 1000, cpu / n * 1000, cpu / n * 60))

    # ---------------- 2) 上传：PBO 异步版本
    pbo = ctypes.c_uint()
    gl("glGenBuffers", None, ctypes.c_int, ctypes.POINTER(ctypes.c_uint))(1, ctypes.byref(pbo))
    gl("glBindBuffer", None, ctypes.c_uint, ctypes.c_uint)(GL_PIXEL_UNPACK_BUFFER, pbo.value)
    gl("glBufferData", None, ctypes.c_uint, ctypes.c_ssize_t, ctypes.c_void_p,
       ctypes.c_uint)(GL_PIXEL_UNPACK_BUFFER, nbytes, None, GL_STREAM_DRAW)

    def upload_pbo():
        gl("glBindBuffer", None, ctypes.c_uint, ctypes.c_uint)(GL_PIXEL_UNPACK_BUFFER, pbo.value)
        ptr = gl("glMapBufferRange", ctypes.c_void_p, ctypes.c_uint, ctypes.c_ssize_t,
                 ctypes.c_ssize_t, ctypes.c_uint)(GL_PIXEL_UNPACK_BUFFER, 0, nbytes, 0x0002)
        if ptr:
            ctypes.memmove(ptr, arr.ctypes.data, nbytes)
            gl("glUnmapBuffer", ctypes.c_ubyte, ctypes.c_uint)(GL_PIXEL_UNPACK_BUFFER)
        sub(GL_TEXTURE_2D, 0, 0, 0, W, H, GL_RGBA, GL_UNSIGNED_BYTE, None)
        gl("glBindBuffer", None, ctypes.c_uint, ctypes.c_uint)(GL_PIXEL_UNPACK_BUFFER, 0)

    ok = True
    try:
        for _ in range(10):
            upload_pbo()
        gl("glFinish", None)()
        c0, t0 = proc_cpu(), time.perf_counter()
        for _ in range(n):
            upload_pbo()
        gl("glFinish", None)()
        wall = time.perf_counter() - t0
        cpu = (proc_cpu() - c0) / 1e7
        w("")
        w("[2] 上传 CPU→GPU（PBO 中转）")
        w("    %.3f ms/帧 墙钟 / %.3f ms/帧 CPU   → 60fps 核当量 %.3f"
          % (wall / n * 1000, cpu / n * 1000, cpu / n * 60))
    except Exception as e:                                        # noqa: BLE001
        ok = False
        w("")
        w("[2] PBO 路径失败：%s" % str(e)[:120])
    gl("glBindBuffer", None, ctypes.c_uint, ctypes.c_uint)(GL_PIXEL_UNPACK_BUFFER, 0)

    # ---------------- 3) 回读 GPU → CPU（NDI 必须付）
    # ⚠ 必须把纹理挂到 FBO 上再读：从没渲染过的默认 framebuffer 读会被驱动短路
    #   （会量出 0.05ms 这种比 PCIe 带宽还快的假数字）。
    fbo = ctypes.c_uint()
    gl("glGenFramebuffers", None, ctypes.c_int, ctypes.POINTER(ctypes.c_uint))(1, ctypes.byref(fbo))
    gl("glBindFramebuffer", None, ctypes.c_uint, ctypes.c_uint)(0x8D40, fbo.value)
    gl("glFramebufferTexture2D", None, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
       ctypes.c_uint, ctypes.c_int)(0x8D40, 0x8CE0, GL_TEXTURE_2D, tex.value, 0)

    out = np.empty((H, W, 4), np.uint8)
    rp = gl("glReadPixels", None, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p)

    def readback_sync():
        rp(0, 0, W, H, GL_RGBA, GL_UNSIGNED_BYTE, ctypes.c_void_p(out.ctypes.data))

    for _ in range(20):
        readback_sync()
    gl("glFinish", None)()
    c0, t0 = proc_cpu(), time.perf_counter()
    for _ in range(n):
        readback_sync()
    gl("glFinish", None)()
    wall = time.perf_counter() - t0
    cpu = (proc_cpu() - c0) / 1e7
    w("")
    w("[3] 回读 GPU→CPU（glReadPixels 同步）—— 走 NDI 时必须付")
    w("    %.3f ms/帧 墙钟 / %.3f ms/帧 CPU   → 60fps 核当量 %.3f"
      % (wall / n * 1000, cpu / n * 1000, cpu / n * 60))

    # ---------------- 4) 对照：纯内存拷贝 + Spout sendImage
    dst = np.empty((H, W, 4), np.uint8)
    for _ in range(20):
        np.copyto(dst, arr)
    c0, t0 = proc_cpu(), time.perf_counter()
    for _ in range(n):
        np.copyto(dst, arr)
    wall = time.perf_counter() - t0
    cpu = (proc_cpu() - c0) / 1e7
    w("")
    w("[4] 对照：整幅 memcpy %.1f MB" % (nbytes / 1048576))
    w("    %.3f ms/帧 墙钟 / %.3f ms/帧 CPU   → 60fps 核当量 %.3f"
      % (wall / n * 1000, cpu / n * 1000, cpu / n * 60))

    try:
        from SpoutGL import SpoutSender
        from SpoutGL.enums import GL_BGRA_EXT
        snd = SpoutSender()
        snd.setSenderName("AutoVJ-gpu-bench")
        snd.createOpenGL()
        mv = memoryview(arr).cast("B")
        # 签名: sendImage(buffer, height, width, glFormat, bInvert, invertAxis)
        send1 = lambda: snd.sendImage(mv, H, W, GL_BGRA_EXT, True, 0)
        for _ in range(20):
            send1()
        time.sleep(0.3)
        c0, t0 = proc_cpu(), time.perf_counter()
        for _ in range(n):
            send1()
        wall = time.perf_counter() - t0
        cpu = (proc_cpu() - c0) / 1e7
        w("")
        w("[5] 对照：Spout sendImage（现状，CPU 共享内存路径）")
        w("    %.3f ms/帧 墙钟 / %.3f ms/帧 CPU   → 60fps 核当量 %.3f"
          % (wall / n * 1000, cpu / n * 1000, cpu / n * 60))
        try:
            snd.releaseSender()
        except Exception:                                       # noqa: BLE001
            pass
    except Exception as e:                                        # noqa: BLE001
        w("")
        w("[5] Spout 不可用：%s" % str(e)[:200])

    w("")
    w("读法：")
    w("  · [1]/[2] 是「合成留在 CPU、输出走 GPU」这条路每帧必须付的上传钱；")
    w("  · 若 [1] + short sendTexture 仍 < [5]，说明只换输出就能省一部分；")
    w("  · 但 [3] 说明只要接收端（尤其 NDI）只吃 CPU 帧，回读就免不掉 ——")
    w("    所以真正能省到底的只有「整条链（合成+颜色+后处理）都留在 GPU」。")

    glfw.destroy_window(win)
    with open("_gpu_upload_bench.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(log))
    print("\n→ 已写入 _gpu_upload_bench.txt")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 200)
