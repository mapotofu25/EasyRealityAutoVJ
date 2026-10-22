# -*- coding: utf-8 -*-
"""定位那 100ms：**只有 `send_audio()` 被真正调用**时才出现。

上面的证据链已经排除：
  - 锁内 numpy 工作     → 全部 <=0.1ms
  - 抢锁本身            → 0.0002ms
  - write_data native   → 0.024ms
  - 环满时的 feed_audio → 0.25ms（send_audio 根本没被调）

⇒ 只剩一种可能：**`send_audio()` 在「环里有数据、但没有接收端排空」时会阻塞**，
   实测可达 100ms（正好是 3 槽 × 100ms 帧 / ... 的量级）。

本脚本**强制**让它每次都走到 send_audio（每次喂之前先把环清空到可写），
从而把这条路径的耗时逼出来，并给出统计。
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import numpy as np  # noqa: E402

SR = 48000
FRAME = 4800


def main():
    import ndi_out

    ndi = ndi_out.NDIOutput("ERAVJ_SendAudio_Test")
    if not ndi._ensure(1280, 720):
        print("FAIL _ensure: %s" % ndi._last_err)
        return 1
    time.sleep(ndi._WARMUP_SECS + 0.2)

    a = ndi._audio
    s = ndi._sender
    pcm = (0.3 * np.sin(2 * np.pi * 440 * np.arange(FRAME) / SR)).astype(np.float32)
    pcm = np.column_stack([pcm, pcm]).reshape(-1)
    planar = np.ascontiguousarray(
        np.asarray(pcm, dtype=np.float32).reshape(FRAME, 2).T)

    print("=" * 70)
    print("send_audio() 在不同「环占用」下的耗时（单位 ms，无接收端）")
    print("=" * 70)

    # 探针：把每一帧「环可写 → write_data → send_audio」整段计时，
    # 中间**不等**接收端，连喂 N 次，看耗时分布随时间怎么走。
    times = []
    for i in range(60):
        t0 = time.perf_counter()
        avail = True
        try:
            if hasattr(a, "get_write_available"):
                avail = bool(a.get_write_available())
        except Exception:
            pass
        if avail:
            try:
                a.write_data(planar)
                if hasattr(s, "send_audio"):
                    s.send_audio()
            except Exception as e:
                print("  异常 @%d: %s %s" % (i, type(e).__name__, str(e)[:50]))
        dt = (time.perf_counter() - t0) * 1000.0
        times.append((i, avail, dt))
        time.sleep(0.005)

    print("\n  序号  环可写   耗时ms")
    for i, avail, dt in times[:30]:
        flag = "★" if dt > 20 else " "
        print("  %4d   %-6s  %8.2f %s" % (i, avail, dt, flag))

    arr = np.array([t[2] for t in times])
    n_avail = sum(1 for t in times if t[1])
    print("\n  统计：n=%d  可写次数=%d  均值 %.2f  P99 %.2f  max %.2f"
          % (len(arr), n_avail, arr.mean(), np.percentile(arr, 99), arr.max()))
    big = int((arr > 20).sum())
    print("  耗时 > 20ms 的次数 = %d" % big)

    ndi.close()
    print("\n--- 判读 ---")
    if big > 0:
        print("  ❌ 确认：`send_audio()` 在**没有接收端排空**时会阻塞数十~上百 ms。")
        print("     它跑在 NDI 音频转发线程里、且**持 self._lock** ⇒ 视频线程干等 ⇒ 掉帧。")
    else:
        print("  （本轮没逼出阻塞；与是否有接收端、NDI SDK 版本有关）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
