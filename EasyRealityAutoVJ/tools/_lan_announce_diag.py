# -*- coding: utf-8 -*-
"""诊断：B 机 _announce() 的包，A 机到底有没有收到？

关键怀疑点：B 的 `sendto` 目标是 `(host, self.port)`，而 `self.port` 是 **B 的**监听端口。
真实两机上 A 的源端口通常等于 A 的 port（用户填一致）所以没事；
但同机自测时 A 用的是**另一个端口** ⇒ 永远收不到。
本脚本用「A 监听 B 的 port」这一真实两机等价拓扑来验证。
"""
import os
import socket
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import lan_audio as LA          # noqa: E402
import lan_audio_recv as R      # noqa: E402

PORT = 47852


def main():
    dev = R.LanDevice(port=PORT)
    if not dev.start():
        print("FAIL 启动失败: %s" % dev.error)
        return 1

    # 模拟 A 机 socket：**监听同一个 PORT**（真实两机上 A 的源端口 = 它的发送端口，
    # 用户通常两边都填 47811，所以 A 的源端口恰好也是 47811）
    a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    a.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    a.bind(("0.0.0.0", PORT))
    a.setblocking(False)

    print("B 机（监听 %d）开始每秒宣告…" % PORT)
    n = 0
    srcs = set()
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 6.0:
        try:
            buf, addr = a.recvfrom(4096)
            m = LA.unpack(buf)
            if m:
                n += 1
                srcs.add((addr[0], addr[1], m["type"]))
        except BlockingIOError:
            time.sleep(0.01)

    print("\nA 机（同端口）在 6 秒内收到的 B 宣告：%d 个" % n)
    for s in sorted(srcs):
        tname = {1: "DATA", 2: "HELLO", 3: "BYE"}.get(s[2], s[2])
        print("   来源 %s:%d  type=%s" % (s[0], s[1], tname))
    if n > 0:
        print("  ✅ 宣告可达（真实两机上 A 会收到并把目标切到 B）")
        rc = 0
    else:
        print("  ❌ 宣告不可达 —— 需检查 sendto 目标端口")
        rc = 1

    a.close()
    dev.stop()
    print("\n结论：%s" % ("通过" if rc == 0 else "失败"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
