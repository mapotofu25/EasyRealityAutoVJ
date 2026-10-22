# -*- coding: utf-8 -*-
"""验证「B 机主动宣告」能救活「A 机点对点模式」的场景。

现场真实拓扑：
  A 机(192.168.31.96) 用的是**旧版发送端**，而且很可能在界面里手填了目标 IP
  ⇒ 它只往那个 IP 发，不广播；B 机被动等 = 永远收不到。

本测试模拟"A 机发到一个**错的**地址"（黑洞），只有 B 机主动 HELLO 才能让它回连。
用同机两个端口避免 UDP 同端口单播只投递一个 socket 的坑。
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

PORT_RX = 47882          # B 机监听
PORT_TX = 47883          # A 机发送源端口
BLACKHOLE = 47899        # A 机被配置成的"错误目标"


def main():
    rc = 0
    dev = R.LanDevice(port=PORT_RX)
    if not dev.start():
        print("FAIL 接收端启动失败: %s" % dev.error)
        return 1

    # A 机：配成点对点发到黑洞地址（模拟旧版/手填错地址）
    snd = S.Sender(port=PORT_TX, dest=("127.0.0.1", BLACKHOLE), sr=48000)
    snd._sock = LA.make_sender(PORT_TX)                          # noqa: SLF001
    snd.running = True
    threading.Thread(target=snd._send_loop, name="tx", daemon=True).start()

    # 声源：持续推数据（模拟"演出中永远有音频"）
    stop = threading.Event()

    def src():
        ph = 0.0
        nxt = time.perf_counter()
        while not stop.is_set():
            n = np.arange(480, dtype=np.float64) + ph
            x = (0.5 * np.sin(2 * np.pi * 440.0 * n / 48000)).astype(np.float32)
            ph += 480
            snd._push(np.column_stack([x, x]), LA.qpc_now())     # noqa: SLF001
            nxt += 0.01
            d = nxt - time.perf_counter()
            if d > 0:
                time.sleep(d)

    threading.Thread(target=src, name="src", daemon=True).start()

    print("A 机点对点发往黑洞 %d；观察 B 机能否靠主动宣告把 A 机拉回来…" % BLACKHOLE)
    peer_seen = None
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 12.0:
        s = dev.snapshot()
        if s["packets"] > 0 and s["peer"]:
            peer_seen = s["peer"]
            break
        time.sleep(0.2)

    s = dev.snapshot()
    print("\n--- 结果 ---")
    print("  B 机收到包数 = %d" % s["packets"])
    print("  B 机认定的对端 = %r" % s["peer"])
    print("  A 机学到的对端 = %r" % (snd._peer,))                 # noqa: SLF001
    print("  A 机 mode = %s" % snd.snapshot()["mode"])
    if peer_seen:
        print("  ✅ A 机被拉回来了，B 机收到音频（耗时 %.1fs）" % (time.perf_counter() - t0))
    else:
        print("  ❌ 没拉回来")
        rc = 1

    # 反向验证：再过 2 秒应持续有包
    n1 = s["packets"]
    time.sleep(2.0)
    n2 = dev.snapshot()["packets"]
    print("  2 秒内新增 %d 包（应约 200）" % (n2 - n1))
    if n2 - n1 < 100:
        print("  FAIL 数据流没有持续")
        rc = 1

    stop.set()
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
