# -*- coding: utf-8 -*-
"""局域网音频发送核心（A 机）—— 采集 → UDP 发送。

独立小 exe（`lan_audio_sender.pyw`）和自测脚本都调这里的 `Sender`。

## 设计要点

1. **采集用 `wasapi_capture`**（与主程序同一条路）：它保留驱动原始包长与 QPC 时间戳，
   不攒块、不填零 ⇒ 发出去的包边界就是驱动给的边界，B 机接上即用。

2. **发送线程与采集线程分离**（项目铁律 8）：
   采集线程只管入队，绝不碰 socket。socket 发送可能被系统缓冲拖住，
   一旦拖住采集，能量/时间轴一起崩。队列满就丢最旧的包（低延迟优先）。

3. **目标缓冲深度控制（关键）**：
   不做任何花哨的时钟同步 —— 直接按「A 机 QPC 与 B 机 QPC 的当前差值」估网络+缓冲延迟，
   用**累积漂移**做微调。B 机侧收到后按目标深度决定"这一包是立刻播还是先垫进缓冲"。
   本文件只负责发送端；接收端的抖动缓冲见 `lan_audio_recv.py`。

4. **丢包不重传**：UDP 语义。`seq` 只用于统计。
"""

import threading
import time

import numpy as np

import lan_audio as LA


class SendStats:
    """发送端统计（喂给界面显示）。"""

    def __init__(self):
        self.packets = 0
        self.bytes = 0
        self.dropped = 0
        self.t0 = time.perf_counter()
        self.last_err = ""
        # ★ 自愈重连统计（见 `_capture_loop`）：让界面能显示"它自己恢复过一次"
        self.reconnects = 0
        self.last_reconnect = 0.0
        # ★★ 2026-10-08：因"源地址不可达"而拒收的 HELLO 数（诊断虚拟网卡问题）
        self.bad_src = 0
        # ★ 因"peer 太久没确认"而回退广播的次数（自愈：锁死到不可达的 peer）
        self.peer_timeouts = 0

    def rate(self):
        dt = time.perf_counter() - self.t0
        return (self.packets / dt) if dt > 0 else 0.0

    def mbps(self):
        dt = time.perf_counter() - self.t0
        return (self.bytes * 8 / 1e6 / dt) if dt > 0 else 0.0


