# -*- coding: utf-8 -*-
r"""QA 独立验证：局域网音频「宣告抢包」P0 修复（2026-10-08）。

## 与工程师脚本（_test_ann_nosteal.py）**方法上的不同**

工程师的脚本在主线程里**直接调用** `LanDevice._announce()`，并用一个同进程的
发送线程打包。本脚本刻意换一套方法，以独立佐证同一结论：

  ① **真实生命周期路径**：起真实 `LanDevice.start()`，让 `_announce_worker`
     后台线程按 1 秒节拍**自己**跑 `_announce()`（工程师脚本绕过了这个线程）。
  ② **独立进程发包**：用一个**子进程**（本文件的 `--blast` 模式）持续向
     `<本机IP>:<端口>` 打 DATA 包，子进程结束时回报 `SENT <n>`，主进程用
     `LanDevice.stats` 对账 —— 跨进程，排除同进程 GIL/调度耦合。
  ③ **外部客观观测**：并发采样 `netstat -ano -p UDP`，断言测试端口**恒为 1 行**
     绑定（修复后不应出现第二个 bind((网卡IP,端口)) 的 socket）。
  ④ **socket 创建监护**：把 `socket.socket` 换成会**记录 bind() 目标**的子类，
     连续调用 `_announce()` 200 次，断言**没有任何** bind 落在测试端口上
     （比 netstat 采样更严格，不会漏掉"建了就关"的瞬时 socket）。

## 反向复现（证明本脚本不是空过）

    venv\Scripts\python.exe tools/_qa_ann_verify.py --old [port]

`--old` 用 monkeypatch 把 `LanDevice._announce` 换回**旧写法**
（每张网卡建 `bind((网卡IP, 端口))` 的 socket 并在宣告期间持有），
此时上面 ③④ 与实时丢包判据**必须失败** —— 证明脚本真的在测这件事。

## 用法
    venv\Scripts\python.exe tools/_qa_ann_verify.py [port]        # 修复版（期望全过）
    venv\Scripts\python.exe tools/_qa_ann_verify.py --old [port]  # 反向复现（期望抢包判据失败）
    venv\Scripts\python.exe tools/_qa_ann_verify.py --blast IP PORT SEC   # 内部用

默认端口 **47895**（4789x 段，绝不用正式端口 47811）。
"""
import ctypes
import os
import socket
import subprocess
import sys
import threading
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

import lan_audio as LA                                          # noqa: E402
import lan_audio_recv as LR                                     # noqa: E402

OK = [0]
FAIL = [0]


def check(cond, msg):
    if cond:
        OK[0] += 1
        print("  \u2713 %s" % msg)
    else:
        FAIL[0] += 1
        print("  \u2717 %s   <<< 失败" % msg)


def handle_count():
    """当前进程句柄数（Windows）。失败返回 -1。"""
    try:
        k = ctypes.windll.kernel32
        k.GetCurrentProcess.restype = ctypes.c_void_p
        k.GetProcessHandleCount.argtypes = [ctypes.c_void_p,
                                            ctypes.POINTER(ctypes.c_ulong)]
        k.GetProcessHandleCount.restype = ctypes.c_int
        c = ctypes.c_ulong(0)
        ph = k.GetCurrentProcess()
        ok = k.GetProcessHandleCount(ph, ctypes.byref(c))
        return int(c.value) if ok else -1
    except Exception:                                            # noqa: BLE001
        return -1


def udp_bindings(port):
    """netstat 里所有绑在 port 上的 IPv4 UDP 行（字符串列表）。"""
    try:
        r = subprocess.run(["netstat", "-ano", "-p", "UDP"],
                           capture_output=True, text=True,
                           encoding="gbk", errors="replace",
                           **LA._no_window_kwargs())             # noqa: SLF001
    except Exception:                                            # noqa: BLE001
        return []
    out = []
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0].upper() == "UDP" and \
                parts[1].endswith(":%d" % port):
            out.append(line.strip())
    return out


# ------------------------------------------------------------------ 子进程发包
def do_blast(ip, port, sec):
    """向 (ip, port) 持续打 DATA 包，结束打印 SENT <n>。"""
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        tx.bind((ip, 0))
    except OSError:
        pass
    seq = 0
    n = 0
    gap = float(os.environ.get("QA_BLAST_GAP", "0.004"))
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < sec:
        seq = (seq + 1) & 0xFFFF
        pkt = LA.pack(LA.TYPE_DATA, seq, 2, 48000, 128,
                      LA.qpc_now(), b"\x00" * 512)
        try:
            tx.sendto(pkt, (ip, port))
            n += 1
        except OSError:
            pass
        time.sleep(gap)                         # 默认 250 pps；QA_BLAST_GAP 可调
    try:
        tx.close()
    except OSError:
        pass
    print("SENT %d" % n)


