# -*- coding: utf-8 -*-
"""P0 修复（局域网）：发送端把自己广播的回声当成对端。

审计结论（audit-cuelan）：
  发送端用**同一个 socket** 既发 DATA 又收 HELLO。它广播出去的 DATA 会被本机
  再收回来（`sendto` 到子网广播地址时，Linux/Windows 会把包回环给本机 socket），
  而 `_poll_peer` 只挡了"不可达源地址"，**没挡自己**。
  ⇒ `self._peer` 被学成**自己的 IP**，`_targets()` 从此只发给自己
  ⇒ 音频原路返回本机（对端收不到），而界面显示"已握手、对端 192.168.x.**(自己)**"。

修法：
  1. `start()` 时缓存本机 IPv4 集合；自己的包**直接丢**（计入 `self_echo` 计数）。
  2. `_targets()` 里，若 peer 落在本机地址集内 ⇒ 当作没学到，回退广播。
  3. `_send` 单目标失败不再拖垮整批；目标为空直接返回（不白 pack）。
"""

import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = os.path.join(ROOT, "src", "lan_audio_send.py")


def main():
    s = io.open(P, encoding="utf-8").read()

    # ① start() 里缓存本机地址
    old1 = """        self._stop.clear()
        self.stats = SendStats()
        self.error = \"\""""
    new1 = """        self._stop.clear()
        self.stats = SendStats()
        self.error = ""
        # ★★ P0 审计修复：缓存本机 IPv4。发送端用**同一个 socket** 既发 DATA 又收 HELLO，
        #   而发给子网广播地址的包会被本机回环收回来 ⇒ 以前会把自己学成"对端"，
        #   于是音频只发回自己（对端收不到），界面还显示"已握手"。
        try:
            self._my_ips = set(LA.local_ipv4_all())
        except Exception:                                        # noqa: BLE001
            self._my_ips = set()"""
    assert old1 in s, "① start 锚点没找到"
    s = s.replace(old1, new1, 1)

    # ② _poll_peer 里丢自己的包
    old2 = """            # ★ 闸①：源地址不可达 ⇒ 绝不认（这正是虚拟网卡坑的来源）
            if LA.is_unreachable_src(addr[0]):
                self.stats.bad_src += 1
                continue"""
    new2 = """            # ★ 闸⓪：**自己的包直接丢**（子网广播会被本机回环收回来）。
            #   不丢的话 `self._peer` 会被学成本机地址 ⇒ 音频只发回自己。
            if addr[0] in getattr(self, "_my_ips", ()):
                self.stats.self_echo = int(getattr(self.stats, "self_echo", 0)) + 1
                continue
            # ★ 闸①：源地址不可达 ⇒ 绝不认（这正是虚拟网卡坑的来源）
            if LA.is_unreachable_src(addr[0]):
                self.stats.bad_src += 1
                continue"""
    assert old2 in s, "② poll_peer 锚点没找到"
    s = s.replace(old2, new2, 1)

    # ③ _targets：peer 是本机 ⇒ 回退广播
    old3 = """        if self._peer:
            # ★ 心跳超时 ⇒ 丢弃这个 peer，回退广播（等它再来 HELLO）"""
    new3 = """        # ★ 自愈：万一 peer 已经被学成本机地址（旧版本写的配置/竞态），立刻丢掉
        if self._peer and self._peer[0] in getattr(self, "_my_ips", ()):
            self._peer = None
        if self._peer:
            # ★ 心跳超时 ⇒ 丢弃这个 peer，回退广播（等它再来 HELLO）"""
    assert old3 in s, "③ targets 锚点没找到"
    s = s.replace(old3, new3, 1)

    # ④ _send：目标为空不白 pack；单目标失败不拖垮整批
    old4 = """    def _send(self, kind, seq, ch, sr, n, qpc, payload):
        pkt = LA.pack(kind, seq, ch, sr, n, qpc, payload)
        tg = self._targets()
        for a in tg:
            self._sock.sendto(pkt, a)
        self.stats.packets += 1
        self.stats.bytes += len(pkt) * len(tg)"""
    new4 = """    def _send(self, kind, seq, ch, sr, n, qpc, payload):
        tg = self._targets()
        if not tg:                       # ★ 没有目标就别 pack（未握手时 ~100 次/秒，白烧 CPU）
            return
        pkt = LA.pack(kind, seq, ch, sr, n, qpc, payload)
        sent = 0
        for a in tg:
            try:
                self._sock.sendto(pkt, a)
                sent += 1
            except OSError:
                # ★ 单个目标失败（网卡消失/ARP 未解析）不能拖垮整批
                self.stats.send_errors = int(getattr(self.stats, "send_errors", 0)) + 1
        self.stats.packets += 1
        self.stats.bytes += len(pkt) * sent"""
    assert old4 in s, "④ send 锚点没找到"
    s = s.replace(old4, new4, 1)

    io.open(P, "w", encoding="utf-8").write(s)
    print("lan_audio_send.py 已修 4 处（自回声/回退广播/空目标/单目标失败）")


if __name__ == "__main__":
    sys.exit(main())
