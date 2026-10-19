# -*- coding: utf-8 -*-
"""回归测试：局域网音频「宣告源地址被虚拟网卡抢走」根治（2026-10-08）。

## 背景（现场症状，用户原话）

  「我这边软件都没打开，发送端也会显示已 192.168.31.150 握手」
  「选了局域网音频，但状态一直显示『没收到包』」

## 根因（实测证据，见 tools/_archive/_lan_*.py）

B 机 `LanDevice._announce()` 从 `self._sock`（bind 0.0.0.0）发
`255.255.255.255`。本机有穿透工具虚拟网卡（natpierce `10.6.22.1`）时，
**受限广播的出站路由被虚拟网卡抢走** ⇒ A 机看到的源 IP 是 `10.6.22.1`
⇒ `_poll_peer` 把 peer 学成 `10.6.22.1:47811`（不可达）
⇒ `_targets()` 只发它、永不广播 ⇒ 两边永久失联，**双方界面都显示"已握手"**。

## 本测试守住什么（每一条都对应一个曾真实发生的 bug）

  T1 `announce_targets()`：目标**不含 `255.255.255.255`**（那条会被虚拟网卡
     抢源地址），且端口 = 监听端口；并确认**旧的抢包函数 `announce_sockets`
     已彻底移除**（P0 回归根治：它每秒造成 ~330ms 接收黑洞、吞 ~1/3 音频）。
  T2 `trivial_nics()` / `_isolated_nics()` **跳过孤立网卡**（网段里只有自己 =
     虚拟网卡），且 `announce_targets()` 不使用孤立网卡的子网广播。
  T3 `is_unreachable_src()` 判据：拒虚拟/回环，**不拒** 10.x 真实企业网。
  T4 发送端 `_poll_peer` 拒收不可达源地址（闸①）。
  T5 发送端 peer **允许被更新**（不再一次锁死）。
  T6 发送端 peer **心跳超时回退广播**（铁律 #0：绝不"一次失败就永久停"）。
  T7 接收端 `_rx_loop` 对 DATA 也拒不可达源地址（双保险）；`_announce` 用
     `announce_targets`（**不再**用会抢包的 `announce_sockets`）。
  T8 三份文件都能 import（无语法/引用错）。
  T9 `announce_targets()` 全程**只调一次 `lan_hosts()`**（arp -a 耗时关键：
     重复调用会把单次宣告从 ~155ms 拖到 ~427ms）。

用法（make_zip 前必跑）：
    venv\\Scripts\\python.exe tools/_test_lan_announce_fix.py
"""
import os
import re
import socket
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src")
sys.path.insert(0, SRC)

import lan_audio as LA   # noqa: E402
import lan_audio_recv as LR   # noqa: E402

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


def src(p):
    with open(os.path.join(SRC, p), encoding="utf-8") as f:
        return f.read()


print("=" * 74)
print("回归：宣告源地址被虚拟网卡抢走（2026-10-08）")
print("=" * 74)

# ---------------------------------------------------------------- T1
print("\nT1 announce_targets() 目标合法（不含 255.255.255.255、端口=监听端口），"
      "且旧的抢包函数 announce_sockets 已移除")
try:
    tg = LA.announce_targets(LA.DEFAULT_PORT)
    check(bool(tg), "announce_targets() 返回非空（本机应至少有 1 张真实网卡）：%s"
          % (tg[:4],))
    addrs = [a for a, _p in tg]
    ports = set(p for _a, p in tg)
    check(ports == {LA.DEFAULT_PORT},
          "所有目标端口 = 监听端口 %d（对端学到『来源IP:来源端口』，源端口必须"
          "一致）：实际 %s" % (LA.DEFAULT_PORT, sorted(ports)))
    check("255.255.255.255" not in addrs,
          "目标**不含** 255.255.255.255（受限广播会被虚拟网卡抢源地址 —— 老 bug"
          "的根源）")
    check(not hasattr(LA, "announce_sockets"),
          "旧的 announce_sockets() 已彻底移除（它 bind((网卡IP,端口)) 会每秒造成"
          " ~330ms 接收黑洞）")
except Exception as e:                                           # noqa: BLE001
    check(False, "announce_targets() 抛异常：%s" % e)

# ---------------------------------------------------------------- T2
print("\nT2 孤立网卡被跳过，且不进入 announce_targets() 的目标")
try:
    nics = LA._local_ipv4_with_prefix()                          # noqa: SLF001
    skip = set(LA.trivial_nics(LA.DEFAULT_PORT))
    my = set(LA.local_ipv4_all())
    print("     本机网卡=%s  孤立=%s" % (sorted(my), sorted(skip)))
    tg = LA.announce_targets(LA.DEFAULT_PORT)
    tset = set(a for a, _p in tg)
    used_nics = [ip for ip, _p in nics if ip not in skip]
    for ip in used_nics:
        b = LA._subnet_broadcast(ip, 24)                         # noqa: SLF001
        check(b in tset, "非孤立网卡 %s 的子网广播 %s 在目标里" % (ip, b))
    for ip in skip:
        b = LA._subnet_broadcast(ip, 24)                         # noqa: SLF001
        check(b not in tset, "孤立网卡 %s 的子网广播 %s 未被使用" % (ip, b))
