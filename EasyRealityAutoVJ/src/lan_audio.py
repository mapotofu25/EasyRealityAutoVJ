# -*- coding: utf-8 -*-
"""原生局域网音频传输 —— 自研轻量 UDP 协议（A 机采集发送 / B 机接收）。

## 为什么不用 NDI
用户实测：NDI 运行库 `Processing.NDI.Lib.x64.dll` 单个 27.4 MB，
整个 `cyndilib` 包 107 MB；而它在本项目的用途**仅仅是**传一路 48k 立体声 PCM。
本模块把这条链压到 **零第三方依赖**（只用标准库 socket + numpy），
且协议完全可控 —— 现场出问题能自己加日志，不用等上游。

## 协议（LANPCM/1）

纯 UDP，一包 = 一个音频包，**原始无损 float32 小端**（用户选定：不压缩）。

### 包结构
```
偏移  长度  含义
0     4     magic  'L','A','N','1'（0x4C 41 4E 31）
4     1     type   1 = DATA，2 = HELLO，3 = BYE
5     1     ch     声道数（1 或 2）
6     2     seq    uint16 小端，音频包序号（诊断丢包用，不回绕语义）
8     4     sr     uint32 小端，采样率
12    4     n      uint32 小端，本包帧数
16    8     qpc    uint64 小端，该包首帧的采集时刻（100ns，Windows QPC）
24    ...   payload = n * ch * 4 字节 float32 小端（交错）
```

### 为什么带 qpc
A 机的采集层（`wasapi_capture`）保留了驱动给的真实 QPC 时间戳。
把它一起发过来，B 机才能知道"这包音频在 A 机是第几微秒采到的"，
从而做出**基于真实时间的目标缓冲深度控制**（而不是猜）。
这是自研协议相对 NDI 的一个实打实的优势 —— NDI 里这部分被 SDK 吃掉了。

### 为什么 UDP 而不是 TCP
演出场景要的是**低延迟**而不是"一个字节都不丢"。
TCP 一旦丢包会阻塞重传，把后续所有音频一起顶在门口（越积越久）；
UDP 丢包就是丢那 10ms，下一包立刻接上。卡顿感远小于 TCP。
丢包用 `seq` 计数，在 B 机的状态里显示出来 —— 丢多少用户能看见。

### 端口
默认 `47811`（单个 UDP 端口收音频）。`HELLO` / `BYE` 走同一个端口，
不需要额外的发现端口：B 机只要收到了 HELLO 就知道 A 机在哪个地址。
"""

import socket
import struct
import subprocess
import sys
import time

MAGIC = b"LAN1"
TYPE_DATA = 1
TYPE_HELLO = 2
TYPE_BYE = 3


def _no_window_kwargs():
    """返回"让子进程不弹控制台窗口"的 subprocess 参数（Windows 专用）。

    ## ⚠⚠ 为什么必须有（2026-10-08 现场事故）

    接收端每秒要宣告一次（`lan_audio_recv._announce_worker`），
    而宣告要读 ARP 表 ⇒ `subprocess.run(["arp", "-a"], ...)`。
    **Windows 上 `subprocess` 默认会为新控制台程序创建一个真窗口** ——
    于是屏幕上**每秒闪一下黑色命令行窗口**（`C:\\WINDOWS\\SYSTEM32\\arp.exe`）。

    用户的原话是「**一直有个弹窗在闪**」。它看起来像崩溃弹窗，其实只是
    `arp.exe` 的控制台。而且 `arp -a` 实测 **152.8ms**，所以每闪一次还会
    卡一下 —— 与「软件非常卡」叠在一起，非常像"软件坏了"。

    ## 做法
    `CREATE_NO_WINDOW`（0x08000000）让子进程不分配控制台；
    `STARTUPINFO(dwFlags=STARTF_USESHOWWINDOW, wShowWindow=SW_HIDE)` 兜底
    （有些 shell/包装器不认 creationflags）。
    非 Windows 平台返回空字典（`subprocess` 不认这两个参数）。
    """
    if not sys.platform.startswith("win"):
        return {}
    si = None
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0        # SW_HIDE
    except Exception:                                            # noqa: BLE001
        si = None
    kw = {"creationflags": 0x08000000}     # CREATE_NO_WINDOW
    if si is not None:
        kw["startupinfo"] = si
    return kw