# ------------------------------------------------------------------ 旧写法（反向复现）
def install_old_announce(hold_sec=0.25):
    """把 LanDevice._announce 换回"每网卡 bind((网卡IP,端口)) 并持有"的旧写法。"""
    def _old_announce(self):
        if self._sock is None:
            return
        pkt = LA.pack(LA.TYPE_HELLO, 0, 2, self.samplerate or 48000, 0,
                      LA.qpc_now(), b"")
        socks = []
        for ip, _p in LA._local_ipv4_with_prefix():              # noqa: SLF001
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.bind((ip, self.port))          # ← 旧写法的致命点
                socks.append(s)
            except OSError:
                pass
        # 宣告期间持有（旧实现含 arp -a，实测约 150~330ms 的黑洞窗口）
        time.sleep(hold_sec)
        b = LA._subnet_broadcast(LA.local_ip() or "0.0.0.0", 24)  # noqa: SLF001
        for s in socks:
            try:
                s.sendto(pkt, (b or "255.255.255.255", self.port))
            except OSError:
                pass
        for s in socks:
            try:
                s.close()
            except OSError:
                pass
        self._ann_targets_ok = False
    LR.LanDevice._announce = _old_announce


# ------------------------------------------------------------------ 主流程
def main(fixed_mode, port):
    ip = LA.local_ip()
    print("=" * 78)
    print("QA 独立验证   mode=%s   端口=%d   本机局域网 IP=%s"
          % ("FIXED" if fixed_mode else "OLD(反向复现)", port, ip))
    print("=" * 78)
    if not ip:
        print("本机无可用局域网 IP，跳过（不算失败）")
        return

    if not fixed_mode:
        install_old_announce()
        print("[已 monkeypatch] LanDevice._announce -> 旧写法（bind 具体IP+同端口，持有 250ms）\n")

    # ---------------- 1. announce_targets 合法性（端口传播 + 不含受限广播）--
    print("1. announce_targets(port) 返回合法性")
    try:
        for p in (port, 47888):
            tg = LA.announce_targets(p)
            addrs = [a for a, _q in tg]
            ports = set(q for _a, q in tg)
            check(bool(tg), "announce_targets(%d) 非空：%s" % (p, tg[:5]))
            check(ports == {p},
                  "所有目标端口 == 传入端口 %d（若变成随机端口 ⇒ 对端把音频发到没人听的"
                  "端口 ⇒ 症状 pps≈0.7）：实际 %s" % (p, sorted(ports)))
            check("255.255.255.255" not in addrs,
                  "目标不含 255.255.255.255（该受限广播会被虚拟网卡抢源地址）")
    except Exception as e:                                       # noqa: BLE001
        check(False, "announce_targets 抛异常：%r" % e)

    # ---------------- 2. 真实生命周期：netstat 恒 1 行 + 实时丢包 -----------
    print("\n2. 真实 start()（后台宣告线程）+ 子进程打 DATA + netstat 并发采样")
    dev = LR.LanDevice(port=port)
    if not dev.start():
        check(False, "LanDevice.start() 失败：%s" % dev.error)
        return
    time.sleep(0.3)

    max_rows = [0]
    row_samples = []
    ns_stop = threading.Event()

    def ns_sampler():
        while not ns_stop.is_set():
            rows = udp_bindings(port)
            if len(rows) > max_rows[0]:
                max_rows[0] = len(rows)
                row_samples.append(list(rows))
            time.sleep(0.03)

    tns = threading.Thread(target=ns_sampler, daemon=True)
    tns.start()

    p0 = dev.stats.packets
    l0 = dev.stats.lost
    sec = 3.0
    proc = subprocess.Popen([sys.executable, os.path.abspath(__file__),
                             "--blast", ip, str(port), str(sec)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, _err = proc.communicate(timeout=sec + 15)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _err = proc.communicate()
    ns_stop.set()
    tns.join(timeout=1.0)
    time.sleep(0.2)

    sent = 0
    for ln in (out or b"").decode("utf-8", "replace").splitlines():
        if ln.startswith("SENT "):
            sent = int(ln.split()[1])
    dp = dev.stats.packets - p0
    dl = dev.stats.lost - l0
    loss = (sent - dp) / max(1, sent) * 100.0
    print("     子进程发出 DATA = %d ；LanDevice 净收（含~几包自身HELLO回声）= %d"
          % (sent, dp))
    print("     seq 跳变推出丢包 stats.lost 增量 = %d ；估算丢包率 = %.2f%%"
          % (dl, loss))
    print("     netstat 采样到的最大绑定行数 = %d" % max_rows[0])
    if max_rows[0] > 1:
        for r in row_samples[:3]:
            print("       异常采样（>1 行）：%s" % r)
    check(sent > 300, "样本充足（发出 %d 个包）" % sent)
    check(dp >= sent * 0.95,
          "LanDevice 净收 %d ≥ 发出 %d 的 95%%（丢包率 %.2f%%）—— 真实宣告线程运行期间"
          "无接收黑洞" % (dp, sent, loss))
    check(max_rows[0] == 1,
          "netstat 里测试端口**恒为 1 行**绑定（修复后不应有第二个 bind(具体IP,端口) "
          "的 socket）：实测最大 %d 行" % max_rows[0])
    peer = dev.peer
    # ⚠ 同机自测：发送子进程源 IP == 本机 IP ⇒ 落在 _my_ips 里会被 _rx_loop 当"自身回声"
    #   丢弃（这是**正确**的防自听回环行为），所以 peer 通常为空。只有当对端在**另一台**
    #   机器上时才应学到 peer。这里只断言"要么为空(自滤)，要么等于发送方 IP"。
    check(peer == "" or peer.startswith(ip),
          "peer 语义正确：peer=%r（同机自测下为空 = 自身回声被正确丢弃）" % peer)

    # ---------------- 3. 连续 200 次 _announce：无新增 bind + 句柄稳定 ------
    print("\n3. 连续调用 _announce() x200：无 socket bind 到测试端口 + 句柄不增长")
    binds = []
    orig_socket = socket.socket

    class _RecSocket(orig_socket):              # 记录所有 bind 目标
        def bind(self, addr):
            try:
                binds.append(tuple(addr))
            except Exception:                                    # noqa: BLE001
                binds.append(addr)
            return super().bind(addr)

    socket.socket = _RecSocket
    try:
        h0 = handle_count()
        ns_extra = 0
        for i in range(200):
            dev._announce()
            if i % 20 == 0:
                ns_extra = max(ns_extra, len(udp_bindings(port)))
        h1 = handle_count()
    finally:
        socket.socket = orig_socket
    bad_binds = [b for b in binds if len(b) == 2 and b[1] == port]
    print("     200 次 _announce 期间 socket.bind() 调用：%d 次（落在测试端口 %d 的：%d 次）"
          % (len(binds), port, len(bad_binds)))
    print("     句柄数 before=%s after=%s   netstat 采样最大行数=%d"
          % (h0, h1, ns_extra))
    if bad_binds:
        print("       非法 bind：%s" % bad_binds[:5])
    check(not bad_binds,
          "200 次 _announce **没有任何** socket 绑到测试端口（抢包 socket 已彻底消失）")
    check(ns_extra == 1, "循环中 netstat 采样仍恒为 1 行：实测 %d" % ns_extra)
    if h0 > 0 and h1 > 0:
        check(h1 - h0 <= 8,
              "句柄数不增长（%d -> %d，容差 8）" % (h0, h1))
    else:
        print("     (句柄计数不可用，跳过 —— 标注为未验证项)")

    dev.stop()
    time.sleep(1.2)

    # ---------------- 4. 边界：lan_hosts() 为空 --------------------------
    print("\n4. 边界：lan_hosts() 返回 [] 时 announce_targets 仍非空且不含受限广播"
          "（P0：不得误判真实网卡孤立）")

    class _FakeSock:
        def __init__(self):
            self.sent = []

        def sendto(self, data, addr=None):
            self.sent.append(tuple(addr))

    orig_lh = LA.lan_hosts
    LA.lan_hosts = lambda *a, **k: []                            # 强制空
    try:
        # ★★ 2026-10-08 P0（QA 抓到）：hosts=[] 时**不得**把所有网卡判孤立 ⇒
        #    announce_targets() 必须仍非空、且不含 255.255.255.255。
        tg = LA.announce_targets(port)
        addrs = [a for a, _q in tg]
        print("     hosts=[] → announce_targets = %s" % (tg,))
        check(bool(tg),
              "hosts=[] 时 announce_targets() **非空**（否则回退到被抢源地址的坏路径）")
        check("255.255.255.255" not in addrs,
              "hosts=[] 时目标**不含** 255.255.255.255")

        dev4 = LR.LanDevice(port=port)
        fs = _FakeSock()
        dev4._sock = fs
        raised = ""
        try:
            dev4._announce()
        except Exception as e:                                   # noqa: BLE001
            raised = repr(e)
        check(not raised, "lan_hosts()=[] 时 _announce() 不抛异常%s"
              % ("" if not raised else "：%s" % raised))
        check(len(fs.sent) >= 1,
              "仍发出 ≥1 个包：实际 %d 个 -> %s" % (len(fs.sent), fs.sent[:4]))
        subnet_bcast = [t for t in fs.sent
                        if t[0].endswith(".255") and t[0] != "255.255.255.255"]
        check(bool(subnet_bcast),
              "发出的是**子网定向广播**（源地址正确）：%s" % subnet_bcast[:3])
        check("255.255.255.255" not in [t[0] for t in fs.sent],
              "_announce 在 hosts=[] 时也**不发** 255.255.255.255")
        check(dev4._ann_targets_ok,
              "走的是 announce_targets 主力路径（非回退分支）：_ann_targets_ok=%s"
              % dev4._ann_targets_ok)
    except Exception as e:                                       # noqa: BLE001
        check(False, "边界4 抛异常：%r" % e)
    finally:
        LA.lan_hosts = orig_lh

    # ---------------- 5. 边界：peer_hint 非空 ---------------------------
    print("\n5. 边界：peer_hint 非空 -> 定向单播")
    try:
        dev5 = LR.LanDevice(port=port)
        dev5.peer_hint = "10.0.0.7"
        dev5.peer_hint_port = 47811
        fs5 = _FakeSock()
        dev5._sock = fs5
        dev5._announce()
        check(("10.0.0.7", 47811) in fs5.sent,
              "向 peer_hint 定向单播 %s 已发出" % (("10.0.0.7", 47811),))
        others = [t for t in fs5.sent if t != ("10.0.0.7", 47811)]
        print("     除定向单播外，另发往 %d 个目标（子网广播/同网段单播）：%s"
              % (len(others), others[:4]))
        note = ("说明（已与 team-lead 确认）：peer_hint 非空时**有意**仍再发一轮"
                "子网广播/同网段单播 —— 成本极低、能让对方更快学到地址，属预期行为。")
        print("     %s" % note)
    except Exception as e:                                       # noqa: BLE001
        check(False, "边界5 抛异常：%r" % e)

    # ---------------- 6. 边界：start -> stop 后宣告线程停止 --------------
    print("\n6. 边界：start() -> stop() 后 lan-audio-ann 线程停止（不残留、不再发包）")
    try:
        dev6 = LR.LanDevice(port=port)
        dev6.start()
        time.sleep(0.4)
        alive_before = [t.name for t in threading.enumerate()
                        if t.name == "lan-audio-ann"]
        dev6.stop()
        time.sleep(1.5)
        alive_after = [t.name for t in threading.enumerate()
                       if t.name == "lan-audio-ann"]
        binds_left = udp_bindings(port)
        print("     stop 前存活 ann 线程=%s ；stop 后=%s ；netstat 残留=%d 行"
              % (alive_before, alive_after, len(binds_left)))
        check(len(alive_before) >= 1, "start() 后 lan-audio-ann 线程在跑")
        check(len(alive_after) == 0, "stop() 后 lan-audio-ann 线程已退出")
        check(len(binds_left) == 0, "stop() 后端口无残留绑定（socket 已关闭）")
    except Exception as e:                                       # noqa: BLE001
        check(False, "边界6 抛异常：%r" % e)

    # ---------------- 结论 -------------------------------------------
    print("\n" + "=" * 78)
    if fixed_mode:
        if FAIL[0] == 0:
            print("QA 结论：✅ 独立方法全部通过 —— 宣告不再抢走 self._sock 的包")
        else:
            print("QA 结论：❌ 有 %d 项失败 —— 见上" % FAIL[0])
    else:
        # 反向复现：期望抢包相关判据失败
        steal_ok = (max_rows[0] == 1)
        print("反向复现结论：%s（旧写法下 netstat 最大行数=%d，期望 >1；"
              "丢包率=%.2f%%，期望显著>5%%）"
              % ("✅ 验证脚本有效（旧写法被当场抓出）" if
                 (max_rows[0] > 1 or loss > 5.0) else
                 "⚠ 未复现（请人工核对）", max_rows[0], loss))
    print("=" * 78)
    sys.exit(0 if FAIL[0] == 0 else 1)


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--blast":
        do_blast(args[1], int(args[2]), float(args[3]))
    else:
        fixed = not (args and args[0] == "--old")
        rest = [a for a in args if a != "--old"]
        p = int(rest[0]) if rest else 47895
        main(fixed, p)
