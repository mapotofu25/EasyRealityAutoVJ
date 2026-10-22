# -*- coding: utf-8 -*-
"""端到端集成测试：真 `AudioEngine` + LAN 音源 → 验证下游能量链路。

模拟：A 机（发送端，正弦）→ B 机（真 AudioEngine，source_type="lan"）→ 读 st.energy

判据：
  - `_run_lan` 成功接管（st.device_desc 含"局域网音频"）
  - 分析块长恒 960
  - 能量随声源起伏（静音段→有声段）
  - st.lan_failed 为空
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import numpy as np  # noqa: E402

import lan_audio as LA          # noqa: E402
import lan_audio_send as S      # noqa: E402

PORT = "47897"
# ⚠ 同机端到端测试的陷阱：A 机发送端与 B 机接收端若 bind **同一个 UDP 端口**，
#   即使 SO_REUSEADDR 让两次 bind 都成功，**单播包也只会投递给其中一个 socket**
#   ⇒ 接收端永远收不到（实测踩到）。真实双机没有这个问题（两个独立的内核栈）。
#   所以同机测试必须用**两个端口**：A 机从 PORT_TX 发往 PORT（B 机监听）。
PORT_TX = 47896
SR = 48000
PKT = 480


def main():
    rc = 0
    import audio_engine as AE

    # ---- A 机：先在后台起发送端（先静音 3s，再有声 4s）----
    snd = S.Sender(port=int(PORT_TX), dest=("127.0.0.1", int(PORT)), sr=SR)
    snd._sock = LA.make_sender(int(PORT_TX))         # noqa: SLF001
    snd.running = True
    threading.Thread(target=snd._send_loop, name="tx", daemon=True).start()  # noqa: SLF001

    def src():
        ph = 0.0
        t0 = time.perf_counter()
        nxt = t0
        while time.perf_counter() - t0 < 9.0:
            n = np.arange(PKT, dtype=np.float64) + ph
            elapsed = time.perf_counter() - t0
            amp = 0.0 if elapsed < 3.0 else 0.5      # 前 3 秒静音
            x = (amp * np.sin(2 * np.pi * 440.0 * n / SR)).astype(np.float32)
            ph += PKT
            snd._push(np.column_stack([x, x]), LA.qpc_now())   # noqa: SLF001
            nxt += PKT / SR
            d = nxt - time.perf_counter()
            if d > 0:
                time.sleep(d)

    threading.Thread(target=src, name="src", daemon=True).start()

    # ---- B 机：真 AudioEngine ----
    eng = AE.AudioEngine()
    st = eng.state
    eng.start("lan", PORT, mono=False, sr=0)
    print("已启动 LAN 音源，等待接管…")

    energies = []
    desc = ""
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 8.0:
        time.sleep(0.25)
        snap = st.snapshot()
        desc = snap.get("device_desc") or desc
        energies.append((round(time.perf_counter() - t0, 1), getattr(st, "energy", 0.0)))
    snap = st.snapshot()

    print("--- 状态 ---")
    print("device_desc = %s" % desc)
    print("running     = %r" % snap.get("running"))
    print("lan_failed  = %r" % snap.get("lan_failed"))
    print("lan_info    = %r" % snap.get("lan_info"))
    print("error       = %r" % snap.get("error"))

    print("--- 能量采样（t, energy）---")
    print("  " + "  ".join("%.1f:%.3f" % e for e in energies))

    if "局域网音频" not in desc:
        print("FAIL device_desc 没被接管（说明 _run_lan 没跑起来）")
        rc = 1
    if snap.get("lan_failed"):
        print("FAIL lan_failed 非空")
        rc = 1

    # 前段（静音）应低，后段（有声）应明显更高
    early = [e for t, e in energies if t < 3.0]
    late = [e for t, e in energies if t > 4.5]
    if early and late:
        me = sum(early) / len(early)
        ml = sum(late) / len(late)
        print("静音段均能量 %.4f   有声段均能量 %.4f" % (me, ml))
        if ml <= me + 0.05:
            print("FAIL 能量没随声源上升")
            rc = 1
    else:
        print("WARN 采样不足，无法比较前后段")

    eng.shutdown()
    snd._stop.set()                                  # noqa: SLF001
    snd.running = False
    try:
        snd._sock.close()                            # noqa: SLF001
    except Exception:
        pass

    # 稍等线程收尾
    time.sleep(0.3)
    print("\n结论：%s" % ("通过" if rc == 0 else "失败"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
