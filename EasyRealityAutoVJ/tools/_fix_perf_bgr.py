# -*- coding: utf-8 -*-
"""性能修复：QImage↔BGR 跨通道拷贝（P1，两条审计线独立指向同一点）。

实测（tools/_perf_bgr_view.py，含逐像素等价断言）：
  1280x720  ：读 4.46→0.74ms（6.0×）  写 3.28→0.84ms（3.9×）  合计省 5.6ms/帧
  1920x1080 ：读 8.86→1.14ms（7.8×）  写 7.54→2.86ms（2.6×）  合计省 12.4ms/帧
  两条路径均**逐像素完全一致**（np.array_equal 通过）。

原因：旧实现用 `ascontiguousarray(arr[:, :, :3])` / 跨步赋值，逐元素 1.7ns，
      在 3 通道跨步布局上只有 0.62 GB/s；cv2.cvtColor 走 SIMD，快 4~8 倍。
"""

import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PP = os.path.join(ROOT, "src", "postprocess.py")


def main():
    s = io.open(PP, encoding="utf-8").read()

    old = '''def _np_view_bgr(img):
    """QImage(RGB32/ARGB32) → 连续 BGR (H,W,3) 副本"""
    w, h = img.width(), img.height()
    bpl = img.bytesPerLine()
    arr = np.frombuffer(img.bits(), dtype=np.uint8, count=bpl * h).reshape(h, bpl)
    return np.ascontiguousarray(arr[:, :w * 4].reshape(h, w, 4)[:, :, :3])'''
    new = '''def _np_view_bgr(img):
    """QImage(RGB32/ARGB32) → 连续 BGR (H,W,3) 副本

    ★ 2026-10-19 性能修复：旧写法 `ascontiguousarray(arr[:, :w*4].reshape(h,w,4)[:, :, :3])`
      是**3 通道跨步拷贝**，实测 720p 4.46ms / 1080p 8.86ms（只有 0.62 GB/s）。
      `cv2.cvtColor` 走 SIMD：720p 0.74ms / 1080p 1.14ms（快 6~8 倍），
      且输出**逐像素完全一致**（见 tools/_perf_bgr_view.py 的 array_equal 断言）。
    """
    w, h = img.width(), img.height()
    bpl = img.bytesPerLine()
    arr = np.frombuffer(img.bits(), dtype=np.uint8, count=bpl * h).reshape(h, bpl)
    v4 = arr[:, :w * 4].reshape(h, w, 4)
    return cv2.cvtColor(v4, cv2.COLOR_BGRA2BGR)'''
    assert old in s, "① _np_view_bgr 锚点没找到"
    s = s.replace(old, new, 1)

    old2 = '''def write_back_bgr(img, bgr):
    """把 (h,w,3) BGR 写回 QImage（跳过 alpha 通道）。

    注意 `_np_view_bgr` 返回的是**副本**（ascontiguousarray 会拷贝），
    所以改完必须显式写回，不能指望改副本就改了画布。"""
    w, h = img.width(), img.height()
    bpl = img.bytesPerLine()
    dst = np.frombuffer(img.bits(), dtype=np.uint8, count=bpl * h).reshape(h, bpl)
    dst[:, :w * 4].reshape(h, w, 4)[:, :, :3] = bgr'''
    new2 = '''def write_back_bgr(img, bgr):
    """把 (h,w,3) BGR 写回 QImage（**保留原 alpha**）。

    ★ 2026-10-19 性能修复：旧写法 `dst[:, :, :3] = bgr` 是跨步赋值，
      实测 720p 3.28ms / 1080p 7.54ms。改成 `cv2.cvtColor(..., dst=v4)`
      （720p 0.84ms / 1080p 2.86ms，快 2.6~3.9 倍），再回填原来的 alpha
      —— alpha 必须保留：码表图层的透明区就靠它，写成 255 会把透明变黑。
      逐像素等价性由 tools/_perf_bgr_view.py 断言。
    """
    w, h = img.width(), img.height()
    bpl = img.bytesPerLine()
    dst = np.frombuffer(img.bits(), dtype=np.uint8, count=bpl * h).reshape(h, bpl)
    v4 = dst[:, :w * 4].reshape(h, w, 4)
    alpha = v4[:, :, 3].copy()          # 1 通道，1080p ≈ 2MB，约 0.2ms
    cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA, dst=v4)
    v4[:, :, 3] = alpha'''
    assert old2 in s, "② write_back_bgr 锚点没找到"
    s = s.replace(old2, new2, 1)

    io.open(PP, "w", encoding="utf-8").write(s)
    print("postprocess.py 两处已改")
    return 0


if __name__ == "__main__":
    sys.exit(main())