class Sender:
    """A 机：从 WASAPI 采集 → UDP 广播/单播给 B 机。

    ## 用法
        s = Sender(device=None, sr=48000, dest=("192.168.1.20", 47811))
        s.start()          # 起采集+发送线程
        ...
        s.stop()
        print(s.snapshot())

    `dest=None` ⇒ 用广播（B 机自动发现，适合"不想查 IP"的场景）。
    """

    def __init__(self, device=None, sr=48000, dest=None, port=LA.DEFAULT_PORT,
                 block_ms=10.0, is_loopback=True):
        self.device_name = device or ""
        self.sr = int(sr) if sr else 48000
        self.dest = tuple(dest) if dest else None
        self.port = int(port)
        self.block_ms = float(block_ms)
        self.is_loopback = bool(is_loopback)

        self.stats = SendStats()
        self.error = ""
        self.running = False
        self._stop = threading.Event()
        self._threads = []
        self._sock = None
        self._q = None                # 采集 → 发送 的队列（最新优先）
        self._queue_lock = threading.Lock()
        self._latest = None           # 只在队列空时用：直接投最新一包
        self._peer = None             # B 机发 HELLO 后学到的真实地址
        self._peer_t = 0.0            # ★ 学到 peer 的时刻（用于"太久没确认就回退广播"）
        self._peer_confirm_t = 0.0    # ★ 最近一次收到该 peer 确认包的时刻
        self._device_desc = ""
        self._native_sr = 0
        self._pkt_frames = 0
        self._cap_attempt = 0         # 采集重连退避档位（成功即清零）

    # ---------------- 生命周期 ----------------
    def start(self):
        if self.running:
            return True
        self._stop.clear()
        self.stats = SendStats()
        self.error = ""
        try:
            self._sock = LA.make_sender(self.port)
        except Exception as e:                                   # noqa: BLE001
            self.error = "无法绑定 UDP 端口 %d：%s" % (self.port, e)
            return False
        self.running = True
        t1 = threading.Thread(target=self._send_loop, name="lan-audio-tx", daemon=True)
        t2 = threading.Thread(target=self._capture_loop, name="lan-audio-cap", daemon=True)
        self._threads = [t1, t2]
        for t in self._threads:
            t.start()
        return True

    def stop(self, timeout=2.0):
        self._stop.set()
        self.running = False
        for t in self._threads:
            try:
                t.join(timeout)
            except Exception:                                    # noqa: BLE001
                pass
        self._threads = []
        # 通知 B 机我们下线了（不保证送达，B 机有超时兜底）
        try:
            if self._sock is not None:
                self._send(LA.TYPE_BYE, 0, 1, self.sr, 0, 0, b"")
        except Exception:                                        # noqa: BLE001
            pass
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:                                        # noqa: BLE001
            pass
        self._sock = None

    # ---------------- 发送 ----------------

    # ★★ peer 心跳超时（秒）。超过这么久没收到该 peer 的 HELLO ⇒ 判定它失效，
    #   回退广播重新学（铁律 #0：绝不能"锁死"）。
    #   取值依据：B 机 `_announce` 每秒发 1 个 HELLO，容 5 个丢包窗口足够；
    #   而现场"发送端学到错地址"到"用户发现问题"通常要几十秒，5 秒能很快自愈。
    _PEER_STALE_SEC = 5.0
    def _targets(self):
        """发到哪里。

        策略（2026-10-08 定稿）：
        1. **学到 peer** ⇒ 只发 peer（最省带宽、最准）。
        2. 否则 **有用户填的 dest** ⇒ 发 dest。
        3. 否则广播。

        ⚠⚠ 千万不要"peer + 广播同时发"（我一开始就是这么写的，被回归测试当场打回）：
          B 机同时从**单播**和**广播**两条路收到**同一个 seq 的包**，
          缓冲里每包变两份 ⇒ 深度虚高、消费端读出 6 倍数据量、
          还原出的频率直接偏掉（实测 440Hz → 405Hz）。
          修法不是加去重，而是**压根不重复发**。

        ★★ 2026-10-08 补充（铁律 #0：绝不能"一次失败就永久停"）：
          peer 是**学来的**，可能学到错的（虚拟网卡顶掉源地址 ⇒ 学到不可达地址，
          现场实测见 `_poll_peer` 注释）。一旦锁死，`_targets()` 就**永久只发那个
          死地址、再也不广播** ⇒ 两边彻底失联，而界面看着正常。
          ⇒ 现在加**心跳超时**：peer 超过 `_PEER_STALE_SEC` 没被确认（没收到它的
            HELLO）就**回退广播**，并在下一轮 HELLO 里重新学。
            这是"带自愈的重试"，不是"关掉功能"。
        """
        if self._peer:
            # ★ 心跳超时 ⇒ 丢弃这个 peer，回退广播（等它再来 HELLO）
            age = time.perf_counter() - (self._peer_confirm_t or self._peer_t or 0.0)
            if age > self._PEER_STALE_SEC:
                self.stats.peer_timeouts += 1
                self._peer = None
            else:
                return [self._peer]
        if self.dest:
            return [self.dest]
        try:
            return list(LA.broadcast_targets(self.port))
        except Exception:                                        # noqa: BLE001
            return []

    def _send(self, kind, seq, ch, sr, n, qpc, payload):
        pkt = LA.pack(kind, seq, ch, sr, n, qpc, payload)
        tg = self._targets()
        for a in tg:
            self._sock.sendto(pkt, a)
        self.stats.packets += 1
        self.stats.bytes += len(pkt) * len(tg)

    def _send_loop(self):
        """独立发送线程：只做 socket 发送，绝不阻塞采集。"""
        seq = 0
        hello_t = 0.0
        poll_t = 0.0
        while not self._stop.is_set():
            now = time.perf_counter()
            # 周期性宣告 HELLO：让 B 机知道我们在哪、采样率多少
            if now - hello_t >= 1.0:
                hello_t = now
                try:
                    self._send(LA.TYPE_HELLO, 0, 2, self.sr, 0, LA.qpc_now(), b"")
                except Exception as e:                           # noqa: BLE001
                    self.stats.last_err = str(e)[:120]
            # ★★ 收 B 机的 HELLO/BYE —— **必须无条件周期性做，不能只在"没数据发"时做**。
            #   2026-10-08 现场踩到：原写法把它放在 `item is None` 分支里，
            #   而正常演出时**永远有音频要发** ⇒ 这个分支根本不执行 ⇒
            #   发送端永远学不到 B 机的真实地址，只能一直往自己配的地址发。
            #   现象：B 机被动等广播收不到（若 A 机是点对点模式就更收不到），
            #   必须靠 B 机主动单播 HELLO 才能触发一次回连 —— 极难理解。
            #   代价：每 100ms 一次（非阻塞 recvfrom），可忽略。
            if now - poll_t >= 0.1:
                poll_t = now
                self._poll_peer()

            item = self._pop()
            if item is None:
                time.sleep(0.001)
                continue
            arr, qpc = item
            try:
                n, ch = arr.shape[0], arr.shape[1]
                self._send(LA.TYPE_DATA, seq, ch, self.sr, n, qpc,
                           np.ascontiguousarray(arr, dtype=np.float32).tobytes())
                seq = (seq + 1) & 0xFFFF
            except Exception as e:                               # noqa: BLE001
                self.stats.last_err = str(e)[:120]
                self.stats.dropped += 1

    def _poll_peer(self):
        """收 B 机的 HELLO/BYE，学到它的地址。

        ★★ 2026-10-08 根治（现场：用户报「B 机软件都没打开，发送端也显示已握手」）：
          原实现是**收到任何 HELLO 就 `self._peer = addr`**，不校验源地址。
          配合 `_targets()`「有 peer 就只发 peer、永不广播」⇒ 一旦学到一个
          **不可达**地址（实测：本机穿透工具虚拟网卡 `10.6.22.1` 会把受限广播
          `255.255.255.255` 的源地址顶掉，B 机发 HELLO 时源地址变成 `10.6.22.1`），
          发送端就**永久只发那个不可达地址**，两边彻底失联 —— 而界面上
          「对端」那一行照样显示地址，看着像"已握手"。用户完全无从判断。

          现在加两道闸：
            ① **拒绝明显不可达的源地址**（`LA.is_unreachable_src`）——
               回环 / 组播 / 广播 / 198.18~19（代理 TUN 保留段）/ 169.254 / 0.x / 255.x。
               ⚠ 不拒绝 10.x / 172.x / 192.168.x（企业网真的在用 10.x，不能误伤）。
            ② **peer 允许被"更正"**：即使已有 peer，只要新来的 HELLO 来自另一个
               合法地址，就更新过去 —— 这样"学到错的地址"能被后续正确 HELLO 纠正，
               而不是像原来那样一旦锁死就再也回不来。
        """
        if self._sock is None:
            return
        for _ in range(16):
            try:
                buf, addr = self._sock.recvfrom(2048)
            except BlockingIOError:
                return
            except Exception:                                    # noqa: BLE001
                return
            m = LA.unpack(buf)
            if not m:
                continue
            # ★ 闸①：源地址不可达 ⇒ 绝不认（这正是虚拟网卡坑的来源）
            if LA.is_unreachable_src(addr[0]):
                self.stats.bad_src += 1
                continue
            if m["type"] == LA.TYPE_HELLO:
                # ★ 闸②：允许更新（原来只有 `if not self._peer` 时才学 ⇒ 锁死）
                if self._peer != addr:
                    self._peer = addr
                    self._peer_t = time.perf_counter()
                    # 学了新对端 ⇒ 退避计数清零（说明链路是活的）
                    self._cap_attempt = 0
                # ★★ 无论是否换了地址，都要刷新"确认时刻"——
                #   `_targets()` 用它判断 peer 是否已失效（心跳超时回退广播）。
                self._peer_confirm_t = time.perf_counter()
            elif m["type"] == LA.TYPE_BYE:
                # B 机主动说拜拜：清掉它，回到广播（等它下次 HELLO）
                if self._peer == addr:
                    self._peer = None

    def _push(self, arr, qpc):
        """入队（队列满丢最旧）。"""
        with self._queue_lock:
            if self._q is None:
                import collections
                self._q = collections.deque(maxlen=4)   # 最多留 40ms，低延迟优先
            if len(self._q) >= 4:
                self._q.popleft()
                self.stats.dropped += 1
            self._q.append((arr, qpc))

    def _pop(self):
        with self._queue_lock:
            if self._q:
                return self._q.popleft()
            return self._latest

    # ---------------- 采集 ----------------
    #
    # ★★ 自愈重连（2026-10-08 加，用户要求"推流端设备断线自动重连"）
    #
    # 铁律 #0：**「一次失败就永久停」全是 bug**。采集设备可能因为
    #   - USB 声卡被拔掉/重新插上
    #   - 系统切换默认输出设备（用户在 Windows 声音面板换了一下）
    #   - 驱动短暂重启（尤其 ASIO/独占模式下别的程序抢设备）
    # 而抛一次异常。原实现 `except: self.error = ...; self.running=False`
    # ⇒ **整场不再推流**，而界面上只显示一行错误，用户根本不知道要重启程序。
    #
    # 判据（铁律 #0 统一判据）：**只要线程还活着，就必须按"尚未就绪、可重试"处理**。
    # 重试策略：指数退避 0.5→1→2→4→8→15→30s（封顶 30s），**成功一次即清零**。
    #   - 每轮重试都**重新枚举设备**（设备可能换了名字/索引）
    #   - 退避期间 `self._stop.wait()` ⇒ 可被 stop() 立刻打断
    #   - `reconnects` 计数 + `last_reconnect` 时间给界面看（用户要知道"它自己好了"）
    _REOPEN_DELAYS = (0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0)

    def _capture_loop(self):
        self._cap_attempt = 0
        while not self._stop.is_set():
            ok = self._capture_session()
            if self._stop.is_set():
                break
            if ok:
                # 正常结束（socket 关了 / stop）⇒ 不重连
                if self._stop.is_set():
                    break
            # 走到这里 = 采集会话异常退出 ⇒ 退避后重连
            d = self._REOPEN_DELAYS[min(self._cap_attempt,
                                        len(self._REOPEN_DELAYS) - 1)]
            self._cap_attempt += 1
            self.stats.reconnects += 1
            self.stats.last_reconnect = time.perf_counter()
            self.error = ("音频设备中断，%.1fs 后自动重连（第 %d 次）"
                          % (d, self.stats.reconnects))
            # ⚠ 退避等待用 Event.wait ⇒ stop() 能立刻打断，不会卡住退出
            if self._stop.wait(d):
                break
            self.error = ""
        self.running = False

    def _capture_session(self):
        """跑一次采集会话。返回 True = 正常退出（别重连），False = 异常（该重连）。

        ⚠ 本函数**每轮都重新枚举设备** —— 设备可能已被换掉/改名。
        """
        try:
            import soundcard as sc
            import wasapi_capture as wc
        except Exception as e:                                   # noqa: BLE001
            self.error = "音频采集组件不可用：%s" % e
            return True                                          # 组件没了，重试无意义
        try:
            mics = sc.all_microphones(include_loopback=True)
        except Exception as e:                                   # noqa: BLE001
            self.error = "枚举音频设备失败：%s" % e
            return False                                         # 设备列表拿不到 ⇒ 重试

        # 选设备：优先 loopback（"系统声音"），名字能对上就用对上的
        cands = [m for m in mics if bool(m.isloopback) == self.is_loopback]
        if not cands:
            cands = list(mics)
        if not cands:
            self.error = "找不到可采集的音频设备"
            return False
        mic = cands[0]
        if self.device_name:
            for m in cands:
                if self.device_name in m.name or m.name in self.device_name:
                    mic = m
                    break
        self._device_desc = mic.name

        try:
            dev = wc.wrap(mic)
        except Exception:                                        # noqa: BLE001
            dev = mic

        # 原生采样率：尽量不自作主张重采样
        sr = self.sr
        try:
            from audio_engine import _native_samplerate
            sr = _native_samplerate(mic) or sr
        except Exception:                                        # noqa: BLE001
            pass
        self._native_sr = sr
        # 包长 = 10ms（与驱动周期一致）⇒ 下游零额外延迟
        pkt = max(64, int(round(sr * self.block_ms / 1000.0)))
        self._pkt_frames = pkt
        self.sr = sr

        try:
            with dev.recorder(samplerate=sr, channels=2) as rec:
                self._cap_attempt = 0        # ★ 成功打开 ⇒ 退避清零
                while not self._stop.is_set():
                    try:
                        data = rec.record(numframes=pkt)
                    except Exception as e:                        # noqa: BLE001
                        self.error = "采集出错：%s" % e
                        return False                             # 采集异常 ⇒ 重连
                    if data is None or len(data) == 0:
                        # ⚠ 返回 None 是 wasapi_capture 的"还没攒够"语义，
                        #   **不是错误** ⇒ 只 sleep，不算失败（否则会疯狂重连）。
                        time.sleep(0.001)
                        continue
                    if data.ndim == 1:
                        data = np.column_stack([data, data])
                    arr = np.ascontiguousarray(data, dtype=np.float32)
                    qpc = int(getattr(rec, "last_qpc", 0) or LA.qpc_now())
                    self._push(arr, qpc)
        except Exception as e:                                    # noqa: BLE001
            self.error = "打开采集设备失败：%s" % e
            return False                                         # 打开失败 ⇒ 重连
        return True

    # ---------------- 状态 ----------------
    def snapshot(self):
        return {
            "running": self.running,
            "device": self._device_desc,
            "sr": self.sr or self._native_sr,
            "pkt": self._pkt_frames,
            "packets": self.stats.packets,
            "dropped": self.stats.dropped,
            "pps": self.stats.rate(),
            "mbps": self.stats.mbps(),
            "peer": ("%s:%d" % self._peer) if self._peer else "",
            "mode": "点对点" if self._peer else ("指定地址" if self.dest else "广播"),
            "error": self.error or self.stats.last_err,
            # ★ 自愈重连：界面据此提示"设备中断后已自动恢复"
            "reconnects": self.stats.reconnects,
            "last_reconnect": self.stats.last_reconnect,
            # ★★ 2026-10-08 诊断：被拒的不可达源地址数 / peer 失效回退广播次数
            "bad_src": self.stats.bad_src,
            "peer_timeouts": self.stats.peer_timeouts,
        }
