# -*- coding: utf-8 -*-
r"""验证：`announce_targets()` **不会**再创建任何绑在本端口的 socket。

## ⚠ 2026-10-08 更新（P0 回归根治后）
旧脚本验证的是 `announce_sockets()` 能否在「接收 socket 已占用同端口」时建立
—— 而**它无论能否建立都是错的**：只要建了 bind((具体IP, 端口)) 的 socket，
Windows 上它就会抢走 `self._sock` 的包（每秒 ~330ms 黑洞，吞 ~1/3 音频）。
⇒ 现在改为验证**修复后**的形态：`announce_targets()` 只返回地址列表，
**一个额外 socket 都不建** ⇒ netstat 里始终只有 1 行 `0.0.0.0:端口`。

## 用法
    venv\Scripts\python.exe tools/_test_ann_sock_conflict.py [port]
默认 **47899**（避开正式端口 47811）。
"""
import os
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

import lan_audio as LA                                          # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 47899


def udp_bindings(port):
    """netstat 里所有绑在 port 上的 UDP 行。"""
    try:
        r = subprocess.run(["netstat", "-ano", "-p", "UDP"],
                           capture_output=True, text=True,
                           encoding="gbk", errors="replace",
                           **LA._no_window_kwargs())
    except Exception:                                             # noqa: BLE001
        return []
    out = []
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0].upper() == "UDP" and parts[1].endswith(
                ":%d" % port):
            out.append(line.strip())
    return out


def main():
    print("=" * 74)
    print("端口 = %d" % PORT)
    print("=" * 74)

    print("\n① 只有接收 socket（模拟 LanDevice.start 之后）")
    recv = LA.make_receiver(PORT)
    print("   make_receiver -> %r" % (recv.getsockname(),))
    binds1 = udp_bindings(PORT)
    print("   netstat 里 %d 行:" % len(binds1))
    for b in binds1:
        print("     ", b)

    print("\n② 调 announce_targets(%d)（应**不新建任何 socket**）" % PORT)
    t0 = time.perf_counter()
    tg = LA.announce_targets(PORT)
    dt = (time.perf_counter() - t0) * 1000
    print("   --> 返回 %d 个目标（耗时 %.1f ms）" % (len(tg), dt))
    for a in tg[:8]:
        print("       目标 %r" % (a,))

    binds2 = udp_bindings(PORT)
    print("\n③ 此时 netstat 里 %d 行（应仍只有 1 行 0.0.0.0:%d）:"
          % (len(binds2), PORT))
    for b in binds2:
        print("     ", b)

    print("\n④ 确认旧的抢包函数已删除")
    has_old = hasattr(LA, "announce_sockets")
    print("   hasattr(lan_audio, 'announce_sockets') = %s（应为 False）" % has_old)

    recv.close()

    print("\n" + "=" * 74)
    ok = (len(tg) > 0 and len(binds2) == 1 and not has_old)
    if ok:
        print("结论：✅ announce_targets 可用且**不建任何额外 socket**"
              "（不会抢 self._sock 的包）—— 接收黑洞已消除")
    else:
        print("结论：❌ 有问题（目标数=%d，绑定行数=%d，旧函数残留=%s）"
              % (len(tg), len(binds2), has_old))
    print("=" * 74)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
