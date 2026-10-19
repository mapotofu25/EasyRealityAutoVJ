# -*- coding: utf-8 -*-
"""给 NDI 发送端加"刚 open 的静默期"，避开 native 段错误。

实测（2026-10-01）：cyndilib 的 `Sender` 在 `open()` 之后**需要一小段时间**才吃帧：
  · `open()` → 立刻 `send_audio()`  ⇒ **段错误（native crash，Python 抓不住）**
  · `open()` → 等 0.3s → `send_audio()` ⇒ 正常
同一个原因，视频帧刚 open 就写会抛 `Object is not writable`（被丢掉）。
现场路径正好踩这个：视频线程刚建好 sender，音频线程 20ms 后就来了 ⇒ 立刻段错误/丢块。

修法：`_ensure()` 里记 `_opened_at`，`send_video`/`feed_audio` 在开门后的 `_WARMUP_SECS`
之内只丢帧不发；`close()` 时清零。
"""
import io
import os
import sys

P = r"P:\AutoVJ\src\ndi_out.py"
PAT = []


def rep(old, new, cnt=1):
    PAT.append((old, new, cnt))


# 1) 补 import time
rep("import threading\n", "import threading\nimport time\n")

# 2) 类常量 + 状态
rep("""class NDIOutput:
    def __init__(self, name="EasyRealityAutoVJ"):""",
    """class NDIOutput:
    # ⚠⚠ `open()` 之后的**静默期**：cyndilib 实测在这个窗口内提交帧会**段错误**
    #   （native crash，Python 的 try/except 抓不住）。现场路径正好踩：
    #   视频线程刚建好 sender，音频线程 ~20ms 后就 feed_audio ⇒ 立刻崩/丢。
    _WARMUP_SECS = 0.3

    def __init__(self, name="EasyRealityAutoVJ"):""")

rep("""        self._err_logged = None         # 视频侧同样只记第一条""",
    """        self._err_logged = None         # 视频侧同样只记第一条
        self._opened_at = 0.0           # sender 的 open() 时刻（算静默期用）""")

# 3) open 成功后记时间
rep("""            s.open()
            self._sender, self._video, self._audio = s, v, a""",
    """            s.open()
            self._opened_at = time.perf_counter()   # ★ 开始算静默期（见 _WARMUP_SECS）
            self._sender, self._video, self._audio = s, v, a""")

# 4) close 清零
rep("""        self._w = None
        self._h = None""",
    """        self._w = None
        self._h = None
        self._opened_at = 0.0""")

# 5) send_video 静默期
rep("""                v = self._video
                if hasattr(v, "get_write_available") and not v.get_write_available():""",
    """                # ⚠ 开门静默期：这段时间内提交帧会抛（视频）/崩（音频），一律丢掉
                if time.perf_counter() - self._opened_at < self._WARMUP_SECS:
                    self._dropped += 1
                    return
                v = self._video
                if hasattr(v, "get_write_available") and not v.get_write_available():""")

# 6) feed_audio 静默期
rep("""                ch = int(a.num_channels or 2) or 2""",
    """                # ⚠ 开门静默期：这一条是**防段错误**的（实测 open 后立刻 send_audio 会崩）
                if time.perf_counter() - self._opened_at < self._WARMUP_SECS:
                    self._audio_dropped += 1
                    return
                ch = int(a.num_channels or 2) or 2""")


def main():
    s = io.open(P, encoding="utf-8").read()
    for i, (old, new, cnt) in enumerate(PAT, 1):
        n = s.count(old)
        if n != cnt:
            print("✗ 第 %d 处命中 %d 次（应为 %d）：%s" % (i, n, cnt, old.strip()[:60]))
            return 1
    for old, new, cnt in PAT:
        s = s.replace(old, new)
    io.open(P, "w", encoding="utf-8", newline="\n").write(s)
    print("✓ ndi_out.py 已加开门静默期（%d 处）" % len(PAT))
    import py_compile
    py_compile.compile(P, doraise=True)
    print("语法 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