# magic(4s) + type(B) + channels(B) + seq(H) + samplerate(I) + nframes(I) + qpc(Q)
# ⚠ struct 默认按 C 对齐补齐，这里显式用 '<' 关掉对齐 ⇒ 4+1+1+2+4+4+8 = 24，正好、无填充。
HDR = struct.Struct("<4sBBHIIQ")
HDR_SIZE = 24
DEFAULT_PORT = 47811
MAX_PAYLOAD = 8192                    # 一个包最多 8192 帧（170ms @48k，足够冗余）

# ⚠ 接收缓冲开大：演出里一帧渲染卡顿可能让接收线程晚来 100ms，
#   内核 UDP 缓冲太小会直接丢包（用户看不到原因）。512KB ≈ 1.4s @48k 立体声。
RcvBufSize = 1 << 19


def pack(kind, seq, channels, samplerate, nframes, qpc, payload):
    """组一个 UDP 包。payload 必须是 C 连续的 float32 字节串。"""
    return HDR.pack(MAGIC, kind, int(channels), int(seq) & 0xFFFF,
                    int(samplerate), int(nframes), int(qpc)) + payload


def unpack(buf):
    """解包 → dict，或 None（不是我们的包 / 太短）。

    返回键：type / channels / seq / samplerate / nframes / qpc / payload(memoryview)
    """
    if len(buf) < HDR_SIZE:
        return None
    try:
        magic, kind, ch, seq, sr, n, qpc = HDR.unpack_from(buf, 0)
    except struct.error:
        return None
    if magic != MAGIC:
        return None
    return {
        "type": kind,
        "channels": ch,
        "seq": seq,
        "samplerate": sr,
        "nframes": n,
        "qpc": qpc,
        "payload": memoryview(buf)[HDR_SIZE:],
    }



def local_ipv4_all():
    """本机所有 IPv4（含虚拟网卡）—— 用来识别"这个包是不是我自己发的"。"""
    out = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and ip not in out:
                out.append(ip)
    except Exception:                                            # noqa: BLE001
        pass
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        if ip and ip not in out:
            out.append(ip)
    except Exception:                                            # noqa: BLE001
        pass
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:                                    # noqa: BLE001
                pass
    # 总是把回环也放进来（同机自测时 peer 可能是 127.0.0.1）
    if "127.0.0.1" not in out:
        out.append("127.0.0.1")
    return out


def lan_hosts(limit=254):
    """本网段（按本机真实 IP 的前三段）上**已知活跃**的设备 IP 列表。

    取自系统 ARP 表 —— 只含近期有过通信的设备，不用扫全网段（那样太慢且像攻击）。
    用途：接收端"主动宣告"时定向单播，避开广播被拦的场景。
    """
    my = local_ip()
    if not my:
        return []
    pfx = ".".join(my.split(".")[:3]) + "."
    out = []
    try:
        r = subprocess.run(["arp", "-a"], capture_output=True, text=True,
                           encoding="gbk", errors="replace",
                           **_no_window_kwargs())
        for line in (r.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0].count(".") == 3:
                ip, mac = parts[0], parts[1]
                if not ip.startswith(pfx):
                    continue
                if ip.endswith((".255", ".0")) or ip == my:
                    continue
                if mac == "ff-ff-ff-ff-ff-ff":
                    continue
                if ip not in out:
                    out.append(ip)
    except Exception:                                            # noqa: BLE001
        pass
    return out[:limit]


def broadcast_targets(port=DEFAULT_PORT):
    """给"自动发现"用的广播地址列表。

    ## ⚠⚠ 为什么不能只发 255.255.255.255（2026-10-08 实测踩到）

    本机装了代理软件（Clash / Mihomo / v2ray 等）时会创建一张 TUN 虚拟网卡
    （典型地址 `198.18.0.1`）。此时 **`255.255.255.255` 这条受限广播会被 TUN
    抢走出站路由** —— 实测：发 255.255.255.255 时接收端看到的来源是 `198.18.0.1`
    （虚拟网卡），而发 `192.168.31.255` 时来源才是真实的 `192.168.31.150`。
    ⇒ 只发受限广播的话，**真实局域网上的另一台电脑永远收不到**，
      而发送端界面看起来一切正常（包确实"发出去了"）。这种"静默失效"极难排查。

    ## 做法
    尽量**枚举每张真实网卡的子网定向广播地址**（如 192.168.31.255）并一起发，
    同时保留 255.255.255.255 兜底（万一枚举失败 / 无代理时它也能работать）。
    """
    out = []
    # 受限广播兜底：没有虚拟网卡时就靠它
    out.append(("255.255.255.255", int(port)))
    for ip, prefix in _local_ipv4_with_prefix():
        b = _subnet_broadcast(ip, prefix)
        if b:
            t = (b, int(port))
            if t not in out:
                out.append(t)
    return out


