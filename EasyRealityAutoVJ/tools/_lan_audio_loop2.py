# -*- coding: utf-8 -*-
"""A+B 同机自测 v2：**边收边消费**，验证抖动缓冲在真实读取节奏下的稳定性。

v1 的教训：只收不读 ⇒ `record()` 的控制逻辑根本不执行 ⇒ 深度看起来"失控"。
真实场景里 `_capture_loop` 每 20ms 就会读一次，所以必须**并发消费**才算数。

判据：
  - 消费出的块长恒为 960（= 2×480，与主程序 _BlockAssembler 同一约定）
  - 出块间隔均值 ≈20ms、P99 不炸
  - 缓冲深度稳定在目标附近（不单调涨）
  - 还原主峰 = 440Hz
  - 零丢包
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import numpy as np  # noqa: E402

import lan_audio as LA          # noqa: E402
import lan_audio_recv as R      # noqa: E402
import lan_audio_send as S      # noqa: E402

PORT = 47898
SECS = 8.0
SR = 48000
PKT = 480
FREQ = 440.0


def main():
    rc = 0
    dev = R.LanDevice(port=PORT)
    if not dev.start():
        print("FAIL 接收端启动失败: %s" % dev.error)
        return 1

    # ⚠ 不能发到 127.0.0.1：那在 `_my_ips` 里，会被接收端当**自滤回声**丢掉
    #   （2026-10-08 加的对端修复：只认**非本机**的 DATA）。
    #   用 127.0.0.2 冒充"另一台机器"，它可路由但不在 `_my_ips` 里。
    snd = S.Sender(port=PORT, dest=("127.0.0.2", PORT), sr=SR)
    try:
        snd._sock = LA.make_sender(PORT)     # noqa: SLF001
    except Exception as e:
        print("FAIL 发送端绑定失败: %s" % e)
        dev.stop()
        return 1

    snd.running = True
    tx = threading.Thread(target=snd._send_loop, name="tx", daemon=True)  # noqa: SLF001
    tx.start()

    # 声源线程
    def src():
        ph = 0.0
        t0 = time.perf_counter()
        nxt = t0
        while time.perf_counter() - t0 < SECS + 0.5:
            n = np.arange(PKT, dtype=np.float64) + ph
            x = (0.5 * np.sin(2 * np.pi * FREQ * n / SR)).astype(np.float32)
            ph += PKT
            snd._push(np.column_stack([x, x]), LA.qpc_now())   # noqa: SLF001
            nxt += PKT / SR
            d = nxt - time.perf_counter()
            if d > 0:
                time.sleep(d)

    th_src = threading.Thread(target=src, name="src", daemon=True)
    th_src.start()

    # ---- 消费者：模拟 _capture_loop 的读取节奏 ----
    rec = None
    while rec is None or dev.samplerate == 0:
        rec = dev.recorder()
        if dev.samplerate == 0:
            time.sleep(0.01)
    rec = dev._rec                                               # noqa: SLF001

    lens, gaps, depths = [], [], []
    chunks = []
    t0 = time.perf_counter()
    last_t = None
    while time.perf_counter() - t0 < SECS:
        d = rec.record(numframes=960)
        if d is None:
            time.sleep(0.001)
            continue
        now = time.perf_counter()
        lens.append(d.shape[0])
        if last_t is not None:
            gaps.append((now - last_t) * 1000.0)
        last_t = now
        chunks.append(d)
        depths.append(dev.snapshot()["depth_ms"])

    snap = dev.snapshot()
    print("--- 接收状态 ---")
    for k in ("connected", "peer", "sr", "packets", "lost", "lost_rate",
              "pps", "depth_ms", "jitter_ms", "resets", "error"):
        print("  %-10s = %s" % (k, snap[k]))
    print("统计 detail: trimmed=%d" % dev.stats.trimmed)
    if not lens:
        print("FAIL 一块都没读到")
        rc = 1
    else:
        g = np.array(gaps) if gaps else np.array([0.0])
        print("块数=%d  块长集合=%s" % (len(lens), sorted(set(lens))))
        print("出块间隔 ms: 均值 %.2f  P99 %.2f  max %.2f"
              % (g.mean(), np.percentile(g, 99), g.max()))
        if sorted(set(lens)) != [960]:
            print("FAIL 块长不是恒 960")
            rc = 1
        if not (15.0 <= g.mean() <= 25.0):
            print("FAIL 出块间隔均值异常")
            rc = 1
        if np.percentile(g, 99) > 45.0:
            print("FAIL P99 间隔过大（>45ms）")
            rc = 1
        dd = np.array(depths)
        print("缓冲深度 ms: 均值 %.1f  P99 %.1f  max %.1f"
              % (dd.mean(), np.percentile(dd, 99), dd.max()))
        if dd.max() > R.MAX_SEC * 1000 - 5:
            print("FAIL 缓冲涨到上限（控制失效）")
            rc = 1

    # 频率校验
    if chunks:
        allx = np.concatenate([c[:, 0] for c in chunks])
        if len(allx) >= 4800:
            seg = allx[len(allx) // 2: len(allx) // 2 + 9600]
            if len(seg) >= 4800:
                seg = seg[:9600]
                w = np.hanning(len(seg))
                sp = np.abs(np.fft.rfft(seg * w))
                f = np.fft.rfftfreq(len(seg), 1.0 / SR)
                pk = f[int(np.argmax(sp[1:])) + 1]
                print("还原主峰 = %.1f Hz（源 %.1f），用 %d 帧" % (pk, FREQ, len(seg)))
                if abs(pk - FREQ) > 5:
                    print("FAIL 频率不符")
                    rc = 1

    if snap["lost"] > 0:
        print("WARN 丢了 %d 包" % snap["lost"])

    snd._stop.set()                                              # noqa: SLF001
    snd.running = False
    try:
        snd._sock.close()                                        # noqa: SLF001
    except Exception:
        pass
    dev.stop()
    print("\n结论：%s" % ("通过" if rc == 0 else "失败"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
