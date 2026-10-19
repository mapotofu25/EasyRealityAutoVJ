# -*- coding: utf-8 -*-
r"""验证：宣告路径**不再抢走** `self._sock`（绑 0.0.0.0:port）的收包。

## 背景（2026-10-08 P0 回归）
上一版 `LanDevice._announce()` 每秒调 `lan_audio.announce_sockets()`，后者给
**每张真实网卡**各建一个 socket 并 `bind((网卡IP, 监听端口))`。Windows 上
「**同端口下绑具体 IP 的 socket 会优先收包**」（与 Linux 相反）⇒ 这些 socket
在存活期间（含 `arp -a`，实测 ~330ms）把对端发往本机 `具体IP:port` 的 DATA 包
**全部收下并丢弃** ⇒ 每秒一个 ~330ms 的「接收黑洞」⇒ **约 1/3 音频被吞**。

## 本脚本证明什么（两条独立判据）
  ① 现象复现（平台行为）：
     ①a 只有接收 socket 时 → `self._sock` 能收到**全部**发往 `ip:port` 的包。
     ①b 再手动建一个 `bind((ip, port))` 的 socket（= 旧写法）→ 它**抢走**
         `self._sock` 的包（`self._sock` 收 0）。
  ② 修复验证（真实调用路径）：**真实调用** `LanDevice._announce()` 的同时
     持续向 `ip:port` 打 DATA 包，`self._sock` 必须收到**全部**（丢包率 ≈ 0）。

## ★★ 反向可复现（证明本测试有效、不是空过）
把修复**改回旧写法** —— 即让 `LanDevice._announce()` 重新调用
`lan_audio.announce_sockets()`（每张网卡 `bind((网卡IP, 监听端口))`，并在
函数存活期内不关闭）—— 则第 ② 步会出现**约 1/3 的持续丢包**，本脚本**必然失败**。
（第 ① 步在**任何**代码版本上都独立复现同一条平台行为，用于说明 ② 失败的含义。）

## 用法
    venv\Scripts\python.exe tools/_test_ann_nosteal.py [port]
默认 **47896**（刻意避开正式端口 47811，免得干扰正在运行的软件）。
"""
import os
import socket
import sys
import threading
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

import lan_audio as LA                                          # noqa: E402
import lan_audio_recv as LR                                     # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 47896
BLAST_GAP = 0.004          # 每 4ms 打一个包（250 pps）
BLAST_SEC = 2.0            # 持续打 2 秒（≈ 500 包，跨 ~13 次宣告）
LOSS_TOL = 5.0             # 丢包率容忍上限（%）；旧写法实测 ~33%

ok_n = 0
fail_n = 0


def check(cond, msg):
    global ok_n, fail_n
    if cond:
        ok_n += 1
        print("  ✓ %s" % msg)
    else:
        fail_n += 1
        print("  ✗ %s   <<< 失败" % msg)


def drain(nb_sock, only_data=False):
    """把非阻塞 socket 里的包全部取空，返回条数。only_data=True 时只数 DATA。"""
    cnt = 0
    while True:
        try:
            buf, _a = nb_sock.recvfrom(65535)
        except BlockingIOError:
            break
        except OSError:
            break
        if only_data:
            m = LA.unpack(buf)
            if not m or m["type"] != LA.TYPE_DATA:
                continue
        cnt += 1
    return cnt


def blast(tx, pkt, ip, n=5):
    sent = 0
    for _ in range(n):
        try:
            tx.sendto(pkt, (ip, PORT))
            sent += 1
        except OSError as e:
            print("     发送失败：%s" % e)
        time.sleep(0.02)
    return sent