def _subnet_broadcast(ip, prefix):
    """由「IP + 前缀长度」算出子网定向广播地址。前缀未知返回 None。"""
    if not prefix or not (0 < prefix <= 32):
        return None
    try:
        parts = [int(x) for x in ip.split(".")]
        if len(parts) != 4:
            return None
        v = (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]
        mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
        b = (v & mask) | (~mask & 0xFFFFFFFF)
        return "%d.%d.%d.%d" % ((b >> 24) & 255, (b >> 16) & 255,
                                (b >> 8) & 255, b & 255)
    except Exception:                                            # noqa: BLE001
        return None


def _local_ipv4_with_prefix():
    """本机 IPv4 与其前缀长度列表（排除虚拟/回环）。

    ⚠ 只用标准库：走 `GetAdaptersAddresses` 的 ctypes 太重，
      这里用「UDP connect 探测 + 常见子网掩码推断」的组合，
      不够精确但足够发广播（多发几个地址的代价是可以忽略的）。
      实测最稳的推断：私有段默认 /24（家用/小型场地几乎都是 /24）。

    ★★ 2026-10-08 补充（用户报「软件没打开也显示已握手」时查到的根因）：
      本机除真实网卡 `192.168.31.150` 外，还有穿透工具建的 **`10.6.22.1`**
      —— 它**不在**下面的 `_VIRTUAL` 黑名单里，于是被当成"真实网卡"保留下来，
      连"向它发广播"都被允许。实测证据（`tools/_lan_bcast_src.py`）：
      发 `255.255.255.255` 时**源地址变成 `10.6.22.1`** ⇒ A 机把 peer 学成
      这个不可达地址 ⇒ 两边永久失联，而双方界面都显示"已握手"。
      ⇒ 这里**不能**简单地"把 10.6. 加进黑名单"（企业网真的用 10.x）。
        正确判据是**「这张网卡的子网里能不能找到对端」**，那属于上层逻辑；
        本函数只负责**把网卡都列出来**，让 `announce_targets()` 逐卡生成
        「子网定向广播 + 同网段已知主机」目标
        —— 这样**每张网卡都有机会以正确源地址发出**，虚拟网卡那条自然是废包，
        而真实网卡那条一定带着正确源地址到达 A 机。
      这就是"不猜、而是让每条路都试一遍"的思路（与项目铁律 #0 一致）。
    """
    out = []
    _VIRTUAL = ("198.18.", "198.19.", "169.254.", "0.", "127.", "255.",
                # ★ 本机实测：`natpierce` 之类的穿透工具会占一张 10.6.22.1 的虚拟网卡，
                #   对它发广播毫无意义（只有它自己收得到）。10.x 里 **真正的局域网**
                #   通常是 10.0/8 的企业网；但我们也无法可靠区分，
                #   所以策略是：**保留 10.x 但排在私有段最后**，同时不发 10.x 的 /8 广播
                #   （/8 广播 = 10.255.255.255，那个地址基本没人用，纯噪音）。
                )
    seen = set()

    def _add(ip):
        if not ip or ip in seen:
            return
        seen.add(ip)
        if any(ip.startswith(p) for p in _VIRTUAL):
            return
        if ip.startswith("172."):
            try:
                if 17 <= int(ip.split(".")[1]) <= 31:
                    return                      # Docker / WSL 段
            except Exception:                                    # noqa: BLE001
                pass
        # ⚠ 只发 /24 定向广播。
        #   曾想给 10.x 额外发 /8（10.255.255.255）—— 实测那是纯噪音地址，
        #   而且会把包发到不该去的网络；去掉。
        out.append((ip, 24))

    try:
        # 1) 主机名解析
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            _add(info[4][0])
    except Exception:                                            # noqa: BLE001
        pass
    # 2) UDP 出口兜底（可能拿到虚拟网卡地址，_add 内部已过滤）
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        _add(s.getsockname()[0])
    except Exception:                                            # noqa: BLE001
        pass
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:                                    # noqa: BLE001
                pass
    return out


