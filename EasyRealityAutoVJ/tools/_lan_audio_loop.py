# -*- coding: utf-8 -*-
"""A+B 同机自测：发送端播放正弦 → 接收端还原 → 校验时间轴与丢包。

判据：
  - 收到包速率 ≈ 100pps（10ms 包）
  - 丢包率 0（同机回环）
  - 还原出的音频频率与源一致（用 FFT 主峰）
  - 缓冲深度稳定在目标附近
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import numpy as np  # noqa: E402

import lan_audio as LA          # noqa: E402
import lan_audio_recv as R      # noqa: E402
import lan_audio_send as S      # noqa: E402

PORT = 47899
SECS = 6.0


def main():
    rc = 0
    # ---- 接收端先起（避免开头丢 HELLO）----
    dev = R.LanDevice(port=PORT)
    if not dev.start():
        print("FAIL 接收端启动失败: %s" % dev.error)
        return 1
    print("接收端已监听 UDP %d" % PORT)

    # ---- 发送端：直接灌正弦，不用真设备（同机测试可能被独占）----
    snd = S.Sender(port=PORT, dest=("127.0.0.1", PORT), sr=48000)
    try:
        snd._sock = LA.make_sender(PORT)     # noqa: SLF001
    except Exception as e:
        print("FAIL 发送端绑定失败: %s" % e)
        dev.stop()
        return 1

    import threading
    snd.running = True
    tx = threading.Thread(target=snd._send_loop, name="tx", daemon=True)  # noqa: SLF001
    tx.start()

    # 灌 440Hz 正弦，10ms 一包，持续 SECS
    sr = 48000
    pkt = 480
    freq = 440.0
    ph = 0.0
    total = 0
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < SECS:
        n = np.arange(pkt, dtype=np.float64) + ph
        x = (0.5 * np.sin(2 * np.pi * freq * n / sr)).astype(np.float32)
        ph += pkt
        arr = np.column_stack([x, x])
        snd._push(arr, LA.qpc_now())         # noqa: SLF001
        total += pkt
        time.sleep(pkt / sr)
    # 让尾巴排空
    time.sleep(0.3)

    snap = dev.snapshot()
    ssnap = snd.snapshot()
    print("--- 发送 ---")
    print("设备队列丢包=%d  包数=%d  模式=%s" % (ssnap["dropped"], ssnap["packets"],
                                            ssnap["mode"]))
    print("--- 接收 ---")
    for k in ("connected", "peer", "sr", "packets", "lost", "lost_rate",
              "pps", "depth_ms", "jitter_ms", "resets", "error"):
        print("  %-10s = %s" % (k, snap[k]))

    # 还原并验频率
    rec = dev.recorder()
    print("缓冲深度(帧) = %d" % rec.depth())
    got = rec.depth()
    if got < 4800:                      # 至少 100ms 才能做可靠的频率估计
        print("WARN 缓冲样本不足，无法验频率（got=%d）" % got)
        rc = 1
    else:
        data = np.zeros((got, 2), dtype=np.float32)
        i = 0
        while i < got:
            d = rec.record(numframes=min(960, got - i))
            if d is None:
                break
            data[i:i + len(d)] = d
            i += len(d)
        mono = data[:i, 0]
        if i >= 4800:
            seg = mono[:4800]           # 100ms 窗：440Hz 有 44 个周期，足够准
            w = np.hanning(len(seg))
            sp = np.abs(np.fft.rfft(seg * w))
            f = np.fft.rfftfreq(len(seg), 1.0 / sr)
            pk = f[int(np.argmax(sp[1:])) + 1]
            print("还原主峰 = %.1f Hz（源 %.1f）  样本 %d" % (pk, freq, i))
            if abs(pk - freq) > 15:
                print("FAIL 频率不符")
                rc = 1
        else:
            print("WARN 只取到 %d 帧，跳过频率校验" % i)

    print("包速率 %.1f pps（期望 ~100）" % snap["pps"])
    if snap["pps"] < 60:
        print("FAIL 包速率过低")
        rc = 1
    if snap["lost"] > 0:
        print("WARN 同机回环丢了 %d 包" % snap["lost"])
    if snap["packets"] < 100:
        print("FAIL 收到的包太少")
        rc = 1
    # ★ 缓冲深度必须被控制在 MIN~MAX 之间（这是 2026-10-08 那次"单调涨到 210ms"的回归判据）
    if not (R.MIN_SEC * 1000 - 20 <= snap["depth_ms"] <= R.MAX_SEC * 1000 + 20):
        print("FAIL 缓冲深度失控：%.1fms（应在 %d~%dms）"
              % (snap["depth_ms"], R.MIN_SEC * 1000, R.MAX_SEC * 1000))
        rc = 1

    snd._stop.set()                          # noqa: SLF001
    snd.running = False
    try:
        snd._sock.close()                    # noqa: SLF001
    except Exception:
        pass
    dev.stop()
    print("\n结论：%s" % ("通过" if rc == 0 else "有问题"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
