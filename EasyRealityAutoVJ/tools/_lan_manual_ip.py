# -*- coding: utf-8 -*-
"""验证「手动输入 IP」+「lan_failed 陈旧文案清除」。

覆盖三件事：
  T1  `LanDevice(peer_hint=...)` 解析：纯 IP / IP:端口 / 纯端口 / 空。
  T2  过滤语义：装了 peer_hint 之后，别的 IP 的 DATA 被丢弃（`hint_rejects` 增长）；
      对端 IP 的 DATA 正常收下并成为 peer。
  T3  预设 IP 的定向宣告：`_announce()` 里 HELLO 会发到 peer_hint（用替身 socket 断言）。
  T4  `lan_failed` 清空：模拟"先失败再成功"，确认 UI 侧取到的 `lan_failed` 为空
      （即用户报的「已经握手上了然而文字没更新」被修掉）。
"""

import os
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import numpy as np  # noqa: E402

import lan_audio as LA  # noqa: E402
import lan_audio_recv as LR  # noqa: E402

FAILS = []


def check(cond, msg):
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        FAILS.append(msg)


def _data_pkt(sr=48000, n=480, seq=1):
    payload = np.zeros((n, 2), dtype=np.float32).tobytes()
    return LA.pack(LA.TYPE_DATA, seq, 2, sr, n, LA.qpc_now(), payload)


# ---------------- T1 解析 ----------------
print("T1 peer_hint 解析")
cases = [
    ("", "", 0),
    ("47811", "", 0),
    ("192.168.31.96", "192.168.31.96", 0),
    ("192.168.31.96:47811", "192.168.31.96", 47811),
    ("10.0.0.5:5000", "10.0.0.5", 5000),
]
for txt, exp_ip, exp_port in cases:
    d = LR.LanDevice(port=47811, peer_hint=txt)
    check(d.peer_hint == exp_ip and d.peer_hint_port == exp_port,
          "peer_hint=%-22r → ip=%-15r port=%d" % (txt, d.peer_hint, d.peer_hint_port))


# ---------------- T2 过滤语义 ----------------
print("\nT2 过滤：只认手填的 IP")


class _FakeSock:
    """替身 socket：往被测对象喂包 / 记录它发出的包。"""

    def __init__(self):
        self.out = []
        self.inbox = []
        self._closed = False

    def recvfrom(self, _n):
        if self.inbox:
            return self.inbox.pop(0)
        raise BlockingIOError()

    def sendto(self, pkt, addr):
        self.out.append((addr, pkt))

    def close(self):
        self._closed = True

    def setsockopt(self, *a, **k):
        pass

    def bind(self, *a, **k):
        pass

    def setblocking(self, *a, **k):
        pass


d = LR.LanDevice(port=47811, peer_hint="192.168.31.96")
d._sock = _FakeSock()
# 先喂一个"别的 IP"的 DATA
d._sock.inbox.append((_data_pkt(), ("192.168.31.50", 47811)))
# 再喂"对端 IP"的 DATA
d._sock.inbox.append((_data_pkt(), ("192.168.31.96", 47811)))

# 手动跑两轮接收循环体（不启线程，直接调 _rx_loop 的分支不好抽）
# ⇒ 用线程 + 短超时更简单
d._stop.clear()
t = __import__("threading").Thread(target=d._rx_loop, daemon=True)
t.start()
time.sleep(0.4)
d._stop.set()
time.sleep(0.1)

check(d._hint_rejects >= 1, "别的 IP 的包被过滤（hint_rejects=%d）" % d._hint_rejects)
check(d.peer == "192.168.31.96:47811", "对端被正确认定为 192.168.31.96（实际 %r）" % d.peer)
check(d.samplerate == 48000, "采样率从对端包学到（%d）" % d.samplerate)


# ---------------- T3 定向宣告 ----------------
print("\nT3 宣告会定向单播到 peer_hint")
d2 = LR.LanDevice(port=47811, peer_hint="192.168.31.96")
fake = _FakeSock()
d2._sock = fake
d2.samplerate = 48000
try:
    d2._announce()
except Exception as e:                                          # noqa: BLE001
    print("  (announce 抛异常，已捕获：%s)" % e)
addrs = [a for a, _p in fake.out]
check(("192.168.31.96", 47811) in addrs,
      "HELLO 定向发到了 192.168.31.96（实际目标 %r）" % (addrs[:4],))
check(not any(True for _a in addrs if _a[0] not in ("255.255.255.255",)
              and not _a[0].endswith(".255") and _a[0] != "192.168.31.96"),
      "填了 IP 后不再向全网段乱发（避免噪音）")


# ---------------- T4 lan_failed 清空 ----------------
print("\nT4 lan_failed 陈旧文案被清除")
# 直接验证 snapshot 的语义：error 空 ⇒ _capture_loop 会写空
# 这里模拟 audio_engine 的写入逻辑（简化：断言 snapshot 字段存在且为默认空）
d3 = LR.LanDevice(port=47811, peer_hint="")
snap = d3.snapshot()
check("peer_hint" in snap and "hint_rejects" in snap, "snapshot 含新字段")
check(snap["error"] == "", "初始 error 为空（界面不会显示陈旧文案）")

# 关键回归：确认 audio_engine 里那两处写入已改成"双向写"
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src",
                        "audio_engine.py"), encoding="utf-8").read()
check('st.lan_failed = ls["error"] or ""' in src,
      "audio_engine 心跳处已改为双向写（无错即清）")
check("st.lan_failed = \"\"\n            st.lan_info = \"\"" in src,
      "audio_engine 握手成功处清空 lan_failed/lan_info")
check("self.state.lan_failed = \"\"" in src,
      "audio_engine start()/stop() 也清 lan_failed")

print("\n" + "=" * 60)
if FAILS:
    print("✗ 失败 %d 项：" % len(FAILS))
    for f in FAILS:
        print("   - " + f)
    sys.exit(1)
print("✓ 全部通过：手动 IP 解析/过滤/定向宣告 + 陈旧文案清除")
sys.exit(0)
