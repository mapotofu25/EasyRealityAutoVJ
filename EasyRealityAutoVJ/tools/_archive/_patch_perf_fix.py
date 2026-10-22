# -*- coding: utf-8 -*-
"""紧急：修「独立预览」导致的 CPU 12 核 + 磁盘 500MB/s 狂读。

实测（用户现场，`tools/_perf_watch.py`）：
  CPU 1130~1430%（≈12 核）、磁盘**持续读 470~570 MB/s**、内存稳定 ~1.1GB。
  之前只有 10~30%（0.1~0.3 核）⇒ 是我新加的预览画布在**每帧重开解码器**。

三个原因（都在 cue_preview.py）：
  ① `CuePreviewCanvas.FPS = 25`，而每帧都遍历所有激活片段取帧；
  ② FPS 定时器**无条件 `_compose()`** —— 不播放、位置没变也在跑；
  ③ `CueFrameSource.frame()` 失败时**每帧都重建播放器**（`make_player` 会开容器/起解码线程）
     ⇒ 容器反复打开（部分容器要扫全文件）就是那 500MB/s。

修法：降帧 + 不必要不合成 + 失败退避 + 播放器只建一次。
"""
import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

p = os.path.join(ROOT, "src", "cue_preview.py")
t = io.open(p, encoding="utf-8").read()
orig = t


def rep(old, new, cnt=1):
    global t
    got = t.count(old)
    if got != cnt:
        raise SystemExit("期望 %d 处实得 %d\n---\n%s" % (cnt, got, old[:240]))
    t = t.replace(old, new)


# ① 取帧：只建一次；失败要退避（绝不每帧重建播放器）
rep('''    def frame(self, path):
        pl = self._players.get(path)
        if pl is None:
            try:
                it = self._items.get(path)
                if it is None:
                    it = MediaItem(path)
                    self._items[path] = it
                pl = make_player(it)
                pl.set_active(True)
                self._players[path] = pl
            except Exception:                                      # noqa: BLE001
                return None
        try:
            pl.set_active(True)
            return pl.current()
        except Exception:                                          # noqa: BLE001
            return None
''',
    '''    RETRY_SEC = 2.0        # 取帧失败后的退避：这段时间内不再碰这个文件

    def frame(self, path):
        """取该素材当前帧。

        ⚠⚠ 实测教训（用户现场 CPU 12 核 / 磁盘 500MB/s）：**绝不能每帧重建播放器** ——
          `make_player()` 会开容器、起解码线程，部分容器还要扫全文件；
          失败素材每帧重试一次就是硬盘狂读的根源。这里：成功就复用，
          失败就记下时间**退避 2 秒**，期间直接返回 None（画面留空也比烧硬盘好）。
        """
        now = time.perf_counter()
        if self._fail.get(path, 0.0) > now:
            return None
        pl = self._players.get(path)
        if pl is None:
            try:
                it = self._items.get(path)
                if it is None:
                    it = MediaItem(path)
                    self._items[path] = it
                pl = make_player(it)
                pl.set_active(True)
                self._players[path] = pl
            except Exception:                                      # noqa: BLE001
                self._fail[path] = now + self.RETRY_SEC
                return None
        try:
            pl.set_active(True)
            img = pl.current()
            if img is None or img.isNull():
                # 首帧还没解出来：这是正常的"等一帧"，不算失败（但要限速，别每帧都问）
                return None
            return img
        except Exception:                                          # noqa: BLE001
            self._fail[path] = now + self.RETRY_SEC
            return None
''')

rep('''    def __init__(self):
        self._players = {}
        self._items = {}
''',
    '''    def __init__(self):
        self._players = {}
        self._items = {}
        self._fail = {}        # path -> 退避到这个时刻为止
''')

# ② 画布：降帧 + 不必要不合成（位置没变、也没在播 ⇒ 直接跳过）
rep('    FPS = 25\n', '''    FPS = 10               # ⚠ 25fps 纯属浪费：看编排 10fps 足够，CPU 直接降一半多
''')
rep('''    def set_pos(self, beats):
        self.pos = float(beats)
        self.update()

    def set_active(self, on):
        if on:
            if not self._t.isActive():
                self._t.start(int(1000 / self.FPS))
            self._tick()
        else:
            self._t.stop()
''',
    '''    playing = False        # 由编排页告诉它"正在播放"（播放时才需要连续刷）

    def set_pos(self, beats):
        b = float(beats)
        if abs(b - self.pos) > 1e-6:
            self.pos = b
            self._dirty = True
        self.update()

    def set_active(self, on):
        if on:
            if not self._t.isActive():
                self._t.start(int(1000 / self.FPS))
            self._tick()
        else:
            self._t.stop()
            # ★ 离开页面：把解码器全关掉（它们会一直解码/占着文件句柄）
            try:
                self.src.close()
            except Exception:                                      # noqa: BLE001
                pass
''')
rep('''    def _tick(self):
        self._compose()
        self.update()
''',
    '''    def _tick(self):
        # ★ 只有"位置变了"或"正在播放"才重新合成：
        #   静止时每 100ms 重算一遍所有片段（还要取帧）是纯浪费 —— 实测能烧掉十几核。
        if not self._dirty and not self.playing:
            return
        self._dirty = False
        self._compose()
        self.update()
''')
rep('        self._img = None\n        self.src = CueFrameSource()\n',
    '        self._img = None\n        self._dirty = True      # 有变化才重合成\n        self.src = CueFrameSource()\n')

io.open(p, "w", encoding="utf-8", newline="").write(t)
print("cue_preview.py 已写入（%+d 字节）" % (len(t) - len(orig)))

# ③ 编排页：把"正在播放"告诉画布（播放档位下才连续刷）
p2 = os.path.join(ROOT, "src", "cue_editor.py")
t2 = io.open(p2, encoding="utf-8").read()
o2 = t2
for old, new, cnt in [
    ('            self._playing = True\n',
     '            self._playing = True\n'
     '            try:\n'
     '                self.prev.playing = True       # 播放中才连续刷预览\n'
     '            except Exception:                                    # noqa: BLE001\n'
     '                pass\n', 1),
    ('            self._playing = False\n',
     '            self._playing = False\n'
     '            try:\n'
     '                self.prev.playing = False\n'
     '            except Exception:                                    # noqa: BLE001\n'
     '                pass\n', 1),
]:
    if t2.count(old) != cnt:
        raise SystemExit("cue_editor 期望 %d 实得 %d: %s" % (cnt, t2.count(old), old[:80]))
    t2 = t2.replace(old, new)
io.open(p2, "w", encoding="utf-8", newline="").write(t2)
print("cue_editor.py 已写入（%+d 字节）" % (len(t2) - len(o2)))