def is_unreachable_src(ip):
    """这个源地址**是不是明显不可达**（学会它就等于失联）？

    ★★ 2026-10-08：`_poll_peer` 原来"收到谁就认谁"，结果被虚拟网卡坑死
      （见 `announce_targets` 的注释：源地址变成 `10.6.22.1` ⇒ peer 锁死）。

    判据（保守：只拒绝**确定的**不可达地址，绝不误伤真实局域网）：

      · 回环 / 链路本地 / 组播 / 广播 / 0.x / 255.x → 一定不可达
      · `198.18.` / `198.19.` → 代理 TUN 的保留段（RFC 2544 基准测试段，
        Clash / Mihomo / v2ray 默认就用它，**真实局域网绝不会用**）
      · 169.254. → APIPA，没拿到 DHCP 时的自分配地址

    ⚠ **不拒绝** 10.x / 172.x / 192.168.x —— 它们是合法私有段，
      哪怕本机恰好有一张 10.6.22.1 的虚拟网卡，也不能一刀切掉 10.x
      （企业网真的在用）。真正解决虚拟网卡的办法是 `announce_targets()`
      让包从**正确网卡**发出去，而不是在这里拉黑。
    """
    ip = (ip or "").strip()
    if not ip:
        return True
    if ip.startswith(("127.", "0.", "255.", "169.254.", "224.", "239.", "198.18.",
                      "198.19.")):
        return True
    if ip == "255.255.255.255":
        return True
    return False


def _vlans_of(ips):
    """把 IP 列表按"前三段"归组，返回 {前缀: [ip,...]}。

    用途：识别**孤立网卡** —— 一张网卡的网段里只有它自己（没有别的设备），
    几乎一定是虚拟网卡 / 未接线的接口，对它广播纯属噪音。

    ⚠ 判据必须保守：只把"该网段真的只有自己一个"的标为孤立。
      真实局域网一定有路由器（.1）、网关、别的设备 ⇒ 不会只剩自己。
    """
    out = {}
    for ip in ips or []:
        try:
            pfx = ".".join(ip.split(".")[:3])
        except Exception:                                        # noqa: BLE001
            continue
        out.setdefault(pfx, []).append(ip)
    return out


def _isolated_nics(hosts, nics=None):
    """给定「网段内已知活跃主机」列表，算出**孤立网卡**（其网段里只有自己）的 IP。

    用途见 `trivial_nics` / `announce_targets`：孤立网卡几乎一定是穿透工具的
    虚拟网卡（如 `10.6.22.1`），对它宣告毫无意义。

    ⚠ 与 `trivial_nics()` 的唯一区别：本函数**接受外部传入的 `hosts`**，
      从而**避免重复调用 `lan_hosts()`**（内部 `arp -a` 实测 152ms。
      2026-10-08：`announce_sockets()` 曾因重复调用导致单次耗时 427ms，
      本函数就是为消除那次重复而拆出来的）。
      这是"纯计算、零 I/O"的函数 —— 调用方负责把 `hosts` 备好。

    ## ★★ 2026-10-08 P0（QA 独立复核抓到，必须守住）
    `hosts` 为空（或全是本机自己的 IP）时，**绝不能**判任何网卡为孤立。
    因为"一张 ARP 邻居都没有"此时是**整张表的现象**，不是某张网卡的特征：

      · 若 hosts=[] 就把每张网卡都判孤立 ⇒ `announce_targets()` 返回空
        ⇒ `_announce()` 落到回退分支（含 `255.255.255.255`，源地址会被虚拟网卡
        抢成 `10.6.22.1`）⇒ A 机学到不可达 peer ⇒ **永久失联（无自愈路径）**。
        QA 复现原话：`B2 lan_hosts() 返回 []：仍发出 3 个包
                     [255.255.255.255, 10.6.22.255, 192.168.31.255]`

    ⚠ 判据必须**先问**「这段时间 / 这个前提下，它本来该不该有数据」——
      「没有 ARP 邻居」≠「这张网卡孤立」。
      宁可多发几个源地址正确的子网广播，**绝不误伤真实网卡**。
    """
    my = set(local_ipv4_all())
    # 先剔除 hosts 里属于**本机自己**的 IP，得到真正的"外部邻居"。
    neighbors = [h for h in (hosts or []) if h and h not in my]
    if not neighbors:
        # ARP 表为空 / 没有任何外部设备 ⇒ 无法区分"孤立"（这是全表现象）⇒
        # **一律不判孤立**，交给 `announce_targets()` 正常发子网定向广播。
        return []
    groups = _vlans_of(list(my) + list(neighbors))
    if nics is None:
        nics = _local_ipv4_with_prefix()
    out = []
    for ip, _p in nics:
        pfx = ".".join(ip.split(".")[:3])
        peers = [x for x in groups.get(pfx, []) if x != ip]
        if not peers:
            out.append(ip)          # 该网段只有自己（且确实存在别的网段设备）⇒ 孤立/虚拟
    return out


