# -*- coding: utf-8 -*-
"""NDI 音频发送路径自测（2026-10-01）。

用法（**必须用 -u，脚本必须是文件**）：
    P:/AutoVJ/venv/Scripts/python.exe -u P:/AutoVJ/tools/_test_ndi_audio2.py all

`all` 是父进程，它把每个子用例都放进**独立子进程**里跑 —— 因为「没有接收端时
调 `Sender.send_audio()` 会段错误」是 native crash，Python 的 try/except 抓不住，
把危险调用放在子进程里才不会把整个自测搞崩。

子模式（一般不用手跑，`all` 会自动调）：
    safe   仅构造 + 内省（不调用任何 send_*），验证 AudioSendFrame 的
            shape/dtype/num_channels/sample_rate 语义，以及 **open() 之后再改
           sample_rate 是否生效 / 是否报错**。
    gate   用真实的 ndi_out.NDIOutput 走一遍 _ensure()/feed_audio()，
            在**没有接收端**时验证「连接数门控」这条早退路径（此路径不碰 native）。
    danger 同上，但**强行**走到 send_audio()（无接收端）→ 观察是否段错误。
    loop   同机回环：真实 Sender + Finder + Receiver，发视频+音频，看能不能收到
            音频帧、收到的 sample_rate 是多少。若被沙箱挡组播则连不上 —— 会明确
            报告「UNREACHABLE」，不算失败。
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

PY = sys.executable
THIS = os.path.abspath(__file__)


def log(*a):
    print(*a)
    sys.stdout.flush()


# ---------------------------------------------------------------- safe
def mode_safe():
    import numpy as np
    from cyndilib.sender import Sender
    from cyndilib.video_frame import VideoSendFrame
    from cyndilib.audio_frame import AudioSendFrame

    log("[safe] 构造 Sender + VideoSendFrame + AudioSendFrame（完全照抄 ndi_out._ensure）")
    s = Sender("AutoVJ-Selftest-Safe")
    v = VideoSendFrame()
    v.set_resolution(640, 360)
    v.set_fourcc(0x41524742)          # 'BGRA'
    v.set_frame_rate(60)
    v.set_progressive(True)
    s.set_video_frame(v)

    a = AudioSendFrame()
    a.sample_rate = 48000             # open() 之前设
    a.num_channels = 2
    a.set_max_num_samples(4800)
    log("[safe] open() 之前: sample_rate=%r num_channels=%r num_samples=%r max_num_samples=%r"
        % (a.sample_rate, a.num_channels, a.num_samples, a.max_num_samples))
    log("[safe] open() 之前: shape=%r strides=%r attached_to_sender=%r"
        % (a.shape, a.strides, a.attached_to_sender))

    s.set_audio_frame(a)
    log("[safe] set_audio_frame 之后: shape=%r strides=%r" % (a.shape, a.strides))
    s.open()
    log("[safe] open() 完成")
    log("[safe] open() 之后: sample_rate=%r num_channels=%r num_samples=%r shape=%r strides=%r"
        % (a.sample_rate, a.num_channels, a.num_samples, a.shape, a.strides))
    log("[safe] open() 之后: num_samples=%r channel_stride(property)=%r"
        % (a.num_samples, a.channel_stride))

    # ★ 关键实验：open() 之后再改 sample_rate —— 会不会报错？会不会生效？
    try:
        a.sample_rate = 44100
        log("[safe] ★ open() 后再设 a.sample_rate=44100: 没有抛异常；读回 a.sample_rate=%r"
            % (a.sample_rate,))
    except Exception as e:
        log("[safe] ★ open() 后再设 a.sample_rate=44100 抛异常: %s: %s"
            % (type(e).__name__, e))
    try:
        a.num_channels = 1
        log("[safe] !! open() 后设 num_channels=1 竟然成功 -> %r" % (a.num_channels,))
    except Exception as e:
        log("[safe] open() 后设 num_channels=1 抛异常（预期）: %s: %s"
            % (type(e).__name__, e))

    log("[safe] get_num_connections(0.0) = %r" % (s.get_num_connections(0.0),))

    # write_data 只写本地缓冲，不碰 native 发送 —— 安全
    log("[safe] get_write_available() 前 = %r" % (a.get_write_available(),))
    for n in (1024, 4800, 4801):
        planar = np.ascontiguousarray(
            np.random.default_rng(1).standard_normal((2, n)).astype("float32") * 0.001)
        try:
            a.write_data(planar)
            log("[safe] write_data(shape=%r) OK  事后 get_write_available()=%r  "
                "shape=%r num_samples=%r"
                % (planar.shape, a.get_write_available(), a.shape, a.num_samples))
        except Exception as e:
            log("[safe] write_data(shape=%r) 抛异常: %s: %s"
                % (planar.shape, type(e).__name__, e))
    # 传交错 (n, ch) 看看报什么
    bad = np.ascontiguousarray(np.zeros((1024, 2), dtype="float32"))
    try:
        a.write_data(bad)
        log("[safe] write_data(交错 (n,2)) 竟然 OK -> shape=%r" % (a.shape,))
    except Exception as e:
        log("[safe] write_data(交错 (n,2)) 抛异常: %s: %s" % (type(e).__name__, e))
    # 传一维
    bad1 = np.zeros(2048, dtype="float32")
    try:
        a.write_data(bad1)
        log("[safe] write_data(一维) 竟然 OK")
    except Exception as e:
        log("[safe] write_data(一维) 抛异常: %s: %s" % (type(e).__name__, e))
    try:
        s.close()
    except Exception:
        pass
    log("[safe] DONE")


# ---------------------------------------------------------------- gate
def mode_gate():
    import time
    import numpy as np
    from ndi_out import NDIOutput

    log("[gate] 构造 NDIOutput 并 _ensure(640,360)（不发送，只建 sender）")
    ndi = NDIOutput("AutoVJ-Selftest-Gate")
    ok = ndi._ensure(640, 360)
    log("[gate] _ensure -> %r  enabled=%r  last_err=%r" % (ok, ndi.enabled, ndi._last_err))
    if not ok:
        log("[gate] FAIL 建不起来")
        return
    a = ndi._audio
    log("[gate] 建好后 audio: sample_rate=%r num_channels=%r num_samples=%r shape=%r"
        % (a.sample_rate, a.num_channels, a.num_samples, a.shape))
    log("[gate] _audio_sr=%r" % (ndi._audio_sr,))
    log("[gate] _conns() = %r（0 = 当前本机没有接收端）" % (ndi._conns(),))

    time.sleep(0.35)   # 跳过 0.3s 暖机

    # 三种采样率都喂一遍：看 feed_audio 的早退路径
    for sr in (48000, 44100, 96000):
        ndi._audio_dropped = 0
        ndi._audio_ok = 0
        pcm = np.random.default_rng(2).standard_normal(1024 * 2).astype("float32") * 0.001
        ndi.feed_audio(pcm, sr)
        log("[gate] feed_audio(sr=%d): ok=%d dropped=%d a.sample_rate读回=%r  err=%r "
            "shape=%r" % (sr, ndi._audio_ok, ndi._audio_dropped, a.sample_rate,
                          ndi._audio_err, a.shape))
    log("[gate] 说明：ok=0 表示**一个音频块都没发出去**（连接数门控挡掉）")
    try:
        ndi.close()
    except Exception:
        pass
    log("[gate] DONE")


# ---------------------------------------------------------------- danger
def mode_danger():
    """无接收端强行 send_audio —— 验证「会不会段错误」。"""
    import time
    import numpy as np
    from cyndilib.sender import Sender
    from cyndilib.video_frame import VideoSendFrame
    from cyndilib.audio_frame import AudioSendFrame

    s = Sender("AutoVJ-Selftest-Danger")
    v = VideoSendFrame()
    v.set_resolution(640, 360)
    v.set_fourcc(0x41524742)
    v.set_frame_rate(60)
    v.set_progressive(True)
    s.set_video_frame(v)
    a = AudioSendFrame()
    a.sample_rate = 48000
    a.num_channels = 2
    a.set_max_num_samples(4800)
    s.set_audio_frame(a)
    s.open()
    log("[danger] open() 完成，conns=%r" % (s.get_num_connections(0.0),))
    time.sleep(0.35)
    planar = np.ascontiguousarray(np.zeros((2, 1024), dtype="float32"))
    a.write_data(planar)
    log("[danger] write_data OK，准备调用 send_audio()（无接收端）")
    r = s.send_audio()
    log("[danger] send_audio() 返回 %r（没有段错误）" % (r,))
    r2 = s.send_audio()
    log("[danger] 第二次 send_audio() 返回 %r" % (r2,))
    log("[danger] DONE")


# ---------------------------------------------------------------- crash
def mode_crash():
    """逐步定位：有接收端时，audio 的 write_data / send_audio 到底哪一步崩。"""
    import time
    import numpy as np
    from cyndilib.finder import Finder
    from cyndilib.receiver import Receiver, ReceiveFrameType
    from cyndilib.audio_frame import AudioRecvFrame
    from ndi_out import NDIOutput

    NAME = "AutoVJ-Selftest-Crash"
    W, H, SR = 640, 360, 48000
    CHUNK = 1024

    ndi = NDIOutput(NAME)
    if not ndi._ensure(W, H):
        log("[crash] FAIL 建不起来: %r" % (ndi._last_err,))
        return
    ndi._opened_at = 0.0    # 跳过暖机期
    s = ndi._sender
    a = ndi._audio
    log("[crash] sender 就绪: audio shape=%r num_samples=%r sample_rate=%r conns=%r"
        % (a.shape, a.num_samples, a.sample_rate, ndi._conns()))

    f = Finder()
    t0 = time.time()
    found = None
    while time.time() - t0 < 6.0:
        f.wait(0.5)
        f.update_sources()
        for nm in f.get_source_names():
            if NAME in nm:
                found = f.get_source(nm)
                break
        if found:
            break
    if not found:
        log("[crash] UNREACHABLE：看不到源（沙箱挡组播）")
        return
    recv = Receiver()
    recv.set_audio_frame(AudioRecvFrame(8))
    recv.set_source(found)
    log("[crash] receiver 连上: is_connected=%r" % (recv.is_connected(),))

    # 等连接数 >0（NDI 的 no_connections 要几帧才认到）
    t0 = time.time()
    while time.time() - t0 < 5.0 and ndi._conns() <= 0:
        time.sleep(0.05)
    log("[crash] 发送端 _conns()=%r  (接收端自己看到 %r)"
        % (ndi._conns(), recv.get_num_connections()))

    tone = (np.sin(2 * np.pi * 440 * np.arange(CHUNK) / SR) * 0.2).astype("float32")
    inter = np.repeat(tone, 2)
    nrecv = 0
    for i in range(6):
        log("[crash] --- round %d ---" % i)
        log("[crash]   pre  : write_available=%r shape=%r num_samples=%r"
            % (a.get_write_available(), a.shape, a.num_samples))
        planar = np.ascontiguousarray(np.vstack([tone, tone]))
        a.write_data(planar)
        log("[crash]   write_data OK; write_available=%r num_samples=%r"
            % (a.get_write_available(), a.num_samples))
        rr = s.send_audio()
        log("[crash]   send_audio -> %r" % (rr,))
        try:
            r = recv.receive(ReceiveFrameType.recv_all, 100)
            log("[crash]   receive -> %r audio=%d video=%d"
                % (int(r), nrecv, 0))
            if int(r) & int(ReceiveFrameType.recv_audio):
                nrecv += 1
                af = recv.audio_frame
                log("[crash]   recv audio: sr=%r ch=%r n=%r"
                    % (af.sample_rate, af.num_channels, af.num_samples))
        except Exception as e:
            log("[crash]   receive 抛异常: %s" % (e,))
        time.sleep(0.02)
    log("[crash] DONE nrecv=%d" % nrecv)


# ---------------------------------------------------------------- tx / rx
NAME_X = "AutoVJ-Selftest-X"


# ---------------- p_metadata 补丁（见 _verify_pmetadata_fix.py / 诊断报告）---------
def _mbi_readable(addr, size=8):
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class MBI(ctypes.Structure):
        _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                    ("AllocationProtect", wintypes.DWORD), ("RegionSize", ctypes.c_size_t),
                    ("State", wintypes.DWORD), ("Protect", wintypes.DWORD),
                    ("Type", wintypes.DWORD)]
    if not addr:
        return False
    mbi = MBI()
    if not k32.VirtualQuery(ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
        return False
    if mbi.State != 0x1000 or (mbi.Protect & 0x01) or (mbi.Protect & 0x100):
        return False
    base = mbi.BaseAddress or 0
    return base <= addr and addr + size <= base + mbi.RegionSize


def patch_audio_meta_null(a, span=1024):
    """把 AudioSendFrame 里所有音频帧结构体的 p_metadata 置 NULL。"""
    import ctypes
    base = id(a)
    n = 0
    for off in range(8, span, 8):
        p = ctypes.c_void_p.from_address(base + off).value or 0
        if not _mbi_readable(p, 64):
            continue
        if ctypes.c_int.from_address(p + 0).value != 48000:
            continue
        if ctypes.c_int.from_address(p + 4).value != 2:
            continue
        if (ctypes.c_int.from_address(p + 24).value & 0xFFFFFFFF) != 0x70544C46:
            continue
        ctypes.c_void_p.from_address(p + 48).value = None
        n += 1
    return n


def build_sender(ndi, w, h, patch_meta):
    """复刻 ndi_out._ensure 的构造步骤；patch_meta=True 时在 open() 前把
    AudioSendFrame 所有帧结构体的 p_metadata 置 NULL。"""
    from cyndilib.sender import Sender
    from cyndilib.video_frame import VideoSendFrame
    from cyndilib.audio_frame import AudioSendFrame
    s = Sender(ndi.name)
    v = VideoSendFrame()
    v.set_resolution(int(w), int(h))
    v.set_fourcc(0x41524742)
    v.set_frame_rate(60)
    v.set_progressive(True)
    s.set_video_frame(v)
    a = AudioSendFrame()
    a.sample_rate = 48000
    a.num_channels = 2
    a.set_max_num_samples(4800)
    s.set_audio_frame(a)
    if patch_meta:
        n = patch_audio_meta_null(a)
        log("[tx] p_metadata 补丁：把 %d 个音频帧结构体的 p_metadata 置 NULL" % n)
    s.open()
    ndi._sender, ndi._video, ndi._audio = s, v, a
    ndi._w, ndi._h = int(w), int(h)
    ndi._opened_at = time.perf_counter()
    ndi.enabled = True
    ndi._inited = True
    return True


def mode_tx():
    """发送端（独立进程，像真实软件那样）。视频 + 音频同时发，统计成功数。"""
    import time
    import numpy as np
    from ndi_out import NDIOutput

    W, H, SR, CHUNK = 640, 360, 48000, int(os.environ.get("TX_CHUNK", "1024"))
    dur = float(os.environ.get("TX_SECS", "8"))
    kind = os.environ.get("TX_KIND", "both")
    log("[tx] TX_KIND=%s" % kind)
    ndi = NDIOutput(NAME_X)
    _decl = int(os.environ.get("TX_DECLARE_SR", "0") or 0)
    if _decl:
        ndi.set_audio_rate(_decl)
        log("[tx] set_audio_rate(%d) -> _audio_sr=%r（open 之前）" % (_decl, ndi._audio_sr))
    if not ndi._ensure(W, H):
        log("[tx] FAIL 建不起来 %r" % (ndi._last_err,))
        return
    if os.environ.get("PATCH", "") == "1":
        # 换个分辨率让 _ensure 重建，避免已经 open 过；然后手工 patch 一次
        ndi.close()
        build_sender(ndi, W, H, True)
    rng = np.random.default_rng(7)
    bgra = np.zeros((H, W, 4), dtype=np.uint8)
    bgra[:, :, 3] = 255
    t0 = time.time()
    next_f = time.time()
    next_a = time.time()
    nf = na = 0
    last = t0
    while time.time() - t0 < dur:
        now = time.time()
        if now >= next_f and kind in ("video", "both"):
            bgra[::4] = rng.integers(0, 255, (H // 4, W, 4), dtype=np.uint8)
            ndi.send_video(bgra.tobytes(), W, H)
            nf += 1
            next_f += 1.0 / 30
        while now >= next_a and kind in ("audio", "both"):
            tone = (np.sin(2 * np.pi * 440 * np.arange(CHUNK) / SR) * 0.2).astype("float32")
            ndi.feed_audio(np.repeat(tone, 2), SR)
            na += 1
            next_a += CHUNK / SR
        if now - last >= 1.0:
            last = now
            log("[tx] t=%.1fs 视频=%d 音频=%d  _audio_ok=%d _audio_dropped=%d _audio_err=%r "
                "conns=%r" % (now - t0, nf, na, ndi._audio_ok, ndi._audio_dropped,
                              ndi._audio_err, ndi._conns()))
        time.sleep(0.002)
    log("[tx] 结束: 视频尝试=%d 音频尝试=%d 实际发出音频块=%d 丢弃=%d err=%r"
        % (nf, na, ndi._audio_ok, ndi._audio_dropped, ndi._audio_err))
    log("[tx] DONE")


def mode_rx():
    """接收端（独立进程）。"""
    import time
    import numpy as np
    from cyndilib.finder import Finder
    from cyndilib.receiver import Receiver, ReceiveFrameType
    from cyndilib.audio_frame import AudioRecvFrame
    from cyndilib.video_frame import VideoRecvFrame

    dur = float(os.environ.get("RX_SECS", "12"))
    f = Finder()
    t0 = time.time()
    found = None
    while time.time() - t0 < 10.0:
        f.wait(0.5)
        f.update_sources()
        for nm in f.get_source_names():
            if NAME_X in nm:
                found = f.get_source(nm)
                break
        if found:
            break
    if not found:
        log("[rx] UNREACHABLE：10s 内没发现源 %s；看到的是 %r"
            % (NAME_X, f.get_source_names()))
        return
    log("[rx] 找到源 %r" % found.name)
    recv = Receiver()
    recv.set_video_frame(VideoRecvFrame())
    recv.set_audio_frame(AudioRecvFrame(16))
    recv.set_source(found)
    log("[rx] is_connected=%r" % recv.is_connected())

    nv = na = 0
    first = None
    nz_audio = 0
    t0 = time.time()
    while time.time() - t0 < dur:
        r = recv.receive(ReceiveFrameType.recv_all, 100)
        if int(r) & int(ReceiveFrameType.recv_video):
            nv += 1
        if int(r) & int(ReceiveFrameType.recv_audio):
            na += 1
            af = recv.audio_frame
            try:
                data, ts = af.get_read_data()
                if data is not None and hasattr(data, "size") and data.size:
                    mx = float(np.max(np.abs(data)))
                    if mx > 1e-6:
                        nz_audio += 1
                    if first is None:
                        first = dict(sample_rate=af.sample_rate, ch=af.num_channels,
                                     n=af.num_samples, shape=tuple(data.shape),
                                     absmax=mx,
                                     rms=float(np.sqrt(np.mean(data.astype("float64") ** 2))))
            except Exception as e:
                log("[rx] 取数据异常: %s" % (e,))
    log("[rx] 收到 视频=%d 音频=%d（其中非静音 %d）" % (nv, na, nz_audio))
    if first:
        log("[rx] 第一帧音频: %r" % (first,))
    log("[rx] DONE")


def mode_loop2():
    """父进程：接收端先起，再把发送端作为独立进程拉起（贴近真实部署）。"""
    log("=" * 72)
    log("跨进程回环：rx 子进程 + tx 子进程")
    log("=" * 72)
    env = dict(os.environ)
    env["RX_SECS"] = "16"
    env["TX_SECS"] = "8"
    rx = subprocess.Popen([PY, "-u", THIS, "rx"], cwd=ROOT, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time = __import__("time")
    time.sleep(2.0)
    tx = subprocess.Popen([PY, "-u", THIS, "tx"], cwd=ROOT, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    tx_out, _ = tx.communicate(timeout=120)
    rx_out, _ = rx.communicate(timeout=120)
    log("---------- [tx] 输出 (exit=%s) ----------" % tx.returncode)
    sys.stdout.write(tx_out)
    log("---------- [rx] 输出 (exit=%s) ----------" % rx.returncode)
    sys.stdout.write(rx_out)
    log("========== 跨进程回环结论 ==========")
    if tx.returncode != 0 or rx.returncode != 0:
        log("!! 有子进程异常退出（tx=%s rx=%s）；tx 退出码 -1073741819 = 段错误"
            % (tx.returncode, rx.returncode))
    else:
        log("两个进程都正常退出")


# ---------------------------------------------------------------- loop
def mode_loop():
    import time
    import numpy as np
    import cyndilib
    from cyndilib.finder import Finder
    from cyndilib.receiver import Receiver, ReceiveFrameType
    from cyndilib.audio_frame import AudioRecvFrame
    from ndi_out import NDIOutput

    NAME = "AutoVJ-Selftest-Loop"
    W, H, SR = 640, 360, 48000

    ndi = NDIOutput(NAME)
    _decl = int(os.environ.get("LOOP_DECLARE_SR", "0") or 0)
    if _decl:
        ndi.set_audio_rate(_decl)
        log("[loop] set_audio_rate(%d) -> _audio_sr=%r（open 之前）" % (_decl, ndi._audio_sr))
    if not ndi._ensure(W, H):
        log("[loop] FAIL 发送端建不起来: %r" % (ndi._last_err,))
        return

    f = Finder()
    log("[loop] Finder 启动，等源出现（6s）…  cyndilib=%s ndi=%s"
        % (getattr(cyndilib, "__version__", "?"), cyndilib.get_ndi_version()))
    found = None
    t0 = time.time()
    while time.time() - t0 < 6.0:
        f.wait(0.5)
        f.update_sources()
        names = f.get_source_names()
        for nm in names:
            if NAME in (nm or ""):
                found = f.get_source(nm)
                break
        if found is not None:
            break
    log("[loop] Finder 看到的源: %r" % (f.get_source_names(),))
    if found is None:
        log("[loop] UNREACHABLE：本机 Finder 看不到自己发的源（沙箱/组播被挡）——"
            " 无法做同机回环，**这不代表发送路径有错**")
        try:
            ndi.close()
        except Exception:
            pass
        return
    log("[loop] 找到发送源: %r" % (found.name,))

    recv = Receiver(color_format=0, bandwidth=0)   # 默认
    recv.set_audio_frame(AudioRecvFrame())
    recv.set_source(found)
    log("[loop] receiver is_connected=%r" % (recv.is_connected(),))

    bgra = np.zeros((H, W, 4), dtype=np.uint8)
    bgra[:, :, 3] = 255
    rng = np.random.default_rng(3)

    kind = os.environ.get("LOOP_KIND", "both")
    log("[loop] LOOP_KIND=%s（video=只发视频 / audio=只发音频 / both=都发）" % kind)

    n_audio = 0
    n_video = 0
    first_meta = None
    t0 = time.time()
    next_audio = time.time()
    CHUNK = 1024
    it = 0
    while time.time() - t0 < 10.0:
        it += 1
        log("[loop] it=%d" % it)
        if kind in ("video", "both"):
            bgra[::4] = rng.integers(0, 255, (H // 4, W, 4), dtype=np.uint8)
            ndi.send_video(bgra.tobytes(), W, H)
            log("[loop]   video sent")
        if kind in ("audio", "both"):
            while time.time() >= next_audio:
                tone = (np.sin(2 * np.pi * 440 * np.arange(CHUNK) / SR) * 0.2).astype("float32")
                inter = np.repeat(tone, 2)
                ndi.feed_audio(inter, SR)
                next_audio += CHUNK / SR
            log("[loop]   audio fed (ok=%d dropped=%d err=%r)"
                % (ndi._audio_ok, ndi._audio_dropped, ndi._audio_err))
        log("[loop]   -> receive")
        r = recv.receive(ReceiveFrameType.recv_all, 50)
        log("[loop]   <- receive r=%r" % (int(r),))
        if int(r) & int(ReceiveFrameType.recv_audio):
            n_audio += 1
            if first_meta is None:
                af = recv.audio_frame
                try:
                    data, ts = af.get_read_data()
                except Exception as e:
                    data, ts = ("<%s>" % e), 0
                first_meta = dict(sample_rate=af.sample_rate, no_channels=af.num_channels,
                                  no_samples=af.num_samples)
                if hasattr(data, "shape"):
                    first_meta["rms"] = float(np.sqrt(np.mean(data.astype("float64") ** 2)))
                    first_meta["absmax"] = float(np.max(np.abs(data)))
                    first_meta["shape"] = tuple(data.shape)
                else:
                    first_meta["data"] = str(data)
                log("[loop] ★ 收到第一帧音频: %r" % (first_meta,))
        if int(r) & int(ReceiveFrameType.recv_video):
            n_video += 1

    log("[loop] 10s 内收到 视频帧=%d  音频帧=%d" % (n_video, n_audio))
    log("[loop] 发送端: _audio_ok=%d _audio_dropped=%d _audio_err=%r _dropped=%d _last_err=%r"
        % (ndi._audio_ok, ndi._audio_dropped, ndi._audio_err, ndi._dropped, ndi._last_err))
    log("[loop] _conns()=%r" % (ndi._conns(),))
    if first_meta:
        log("[loop] 回环收到的音频参数: %r" % (first_meta,))
    if n_audio == 0:
        log("[loop] ★★ 结论：**接收端连上了、视频通了，但一帧音频都没收到**")
    else:
        log("[loop] ★★ 结论：音频发送路径**工作正常**（声音问题在更上游）")
    try:
        recv.set_source(None)
    except Exception:
        pass
    try:
        ndi.close()
    except Exception:
        pass
    log("[loop] DONE")


# ---------------------------------------------------------------- parent
MODES = ("safe", "gate", "danger", "crash", "loop", "loop2")


def mode_all():
    log("=" * 72)
    log("NDI 音频自测（父进程编排，每个用例独立子进程）")
    log("python = %s" % PY)
    log("=" * 72)
    results = {}
    for m in MODES:
        log("")
        log("-" * 72)
        log(">>> 子进程 mode=%s" % m)
        log("-" * 72)
        try:
            p = subprocess.run([PY, "-u", THIS, m], cwd=ROOT,
                               capture_output=True, text=True, timeout=120)
            sys.stdout.write(p.stdout)
            if p.stderr:
                sys.stdout.write("[stderr]\n" + p.stderr)
            results[m] = ("exit=%s" % p.returncode)
            if p.returncode not in (0,):
                log("!! mode=%s 异常退出（负数=被信号杀死，-1073741819 即 0xC0000005 访问冲突/段错误）"
                    % m)
        except subprocess.TimeoutExpired:
            results[m] = "TIMEOUT"
            log("!! mode=%s 超时" % m)
        except Exception as e:
            results[m] = "RUNNER-ERR %s" % e
            log("!! mode=%s 运行器异常: %s" % (m, e))
    log("")
    log("=" * 72)
    log("汇总:")
    for m in MODES:
        log("   %-8s -> %s" % (m, results.get(m)))
    log("=" * 72)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    {"safe": mode_safe, "gate": mode_gate, "danger": mode_danger,
     "crash": mode_crash, "loop": mode_loop, "tx": mode_tx, "rx": mode_rx,
     "loop2": mode_loop2, "all": mode_all}.get(mode, mode_all)()
