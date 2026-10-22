# -*- coding: utf-8 -*-
"""修「CPU 一点一点涨到 100%」的机器级原因：三条取帧生成器会**空转**（无 sleep 的死循环）。

现场证据（2026-10-01，用户实例 PID 20704）：
  · 解码线程数 19 → 41；其中"正在软解"的线程 **2 → 18 个**（12 秒内），而按激活逻辑
    每层最多 2 个活跃 ⇒ 多出来的是**卡在生成器里空转**的；
  · py-spy 叶子占比：`_soft_frames:1480`（decode）60% → **75%**、`_rewind:1375` 1.3%
    ⇒ 既是解码、又在反复重绕；
  · 用户描述「不是一下 70，是**一点一点上来**」⇒ 累积型，与"空转的解码器越积越多"一致。

根因：三个生成器都是 `while True: 解码一趟 → _rewind() → 再解码`。
**只要某趟一个帧都没产出**（`decode()` 抛异常 / demux 全是坏包 / `frame_blocks` 全返回 None），
而 `_rewind()` 又成功（能 seek 回片头），这个 while **没有任何 sleep** ⇒
白占一个核 + 反复重开容器（磁盘狂读），而且**永不自愈**。

修法：三处统一加"空转退避 + 放弃"——
  · 连续空转 ≥3 趟开始 sleep（20ms 起、封顶 200ms）；
  · 连续空转 ≥30 趟判定"这个素材解不出来" ⇒ 收工（`_loop` 随后 break 并释放容器）。
正常文件每趟都有产出 ⇒ 行为完全不变（不会多 sleep、不会误放弃）。
"""
import io
import os
import sys

ROOT = r"P:\AutoVJ"
P = os.path.join(ROOT, "src", "media_manager.py")
PAT = []


def rep(old, new, cnt=1):
    PAT.append((old, new, cnt))


# ---------------- 1) 模块级常量 + 日志助手 ----------------
rep("""class GpuDxvPlayer:""",
    """# ---- 解码生成器的"空转退避"（2026-10-01 现场：CPU 从 2 核悄悄涨到满核）----
#   ⚠ 三个取帧生成器都是 `while True: 解码一趟 → _rewind() → 再解码`。只要某趟
#     **一个帧都没产出**（decode 抛异常 / demux 全是坏包 / frame_blocks 全返回 None），
#     而 `_rewind()` 又成功（能 seek 回片头），这个 while 就**没有任何 sleep**
#     ⇒ 白占一个核 + 反复重开容器（磁盘狂读），且**永不自愈**。
#     现场实测：正在软解的线程 12 秒内从 2 个涨到 18 个、CPU 一路到 100%。
#   ⇒ 连续空转就退避，连续空转太多就判定"这个素材解不出来"、收工（释放容器）。
_SOFT_EMPTY_BACKOFF_AFTER = 3      # 连续空转几趟后开始 sleep
_SOFT_EMPTY_GIVEUP = 30            # 连续空转这么多趟 ⇒ 放弃这个素材


def _log_decoder_giveup(path, why):
    \"\"\"解码器放弃时记一行（best-effort，绝不抛）。\"\"\"
    try:
        import stallwatch
        stallwatch.log_line("!! 解码器放弃：%s（%s）—— 已停止该路解码，避免空转烧 CPU"
                            % (os.path.basename(str(path or "")), why))
    except Exception:                                              # noqa: BLE001
        pass


class GpuDxvPlayer:""")

# ---------------- 2) _soft_frames ----------------
rep('''    def _soft_frames(self):
        """软解路径：与 AvAlphaPlayer 完全一致的 PyAV 解码（降级用）。"""
        while True:
            try:
                for frame in self.c.decode(video=0):
                    arr = frame.to_ndarray(format="bgra")
                    h, w = arr.shape[:2]
                    if self._max_w and self._max_h:
                        s = min(self._max_w / w, self._max_h / h)
                        if s < 1.0:
                            arr = cv2.resize(arr, (max(1, int(w * s)), max(1, int(h * s))),
                                             interpolation=cv2.INTER_AREA)
                            h, w = arr.shape[:2]
                    yield QImage(arr.data, w, h, 4 * w, self._qfmt).copy()
            except Exception:
                pass
            if not self._rewind():
                return
''',
    '''    def _soft_frames(self):
        """软解路径：与 AvAlphaPlayer 完全一致的 PyAV 解码（降级用）。

        ⚠ 必须有"空转退避"：见 `_SOFT_EMPTY_BACKOFF_AFTER` 的说明 —— 否则一个解不出来
          的素材会**占满一个核空转**，而且现场就是这样把 CPU 一点点拖到 100% 的。
        """
        empty = 0
        while True:
            got = 0
            try:
                for frame in self.c.decode(video=0):
                    got += 1
                    arr = frame.to_ndarray(format="bgra")
                    h, w = arr.shape[:2]
                    if self._max_w and self._max_h:
                        s = min(self._max_w / w, self._max_h / h)
                        if s < 1.0:
                            arr = cv2.resize(arr, (max(1, int(w * s)), max(1, int(h * s))),
                                             interpolation=cv2.INTER_AREA)
                            h, w = arr.shape[:2]
                    yield QImage(arr.data, w, h, 4 * w, self._qfmt).copy()
            except Exception:                                      # noqa: BLE001
                pass
            empty = 0 if got else empty + 1
            if empty >= _SOFT_EMPTY_BACKOFF_AFTER:
                time.sleep(min(0.2, 0.02 * empty))
            if empty >= _SOFT_EMPTY_GIVEUP:
                _log_decoder_giveup(self.path, "软解连续 %d 趟无帧" % empty)
                return
            if not self._rewind():
                return
''')

