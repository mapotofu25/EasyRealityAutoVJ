# -*- coding: utf-8 -*-
"""给 NDI 发送端加**连接数门控**：没有人连上时**根本不调用** send_video/send_audio。

为什么必须加（2026-10-01 实测）：
  · 没有接收端连上时调 `sender.send_audio()` 会**段错误**（native crash，Python 抓不住）；
    同一位置的 `send_video` 会抛 `Object is not writable`（被丢帧，不致命）。
  · 实测 sender 有 `get_num_connections(timeout) -> int` —— 用它做门控：
    0 连接 ⇒ 这一帧/这一块直接丢掉（同时计入诊断计数），**不碰 native 调用**。
  ⇒ 既避开崩溃，也符合 NDI 语义（没人看就没必要发）。
"""
import io
import os
import sys

P = r"P:\AutoVJ\src\ndi_out.py"
PAT = []


def rep(old, new, cnt=1):
    PAT.append((old, new, cnt))


# 1) 加一个连接数探针
rep("""    def send_video(self, bgra, w, h):""",
    """    def _conns(self):
        \"\"\"当前有几个接收端连着（0 = 没人连，**绝不能**调 send_*）。

        ⚠⚠ 2026-10-01 实测：没有接收端时 `send_audio()` 会**段错误**（让整个软件崩掉），
          `send_video()` 会抛 `Object is not writable`。所以一律先看连接数，0 就直接丢帧。
        \"\"\"
        try:
            return int(self._sender.get_num_connections(0.0))
        except Exception:                                          # noqa: BLE001
            return 1          # 探针本身失败时按"有人连"处理，保持老行为（不误伤正常输出）

    def send_video(self, bgra, w, h):""")

# 2) send_video 门控
rep("""                # ⚠ 开门静默期：这段时间内提交帧会抛（视频）/崩（音频），一律丢掉
                if time.perf_counter() - self._opened_at < self._WARMUP_SECS:
                    self._dropped += 1
                    return""",
    """                # ⚠ 开门静默期：这段时间内提交帧会抛（视频）/崩（音频），一律丢掉
                if time.perf_counter() - self._opened_at < self._WARMUP_SECS:
                    self._dropped += 1
                    return
                # ⚠ 没有接收端连上就**不要碰 native**（实测音频那条路会段错误）
                if self._conns() <= 0:
                    self._dropped += 1
                    return""")

# 3) feed_audio 门控
rep("""                # ⚠ 开门静默期：这一条是**防段错误**的（实测 open 后立刻 send_audio 会崩）
                if time.perf_counter() - self._opened_at < self._WARMUP_SECS:
                    self._audio_dropped += 1
                    return""",
    """                # ⚠ 开门静默期：这一条是**防段错误**的（实测 open 后立刻 send_audio 会崩）
                if time.perf_counter() - self._opened_at < self._WARMUP_SECS:
                    self._audio_dropped += 1
                    return
                # ⚠⚠ 没有任何接收端连上时，`send_audio()` 会**段错误**（实测）。
                #   没人听就没必要发 —— 直接丢块，绝不碰 native 调用。
                if self._conns() <= 0:
                    self._audio_dropped += 1
                    return""")


def main():
    s = io.open(P, encoding="utf-8").read()
    for i, (old, new, cnt) in enumerate(PAT, 1):
        n = s.count(old)
        if n != cnt:
            print("✗ 第 %d 处命中 %d 次（应为 %d）" % (i, n, cnt))
            return 1
    for old, new, cnt in PAT:
        s = s.replace(old, new)
    io.open(P, "w", encoding="utf-8", newline="\n").write(s)
    print("✓ ndi_out.py 已加连接数门控（%d 处）" % len(PAT))
    import py_compile
    py_compile.compile(P, doraise=True)
    print("语法 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
