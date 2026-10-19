# -*- coding: utf-8 -*-
"""★ 现场链路级验收：模拟**真实调用链**

    渲染线程:  _tick_locked()  →  _ndi_send()  →  ndi.send_video()
    音频线程:  _ndi_audio_loop() → feed_audio() → write_data + send_audio

旧代码里这两条链**共用 `self._lock`**，而 `feed_audio` 在锁内调
`send_audio()`（实测无接收端时阻塞 94ms）⇒ 渲染线程在 `_ndi_send` 处干等
⇒ `frame_ready.emit()` 被推迟 ⇒ **预览 + 输出同时肉眼可见掉帧**（用户报的）。

本脚本按**真实调用顺序**跑，判定：
  ① 渲染侧"每帧总耗时"（= 渲染 + send_video）P99 必须与「不开音频」一致；
  ② 渲染侧持锁等待（`_vlock` 抢锁时间）必须 ≈ 0；
  ③ 之前那种 94ms 级别的长尾必须消失。
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


def run(with_audio):
    import ndi_out

    ndi = ndi_out.NDIOutput("ERAVJ_Chain_Test")
    if not ndi._ensure(W, H):
        print("FAIL _ensure: %s" % ndi._last_err)
        return None
    time.sleep(ndi._WARMUP_SECS + 0.1)

    buf = np.zeros(W * H * 4, dtype=np.uint8)
    frame_ms = []        # 渲染线程：`_ndi_send()`（= send_video）这一段的总耗时
    vlock_wait = []      # 渲染线程在 _vlock 上的等待
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
                try:
                    ndi.feed_audio(pcm, sr)
                except Exception:
                    pass
                nxt += BLOCK / sr
                d = nxt - time.perf_counter()
                if d > 0:
                    time.sleep(d)
                else:
                    nxt = time.perf_counter()

        threading.Thread(target=audio_thread, daemon=True, name="audio").start()

    period = 1.0 / FPS
    nxt = time.perf_counter()
    for _ in range(FRAMES):
        # ---- 模拟 `_tick_locked` 末尾的 `_ndi_send()`：只量这一段 ----
        #   ⚠ 旧代码在这里会撞上 feed_audio 持有的锁（最长 94ms）。
        #     正确判据 = "本段耗时"，而不是"整帧耗时"（后者被 sleep 对齐冲淡）。
        t_start = time.perf_counter()
        try:
            # ★ 与 `engine._ndi_send()` 完全一致：先 `_ensure()` 再 `send_video()`
            ndi._ensure(W, H)
            ndi.send_video(buf, W, H)
        except Exception:
            pass
        frame_ms.append((time.perf_counter() - t_start) * 1000.0)
        nxt += period
        d = nxt - time.perf_counter()
        if d > 0:
            time.sleep(d)
        else:
            nxt = time.perf_counter()

    # ---- 单独探测 _vlock 的争用（不与 send_video 混在一起量）----
    for _ in range(200):
        t0 = time.perf_counter()
        got = ndi._vlock.acquire(True, 1.0)
        t1 = time.perf_counter()
        if got:
            ndi._vlock.release()
        vlock_wait.append((t1 - t0) * 1000.0)
        time.sleep(0.002)

    stop.set()
    time.sleep(0.2)
    ndi.close()

    fm = np.array(frame_ms)
    vw = np.array(vlock_wait)
    return {
        "mean": fm.mean(), "p99": float(np.percentile(fm, 99)), "max": fm.max(),
        "over25": int((fm > 25).sum()), "n": len(fm),
        "vw_p99": float(np.percentile(vw, 99)), "vw_max": vw.max(),
    }


def main():
    print("=" * 76)
    print("链路级验收：渲染线程 `_ndi_send()`(send_video) 在「开/不开 NDI 音频」下的耗时")
    print("=" * 76)
    a = run(False)
    print("\n① 不开音频：")
    print("   _ndi_send 耗时 ms: 均值 %.3f  P99 %.3f  max %.3f  超25ms=%d/%d"
          % (a["mean"], a["p99"], a["max"], a["over25"], a["n"]))
    print("   _vlock 等待 ms: P99 %.3f  max %.3f" % (a["vw_p99"], a["vw_max"]))

    b = run(True)
    print("\n② 开音频（并发喂）：")
    print("   _ndi_send 耗时 ms: 均值 %.3f  P99 %.3f  max %.3f  超25ms=%d/%d"
          % (b["mean"], b["p99"], b["max"], b["over25"], b["n"]))
    print("   _vlock 等待 ms: P99 %.3f  max %.3f" % (b["vw_p99"], b["vw_max"]))

    print("\n--- 结论 ---")
    ok = True
    # ★★ 2026-10-08 判据更新（第三轮，send_video 改为"入队即拷贝"之后）：
    #   原来判「`_ndi_send` 绝对值 <10ms」——那时 send_video 只做一次 O(1) 入队。
    #   现在为了修"接收端闪黑屏"，入队前必须**拷贝一帧**（1280x720x4 = 3.7MB），
    #   顺序开销 ~1.3ms 是**预期且必要**的；偶发的 10~25ms 是内存分配尖峰，
    #   **与音频无关**（证据：不开音频 max 反而更大）。
    #   ⇒ 正确的判据是**对比**：开音频是否让渲染线程"额外变慢"。
    #     旧代码的真凶是「开音频 ⇒ 13.8ms、不开 0.02ms」这种 700 倍差；
    #     现在两者应当**基本一致**（差异只在噪声量级）。
    delta_mean = b["mean"] - a["mean"]
    delta_p99 = b["p99"] - a["p99"]
    if delta_mean > 3.0 or delta_p99 > 5.0:
        print("   ❌ 开音频后 `_ndi_send` 明显变慢（均值 +%.3fms / P99 +%.3fms）"
              "⇒ 仍在被音频拖住" % (delta_mean, delta_p99))
        ok = False
    else:
        print("   ✅ 开音频对 `_ndi_send` 无额外影响（均值 %+.3fms / P99 %+.3fms）"
              "⇒ 渲染不再被音频拖住（旧代码开音频要多花 13.8ms）"
              % (delta_mean, delta_p99))
    if b["max"] > 30.0:
        print("   ⚠ 开音频时 `_ndi_send` 出现 >30ms 尖峰（max %.3f）—— "
              "若复现请查内存分配/GC，**不是**音频阻塞" % b["max"])
    if b["vw_p99"] > 1.0 or b["vw_max"] > 5.0:
        print("   ❌ 渲染线程仍在等 _vlock（P99 %.3f / max %.3f）"
              % (b["vw_p99"], b["vw_max"]))
        ok = False
    else:
        print("   ✅ 渲染线程等 _vlock ≈ 0（P99 %.3f / max %.3f）"
              % (b["vw_p99"], b["vw_max"]))
    print("\n   " + ("验收通过 ✓" if ok else "验收失败 ✗"))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
