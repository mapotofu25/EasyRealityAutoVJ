# -*- coding: utf-8 -*-
"""复现用户报的：「开了 NDI 音频输出之后，画面输出和预览都肉眼可见掉帧」。

## 假设（2026-10-08）

`NDIOutput` 里**视频和音频共用同一把 `self._lock`**：

  - `send_video()`  → `with self._lock: ... write_data + send_video`
  - `feed_audio()`  → `with self._lock: ... 重采样 + 累积 + write_data + send_audio`

`send_video` 跑在**引擎渲染线程**（`engine._ndi_send`），`feed_audio` 跑在
**NDI 音频转发线程**。音频这一侧在锁内做的事**远不止一次写**：
np.asarray → reshape → （采样率不等时）`np.interp` 逐声道重采样 → concatenate 累积
→ ascontiguousarray → write_data → send_audio。

只要音频侧**持锁期间**被 `send_audio()`（会进 native、可能被接收端拖住）或那次
`concatenate` 拖住，视频侧这一帧就在 `with self._lock` 上**干等** ⇒ 渲染线程被
污染 ⇒ 预览/输出一起掉帧。这与「音频一开就掉帧、音频一关就好」的现象完全吻合。

本脚本**不猜**，直接测：同一帧率下，
  ① 只发视频；
  ② 发视频 + 并发喂音频（模拟现场）。
比较两组的「帧间隔 P99 / 最大间隔 / 超时(>25ms)帧数」，以及音频侧每次持锁时长。

判据：若 ② 的 P99 显著大于 ①（或持锁时长 P99 远大于 sent_audio 本身耗时），
则确认是**锁竞争**导致掉帧。
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import numpy as np  # noqa: E402

W, H = 1280, 720
FRAMES = 300          # 每轮 300 帧
FPS = 60.0
BLOCK = 1024          # 采集块大小（21ms @48k）


def _frame():
    # 造一帧 BGRA（内容无所谓，只要够大，贴近真实拷贝量）
    return np.zeros(W * H * 4, dtype=np.uint8)


def _connect_receiver(name, timeout=8.0):
    """起一个真接收端连上（**必须有接收端**，否则 `send_audio()` 走的是
    "无连接快速返回"路径，锁竞争根本不体现 —— 这正是前面裸测看不到退化的原因）。"""
    from cyndilib.finder import Finder
    from cyndilib.receiver import Receiver, ReceiveFrameType
    from cyndilib.audio_frame import AudioRecvFrame
    from cyndilib.video_frame import VideoRecvFrame

    fd = Finder()
    fd.open()
    fd.wait_for_sources(timeout)
    src = None
    for nm in fd.get_source_names():
        if name in nm:
            src = fd.get_source(nm)
            break
    if src is None:
        return None, None, None, None, fd
    r = Receiver(source=src)
    af, vf = AudioRecvFrame(), VideoRecvFrame()
    r.set_audio_frame(af)
    r.set_video_frame(vf)
    return r, af, vf, ReceiveFrameType, fd


def run_case(with_audio):
    import ndi_out

    NAME = "ERAVJ_FrameStall_Test"
    ndi = ndi_out.NDIOutput(NAME)
    if not ndi._ensure(W, H):
        print("FAIL NDI _ensure 失败（本机没有 NDI Runtime？）：%s" % ndi._last_err)
        return None

    # ★ 先连一个真接收端：否则 send_audio/send_video 都走"没人连"的快速路径，
    #   测不到真实负载下的锁竞争。
    r, af, vf, RFT, fd = _connect_receiver(NAME, timeout=8.0)
    if r is None:
        print("   （未找到自身源，降级为无接收端测试）")

    buf = _frame()
    # 先把视频推起来让接收端建立连接（NDI 是"收到数据才建连"）
    for _ in range(40):
        try:
            ndi.send_video(buf, W, H)
        except Exception:
            pass
        if r is not None:
            try:
                r.receive(RFT.recv_all, 20)
            except Exception:
                pass
        time.sleep(0.01)
    time.sleep(ndi._WARMUP_SECS + 0.1)
    try:
        conns0 = ndi._conns()
    except Exception:
        conns0 = -1

    gaps = []
    audio_hold = []          # 音频侧单次持锁时长（ms）
    stop = threading.Event()

    if with_audio:
        def audio_thread():
            sr = 48000
            ph = 0.0
            nxt = time.perf_counter()
            while not stop.is_set():
                n = np.arange(BLOCK, dtype=np.float64) + ph
                x = (0.3 * np.sin(2 * np.pi * 440.0 * n / sr)).astype(np.float32)
                ph += BLOCK
                pcm = np.column_stack([x, x]).reshape(-1)
                t0 = time.perf_counter()
                try:
                    ndi.feed_audio(pcm, sr)
                except Exception:
                    pass
                audio_hold.append((time.perf_counter() - t0) * 1000.0)
                nxt += BLOCK / sr
                d = nxt - time.perf_counter()
                if d > 0:
                    time.sleep(d)
                else:
                    nxt = time.perf_counter()

        th = threading.Thread(target=audio_thread, daemon=True, name="audio")
        th.start()

    # 视频：按 60fps 推 FRAMES 帧，量每次 send_video 的**墙钟耗时**
    period = 1.0 / FPS
    nxt = time.perf_counter()
    t_prev = None
    for i in range(FRAMES):
        t0 = time.perf_counter()
        try:
            ndi.send_video(buf, W, H)
        except Exception:
            pass
        t1 = time.perf_counter()
        if t_prev is not None:
            gaps.append((t1 - t_prev) * 1000.0)
        t_prev = t1
        nxt += period
        d = nxt - time.perf_counter()
        if d > 0:
            time.sleep(d)
        else:
            nxt = time.perf_counter()

    stop.set()
    if with_audio:
        time.sleep(0.1)
    for x in (r,):
        try:
            x.disconnect()
        except Exception:
            pass
    try:
        fd.close()
    except Exception:
        pass
    ndi.close()

    g = np.array(gaps) if gaps else np.array([0.0])
    return {
        "conns0": conns0,
        "n": len(g),
        "mean": float(g.mean()),
        "p50": float(np.percentile(g, 50)),
        "p99": float(np.percentile(g, 99)),
        "max": float(g.max()),
        "over25": int((g > 25.0).sum()),
        "audio_hold_p50": float(np.percentile(audio_hold, 50)) if audio_hold else 0.0,
        "audio_hold_p99": float(np.percentile(audio_hold, 99)) if audio_hold else 0.0,
        "audio_hold_max": float(max(audio_hold)) if audio_hold else 0.0,
        "audio_calls": len(audio_hold),
        "audio_ok": ndi._audio_ok,
        "audio_dropped": ndi._audio_dropped,
    }


def main():
    print("=" * 74)
    print("NDI 视频/音频 锁竞争 → 掉帧 复现")
    print("=" * 74)

    r1 = run_case(False)
    if r1 is None:
        return 1
    print("\n① 只发视频：")
    print("   连接数 = %s" % r1["conns0"])
    print("   帧间隔 ms: 均值 %.2f  P50 %.2f  P99 %.2f  max %.2f  超25ms=%d/%d"
          % (r1["mean"], r1["p50"], r1["p99"], r1["max"], r1["over25"], r1["n"]))

    r2 = run_case(True)
    if r2 is None:
        return 1
    print("\n② 视频 + 并发音频：")
    print("   连接数 = %s" % r2["conns0"])
    print("   帧间隔 ms: 均值 %.2f  P50 %.2f  P99 %.2f  max %.2f  超25ms=%d/%d"
          % (r2["mean"], r2["p50"], r2["p99"], r2["max"], r2["over25"], r2["n"]))
    print("   音频侧持锁 ms: P50 %.2f  P99 %.2f  max %.2f  （%d 次调用）"
          % (r2["audio_hold_p50"], r2["audio_hold_p99"], r2["audio_hold_max"],
             r2["audio_calls"]))
    print("   音频 ok=%d dropped=%d" % (r2["audio_ok"], r2["audio_dropped"]))

    print("\n--- 结论 ---")
    worsen = r2["p99"] - r1["p99"]
    print("   P99 帧间隔恶化 = %.2f ms（%.2f → %.2f）"
          % (worsen, r1["p99"], r2["p99"]))
    print("   超 25ms 帧数   = %d → %d" % (r1["over25"], r2["over25"]))
    if r2["audio_hold_p99"] > 25.0:
        print("   ⚠ 音频侧**持锁**P99 已超过一帧周期 ⇒ 视频侧必然干等 ⇒ 掉帧")
    if worsen > 5.0 or r2["over25"] > r1["over25"] + 3:
        print("   ❌ 确认：开启音频后视频明显掉帧（锁竞争）")
        return 2
    print("   ✅ 未观察到明显退化")
    return 0


if __name__ == "__main__":
    sys.exit(main())
