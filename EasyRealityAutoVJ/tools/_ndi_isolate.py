# -*- coding: utf-8 -*-
"""⚠ **第一轮诊断脚本（历史）** —— 用于隔离出"视频掉帧"的成因。

结论（已定案，见 `tools/_ndi_chain_verify.py`）：
  - numpy 工作 / 抢锁 → **不是**主因（B ≈ A）；
  - `send_audio()` 的 native 调用会与 `send_video_async()` 争 NDI SDK 内部互斥
    ⇒ 只要视频提交留在渲染线程上，开音频就必然掉帧。

最终修法：`send_video` 只入队，`NDIOutput._video_loop` 专用线程提交。
本脚本保留仅作证据留档（它的 "nosend 代理" 手法对排查仍有用）。

----- 以下为原始说明 -----

三组对照，同一视频负载：
  A 只发视频                         （基线）
  B 并发喂音频，但**跳过 send_audio**（只做 numpy + write_data，出锁后不提交）
  C 并发喂音频，完整走（含 send_audio）
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import numpy as np  # noqa: E402

W, H = 1280, 720
FRAMES = 400
FPS = 60.0
BLOCK = 1024


def run_case(mode):
    """mode: 'none' | 'nosend' | 'full'"""
    import ndi_out

    ndi = ndi_out.NDIOutput("ERAVJ_Isolate_Test")
    if not ndi._ensure(W, H):
        print("FAIL _ensure")
        return None
    time.sleep(ndi._WARMUP_SECS + 0.1)

    # nosend 模式：让 feed_audio 的"提交"分支跳过（用 ndi_out 里的 _do_send 无法直接控，
    # 改为**替换 self._sender 上的 send_audio 属性不可行**（Cython 只读）⇒
    # 直接把 `ndi._sender` 换成一个代理，只屏蔽 send_audio，其余转发）。
    if mode == "nosend":
        class _Proxy:
            def __init__(self, inner):
                self.__dict__["_inner"] = inner

            def send_audio(self):
                return True     # 空操作：模拟"不调用 native send_audio"

            def __getattr__(self, k):
                return getattr(self.__dict__["_inner"], k)

            def __setattr__(self, k, v):
                setattr(self.__dict__["_inner"], k, v)

        ndi._sender = _Proxy(ndi._sender)

    buf = np.zeros(W * H * 4, dtype=np.uint8)
    gaps = []
    stop = threading.Event()
    audio_times = []

    if mode != "none":
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
                audio_times.append((time.perf_counter() - t0) * 1000.0)
                nxt += BLOCK / sr
                d = nxt - time.perf_counter()
                if d > 0:
                    time.sleep(d)
                else:
                    nxt = time.perf_counter()

        threading.Thread(target=audio_thread, daemon=True).start()

    period = 1.0 / FPS
    nxt = time.perf_counter()
    t_prev = None
    for _ in range(FRAMES):
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
    time.sleep(0.2)
    ndi.close()

    g = np.array(gaps)
    a = np.array(audio_times) if audio_times else np.array([0.0])
    return (g.mean(), np.percentile(g, 99), g.max(), int((g > 25).sum()), len(g),
            a.mean(), np.percentile(a, 99), a.max())


def main():
    print("=" * 78)
    print("隔离：视频掉帧的成因（A 基线 / B 无 send_audio / C 完整）")
    print("=" * 78)
    res = {}
    for mode, label in (("none", "A 只发视频"), ("nosend", "B 音频但不 send_audio"),
                        ("full", "C 音频完整")):
        r = run_case(mode)
        if r is None:
            return 1
        res[mode] = r
        print("\n%s：" % label)
        print("   视频帧间隔 ms: 均值 %.2f  P99 %.2f  max %.2f  超25ms=%d/%d"
              % (r[0], r[1], r[2], r[3], r[4]))
        if mode != "none":
            print("   音频 feed_audio ms: 均值 %.2f  P99 %.2f  max %.2f"
                  % (r[5], r[6], r[7]))

    print("\n--- 判读 ---")
    a, b, c = res["none"], res["nosend"], res["full"]
    print("   P99: A=%.2f  B=%.2f  C=%.2f" % (a[1], b[1], c[1]))
    print("   超25ms: A=%d  B=%d  C=%d" % (a[3], b[3], c[3]))
    db = b[1] - a[1]
    dc = c[1] - a[1]
    print("   B-A = %.2f ms（音频 numpy 工作本身的代价）" % db)
    print("   C-A = %.2f ms（含 send_audio）" % dc)
    if dc > 2.0 and db < 1.0:
        print("   ⇒ ★ 主因是 **send_audio() 的 native 争用**（即使移出锁，仍在抢 CPU/GIL）")
    elif db >= 1.0:
        print("   ⇒ ★ 主因是 **音频线程本身的 numpy/调度代价**")
    else:
        print("   ⇒ 两者都不明显")
    return 0


if __name__ == "__main__":
    sys.exit(main())
