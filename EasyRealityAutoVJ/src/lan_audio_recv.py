# -*- coding: utf-8 -*-
"""局域网音频接收（B 机）—— UDP 收包 → 抖动缓冲 → 供 `audio_engine` 消费。

## 为什么要有抖动缓冲

UDP 包到达时刻**不是**音频时刻：网络会抖动（本机 loopback 也可能 ±2ms），
A 机渲染卡一下可能晚到 50ms。若收到就立刻交出去，下游看到的时间轴就是抖的
—— 表现和"咔哒"一模一样（我们刚在 NDI 那条链上花了一整天修这个）。

正确做法：**先垫一段缓冲**，让"消费速度"与"到达速度"解耦；只在缓冲**快空/快满**时
用极小的速率微调（±0.2%）拉回来。这就是所有网络音频的标准做法。

### 目标深度怎么定
我们用 A 机发来的 QPC 做**真实延迟测量**（自研协议的红利）：
    延迟 ≈ (B 机当前 QPC − 换算到同一时基的 A 机 QPC) − 已缓冲时长
但两台机器的 QPC **时基不同**（各自的开机时刻），不能直接减。
所以采用**相对量**：持续记录「收到的包 QPC − 当时的本地 QPC」这个差值，
它在没有漂移时是常数；它的**变化量**就是网络延迟的变化。
用它的滑动最小值做基准（最小延迟 = 网络最空时），
    drift = 当前差值 − 最小差值
    drift > 阈值 ⇒ 网络变堵/对端变慢 ⇒ 加一点点缓冲
    drift < 0    ⇒ 比基准还快（对端时钟更快）⇒ 减缓冲
这是成熟的"自适应播放"思路，不需要两边对钟。

## 接口约定
`LanDevice` 刻意做成 `soundcard` 的 recorder 形状，让
`audio_engine._capture_loop` 原样复用（与 `asio_engine.AsioDevice` 同一套路）：
    dev = LanDevice(port=47811)
    dev.start()                       # 起接收线程
    with dev.recorder(samplerate=0, channels=2) as rec:
        data = rec.record(numframes=960)   # 返回 (n,2) float32，或 None
"""

import collections
import os
import threading
import time

import numpy as np

import lan_audio as LA

# 目标缓冲深度（秒）。10ms 包 ⇒ 6 包。
# ⚠ 太小（<3 包）网络一抖就断；太大（>20 包）延迟明显、VJ 跟不上音乐。
TARGET_SEC = 0.060
MIN_SEC = 0.020
MAX_SEC = 0.200


class RecvStats:
    def __init__(self):
        self.packets = 0
        self.lost = 0                 # 由 seq 跳变推出的丢包数
        self.resets = 0               # 缓冲被清空次数（断流后重连）
        self.last_seq = -1
        self.t0 = time.perf_counter()
        self.last_rx = 0.0            # 最近一次收到包的时间
        self.jitter_ms = 0.0          # 最近一次延迟相对基准的偏移
        self.depth_ms = 0.0           # 当前缓冲深度
        self.trimmed = 0              # 因缓冲过深而被丢帧的次数
        self.rx_pps = 0.0
        self._pps_t0 = time.perf_counter()
        self._pps_n = 0
        # 去重（防单播+广播双份）
        self._last_dup = -1
        self._dup_n = 0
        self._dup_t = 0.0