except Exception as e:                                           # noqa: BLE001
    check(False, "T2 抛异常：%s" % e)

# ---------------------------------------------------------------- T3
print("\nT3 is_unreachable_src() 判据（拒虚拟、不拒真实私有段）")
CASES = [
    ("192.168.31.150", False), ("192.168.31.96", False),
    ("10.6.22.1", False),        # 10.x 是合法私有段，**绝不能拒**
    ("10.0.0.5", False), ("172.16.5.5", False), ("172.31.1.1", False),
    ("198.18.0.1", True), ("198.19.1.1", True),
    ("127.0.0.1", True), ("169.254.1.1", True),
    ("0.0.0.0", True), ("255.255.255.255", True), ("224.0.0.1", True),
    ("", True),
]
bad = []
for ip, want in CASES:
    got = LA.is_unreachable_src(ip)
    if got != want:
        bad.append("%r 期望拒=%s 实际=%s" % (ip, want, got))
check(not bad, "全部 %d 个用例正确%s" % (len(CASES), ("" if not bad else "  " + "; ".join(bad))))

# ---------------------------------------------------------------- T4/T5/T6
print("\nT4~T6 发送端 _poll_peer / _targets 的三道闸")
snd = src("lan_audio_send.py")
check("is_unreachable_src" in snd,
      "T4 _poll_peer 里调用了 is_unreachable_src（拒不可达源地址）")
check("bad_src" in snd and "self.stats.bad_src += 1" in snd,
      "T4 拒收有计数（stats.bad_src）")
check("self._peer != addr" in snd,
      "T5 peer 允许被**更新**（不再是 `if not self._peer` 一次锁死）")
check("_PEER_STALE_SEC" in snd and "peer_timeouts" in snd,
      "T6 peer 心跳超时回退广播（铁律 #0：绝不永久停）")
# 超时判定必须在 _targets 里（发送路径上）
m = re.search(r"def _targets\(self\):(.*?)def ", snd, re.S)
check(bool(m) and "_PEER_STALE_SEC" in (m.group(1) if m else ""),
      "T6 超时判定位于 _targets()（发送路径即时生效）")
# 确认时刻必须被刷新（否则永远超时，退化成"从不发 peer"）
check("self._peer_confirm_t = time.perf_counter()" in snd,
      "T6 收到 HELLO 时刷新 _peer_confirm_t（否则会永远超时）")

# ---------------------------------------------------------------- T7
print("\nT7 接收端 _rx_loop 对 DATA 也拒不可达源地址；_announce 用 announce_targets")
rcv = src("lan_audio_recv.py")
check("is_unreachable_src" in rcv and "_unreach_rejects" in rcv,
      "接收端有不可达源地址过滤 + 计数")
check("announce_targets" in rcv,
      "_announce 使用 announce_targets（逐网卡目标，源地址由路由表钉死）")
check("announce_sockets" not in rcv,
      "_announce **不再**使用 announce_sockets（P0：它绑同端口会抢 self._sock 的包）")

# ---------------------------------------------------------------- T9
print("\nT9 announce_targets() 全程只调一次 lan_hosts()（arp -a 耗时关键）")
try:
    import time as _t
    calls = [0]
    orig_lh = LA.lan_hosts

    def _counting_lh(*a, **k):
        calls[0] += 1
        return orig_lh(*a, **k)

    LA.lan_hosts = _counting_lh                    # 运行时计数（含 trivial_nics 内部的调用）
    try:
        times = []
        tg = []
        call_cnt = 0
        for _ in range(3):
            calls[0] = 0
            t0 = _t.perf_counter()
            tg = LA.announce_targets(LA.DEFAULT_PORT)
            times.append((_t.perf_counter() - t0) * 1000.0)
            call_cnt = calls[0]
            if call_cnt != 1:
                break
    finally:
        LA.lan_hosts = orig_lh
    check(call_cnt == 1,
          "announce_targets 调用 lan_hosts() %d 次（应为 1）—— 重复调用会把单次"
          "宣告从 ~155ms 拖到 ~427ms" % call_cnt)
    check(bool(tg), "仍返回非空（%d 个目标）" % len(tg))
    # ⚠ `arp -a` 有 OS 级抖动（本机实测 143~199ms）⇒ 取 3 次**最小值**代表代码
    #   的固有成本（抖动只会把耗时抬高，不会压低）—— 这样 gate 既稳又能抓住
    #   "又变回两次 arp" 的回归（那会让最小值也 ~340ms）。
    print("     3 次耗时(ms)：%s" % ["%.0f" % x for x in times])
    check(min(times) < 180.0,
          "单次 announce_targets 最快 %.1f ms < 180 ms（arp -a 抖动已剔除）"
          % min(times))
