# -*- coding: utf-8 -*-
"""验证 NDI 视频帧「入队即拷贝」修复了接收端闪黑屏。

## 复现的问题（2026-10-08 第三轮）

第二轮把 `send_video` 改成"只入队"后，队列里存的是**调用方画布的引用**
（`engine._ndi_send` 传 `np.frombuffer(canvas.bits(), ...)`）。
而画布每帧被 `_compose()` 重画（先清黑底）⇒ 提交线程拿到引用去 `write_data` 时，
内容已是"下一帧的黑底/新内容" ⇒ **接收端周期性闪黑**。

## 本测试怎么做

用一个**会被持续覆写的** buffer 当"画布"，模拟真实时序：
  1. 渲染线程每 16ms：把画布填成 **一个递增的图案**（模拟一帧画面），调用 send_video；
  2. 立刻把画布**清成 0（黑）**，模拟 `_compose` 下一帧先清底；
  3. 提交线程捕获真正 `write_data` 到的内容。

判据：
  - **修复前（存引用）**：提交线程看到的多数是 **全 0（黑）** ⇒ 复现闪黑；
  - **修复后（存拷贝）**：提交线程看到的都是**完整图案**，全黑帧数 ≈ 0。

因为这里不接真接收端（cyndilib 无接收端时写视频会抛），
我们用**替身 video/sender**注入，只验证"拷贝语义"这一件事。
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import numpy as np  # noqa: E402

import ndi_out  # noqa: E402


class _FakeVideo:
    """替身 VideoSendFrame：捕获 write_data 收到的内容。"""

    def __init__(self, sink):
        self._sink = sink

    def get_write_available(self):
        return True

    def write_data(self, buf):
        # 模拟 SDK 把 buffer 拷进自己的内存（这里只记一份快照用于分析）
        self._sink.append(bytes(buf))


class _FakeSender:
    def __init__(self):
        self.frames = []

    def send_video_async(self):
        pass

    def get_num_connections(self, _t=0.0):
        return 1


class _FakeFrame:
    """canvas 替身：一块会被覆写的连续内存。"""

    def __init__(self, w, h, seed=0):
        self.w, self.h = w, h
        self.buf = bytearray(w * h * 4)
        self.seed = seed

    def paint(self, k):
        """画一帧"非黑"图案：每个像素写 (k, k, k, 255)，k 随帧号变。"""
        v = (k & 0xFF)
        arr = np.frombuffer(self.buf, dtype=np.uint8).reshape(-1, 4)
        arr[:, 0] = v
        arr[:, 1] = v
        arr[:, 2] = v
        arr[:, 3] = 255

    def clear(self):
        """清黑底（模拟 _compose 每帧开始时先清画布）。"""
        self.buf[:] = b"\x00" * len(self.buf)

    def view(self):
        return np.frombuffer(self.buf, dtype=np.uint8)


def run():
    w, h = 320, 180          # 用小尺寸加速，语义与 1080p 相同
    dev = ndi_out.NDIOutput("copy-test")
    # 注入替身，绕过真 SDK
    sink = []
    dev._video = _FakeVideo(sink)
    dev._sender = _FakeSender()
    dev._w, dev._h = w, h
    dev.enabled = True
    dev._inited = True
    dev._opened_at = time.perf_counter() - 10.0     # 跳过 warmup

    canvas = _FakeFrame(w, h)
    submitted = {"n": 0}

    def render_loop():
        """模拟渲染线程：画 → send_video → 立刻清底（下一帧准备）。"""
        for k in range(1, 61):
            canvas.paint(k)
            dev.send_video(canvas.view(), w, h)
            submitted["n"] += 1
            canvas.clear()                 # ← 关键：立刻覆写画布
            time.sleep(0.004)              # 4ms/帧，比提交线程的取帧快 ⇒ 制造竞态

    t = threading.Thread(target=render_loop, daemon=True)
    t.start()
    t.join(timeout=5.0)
    time.sleep(0.5)                        # 等提交线程把最后一帧处理完

    dev._vstop = True
    try:
        with dev._vcv:
            dev._vcv.notify_all()
    except Exception:                                              # noqa: BLE001
        pass
    time.sleep(0.3)

    # ---- 分析 ----
    total = len(sink)
    need = w * h * 4
    black = 0
    good = 0
    other = 0
    for b in sink:
        if len(b) != need:
            other += 1
            continue
        arr = np.frombuffer(b, dtype=np.uint8).reshape(-1, 4)
        # "全黑" = 所有像素 RGB 全 0（alpha 不算）
        rgb = arr[:, :3]
        if not rgb.any():
            black += 1
        # "完整图案" = RGB 三通道一致且非 0 且 alpha=255
        elif bool((rgb[:, 0] == rgb[:, 1]).all()) and bool((rgb[:, 1] == rgb[:, 2]).all()) \
                and bool((arr[:, 3] == 255).all()) and int(rgb[0, 0]) != 0:
            good += 1
        else:
            other += 1

    print("=" * 62)
    print("渲染线程提交 %d 帧；提交线程实际 write_data %d 帧" % (submitted["n"], total))
    print("  完整图案帧 : %d" % good)
    print("  全黑帧     : %d   ← 修复前这里会是一大片" % black)
    print("  其它(错乱) : %d" % other)
    print("=" * 62)

    ok = True
    if total == 0:
        print("✗ 提交线程一帧都没拿到（改造后路径没跑起来）")
        ok = False
    if black > max(1, total // 20):
        print("✗ 全黑帧过多（%d/%d）⇒ 画布被覆写的问题仍在" % (black, total))
        ok = False
    if good < total * 0.8:
        print("✗ 完整帧占比过低（%d/%d）⇒ 内容不可信" % (good, total))
        ok = False
    if ok:
        print("✓ 通过：提交线程拿到的都是**入队那一刻的完整拷贝**，")
        print("  渲染线程随后的画布覆写/清黑不再影响已提交的帧 ⇒ 不会闪黑。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