class _LanRecorder:
    """`record(numframes)` 接口的抖动缓冲读取器（形状同 soundcard recorder）。"""

    def __init__(self, owner, samplerate=0, channels=2, blocksize=None):
        self._o = owner
        self.channels = int(channels or 2)
        self.samplerate = int(samplerate or owner.samplerate or 48000)
        self.last_qpc = 0
        self._buf = np.zeros((0, 2), dtype=np.float32)
        self._lock = threading.Lock()

    # -- 供 owner 的接收线程调用 --
    def feed(self, arr):
        with self._lock:
            self._buf = arr if self._buf.shape[0] == 0 else np.concatenate(
                [self._buf, arr], axis=0)

    def clear(self):
        with self._lock:
            self._buf = np.zeros((0, 2), dtype=np.float32)

    def depth(self):
        with self._lock:
            return self._buf.shape[0]

    def record(self, numframes=1024):
        """取 numframes 帧。没攒够就返回 None（**绝不填零** —— 与 wasapi_capture 同一条约定）。

        ## 缓冲深度控制就在这里（唯一的控制点）

        设计：目标是让缓冲深度稳定在 `owner.target_sec`。
        - 深度**过大**（对端时钟偏快 / 网络突发）⇒ 一次多丢几帧，
          把它拉回来。丢帧的听感损失是「极其微小的跳变」，远好于延迟一直涨。
        - 深度**过小**（对端时钟偏慢）⇒ 返回 None 让上层等一等，
          上层循环会 `sleep` 后重试，自然把深度垫回来。
        ⚠ 绝不用「填零/重复帧」来补 —— 那正是 NDI 咔哒的成因，不能重蹈。
        """
        n = int(numframes or 0)
        if n <= 0:
            n = 480
        sr = self.samplerate or 48000
        tgt = int(self._o.target_sec * sr)
        hi = int(tgt * 1.6)              # 超过目标的 1.6 倍 ⇒ 丢
        with self._lock:
            have = self._buf.shape[0]
            if have < n:
                # 垫得还不够：不返回数据（上层会重试）
                return None
            # 深度过大：丢弃多余的（只丢一次到目标，避免连续丢）
            if have > hi and have > n:
                drop = min(have - tgt, have - n)
                if drop > 0:
                    self._buf = self._buf[drop:]
                    self._o.stats.trimmed += 1
            out = self._buf[:n]
            self._buf = self._buf[n:]
        self.last_qpc = self._o.drain_qpc()
        return out

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class LanDevice:
    """B 机的"局域网音源"设备对象（形状兼容 soundcard 的 mic）。

    生命周期：`start()` → 收包线程跑起来 → `recorder()` 取数 → `stop()`。

    `peer_hint`（2026-10-08 新增，用户要求"加一个手动输入 IP 的选项"）：
      用户手填的对端 IP（可留空 = 自动发现）。填了就：
        ① **只接受来自这个 IP 的包**（多机同网段时防止串音）；
        ② 宣告时**定向单播** HELLO 到它（广播被代理/虚拟网卡拦掉时仍能连上）。
      不填 = 老行为（广播 + 谁先发 DATA 谁是 peer）。
    """

    def __init__(self, port=LA.DEFAULT_PORT, bind="0.0.0.0", target_sec=TARGET_SEC,
                 peer_hint=""):
        self.port = int(port)
        self.bind = bind
        self.target_sec = float(target_sec)
        # ★ 手动指定对端 IP（空 = 自动发现）。允许带端口 "1.2.3.4:5" 或纯 IP。
        #   ⚠ 「纯数字」= 端口，**绝不能**当 IP（否则 `peer_hint="47811"` 会变成
        #     "只接受来自 IP 47811 的包" ⇒ 永远收不到 —— 单测 T1 抓到的真 bug）。
        self.peer_hint = ""
        self.peer_hint_port = 0
        h = (peer_hint or "").strip()
        if h and h.isdigit():
            h = ""                       # 纯端口
        if h:
            host, _, p = h.rpartition(":")
            if host and p.isdigit():
                self.peer_hint = host
                try:
                    self.peer_hint_port = int(p)
                except Exception:                                # noqa: BLE001
                    self.peer_hint_port = 0
            elif host:
                # 形如 "abc:def"（不是 IP:端口）⇒ 不认，避免误过滤
                self.peer_hint = ""
            else:
                self.peer_hint = h
        self.samplerate = 0            # 由 A 机的 HELLO/DATA 学到
        self.name = "局域网音频（等待 A 机…）"
        self.isloopback = False
        self.error = ""
        self.peer = ""
        self.stats = RecvStats()
        self._sock = None
        self._stop = threading.Event()
        self._thread = None
        self._ann_thread = None
        self._rec = None
        # 延迟基准（见模块 docstring）
        self._base_delta = None
        self._delta_hist = collections.deque(maxlen=600)
        self._qpc_out = 0
        self._qpc_out_lock = threading.Lock()
        self._last_data_t = 0.0
        # 被手动 IP 过滤掉的包数（诊断：用户能看到"我填的 IP 不对"）
        self._hint_rejects = 0
        # ★★ 2026-10-08：本次宣告是否**用逐网卡目标**发出（非回退到受限广播）。
        #    （`_announce` 里赋值；仅用于自我诊断，不影响逻辑）
        #    ⚠ 名字曾为 `_ann_socks_ok`（逐网卡 socket）；P0 回归后改为"目标"语义
        #      —— 现在宣告**不再建任何额外 socket**，全部从 `self._sock` 发出。
        self._ann_targets_ok = False
        # ★ 因"源地址明显不可达"而丢弃的 DATA 包数（诊断虚拟网卡问题）
        self._unreach_rejects = 0
        # 本机所有 IP（用来识别"自己发的包"，同机自测时避免自己认自己）
        try:
            self._my_ips = set(LA.local_ipv4_all())
        except Exception:                                        # noqa: BLE001
            self._my_ips = set()

    # ---------------- 生命周期 ----------------
    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self.error = ""
        try:
            self._sock = LA.make_receiver(self.port, self.bind)
        except Exception as e:                                   # noqa: BLE001
            self.error = "无法绑定 UDP 端口 %d：%s" % (self.port, e)
            return False
        self._thread = threading.Thread(target=self._rx_loop, name="lan-audio-rx",
                                        daemon=True)
        self._thread.start()
        # 宣告线程（见 `_announce_worker` docstring：绝不能让接收线程做 arp -a）
        self._ann_thread = threading.Thread(target=self._announce_worker,
                                            name="lan-audio-ann", daemon=True)
        self._ann_thread.start()
        return True

    def _announce_worker(self):
        """宣告线程：只在 1 秒节拍上跑 `_announce()`，**绝不占接收线程**。

        ⚠⚠ 为什么必须独立线程（2026-10-08 实测回归）：
          `LAN.lan_hosts()` 内部要 `subprocess` 跑 `arp -a` —— 实测 **152.8 ms**；
          `LAN.broadcast_targets()` 要枚举网卡 —— 实测 **21.2 ms**。
          早先把 `_announce()` 直接写在 `_rx_loop` 里，接收线程每秒被卡 **~174 ms**
          ⇒ 出块间隔 P99 从 23ms 劣化到 160ms（回归测试当场抓到）。
          这违反铁律 #8：**采集/接收线程不做可能被外部拖住的 I/O**。
          ⇒ 宣告全部挪到本线程；接收线程只需 `Event.wait()` 等节拍，开销≈0。
        """
        while not self._stop.is_set():
            try:
                self._announce()
            except Exception:                                    # noqa: BLE001
                pass
            # 可中断的 1 秒等待（stop() 时立刻醒）
            self._stop.wait(1.0)

    def _announce(self):
        """主动向局域网宣告"我在这里"（逐网卡子网定向广播 + 同网段单播）。

        ⚠ 回连不需要额外字段：发送端用的是「收到包的来源地址:端口」，
          而 UDP socket 的源端口就是我们的监听端口。

        ⚠⚠ 本函数**有阻塞耗时**（arp -a），只能在 `_announce_worker` 线程里调，
          绝不能在 `_rx_loop` 里直接调。

        ★★ 2026-10-08 根治（用户报「软件没打开也显示已握手」）：
          原来只从 `self._sock`（bind 0.0.0.0）发 `255.255.255.255`。
          实测本机有穿透工具虚拟网卡 `10.6.22.1` 时，**受限广播 255.255.255.255
          的源地址会变成 `10.6.22.1`** ⇒ A 机把 peer 学成这个不可达地址 ⇒
          永久失联，而双方界面都显示"已握手"（极难排查）。
          ⇒ 现在**不发 `255.255.255.255`**，只发每张真实网卡的子网定向广播 +
            同网段已知主机（`lan_audio.announce_targets`）—— 这些目标都在直连
            子网路由上，源地址由路由表正确决定，虚拟网卡抢不走。

        ★★ 2026-10-08 P0 回归根治（用户报「音频每秒被吞约 1/3」）：
          上一版为解决上面的问题，改成"**每张网卡各建一个
          `bind((网卡IP, 监听端口))` 的 socket**"。**Windows 上这会抢走
          `self._sock` 的收包** —— 同端口下「绑具体 IP」的 socket 优先收包
          （与 Linux 相反，实测见 `tools/_test_ann_steal.py`）。`_announce()` 每秒
          建一次、存活 ~330ms ⇒ 每秒一个 ~330ms 的「接收黑洞」⇒ 约 1/3 音频被吞。
          ⇒ 现在**一个额外 socket 都不建**，全部从 `self._sock` 发出：
              · 源地址正确（由路由表按目标决定）            ✔
              · 源端口 = 监听端口（`self._sock` 本就绑 port）✔ ← 硬要求
              · 无人抢 `self._sock` 的包                     ✔ ← P0
        """
        if self._sock is None:
            return
        pkt = LA.pack(LA.TYPE_HELLO, 0, 2, self.samplerate or 48000, 0,
                      LA.qpc_now(), b"")
        # 0) ★ 手动指定了对端 IP ⇒ **优先定向单播**（最可靠，不受广播被拦影响）。
        #    这是"手动输入 IP"选项能真正解决问题的关键：现场大半是广播被代理
        #    TUN / 跨 VLAN / 交换机隔离挡掉，而定向单播几乎总能通。
        #    ⚠ 定向单播也走 `self._sock`（它在 0.0.0.0 上）—— 但目标是一个**具体
        #      IP**，操作系统会按路由表选正确的网卡，不会走虚拟网卡（与被抢的
        #      受限广播不同）。本机实测：定向 192.168.31.150 的源地址正确。
        if self.peer_hint:
            try:
                self._sock.sendto(pkt, (self.peer_hint,
                                        self.peer_hint_port or self.port))
            except Exception:                                    # noqa: BLE001
                pass
        # ★★ 2026-10-08（QA 独立复核可疑点②）：即使 `peer_hint` 非空，**下面仍会**再走
        #    一遍「子网定向广播 + 同网段单播」。这是**有意保留**的行为 —— **别当 bug 删**：
        #      · 用户手填的 IP 可能填错 / 对端换过地址；多发一轮能让对方更快学到我们；
        #      · 成本极低（每张网卡 1 包/秒），且目标源地址都正确、无副作用。
        #    同理 `peer_hint` 分支**不 return**，继续走主力路径。
        # 1) ★★ 主力路径：逐网卡的**子网定向广播 + 同网段已知主机**
        #    —— 全程**只用 `self._sock`**，**不新建任何绑同端口的 socket**（P0 回归根治）。
        #    目标里**不含 `255.255.255.255`**（那条会被虚拟网卡抢源地址）。
        try:
            targets = LA.announce_targets(self.port)
        except Exception:                                        # noqa: BLE001
            targets = []
        if targets:
            self._ann_targets_ok = True
            for a in targets:
                try:
                    self._sock.sendto(pkt, a)
                except Exception:                                # noqa: BLE001
                    pass
        else:
            # 2) 回退：网卡枚举失败 / 所有网卡都被判为孤立（极罕见）⇒ 用旧广播目标兜底。
            #    ⚠⚠ **必须剔除 `255.255.255.255`**：它是**唯一**会被虚拟网卡抢走源地址
            #      的目标（源会变成不可达的 `10.6.22.1`）⇒ 对端学到不可达 peer ⇒
            #      永久失联（且无自愈路径）—— 这正是本轮要根除的症状。
            #      子网定向广播（`192.168.x.255`）本就源地址正确、够用，保留即可。
            self._ann_targets_ok = False
            try:
                fallback = list(LA.broadcast_targets(self.port))
            except Exception:                                    # noqa: BLE001
                fallback = []
            for a in fallback:
                if not a or a[0] == "255.255.255.255":
                    continue                                     # ★ 绝不发受限广播
                try:
                    self._sock.sendto(pkt, a)
                except Exception:                                # noqa: BLE001
                    pass
        # ⚠ 已删除旧的第 3) 步（逐个 `lan_hosts()` 定向单播）：
        #   `announce_targets()` 已把**同网段**的已知主机纳入目标，无需重复发
        #   （重复发会让对端每收到两份 HELLO，纯噪音）。

    def stop(self, timeout=2.0):
        self._stop.set()
        t = self._thread
        self._thread = None
        if t is not None:
            try:
                t.join(timeout)
            except Exception:                                    # noqa: BLE001
                pass
        at = self._ann_thread
        self._ann_thread = None
        if at is not None:
            try:
                at.join(0.5)
            except Exception:                                    # noqa: BLE001
                pass
        try:
            if self._sock is not None and self.peer:
                host, _, p = self.peer.rpartition(":")
                self._sock.sendto(LA.pack(LA.TYPE_BYE, 0, 2, self.samplerate or 48000,
                                          0, 0, b""), (host, int(p or self.port)))
        except Exception:                                        # noqa: BLE001
            pass
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:                                        # noqa: BLE001
            pass
        self._sock = None
        if self._rec is not None:
            self._rec.clear()

    # ---------------- 接收 ----------------
    def _rx_loop(self):
        """收包线程：只做 recvfrom + 解包 + 入缓冲，绝不碰 numpy 之外的重活。"""
        stat = self.stats
        stat.t0 = time.perf_counter()
        # ★★ 主动宣告（2026-10-08 现场踩到后补）：本机每 1 秒发一次 HELLO。
        #   为什么必须有：发送端只有在**收到我们的 HELLO** 之后才会把目标改成我们的地址。
        #   发送端可能配的是"点对点发给某个 IP"（用户手填过地址），这时它根本不会
        #   广播；如果我们只是被动等，就永远等不到 —— 现场症状正是"对面明明在发，
        #   这台就是收不到"。主动宣告 = 告诉对方"我在这，把音频发给我"。
        # ⚠⚠ 宣告**不在本线程做**（`arp -a` 要 150ms+，会把出块间隔 P99 拖炸，
        #   实测 23ms → 160ms）。这里只等节拍，实际发送在 `_announce_worker`。
        while not self._stop.is_set():
            try:
                buf, addr = self._sock.recvfrom(65536)
            except BlockingIOError:
                time.sleep(0.001)
                continue
            except Exception as e:                               # noqa: BLE001
                if not self._stop.is_set():
                    self.error = "接收出错：%s" % e
                time.sleep(0.05)
                continue
            m = LA.unpack(buf)
            if not m:
                continue
            now = time.perf_counter()
            stat.last_rx = now
            stat.packets += 1
            stat._pps_n += 1
            if now - stat._pps_t0 >= 1.0:
                stat.rx_pps = stat._pps_n / (now - stat._pps_t0)
                stat._pps_t0 = now
                stat._pps_n = 0
            # ★★ 「对端是谁」= **发 DATA 的那台机器**（2026-10-08 修，用户报「IP 显示不对」）。
            #
            # ⚠⚠ 原来的写法（错）：
            #     ```
            #     if not self.peer:
            #         self.peer = "%s:%d" % (addr[0], addr[1])   # ← 任何包都算
            #     if addr[0] in self._my_ips: ...               # ← 自滤在**赋值之后**
            #     ```
            #   两个致命问题：
            #   ① **任何包**（含 HELLO / 广播回声）都能当上对端 ⇒ 网段内**别的设备**、
            #      甚至**我们自己的广播回声**先到，就被认成对端，而且**一旦写入不再更正**
            #      （`if not self.peer` 只认第一个）；
            #   ② 装了代理/VPN 的机器上，`255.255.255.255` 受限广播会**从 TUN 虚拟网卡
            #      绕回本机**，来源显示成 `198.18.0.1` ⇒ 界面显示"对端 198.18.0.1"，
            #      **本机实测就是这个值**（`tools/_lan_peer_diag.py` 复现）。
            #      而自滤检查写在赋值**之后**，等于没挡。
            #
            # ⇒ 现在：**只在收到 DATA（真正的音频流）时**才认定对端，并且：
            #   - 先过自滤（自己的回声绝不认）；
            #   - 对端**允许被更新**（发送端换 IP / 换机重连时能自动跟上），
            #     但只在 DATA 来源真的变了时才改，避免抖动。
            # ★ 2026-10-19：**仅供同机联调**。设 AUTOVJ_LAN_ALLOW_SELF=1 时不再丢弃
            #   "源地址属于本机"的包 —— 否则发送端和演出端装在同一台电脑上永远收不到
            #   （这是"双方显示已握手但没声音/连上又断"的常见原因）。
            #   生产环境**不要设这个变量**：同机时不丢弃会把自己的广播回声当成对端。
            _allow_self = str(os.environ.get("AUTOVJ_LAN_ALLOW_SELF", "")).strip() in ("1", "true", "yes")
            is_self = (addr[0] in self._my_ips) and not _allow_self

            # ★ 自己的回声**最先丢**（在 peer_hint 过滤之前）：否则本机广播的 DATA
            #   会被算进「对端地址不符」计数，界面会误导用户"填的 IP 错了"。
            if is_self:
                continue

            # ★ 手动指定了对端 IP ⇒ **只认它**（2026-10-08，用户要的"手动输入 IP"）。
            #   为什么需要：同网段可能是多台机器 / 多路发送端，或者用户就想锁死到
            #   某一台；自动发现"谁先发 DATA 谁是对端"在多机场景会连错机器。
            #   ⚠ 过滤放在自滤之后：填错 IP 时表现为"一直收不到"，
            #     配合界面上的 `hint_rejects` 计数能立刻看出"包来了但被你的 IP 挡了"。
            if self.peer_hint and addr[0] != self.peer_hint:
                self._hint_rejects += 1
                continue

            if m["type"] in (LA.TYPE_HELLO, LA.TYPE_BYE):
                # HELLO/BYE 只用于发现/告别，**绝不**用它决定"对端是谁"
                # （广播回声、别的设备都会发 HELLO）。
                continue

            # ---- DATA ----
            # ★ 自己的 DATA 回声已在上面（peer_hint 过滤之前）统一丢弃：
            #   发送端在**还没学到对端**时会走广播（见 `lan_audio_send._targets` 第 3 条），
            #   受限广播可能从 TUN 绕回本机。若不丢，本机会把自己的音频当对端音频
            #   再喂一遍（自听回环）。
            #
            # ★★ 2026-10-08：额外拒绝**明显不可达的源地址**。
            #   实测踩到：穿透工具的虚拟网卡（10.6.22.1 / 198.18.0.1）会把
            #   受限广播的源地址顶掉，导致对端把 peer 学成不可达地址。
            #   这里对"来源"再做一层拒绝 —— 双保险。
            #   ⚠ 只拒绝**确定**不可达的（回环/组播/保留段），10.x/172.x/192.168.x
            #     一律放行（企业网真的用 10.x，不能误伤）。
            if LA.is_unreachable_src(addr[0]):
                self._unreach_rejects += 1
                continue
            new_peer = "%s:%d" % (addr[0], addr[1])
            if self.peer != new_peer:
                self.peer = new_peer
            sr = int(m["samplerate"] or 0)
            n = int(m["nframes"] or 0)
            ch = int(m["channels"] or 2)
            if sr != self.samplerate:
                self.samplerate = sr
                self.name = "局域网音频 @ %dHz" % sr
            if n <= 0 or n > LA.MAX_PAYLOAD:
                continue
            need = n * ch * 4
            pl = m["payload"]
            if len(pl) < need:
                continue
            try:
                arr = np.frombuffer(bytes(pl[:need]), dtype=np.float32).reshape(-1, ch)
            except Exception:                                    # noqa: BLE001
                continue
            if ch == 1:
                arr = np.repeat(arr, 2, axis=1)
            elif ch > 2:
                arr = arr[:, :2]
            arr = np.ascontiguousarray(arr, dtype=np.float32)

            # 丢包统计（seq 跳变）
            seq = int(m["seq"])
            if stat.last_seq >= 0:
                gap = (seq - stat.last_seq) & 0xFFFF
                if 1 < gap < 200:
                    stat.lost += gap - 1
            stat.last_seq = seq
            # ★ 去重（防御性，2026-10-08）：发送端若同时走单播+广播，
            #   同一个 seq 会到两次。虽然已让发送端只发一份，这里再挡一层 ——
            #   重复包会让缓冲凭空变长、还原频率偏掉（实测 440→405Hz）。
            if seq == stat._last_dup and stat._dup_n > 0 and \
                    (time.perf_counter() - stat._dup_t) < 0.25:
                stat._dup_n += 1
                continue
            stat._last_dup = seq
            stat._dup_n = 1
            stat._dup_t = time.perf_counter()

            # 延迟基准更新（见模块 docstring）
            q = int(m["qpc"] or 0)
            if q:
                self._update_drift(q, now)
                with self._qpc_out_lock:
                    self._qpc_out = q

            # 断流后重连：缓冲很久没数据 ⇒ 清空重新垫
            if now - self._last_data_t > 0.5 and self._rec is not None \
                    and self._rec.depth() > 0:
                self._rec.clear()
                stat.resets += 1
            self._last_data_t = now

            self._ensure_rec()
            if self._rec is not None:
                self._rec.feed(arr)
            self._update_depth()

    def _ensure_rec(self):
        if self._rec is None:
            self._rec = _LanRecorder(self, self.samplerate or 48000, 2)

    def _update_drift(self, q, now):
        """用「包 QPC − 本地 QPC」的变化量估网络延迟变化。

        ⚠⚠ 两台机 QPC 时基不同 ⇒ 只能比**变化**，不能比绝对值。
          取滑动最小值当基准（网络最空时 = 延迟下限）。

        ⚠⚠ 本函数**只做观测**，绝不修改 `target_sec`。
          2026-10-08 踩到的坑：原来这里写 `target_sec = target + max(0, drift)`，
          而 `drift = delta - min(hist)` **恒 ≥ 0** ⇒ 目标深度**只增不减**，
          一路顶到 MAX_SEC（实测缓冲涨到 210ms）。
          而且 `target_sec` 本来就不是控制量 —— 缓冲深度由**消费侧**决定。
          ⇒ 现在这里只算一个给人看的 `jitter_ms`，控制权全部交给 `_update_depth`。
        """
        local_q = LA.qpc_now()
        freq = LA.qpc_freq()
        # delta 单位：秒。表示"这包音频在 A 机生成时刻 + 传输耗时" 与 本地现在的差
        delta = (local_q - q) / float(freq)
        self._delta_hist.append(delta)
        base = min(self._delta_hist)
        # drift ≥ 0：当前延迟相对**历史最好**延迟多了多少（纯观测量）
        self.stats.jitter_ms = float((delta - base) * 1000.0)

    def _update_depth(self):
        """只更新给人看的深度指标 + 一个**兜底**上限（常规裁剪在 `record()` 里）。"""
        if self._rec is None:
            return
        sr = self.samplerate or 48000
        depth = self._rec.depth()
        self.stats.depth_ms = depth / sr * 1000.0
        # ⚠ 这里只兜底：`record()` 没被调用时（比如上层卡住了）缓冲会无限涨，
        #   硬砍到 MAX_SEC 防止内存无限增长。常规的深度控制不在这里。
        cap = int(MAX_SEC * sr)
        if depth > cap:
            with self._rec._lock:                                # noqa: SLF001
                self._rec._buf = self._rec._buf[depth - cap:]    # noqa: SLF001
            self.stats.trimmed += 1
    def drain_qpc(self):
        with self._qpc_out_lock:
            v = self._qpc_out
            self._qpc_out = 0
        return v

    # ---------------- soundcard 兼容接口 ----------------
    def recorder(self, samplerate=None, channels=None, blocksize=None):
        self._ensure_rec()
        return self._rec

    def __getattr__(self, item):
        # 兜底：让 `audio_engine` 里对 mic 的杂项访问不至于炸
        raise AttributeError(item)

    # ---------------- 状态 ----------------
    def snapshot(self):
        st = self.stats
        return {
            "connected": bool(st.packets and (time.perf_counter() - st.last_rx) < 1.0),
            "peer": self.peer,
            "peer_hint": self.peer_hint,          # ★ 用户手填的对端 IP（空=自动发现）
            "hint_rejects": self._hint_rejects,   # ★ 被手填 IP 过滤掉的包数（诊断）
            "sr": self.samplerate,
            "packets": st.packets,
            "lost": st.lost,
            "lost_rate": (st.lost / max(1, st.lost + st.packets)) * 100.0,
            "pps": st.rx_pps,
            "depth_ms": st.depth_ms,
            "jitter_ms": st.jitter_ms,
            "resets": st.resets,
            "error": self.error,
        }
