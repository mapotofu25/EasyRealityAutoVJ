# -*- coding: utf-8 -*-
r"""验证：`bind((具体IP, 监听端口))` 的 socket（**旧 `announce_sockets` 的行为**）
会**抢走** `make_receiver()`（绑 `0.0.0.0:同端口`）的收包。

## ⚠ 2026-10-08 更新（P0 回归根治后）
旧的 `lan_audio.announce_sockets()` 已**删除** —— 它正是本脚本当年要证明的
「抢包」来源（Windows 上「同端口、绑具体 IP 的 socket 优先收包」）。
本脚本现在**手动复刻**它的行为，作为平台特性的**独立佐证**。
修复后的行为验证见同目录 `tools/_test_ann_nosteal.py`。

## 判据
  · 绑具体 IP 的 socket ⇒ `self._sock` 收 0、ann socket 收满 ⇒ 复现成功
  · 这正是 `_announce` **绝不能再建绑同端口的 socket** 的原因。

## 用法
    venv\Scripts\python.exe tools/_test_ann_steal.py [port]
默认 **47897**（避开正式端口 47811）。
"""
import os
import socket
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

import lan_audio as LA                                          # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 47897


def drain(sock):
    sock.setblocking(False)
    n = 0
    while True:
        try:
            sock.recvfrom(65535)
            n += 1
        except BlockingIOError:
            break
        except Exception:                                        # noqa: BLE001
            break
    return n


def main():
    ip = LA.local_ip()
    print("=" * 74)
    print("端口 %d   本机局域网 IP = %s" % (PORT, ip))
    print("=" * 74)

    recv = LA.make_receiver(PORT)
    print("① 接收 socket = %r（= self._sock 的角色）" % (recv.getsockname(),))

    # ★ 手动复刻旧 announce_sockets 的行为：bind((具体IP, 监听端口))，且不关闭。
    ann = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((ip, PORT))
    ann.append(s)
    print("② 手动建 %d 个 bind((具体IP, 端口)) 的 socket"
          "（模拟旧 announce_sockets 的泄漏态）" % len(ann))
    for x in ann:
        print("     %r" % (x.getsockname(),))

    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    tx.bind((ip, 0))
    pkt = LA.pack(LA.TYPE_DATA, 1, 2, 48000, 128, LA.qpc_now(), b"\x00" * 512)

    print("\n③ 从 %s 向 **%s:%d**（接收端真实局域网地址）发 3 个包"
          % (tx.getsockname()[0], ip, PORT))
    drain(recv)
    for x in ann:
        drain(x)
    sent = 0
    for _ in range(3):
        try:
            tx.sendto(pkt, (ip, PORT))
            sent += 1
            time.sleep(0.02)
        except Exception as e:                                    # noqa: BLE001
            print("     发送失败：%s" % e)
    time.sleep(0.3)
    got_recv = drain(recv)
    got_ann = sum(drain(x) for x in ann)
    print("     发出 %d  →  recv(0.0.0.0) 收到 %d   ann(具体IP) 收到 %d"
          % (sent, got_recv, got_ann))

    print("\n" + "=" * 74)
    if got_recv == 0 and got_ann > 0:
        print("结论：✅ 复现成功 —— 绑具体 IP 的 socket **抢走**了 recv 的包。"
              "这就是 `_announce` 绝不能再建绑同端口 socket 的原因。")
    else:
        print("结论：⚠ 未复现（本平台可能行为不同）—— 请人工核对。")
    print("=" * 74)

    for x in ann:
        try:
            x.close()
        except Exception:                                         # noqa: BLE001
            pass
    tx.close()
    recv.close()


if __name__ == "__main__":
    main()