def main():
    ip = LA.local_ip()
    print("=" * 76)
    print("端口 %d   本机局域网 IP = %s" % (PORT, ip))
    print("=" * 76)
    if not ip:
        print("★ 本机没有可用局域网 IP，无法测（跳过）")
        sys.exit(0)

    recv = LA.make_receiver(PORT)                       # === self._sock 的角色 ===
    recv.setblocking(False)
    print("self._sock = %r" % (recv.getsockname(),))

    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    tx.bind((ip, 0))
    pkt = LA.pack(LA.TYPE_DATA, 1, 2, 48000, 128, LA.qpc_now(), b"\x00" * 512)

    # ------------------------------------------------------------------ ①a
    print("\n①a 基线：只有 self._sock 时，发往 %s:%d 的包应全部收到" % (ip, PORT))
    drain(recv)
    sent = blast(tx, pkt, ip, 5)
    time.sleep(0.3)
    got = drain(recv)
    print("     发出 %d → self._sock 收到 %d" % (sent, got))
    check(got == sent,
          "基线：self._sock 收到全部 %d 个包（说明该端口路径本身是通的）" % sent)

    # ------------------------------------------------------------------ ①b
    print("\n①b 复现旧写法：建一个 bind((%s, %d)) 的 socket（保持不关闭）" % (ip, PORT))
    steal = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    steal.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    steal.bind((ip, PORT))
    steal.setblocking(False)
    drain(recv)
    drain(steal)
    sent2 = blast(tx, pkt, ip, 5)
    time.sleep(0.3)
    got_recv2 = drain(recv)
    got_steal2 = drain(steal)
    print("     发出 %d → self._sock 收到 %d   steal(具体IP) 收到 %d"
          % (sent2, got_recv2, got_steal2))
    check(got_recv2 < sent2 and got_steal2 > 0,
          "复现成功：绑具体 IP 的 socket **抢走**了 self._sock 的包"
          "（这正是 P0 黑洞的成因）")
    try:
        steal.close()
    except OSError:
        pass
    time.sleep(0.2)
    drain(recv)

    # ------------------------------------------------------------------ ②
    print("\n② 修复验证：真实调用 LanDevice._announce()，同时持续打 DATA 包")
    dev = LR.LanDevice(port=PORT)
    dev._sock = recv                                    # 让 _announce 用我们的接收 socket

    gstop = threading.Event()
    sent_box = [0]
    got_box = [0]

    def blaster():
        while not gstop.is_set():
            try:
                tx.sendto(pkt, (ip, PORT))
                sent_box[0] += 1
            except OSError:
                pass
            time.sleep(BLAST_GAP)

    def drainer():
        while not gstop.is_set():
            got_box[0] += drain(recv, only_data=True)
            time.sleep(0.001)

    tb = threading.Thread(target=blaster, daemon=True)
    td = threading.Thread(target=drainer, daemon=True)
    tb.start()
    td.start()

    t0 = time.perf_counter()
    ann_calls = 0
    ann_times = []
    while time.perf_counter() - t0 < BLAST_SEC:
        a0 = time.perf_counter()
        dev._announce()                                 # ← 真实宣告路径
        ann_times.append((time.perf_counter() - a0) * 1000.0)
        ann_calls += 1
        drain(recv, only_data=True)                     # 及时清空，避免内核缓冲溢出

    gstop.set()
    tb.join(timeout=1.0)
    td.join(timeout=1.0)
    time.sleep(0.3)
    got_box[0] += drain(recv, only_data=True)

    sent = sent_box[0]
    got = got_box[0]
    loss = (sent - got) / max(1, sent) * 100.0
    ann_avg = sum(ann_times) / max(1, len(ann_times))
    ann_min = min(ann_times) if ann_times else 0.0
    print("     宣告调用 %d 次（最快 %.1f / 平均 %.1f ms/次）" % (
        ann_calls, ann_min, ann_avg))
    print("     发出 DATA %d  → self._sock 收到 %d  → 丢包率 %.2f%%"
          % (sent, got, loss))
    print("     （旧写法每 1 秒有 ~330ms 黑洞 ⇒ 这里应看到 ~33% 丢包）")
    check(sent > 100, "样本足够（发出 %d 个包）" % sent)
    check(loss <= LOSS_TOL,
          "self._sock 收到**全部**发往 %s:%d 的 DATA（丢包率 %.2f%% ≤ %.1f%%）"
          % (ip, PORT, loss, LOSS_TOL))
    # ⚠ `arp -a` 有 OS 级抖动（本机实测 143~199ms）⇒ 取**最快**一次代表代码的固有
    #   成本（抖动只会抬高、不会压低）；"又变回两次 arp" 的回归会让最快值也 ~340ms。
    check(ann_min < 180.0,
          "_announce 最快单次 %.1f ms < 180 ms（arp -a 抖动已剔除）" % ann_min)

    try:
        tx.close()
    except OSError:
        pass
    try:
        recv.close()
    except OSError:
        pass

    print("\n" + "=" * 76)
    if fail_n == 0:
        print("结论：✅ 宣告期间**没有任何 socket 抢走 self._sock 的收包**——"
              "接收黑洞已消除")
    else:
        print("结论：❌ 仍有抢包（或耗时超标）—— 修复未生效，请检查 _announce")
    print("=" * 76)
    sys.exit(0 if fail_n == 0 else 1)


if __name__ == "__main__":
    main()