except Exception as e:                                           # noqa: BLE001
    check(False, "T9 抛异常：%s" % e)

# ---------------------------------------------------------------- T10
print("\nT10 hosts=[] 时不得误判真实网卡孤立（QA 独立复核抓到的 P0）")
try:
    real_lh = LA.lan_hosts
    LA.lan_hosts = lambda *a, **k: []                # 强制 ARP 表为空
    try:
        tg_empty = LA.announce_targets(LA.DEFAULT_PORT)
    finally:
        LA.lan_hosts = real_lh
    addrs_empty = [a for a, _p in tg_empty]
    print("     hosts=[] → announce_targets = %s" % (tg_empty,))
    check(bool(tg_empty),
          "hosts=[] 时 announce_targets() **非空**（不得把所有网卡都误判为孤立 ⇒ "
          "否则回退到被抢源地址的坏路径）")
    expected = set()
    for ip, _p in LA._local_ipv4_with_prefix():                  # noqa: SLF001
        b = LA._subnet_broadcast(ip, 24)                         # noqa: SLF001
        if b:
            expected.add(b)
    check(bool(expected) and expected <= set(addrs_empty),
          "hosts=[] 时含**全部**真实网卡的子网定向广播：期望 %s ⊆ 实际 %s"
          % (sorted(expected), sorted(addrs_empty)))
    check("255.255.255.255" not in addrs_empty,
          "hosts=[] 时目标**不含** 255.255.255.255")

    # 合成用例（与真实网卡无关），直接锁死 `_isolated_nics` 的判据
    real_all = LA.local_ipv4_all
    LA.local_ipv4_all = lambda: ["192.168.31.150", "10.6.22.1", "127.0.0.1"]
    try:
        iso_empty = set(LA._isolated_nics([], [("192.168.31.150", 24),
                                               ("10.6.22.1", 24)]))
        iso_nonempty = set(LA._isolated_nics(["192.168.31.96"],
                                             [("192.168.31.150", 24),
                                              ("10.6.22.1", 24)]))
    finally:
        LA.local_ipv4_all = real_all
    check(iso_empty == set(),
          "hosts=[] 时 _isolated_nics **不判任何网卡孤立**：实际 %s" % sorted(iso_empty))
    check(iso_nonempty == {"10.6.22.1"},
          "hosts 非空时**仍**按网段区分（10.6.22.1 孤立、192.168.31.150 不孤立）"
          "：实际 %s" % sorted(iso_nonempty))
except Exception as e:                                           # noqa: BLE001
    check(False, "T10 抛异常：%s" % e)

# ---------------------------------------------------------------- T11
print("\nT11 _announce 回退分支也不发 255.255.255.255（源地址必被抢）")
try:
    class _FakeSock:
        def __init__(self):
            self.sent = []

        def sendto(self, data, addr=None):
            self.sent.append(tuple(addr))

    dev11 = LR.LanDevice(port=LA.DEFAULT_PORT)
    fs = _FakeSock()
    dev11._sock = fs
    real_at = LA.announce_targets
    LA.announce_targets = lambda *a, **k: []         # 强制走回退分支
    try:
        dev11._announce()
    finally:
        LA.announce_targets = real_at
    print("     回退分支实际发出：%s" % (fs.sent[:6],))
    check("255.255.255.255" not in [t[0] for t in fs.sent],
          "回退分支**不发** 255.255.255.255（它必被虚拟网卡抢成不可达源地址）")
    check(len(fs.sent) >= 1 and
          all(t[0].endswith(".255") for t in fs.sent),
          "回退分支发的是**子网定向广播**（源地址正确）：%s" % fs.sent[:4])
except Exception as e:                                           # noqa: BLE001
    check(False, "T11 抛异常：%s" % e)

# ---------------------------------------------------------------- T8
print("\nT8 静态：三份文件都能 import（无语法/引用错）")
try:
    import lan_audio_recv    # noqa: F401
    import lan_audio_send    # noqa: F401
    check(True, "lan_audio / lan_audio_recv / lan_audio_send 均可 import")
except Exception as e:                                           # noqa: BLE001
    check(False, "import 失败：%s" % e)

print("\n" + "=" * 74)
print("结果：%d 通过 / %d 失败" % (ok_n, fail_n))
print("=" * 74)
sys.exit(0 if fail_n == 0 else 1)
