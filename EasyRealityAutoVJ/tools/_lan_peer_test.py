# -*- coding: utf-8 -*-
"""回归：接收端「对端 IP」必须只认 **发 DATA 的那台机器**，不能认：
  - 本机自己的广播回声（TUN 虚拟网卡会把它绕回来，曾显示 198.18.0.1）；
  - 网段内其它设备的 HELLO 广播；
  - 发送端在学到对端前广播出来的 DATA（也要丢，否则自听回环）。

## 三个用例
① **噪声先行**：先灌一堆「别的设备」的 HELLO + 本机回声的 HELLO，再让真发送端起流
   ⇒ peer 必须 == 真发送端地址，绝不能是噪声来源。
② **对端可更新**：发送端换地址重连 ⇒ peer 跟着变（不是"只认第一个"锁死）。
③ **自 DATA 丢弃**：本机回声的 DATA 不得被当成对端音频喂进缓冲（packets 计数仍涨，
   但 peer 为空、缓冲深度不涨）。

用法：python tools/_lan_peer_test.py
"""
import os
import socket
import sys
import threading
import time

import numpy as np

sys.path.insert(0, r"P:\ERAVJ\AutoVJ\src")

import lan_audio as LA                                          # noqa: E402
import lan_audio_recv as LR                                     # noqa: E402

PORT = 47833
MYPORT = 47834


def _sock(bind_port, bind_ip="0.0.0.0", broadcast=True):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if broadcast:
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except Exception:
            pass
    s.bind((bind_ip, bind_port))
    s.setblocking(False)
    return s


# 用 127.0.0.2 / 127.0.0.3 当"对端"：它们**不在** local_ipv4_all() 里（不是本机身份），
# 却被系统当作回环可用地址 ⇒ 能真实模拟"另一台机器"，用来验证 peer 学习。
# （与双机等价测试 `tools/_lan_twomachine_test.py` 同一手法。）
PEER_A = "127.0.0.2"
PEER_B = "127.0.0.3"


def _hello_pkt():
    return LA.pack(LA.TYPE_HELLO, 0, 2, 48000, 0, LA.qpc_now(), b"")


def _data_pkt(seq):
    n = 480
    payload = np.zeros((n, 2), dtype=np.float32).tobytes()
    return LA.pack(LA.TYPE_DATA, seq & 0xFFFF, 2, 48000, n, LA.qpc_now(), payload)


def main():
    ok = True

    # ---------- 用例① 噪声先行 ----------
    print("① 先灌噪声（别的设备的 HELLO），再让真发送端起流")
    dev = LR.LanDevice(port=PORT)
    assert dev.start(), dev.error
    noise = _sock(MYPORT, PEER_B)
    # 模拟"别的设备"：发 HELLO（不是 DATA）
    for i in range(5):
        noise.sendto(_hello_pkt(), ("127.0.0.1", PORT))
        time.sleep(0.02)
    time.sleep(0.3)
    peer_after_noise = dev.peer
    print("   噪声后 peer = %r（应仍为空）" % peer_after_noise)
    if peer_after_noise:
        print("   ✗ 噪声被当成了对端")
        ok = False
    else:
        print("   ✓ 噪声未被当成对端")

    # 真发送端起流
    real = _sock(MYPORT + 1, PEER_A)
    for i in range(60):
        real.sendto(_data_pkt(i), ("127.0.0.1", PORT))
        time.sleep(0.01)
    time.sleep(0.5)
    peer_real = dev.peer
    print("   真 DATA 起流后 peer = %r" % peer_real)
    if peer_real.startswith(PEER_A + ":") and peer_real.endswith(str(MYPORT + 1)):
        print("   ✓ peer 正确锁定真发送端")
    else:
        print("   ✗ peer 不对（期望 %s:%d）" % (PEER_A, MYPORT + 1))
        ok = False

    # ---------- 用例② 对端可更新 ----------
    print("")
    print("② 发送端换地址重连 ⇒ peer 应跟着更新")
    real2 = _sock(MYPORT + 2, PEER_B)
    for i in range(60):
        real2.sendto(_data_pkt(i), ("127.0.0.1", PORT))
        time.sleep(0.01)
    time.sleep(0.5)
    peer_new = dev.peer
    print("   换源后 peer = %r" % peer_new)
    if peer_new.endswith(str(MYPORT + 2)):
        print("   ✓ peer 跟随更新")
    else:
        print("   ✗ peer 没更新（仍是 %r）" % peer_new)
        ok = False

    dev.stop()
    try:
        noise.close(); real.close(); real2.close()
    except Exception:
        pass

    # ---------- 用例③ 自 DATA 丢弃 ----------
    print("")
    print("③ 本机回声的 DATA 必须丢弃（不认对端、不喂缓冲）")
    dev3 = LR.LanDevice(port=PORT + 10)
    assert dev3.start(), dev3.error
    echo = _sock(MYPORT + 10, "127.0.0.1")   # ★ 源 IP = 本机（127.0.0.1 在 _my_ips 里）
    for i in range(80):
        echo.sendto(_data_pkt(i), ("127.0.0.1", PORT + 10))
        time.sleep(0.01)
    time.sleep(0.6)
    s3 = dev3.snapshot()
    print("   packets=%d  peer=%r  depth_ms=%.1f"
          % (s3["packets"], s3["peer"], s3["depth_ms"]))
    if s3["peer"] == "":
        print("   ✓ 自己的回声没被认成对端")
    else:
        print("   ✗ 自己的回声被认成对端 %r" % s3["peer"])
        ok = False
    if s3["depth_ms"] < 1.0:
        print("   ✓ 自己的 DATA 没被喂进缓冲（深度 ≈0）")
    else:
        print("   ✗ 自己的 DATA 进了缓冲（深度 %.1fms）—— 会自听回环" % s3["depth_ms"])
        ok = False
    dev3.stop()
    try:
        echo.close()
    except Exception:
        pass

    print("")
    print("结果：" + ("全部通过 ✓" if ok else "未通过 ✗"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
