# -*- coding: utf-8 -*-
"""⚠ **已被 `tools/_ndi_chain_verify.py` 取代，仅留作历史记录。**

本脚本量的是**第一轮修复**（send_audio 移出锁）的效果。当时视频仍留在渲染线程上，
所以"视频帧间隔"依然能看到音频的拖累 —— 第一轮只修掉了"锁等待",
没修掉"SDK 内部把音视频提交串行化"这一层。

真正定案的是 `tools/_ndi_chain_verify.py`：
  `send_video` 改为**只入队**，提交交给 `NDIOutput._video_loop` 专用线程，
  渲染线程的 `_ndi_send()` 从 13.8ms 降到 **0.046ms**（开不开音频都一样）。

## 历史判据（第一轮）

对照 `tools/_ndi_frame_stall.py` 的旧结果（音频侧持锁 P99 ≈ 18ms、
视频 max 帧间隔被拖到 30.5ms）：音频侧持锁耗时 P99 应 < 5ms。
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import numpy as np  # noqa: E402

W, H = 1280, 720
FRAMES = 300
FPS = 60.0
BLOCK = 1024


def _frame():
    return np.zeros(W * H * 4, dtype=np.uint8)


def run_case(with_audio):
    import ndi_out

    ndi = ndi_out.NDIOutput("ERAVJ_LockFix_Test")
    if not ndi._ensure(W, H):
        print("FAIL _ensure: %s" % ndi._last_err)
        return None
    time.sleep(ndi._WARMUP_SECS + 0.1)

    buf = _frame()
    gaps, audio_hold, audio_full = [], [], []
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
                # ★ 分开量两段：
                #   audio_full = 整个 feed_audio（**含锁外的 send_audio**，可以慢）
                #   audio_hold = **只量持锁那段**（用 try-acquire 探针逼近）
                #   我们关心的是持锁时长 —— 它才是能拖住视频的量。
                t0 = time.perf_counter()
                try:
                    ndi.feed_audio(pcm, sr)
                except Exception:
                    pass
                audio_full.append((time.perf_counter() - t0) * 1000.0)
                # 立刻再探一次锁：若瞬间拿到，说明 feed_audio 返回时锁已释放
                t1 = time.perf_counter()
                got = ndi._alock.acquire(True, 0.2)
                wait = (time.perf_counter() - t1) * 1000.0
                if got:
                    ndi._alock.release()
                # 探针等待时间 ≈ 别人持锁的残留（此处应为 0，因为发送已在锁外）
                audio_hold.append(wait)
                nxt += BLOCK / sr
                d = nxt - time.perf_counter()
                if d > 0:
                    time.sleep(d)
                else:
                    nxt = time.perf_counter()

        th = threading.Thread(target=audio_thread, daemon=True, name="audio")
        th.start()

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
    time.sleep(0.2)
    ndi.close()

    g = np.array(gaps) if gaps else np.array([0.0])
    af = np.array(audio_full) if audio_full else np.array([0.0])
    ah = np.array(audio_hold) if audio_hold else np.array([0.0])
    return {
        "n": len(g),
        "mean": float(g.mean()),
        "p99": float(np.percentile(g, 99)),
        "max": float(g.max()),
        "over25": int((g > 25.0).sum()),
        "af_p50": float(np.percentile(af, 50)),
        "af_p99": float(np.percentile(af, 99)),
        "af_max": float(af.max()),
        "ah_p50": float(np.percentile(ah, 50)),
        "ah_p99": float(np.percentile(ah, 99)),
        "ah_max": float(ah.max()),
        "calls": len(audio_full),
    }


def main():
    print("=" * 74)
    print("回归：send_audio 出锁后，NDI 音频不再拖累视频帧节奏")
    print("=" * 74)
    r1 = run_case(False)
    print("\n① 只发视频：")
    print("   帧间隔 ms: 均值 %.2f  P99 %.2f  max %.2f  超25ms=%d/%d"
          % (r1["mean"], r1["p99"], r1["max"], r1["over25"], r1["n"]))

    r2 = run_case(True)
    print("\n② 视频 + 并发音频：")
    print("   帧间隔 ms: 均值 %.2f  P99 %.2f  max %.2f  超25ms=%d/%d"
          % (r2["mean"], r2["p99"], r2["max"], r2["over25"], r2["n"]))
    print("   feed_audio 整体 ms: P50 %.2f  P99 %.2f  max %.2f（含锁外 send_audio，允许慢）"
          % (r2["af_p50"], r2["af_p99"], r2["af_max"]))
    print("   音频返回后**探针抢锁**等待 ms: P50 %.3f  P99 %.3f  max %.3f"
          % (r2["ah_p50"], r2["ah_p99"], r2["ah_max"]))

    print("\n--- 结论 ---")
    ok = True
    if r2["ah_p99"] > 1.0:
        print("   ❌ feed_audio 返回后锁仍被占用（P99 %.3f ms）" % r2["ah_p99"])
        ok = False
    else:
        print("   ✅ feed_audio 返回时锁已释放（P99 %.3f ms）⇒ send_audio 确认在锁外"
              % r2["ah_p99"])
    dp99 = r2["p99"] - r1["p99"]
    print("   视频 P99 恶化 = %.2f ms（%.2f → %.2f）；超25ms 帧数 %d → %d"
          % (dp99, r1["p99"], r2["p99"], r1["over25"], r2["over25"]))
    if dp99 > 4.0 or r2["over25"] > r1["over25"] + 2:
        print("   ❌ 视频仍被拖累")
        ok = False
    else:
        print("   ✅ 视频帧节奏基本不受音频影响")
    print("\n   " + ("全部通过 ✓" if ok else "仍有问题 ✗"))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
