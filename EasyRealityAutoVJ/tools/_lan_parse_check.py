# -*- coding: utf-8 -*-
"""验证 `_run_lan` 的 device_name 解析 —— 用户现场填的值到底是什么行为。"""

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import lan_audio as LA   # noqa: E402


def parse(txt):
    """复刻 audio_engine._run_lan 的解析逻辑（逐行抄，不改）。"""
    port = LA.DEFAULT_PORT
    peer_hint = ""
    txt = (txt or "").strip()
    if txt:
        host, _, tail = txt.rpartition(":")
        if host and tail and tail.isdigit():
            peer_hint = host.strip()
            p = int(tail)
            if 1 <= p <= 65535:
                port = p
        elif txt.replace(".", "").isdigit() and "." in txt:
            peer_hint = txt
        else:
            digits = "".join(c for c in txt if c.isdigit())
            if digits:
                try:
                    p = int(digits)
                    if 1 <= p <= 65535:
                        port = p
                except Exception:
                    pass
    return port, peer_hint


CASES = [
    ("",                    "空 ⇒ 默认端口 + 自动发现"),
    ("47811",               "纯端口"),
    ("192.168.31.96",       "纯 IP（用户说明里写的用法）"),
    ("192.168.31.96:47811", "IP:端口  ← ★ 用户 config 里实际存的值"),
    ("192.168.31.150",      "本机自己的 IP（用户可能填错）"),
    ("10.6.22.1",           "虚拟网卡 IP（用户可能填错）"),
    ("999.999.999.999",     "非法 IP 形状"),
    ("1080",                "端口号但像 4 位数"),
]

print("=" * 78)
print("_run_lan device_name 解析验证")
print("=" * 78)
print("%-24s %-8s %-20s %s" % ("输入", "端口", "peer_hint", "说明"))
print("-" * 78)
for txt, desc in CASES:
    port, hint = parse(txt)
    flag = ""
    if txt and not hint and not txt.isdigit():
        flag = "  ⚠ 未识别成 IP"
    print("%-24s %-8s %-20s %s%s" % (repr(txt), port, repr(hint), desc, flag))

print()
print("=" * 78)
print("关键：用户 config 里的 '192.168.31.96:47811' 解析结果")
print("=" * 78)
port, hint = parse("192.168.31.96:47811")
print("  port      = %s" % port)
print("  peer_hint = %s" % repr(hint))
print()
if hint == "192.168.31.96" and port == 47811:
    print("  ⇒ 解析**正确**：只接受 192.168.31.96 发来的包，端口 47811。")
    print("     而实测 A 机正是 192.168.31.96 ⇒ 过滤不会误伤。")
    print("     ⇒ 解析不是根因。")
else:
    print("  ⇒ ⚠⚠ 解析**错误**！这就是收不到的根因。")