def trivial_nics(port=DEFAULT_PORT):
    """**明显是虚拟/孤立**的网卡 IP 列表（给 `announce_targets` 跳过它们）。

    ⚠ 刻意**不做**的事：不在这里判断"谁是虚拟网卡"（做不到可靠）。
      只判断"这张网卡的网段里有没有别的设备"—— 没有就跳过。
      这样：
        · `192.168.31.150`（网段里有 .1 路由器 / .96 A机）⇒ 保留 ✔
        · `10.6.22.1`（网段里只有它自己）⇒ 跳过 ✔（natpierce 那种点对点虚拟网卡）
      而企业网 10.x（网段里有一堆设备）⇒ 保留 ✔ —— 不会误伤。

    ⚠ 本函数自己会跑一次 `arp -a`。**在宣告路径上不要直接用它** ——
      那里应改调 `_isolated_nics(hosts, nics)`（复用同一个 `hosts`，
      见 `announce_targets`）。本函数保留给测试 / 独立诊断使用。
    """
    try:
        hosts = lan_hosts()
    except Exception:                                            # noqa: BLE001
        hosts = []
    return _isolated_nics(hosts)


def local_ip():
    """本机在局域网里的 IPv4。

    ⚠⚠ 2026-10-08 实测踩到：最朴素的实现（UDP connect 到 10.255.255.255 看出口）
      **会返回虚拟网卡地址**（本机拿到 `198.18.0.1`，是代理/VPN 的 TUN 接口）。
      用户照这个 IP 填到对端 ⇒ **永远连不上**，而且看起来"IP 明明是对的"（界面自己显示的）。
      ⇒ 必须**枚举真实网卡并优先挑私有网段**，而且**排除**已知虚拟网段。

    选法（按优先级）：
      1. 192.168.x.x / 10.x.x.x 且不在虚拟段黑名单里 → 直接用
      2. 172.16~31.x.x 同上
      3. 其它非回环、非虚拟的
      4. 全都选不出 → 返回空串（界面显示"未检测到局域网连接"，绝不显示假 IP）
    """
    cands = [ip for ip, _p in _local_ipv4_with_prefix()]
    if not cands:
        return ""

    def _rank(ip):
        if ip.startswith("192.168."):
            return 0                   # 家用/小型场地最常见
        if ip.startswith("10."):
            return 1
        if ip.startswith("172."):
            return 2
        return 3

    cands.sort(key=_rank)
    return cands[0]


