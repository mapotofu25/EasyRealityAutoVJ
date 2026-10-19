# -*- coding: utf-8 -*-
"""拆解 `NDIOutput.feed_audio()` 持锁期间各步骤的耗时，找出那 ~18ms 到底花在哪。

## 为什么必须拆

`tools/_ndi_frame_stall.py` 实测：音频侧**持锁** P99 ≈ 18ms、max ≈ 19ms，
而一帧只有 16.7ms ⇒ 视频线程撞上就必然掉帧（实测 max 帧间隔 30.5ms）。
但 `send_audio()` 在"无接收端"时只是快速返回 —— 那这 18ms 是哪来的？

本脚本对锁内每一步单独计时（asarray / reshape / interp 重采样 / concatenate
累积 / ascontiguousarray / write_data / send_audio），**不做任何猜测**。
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import numpy as np  # noqa: E402

SR = 48000
BLOCK = 1024


def timed(fn, n=200):
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1000.0)
    a = np.array(ts)
    return a.mean(), np.percentile(a, 99), a.max()


def main():
    import ndi_out

    ndi = ndi_out.NDIOutput("ERAVJ_Split_Test")
    if not ndi._ensure(1280, 720):
        print("FAIL _ensure: %s" % ndi._last_err)
        return 1
    time.sleep(ndi._WARMUP_SECS + 0.1)

    n = np.arange(BLOCK, dtype=np.float64)
    x = (0.3 * np.sin(2 * np.pi * 440.0 * n / SR)).astype(np.float32)
    pcm_i = np.column_stack([x, x]).reshape(-1)          # 交错一维，与采集侧一致

    print("=" * 68)
    print("feed_audio 锁内步骤耗时（%d 次 / 项，单位 ms）" % 200)
    print("=" * 68)

    def s_asarray():
        return np.asarray(pcm_i, dtype=np.float32)

    def s_reshape():
        return np.asarray(pcm_i, dtype=np.float32)[:(pcm_i.size // 2) * 2].reshape(-1, 2)

    def s_interp():
        p = np.asarray(pcm_i, dtype=np.float32).reshape(-1, 2)
        nin = p.shape[0]
        xin = np.arange(nin, dtype=np.float64)
        xout = np.linspace(0.0, nin - 1, nin, dtype=np.float64)
        return np.stack([np.interp(xout, xin, p[:, c].astype(np.float64))
                         for c in range(2)], axis=1).astype(np.float32)

    def s_concat():
        a = np.zeros((4800, 2), dtype=np.float32)
        b = np.zeros((1024, 2), dtype=np.float32)
        return np.concatenate([a, b], axis=0)

    def s_contig():
        c = np.ascontiguousarray(np.zeros((2, 4800), dtype=np.float32))
        return c

    rows = [
        ("np.asarray(交错)", s_asarray),
        ("asarray+reshape", s_reshape),
        ("np.interp 重采样(2ch)", s_interp),
        ("concatenate 累积(5800x2)", s_concat),
        ("ascontiguousarray(2,4800)", s_contig),
    ]
    for label, fn in rows:
        m, p99, mx = timed(fn)
        print("  %-26s 均值 %.4f  P99 %.4f  max %.4f" % (label, m, p99, mx))

    # 整体：直接调 feed_audio（含锁），与上面各步之和对照
    def s_full():
        ndi.feed_audio(pcm_i, SR)

    m, p99, mx = timed(s_full)
    print("\n  feed_audio 整体（含抢锁）   均值 %.4f  P99 %.4f  max %.4f"
          % (m, p99, mx))

    # ★ 关键：把锁**换成**"只取锁立刻放"看基线，判断是不是等待其他持锁者
    def s_lockonly():
        with ndi._alock:
            pass

    m2, p992, mx2 = timed(s_lockonly)
    print("  仅抢锁（无工作）            均值 %.4f  P99 %.4f  max %.4f"
          % (m2, p992, mx2))

    ndi.close()
    print("\n--- 判读 ---")
    print("  若「仅抢锁」就明显 > 0.05ms ⇒ 是**锁竞争**（有别人持锁），不是本函数慢；")
    print("  若「np.interp 重采样」或「feed_audio 整体」很大 ⇒ 是**本函数锁内工作**太重。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
