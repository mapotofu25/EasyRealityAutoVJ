# -*- coding: utf-8 -*-
"""补：空转中的生成器也要能**及时响应 close()**。

为什么必须补：`close()` 只把 `self._dead = True` 置位（不 join）；而卡在取帧生成器
里的线程**从不检查 `_dead`** ⇒ 被 LRU 淘汰后**线程仍在空转、永不退出** ⇒
每淘汰一次就泄漏一个满负荷线程 ⇒ CPU **单调上涨且永不恢复**（现场 19 → 41 个解码线程）。
现在：退避分支里顺手看一眼 `_dead`，被淘汰就立刻收工。
"""
import io
import os
import sys

P = r"P:\AutoVJ\src\media_manager.py"
PAT = []


def rep(old, new, cnt=1):
    PAT.append((old, new, cnt))


# _soft_frames
rep("""            empty = 0 if got else empty + 1
            if empty >= _SOFT_EMPTY_BACKOFF_AFTER:
                time.sleep(min(0.2, 0.02 * empty))
            if empty >= _SOFT_EMPTY_GIVEUP:
                _log_decoder_giveup(self.path, "软解连续 %d 趟无帧" % empty)
                return""",
    """            empty = 0 if got else empty + 1
            if empty >= _SOFT_EMPTY_BACKOFF_AFTER:
                time.sleep(min(0.2, 0.02 * empty))
            if self._dead:             # 已被淘汰/关闭：立刻收工，别继续空转
                return
            if empty >= _SOFT_EMPTY_GIVEUP:
                _log_decoder_giveup(self.path, "软解连续 %d 趟无帧" % empty)
                return""")

# _soft_until_recover
rep("""            empty = 0 if got else empty + 1
            if empty >= _SOFT_EMPTY_BACKOFF_AFTER:
                time.sleep(min(0.2, 0.02 * empty))
            if empty >= _SOFT_EMPTY_GIVEUP:
                _log_decoder_giveup(self.path, "软解(重探)连续 %d 趟无帧" % empty)
                return False""",
    """            empty = 0 if got else empty + 1
            if empty >= _SOFT_EMPTY_BACKOFF_AFTER:
                time.sleep(min(0.2, 0.02 * empty))
            if self._dead:             # 已被淘汰/关闭：立刻收工
                return False
            if empty >= _SOFT_EMPTY_GIVEUP:
                _log_decoder_giveup(self.path, "软解(重探)连续 %d 趟无帧" % empty)
                return False""")

# _gpu_frames
rep("""            empty = 0 if produced else empty + 1
            if empty >= _SOFT_EMPTY_BACKOFF_AFTER:
                time.sleep(min(0.2, 0.02 * empty))
            if empty >= _SOFT_EMPTY_GIVEUP:
                _log_decoder_giveup(self.path, "GPU 路径连续 %d 趟无帧" % empty)
                return""",
    """            empty = 0 if produced else empty + 1
            if empty >= _SOFT_EMPTY_BACKOFF_AFTER:
                time.sleep(min(0.2, 0.02 * empty))
            if self._dead:             # 已被淘汰/关闭：立刻收工
                return
            if empty >= _SOFT_EMPTY_GIVEUP:
                _log_decoder_giveup(self.path, "GPU 路径连续 %d 趟无帧" % empty)
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
    print("✓ 三处空转分支已加 _dead 检查")
    import py_compile
    py_compile.compile(P, doraise=True)
    print("语法 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