def make_sender(port=DEFAULT_PORT, bind="0.0.0.0"):
    """建发送 socket（同时接收 B 机发来的 HELLO — 同一端口双向）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    except Exception:                                            # noqa: BLE001
        pass
    s.bind((bind, int(port)))
    s.setblocking(False)
    return s


# ==========================================================================
# ★★ 2026-10-08 现场踩到（用户报「我这边软件都没打开，发送端也显示已握手」）
#   —— 「宣告源地址被虚拟网卡抢走」的根治。
#
# ## 现象与根因（实测证据）
#
# `LanDevice._announce()` 用 `self._sock`（bind 0.0.0.0）向
# `255.255.255.255` 发 HELLO。本机装了穿透工具（natpierce / 代理 TUN）时，
# **受限广播的出站路由会被虚拟网卡抢走**。实测（本机 192.168.31.150
# + natpierce 虚拟网卡 10.6.22.1）：
#
#     发到 255.255.255.255  ⇒ 对端看到的源 IP = 10.6.22.1   ← ⚠ 不可达
#     发到 192.168.31.255   ⇒ 对端看到的源 IP = 192.168.31.150 ← ✔ 正确
#     发到 192.168.31.150   ⇒ 对端看到的源 IP = 192.168.31.150 ← ✔ 正确
#
# A 机的 `_poll_peer` **不校验源地址**，收到谁就把 peer 记成谁
# ⇒ peer 被锁死到 `10.6.22.1:47811`（一个根本回不去的地址）。
# 而 `_targets()` 一旦有 peer 就**只发 peer、永不广播**
# ⇒ 两边彻底失联。**而双方界面都显示"已握手"** —— 用户完全无从判断。
#
# ## 修法（含 2026-10-08 P0 回归修正）
#
# **不发 `255.255.255.255`**（它正是被抢的那条），只发每张真实网卡的
# 子网定向广播 + 网段内已知主机单播。这些目标都在**直连子网路由**上，
# 源地址由路由表正确决定、虚拟网卡抢不走 —— 因此**不需要额外 socket**。
#
# ⚠⚠ 曾经的错误修法（已废弃，见 `announce_targets` docstring）：
#   "每张网卡各建一个 `bind((网卡IP, 监听端口))` 的 socket"。
#   Windows 上这会**每秒造成 ~330ms 的接收黑洞**（同端口下绑具体 IP 的
#   socket 优先收包），吞掉约 1/3 音频。**绝不要再回到那个写法。**
# ==========================================================================

def announce_targets(port=DEFAULT_PORT):
    """「主动宣告」应发往的地址列表（**不含受限广播 `255.255.255.255`**）。

    返回 `[(ip, port), ...]`。调用方用**接收 socket 本身**（`self._sock`，
    绑 `0.0.0.0`）向这些地址 `sendto` 即可 —— **不需要任何额外 socket**。

    ## ★★ 2026-10-08 P0 回归根治（用户报「音频每秒被吞约 1/3」）

    上一轮为解决「宣告源地址被虚拟网卡抢走」，新增了 `announce_sockets()`：
    **每张真实网卡各建一个 socket 并 `bind((该网卡IP, 监听端口))`**。
    这在 **Windows** 上埋了雷：

      · 接收 socket `self._sock` 绑 `(0.0.0.0, port)`；
      · 宣告 socket 绑 `(192.168.31.150, port)`（**同端口**）。
      · Windows 的收包归属**与 Linux 相反**：同端口下「绑具体 IP」的 socket
        **优先收包**。实测（`tools/_test_ann_steal.py`）：
            从 192.168.31.150 向 192.168.31.150:47897 发 3 个包
            → self._sock 收 0，宣告 socket 收 3。
      · `_announce()` 每秒建一次这些 socket，它们在**函数执行期间存活**
        （含 `arp -a`，实测 ~330ms）⇒ **每秒一个 ~330ms 的「接收黑洞」**，
        期间对端发往本机 `具体IP:port` 的 **DATA 包全被宣告 socket 收下并丢弃**
        ⇒ **约 1/3 音频包被吞**。

    ## 这一版为什么**不再需要任何额外 socket**

    关键实测事实（见 `broadcast_targets` docstring）：`self._sock` 向
    **具体目标 IP** 发送时（子网定向广播如 `192.168.31.255`、同网段单播如
    `192.168.31.96`），**源地址由路由表正确决定，不会被虚拟网卡抢**；
    只有 `255.255.255.255`（受限广播）会被抢。
    ⇒ 只要目标里**不含 `255.255.255.255`**，就不需要"另建 socket 钉源地址"，
      直接用 `self._sock` 发即可：

        · 源地址 = 真实网卡地址（路由表决定）                     ✔
        · 源端口 = 监听端口（`self._sock` 本就绑在 `port`）        ✔ ← 硬要求
        · **不再有第二个绑同端口的 socket** ⇒ 无人抢 `self._sock` 的包 ✔ ← P0

    ## 目标构成
    每张**非孤立**真实网卡的「子网定向广播」+ 同网段内**已知主机单播**。
    ⚠ 全程**只调用一次 `lan_hosts()`**（`arp -a` 实测 152ms）——
      这是把单次宣告耗时压到 < 180ms 的关键（旧版重复调了两次 ⇒ 427ms）。
    """
    nics = _local_ipv4_with_prefix()
    if not nics:
        return []
    try:
        hosts = lan_hosts()          # ★ 全程唯一一次 arp -a（耗时关键，勿加成两次）
    except Exception:                                            # noqa: BLE001
        hosts = []
    # ★ 跳过"孤立网卡"（网段里只有自己 —— 几乎一定是穿透工具的虚拟网卡）。
    #   实测（本机）：`10.6.22.1` 的网段里只有它自己 ⇒ 跳过；
    #   `192.168.31.150` 网段里有 .1/.96/.97/.148 ⇒ 保留。
    #   ⚠ 用 `_isolated_nics(hosts, nics)`（复用上面的 hosts），**不要**用
    #     `trivial_nics()` —— 后者会**再跑一次 arp -a**。
    try:
        skip = set(_isolated_nics(hosts, nics))
    except Exception:                                            # noqa: BLE001
        skip = set()
    out = []
    for ip, prefix in nics:
        if ip in skip:
            continue
        # 该网卡自己的**子网定向广播**（如 192.168.31.255）——**不是** 255.255.255.255
        b = _subnet_broadcast(ip, prefix)
        if b:
            t = (b, int(port))
            if t not in out:
                out.append(t)
        # 同网段的已知设备单播
        # ⚠ 只发**与自己同网段**的目标：实测从 `10.6.22.1` 向 `192.168.31.x`
        #   单播会 `WinError 10051`（跨网段不可达）。虽然失败无害，
        #   但逐包抛异常会拖慢宣告节奏。同网段过滤后，每张网卡只发它真能到的地方。
        pfx = ".".join(ip.split(".")[:3]) + "."
        for h in hosts:
            if not h.startswith(pfx):
                continue             # 跨网段：发不出去，跳过
            t = (h, int(port))
            if t not in out:
                out.append(t)
    return out


def make_receiver(port=DEFAULT_PORT, bind="0.0.0.0"):
    """建接收 socket。

    ⚠ SO_REUSEADDR **必须**：A 机与 B 机是两台机器时无所谓，
      但同一台机器上跑"自测"（A+B 同机）时不加会 bind 失败，
      用户会误以为"功能坏了"。

    ⚠⚠ SO_BROADCAST **也必须**（2026-10-08 踩到）：接收端现在还要**主动广播 HELLO**
      （见 `lan_audio_recv.LanDevice._announce`），告诉 A 机"我在这里"。
      没有 SO_BROADCAST 时 `sendto(255.255.255.255)` 直接 `PermissionError
      [WinError 10013]` —— 而且它发生在 `broadcast_targets()` 的**第一个目标**上，
      会把后面所有目标一起带崩（异常被同一个 try 吞掉）。
      症状极隐蔽：B 机"看起来在宣告"，实际一个包都没出去。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    except Exception:                                            # noqa: BLE001
        pass
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RcvBufSize)
    except Exception:                                            # noqa: BLE001
        pass
    s.bind((bind, int(port)))
    s.setblocking(False)
    return s


def qpc_now():
    """本机 QPC（100ns）。Windows 上就是驱动给时间戳的同一个时钟源。"""
    try:
        import ctypes
        v = ctypes.c_longlong(0)
        ctypes.windll.kernel32.QueryPerformanceCounter(ctypes.byref(v))
        return int(v.value)
    except Exception:                                            # noqa: BLE001
        return int(time.perf_counter() * 1e7)


def qpc_freq():
    """QPC 频率（每秒多少个单位）。Windows 上通常是 10,000,000。"""
    try:
        import ctypes
        v = ctypes.c_longlong(0)
        ctypes.windll.kernel32.QueryPerformanceFrequency(ctypes.byref(v))
        return int(v.value) or 10000000
    except Exception:                                            # noqa: BLE001
        return 10000000
