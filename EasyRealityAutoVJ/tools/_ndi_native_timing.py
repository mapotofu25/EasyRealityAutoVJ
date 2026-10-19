# -*- coding: utf-8 -*-
"""确认 `feed_audio` 那 100ms 到底是不是 cyndilib 的 **native 调用**阻塞。

上一脚本已排除：
  - 锁内 numpy 工作（全部 <=0.1ms）
  - 抢锁本身（0.0002ms）

剩下唯二嫌疑：`a.write_data(planar)` 与 `self._sender.send_audio()`。
本脚本直接对这两个 native 调用单独计时（仍然在无接收端状态下），
并测「环满时的行为」（连续快喂 20 帧，看单次耗时怎么变）。
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import numpy as np  # noqa: E402

SR = 48000
FRAME = 4800          # _FRAME_SAMPLES


def stat(ts):
    a = np.array(ts)
    return a.mean(), np.percentile(a, 99), a.max()


def main():
    import ndi_out

    ndi = ndi_out.NDIOutput("ERAVJ_Native_Test")
    if not ndi._ensure(1280, 720):
        print("FAIL _ensure: %s" % ndi._last_err)
        return 1
    time.sleep(ndi._WARMUP_SECS + 0.2)

    a = ndi._audio
    s = ndi._sender
    planar = np.zeros((2, FRAME), dtype=np.float32)
    try:
        planar[0] = np.sin(2 * np.pi * 440 * np.arange(FRAME) / SR).astype(np.float32) * 0.3
        planar[1] = planar[0]
    except Exception:
        pass

    print("=" * 68)
    print("native 调用单独计时（无接收端，单位 ms）")
    print("=" * 68)

    # ① write_data 单独
    tw = []
    for _ in range(100):
        if hasattr(a, "get_write_available") and not a.get_write_available():
            tw.append(float("nan"))
            continue
        t0 = time.perf_counter()
        try:
            a.write_data(planar)
        except Exception as e:
            tw.append(float("nan"))
            print("   write_data 异常：", type(e).__name__, str(e)[:60])
            break
        tw.append((time.perf_counter() - t0) * 1000.0)
    tw = [x for x in tw if x == x]
    if tw:
        m, p99, mx = stat(tw)
        print("  write_data(2,%d)         均值 %.4f  P99 %.4f  max %.4f  (n=%d)"
              % (FRAME, m, p99, mx, len(tw)))

    # ② send_audio 单独
    ts = []
    for _ in range(100):
        if hasattr(a, "get_write_available") and not a.get_write_available():
            ts.append(float("nan"))
            continue
        t0 = time.perf_counter()
        ok = True
        try:
            if hasattr(s, "send_audio"):
                ok = bool(s.send_audio())
        except Exception as e:
            ok = False
            print("   send_audio 异常：", type(e).__name__, str(e)[:60])
            break
        ts.append((time.perf_counter() - t0) * 1000.0)
        if not ok:
            ts[-1] = float("nan")
    ts = [x for x in ts if x == x]
    if ts:
        m, p99, mx = stat(ts)
        print("  send_audio()             均值 %.4f  P99 %.4f  max %.4f  (n=%d)"
              % (m, p99, mx, len(ts)))
    else:
        print("  send_audio()             全部被 get_write_available 挡下（环满）")

    # ③ ★ 连续快喂：模拟"采集一抖动就连着灌好几块" ⇒ 环满后的行为
    print("\n  连续快喂 30 次 feed_audio（每次 100ms 数据，远超环容量）：")
    per = []
    for i in range(30):
        pcm = np.zeros((FRAME, 2), dtype=np.float32).reshape(-1)
        t0 = time.perf_counter()
        ndi.feed_audio(pcm, SR)
        per.append((time.perf_counter() - t0) * 1000.0)
    m, p99, mx = stat(per)
    print("    feed_audio 耗时：均值 %.2f  P99 %.2f  max %.2f" % (m, p99, mx))
    print("    前 10 次：%s" % ["%.1f" % v for v in per[:10]])
    print("    audio_ok=%d dropped=%d pend=%s"
          % (ndi._audio_ok, ndi._audio_dropped,
             "None" if ndi._pend is None else len(ndi._pend)))

    ndi.close()
    print("\n--- 判读 ---")
    print("  某一项 P99 ≈ 100ms ⇒ **它就是阻塞源**，必须移出锁（或改成异步提交）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