# ---------------- 3) _soft_until_recover ----------------
rep('''        next_probe = time.perf_counter() + self._GPU_REPROBE_SECS
        while True:
            try:
                for frame in self.c.decode(video=0):
                    arr = frame.to_ndarray(format="bgra")''',
    '''        next_probe = time.perf_counter() + self._GPU_REPROBE_SECS
        empty = 0
        while True:
            got = 0
            try:
                for frame in self.c.decode(video=0):
                    got += 1
                    arr = frame.to_ndarray(format="bgra")''')
rep('''                    yield QImage(arr.data, w, h, 4 * w, self._qfmt).copy()
                    now = time.perf_counter()
                    if now >= next_probe:
                        next_probe = now + self._GPU_REPROBE_SECS
                        if worker.available():
                            return True       # ★ 恢复：切回 GPU 路径
            except Exception:
                pass
            if not self._rewind():
                return False''',
    '''                    yield QImage(arr.data, w, h, 4 * w, self._qfmt).copy()
                    now = time.perf_counter()
                    if now >= next_probe:
                        next_probe = now + self._GPU_REPROBE_SECS
                        if worker.available():
                            return True       # ★ 恢复：切回 GPU 路径
            except Exception:                                      # noqa: BLE001
                pass
            # ⚠ 空转退避：理由同 `_soft_frames`（解不出来的素材不能占着一个核空转）
            empty = 0 if got else empty + 1
            if empty >= _SOFT_EMPTY_BACKOFF_AFTER:
                time.sleep(min(0.2, 0.02 * empty))
            if empty >= _SOFT_EMPTY_GIVEUP:
                _log_decoder_giveup(self.path, "软解(重探)连续 %d 趟无帧" % empty)
                return False
            if not self._rewind():
                return False''')

# ---------------- 4) _gpu_frames ----------------
rep("""            gone = False
            try:
                for pkt in self.c.demux(self.st):
                    if not pkt.size:
                        continue""",
    """            gone = False
            produced = 0
            try:
                for pkt in self.c.demux(self.st):
                    if not pkt.size:
                        continue""")
rep("""                    yield QImage(arr.data, dw, dh, 4 * dw, self._qfmt).copy()
            except Exception:
                pass
            if gone:
                continue                  # 回顶部：降级 → 软解 + 周期重探（worker 恢复即回 GPU）
            if not self._rewind():
                return""",
    """                    produced += 1
                    yield QImage(arr.data, dw, dh, 4 * dw, self._qfmt).copy()
            except Exception:                                          # noqa: BLE001
                pass
            if gone:
                continue                  # 回顶部：降级 → 软解 + 周期重探（worker 恢复即回 GPU）
            # ⚠ 空转退避：整趟 demux 一个帧都没解出来（全是坏包 / 不是真 DXV）时，
            #   立刻 `_rewind()` 会变成无 sleep 的死循环，而且每趟都白跑一次 native 解包。
            empty = 0 if produced else empty + 1
            if empty >= _SOFT_EMPTY_BACKOFF_AFTER:
                time.sleep(min(0.2, 0.02 * empty))
            if empty >= _SOFT_EMPTY_GIVEUP:
                _log_decoder_giveup(self.path, "GPU 路径连续 %d 趟无帧" % empty)
                return
            if not self._rewind():
                return""")
rep("""        worker = glctx.GLWorker.get()
        while True:
            if not worker.available():""",
    """        worker = glctx.GLWorker.get()
        empty = 0
        while True:
            if not worker.available():""")


def main():
    s = io.open(P, encoding="utf-8").read()
    for i, (old, new, cnt) in enumerate(PAT, 1):
        n = s.count(old)
        if n != cnt:
            print("✗ 第 %d 处命中 %d 次（应为 %d）：%s" % (i, n, cnt, old.strip()[:70]))
            return 1
    for old, new, cnt in PAT:
        s = s.replace(old, new)
    io.open(P, "w", encoding="utf-8", newline="\n").write(s)
    print("✓ media_manager.py 已加「空转退避 + 放弃」（%d 处）" % len(PAT))
    import py_compile
    py_compile.compile(P, doraise=True)
    print("语法 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
