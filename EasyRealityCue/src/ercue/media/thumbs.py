# -*- coding: utf-8 -*-
"""缩略图生成：视频多帧采样选优 → **PNG 字节**（alpha 素材保留 alpha）。

零 Qt。输出 **PNG 字节**，由 UI 层 `QImage.loadFromData()` 自行解析；本模块
**不返回** `QImage` / `QPixmap`。PNG 由标准库 `zlib` + 手写 chunk 编码（不引入
Pillow/cv2；PyAV 那边没有好用的"内存里出单张 PNG"接口）。

★★ **调用契约（硬约束，旧实现现场卡死的根因）**：
    **绝不在 GUI 线程调用本模块的函数。**
    本函数会：开容器 + 顺序解最多 12 个采样帧（alpha 素材还要先深解最多 24 帧判 alpha）
    + 缩放 + 压缩 PNG。单个素材约 20~120 ms，**164 个素材放主线程上就是几十秒到几分钟**
    → 窗口"未响应"（规格书 §6.5 / 技术选型 §A3 R4）。
    正确用法：**主线程只读缓存**（`resolve_thumb`），缺图统一交后台线程调 `make_thumb`，
    生成完再把结果回主线程刷新。本模块**只提供函数，不做任何线程调度**。

★★ 缩略图缓存与"两个扩展名都试"：
    `cache_key(path) = md5(path + mtime)[:16]`。
    ⚠ 旧实现踩过的坑：**alpha 素材的真实图是 `.png`，默认却猜 `.jpg`**
    （旧版 164 个素材里 49 个 alpha `.mov` 因此"图不见了"）。
    故 `resolve_thumb()` 必须 **`.png` / `.jpg` 两个扩展名都试**。
    本项目 `make_thumb` 统一产出 PNG，但仍保留双扩展名查找以兼容旧缓存/外部产物。
"""

from __future__ import annotations

import hashlib
import os
import struct
import zlib

import numpy as np

from .probe import alpha_from_stream, kind_of

DEFAULT_THUMB_W = 320
# 视频采样点数量（技术选型 §A3：采样 12 个时间点选最优帧）
SAMPLE_COUNT = 12
# 纯黑/纯白过场帧的均值门限（低于/高于则跳过）
_DARK_MEAN = 4.0
_BRIGHT_MEAN = 251.0


# --------------------------------------------------------------------------
# 缓存键 / 缓存查找
# --------------------------------------------------------------------------
def cache_key(path: str) -> str:
    """缩略图缓存键 = `md5(path + mtime)[:16]`（mtime 取整秒；取不到用 0）。

    mtime 进键 ⇒ 素材被替换后自动失效，不会读到旧图。
    """
    try:
        mtime = int(os.path.getmtime(path))
    except OSError:
        mtime = 0
    h = hashlib.md5(("%s|%d" % (str(path), mtime)).encode("utf-8")).hexdigest()
    return h[:16]


def thumb_path(cache_dir: str, path: str, ext: str = ".png") -> str:
    """给定缓存目录，算某个扩展名下的缓存文件路径。"""
    return os.path.join(str(cache_dir), cache_key(path) + ext)


def resolve_thumb(path: str, cache_dir: str) -> str | None:
    """返回**磁盘上确实存在**的缩略图路径（没有则 None）。

    ⚠ 必须 `.png` 和 `.jpg` 两个扩展名都试 —— alpha 素材的真实图是 PNG，而旧实现
    默认猜 JPG，只认一个扩展名就会"图不见了"（规格书 / §A3 记的坑）。
    """
    for ext in (".png", ".jpg"):
        p = thumb_path(cache_dir, path, ext)
        if os.path.isfile(p):
            return p
    return None


# --------------------------------------------------------------------------
# 像素工具（零外部依赖）
# --------------------------------------------------------------------------
def resize_bgra(arr: np.ndarray, max_w: int) -> np.ndarray:
    """等比缩放到宽 ≤ `max_w`（最近邻；缩略图足够，且零依赖）。

    只缩不放：宽度已 ≤ max_w 时原样返回。
    """
    h, w = arr.shape[:2]
    if max_w <= 0 or w <= max_w:
        return np.ascontiguousarray(arr)
    nw = int(max_w)
    nh = max(1, int(round(h * nw / float(w))))
    ys = (np.arange(nh) * h // nh).clip(0, h - 1)
    xs = (np.arange(nw) * w // nw).clip(0, w - 1)
    return np.ascontiguousarray(arr[ys][:, xs])


def encode_png_bgra(arr_bgra: np.ndarray) -> bytes:
    """把 (h,w,4) BGRA uint8 数组编码成 **RGBA8 PNG 字节**（保留 alpha）。

    纯标准库实现（`zlib` + PNG chunk），不依赖 Pillow / PyAV 编码器 / Qt。
    PNG color type 6（真彩 + alpha）；每行 filter 0（None）。
    """
    a = np.ascontiguousarray(arr_bgra)
    if a.ndim != 3 or a.shape[2] < 4:
        raise ValueError("encode_png_bgra 需要 (h,w,4) 的 BGRA 数组")
    h, w = a.shape[:2]
    # BGRA → RGBA
    rgba = np.empty((h, w, 4), dtype=np.uint8)
    rgba[..., 0] = a[..., 2]
    rgba[..., 1] = a[..., 1]
    rgba[..., 2] = a[..., 0]
    rgba[..., 3] = a[..., 3]

    raw = bytearray()
    for y in range(h):
        raw.append(0)                       # filter type 0
        raw += rgba[y].tobytes()
    comp = zlib.compress(bytes(raw), 6)

    def _chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)   # 8bit / colortype6 / 无隔行
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", comp)
            + _chunk(b"IEND", b""))


# --------------------------------------------------------------------------
# 帧采样选优
# --------------------------------------------------------------------------
def _score_alpha(arr_bgra: np.ndarray) -> float:
    """alpha 素材的评分：`alpha>16 占比 × 100 + RGB.std`（技术选型 §A3）。

    alpha 覆盖率优先（选透明区域完整、主体清晰的一帧），再按 RGB 内容丰富度。
    """
    alpha = arr_bgra[..., 3]
    coverage = float((alpha > 16).mean())
    rgb_std = float(arr_bgra[..., :3].std())
    return coverage * 100.0 + rgb_std


def _score_plain(arr_bgra: np.ndarray):
    """普通视频的评分：优先 `std + min(mean, 255-mean)*0.4`，并跳过纯黑/纯白帧。

    返回 `(score, fallback_score)`；`fallback_score` 是仅按 std 的兜底分
    （全被当成黑/白帧过滤时用它，避免返回 None）。
    """
    rgb = arr_bgra[..., :3]
    mean = float(rgb.mean())
    std = float(rgb.std())
    fallback = std
    if mean < _DARK_MEAN or mean > _BRIGHT_MEAN:     # 纯黑/纯白过场帧
        return -1.0, fallback
    return std + min(mean, 255.0 - mean) * 0.4, fallback


def _decode_first(container):
    """解一帧并转 BGRA；解不出返回 None（单帧解码失败不致命）。"""
    try:
        fr = next(iter(container.decode(video=0)), None)
        if fr is None:
            return None
        return np.ascontiguousarray(fr.to_ndarray(format="bgra"))
    except Exception:                               # noqa: BLE001
        return None


def _sample_positions(duration: float) -> list:
    """12 个采样时间点（秒）：均匀取每段的中间，避开首尾黑帧。"""
    if duration <= 0:
        return [0.0]
    step = duration / float(SAMPLE_COUNT)
    return [step * (k + 0.5) for k in range(SAMPLE_COUNT)]


def _pick_video_frame(container, stream, duration: float, has_alpha: bool):
    """采样 12 个时间点，按评分选内容最丰富的一帧（返回 BGRA 数组或 None）。

    - 普通视频：按 `std`（内容对比度）打分，跳过纯黑/纯白过场帧。
    - alpha 视频：按 `alpha>16 占比 × 100 + RGB.std` 打分。
    """
    best, best_score = None, -1.0
    fallback, fb_score = None, -1.0
    tb = float(stream.time_base) if stream.time_base else 0.0

    for sec in _sample_positions(duration):
        arr = None
        if tb > 0:
            try:
                container.seek(int(sec / tb), stream=stream, backward=True)
                arr = _decode_first(container)
            except Exception:                       # noqa: BLE001
                arr = None
        if arr is None and duration <= 0:
            arr = _decode_first(container)          # 拿不到时长的容器：顺序取
        if arr is None:
            continue
        if has_alpha:
            sc = _score_alpha(arr)
            if sc > best_score:
                best_score, best = sc, arr
        else:
            sc, fb = _score_plain(arr)
            if fb > fb_score:
                fb_score, fallback = fb, arr
            if sc > best_score:
                best_score, best = sc, arr

    return best if best is not None else fallback


def make_thumb(path: str, max_w: int = DEFAULT_THUMB_W,
               has_alpha: bool | None = None, max_side: int | None = None) -> bytes | None:
    """生成素材缩略图，返回 **PNG 字节**（失败返回 None，绝不抛）。

    ⚠ **绝不在 GUI 线程调用本函数**（见模块 docstring 的调用契约）。

    参数：
        path       素材路径
        max_w      缩略图最大宽度（等比，只缩不放），默认 320
        has_alpha  已知的 alpha 结论（来自 `probe.probe`）；None = 本函数自己探一次
        max_side   长边上限（可选）：竖屏素材按宽度缩会仍然很高，导出 `.ercue` 的
                   `thumbs/` 要求"长边 ≤256" ⇒ 传 `max_side` 时按**长边**等比缩。
                   `max_w` 与 `max_side` 同时给 = 两者都满足（取更小的结果）。

    行为：
        · 图片：解第一帧（PyAV），直接缩放输出 PNG。
        · 视频：采样 12 个时间点选最优帧（普通按 std、alpha 按覆盖率），缩放输出 PNG。
        · alpha 素材：`to_ndarray("bgra")` 后**保留 alpha 通道**并输出 PNG
          （JPG 会丢透明，故本项目缩略图统一 PNG）。
    """
    path = str(path or "")
    if not path or not os.path.isfile(path):
        return None
    kind = kind_of(path) or "video"

    try:
        import av
        # ★ metadata_errors="replace" 不能省（GBK 元数据 .mov）
        container = av.open(path, metadata_errors="replace")
    except Exception:                               # noqa: BLE001
        return None

    try:
        try:
            stream = container.streams.video[0]
        except Exception:                           # noqa: BLE001
            return None

        if kind == "image":
            # 图片只有一帧：先解出来，alpha 直接从这一帧判（**不能**先探 alpha 再解，
            # 那会把唯一一帧消费掉，第二次 decode 拿不到任何东西）。
            arr = _decode_first(container)
            if arr is None:
                return None
            if has_alpha is None:
                has_alpha = bool(arr.shape[2] >= 4 and int(arr[..., 3].min()) < 255)
        else:
            if has_alpha is None:
                try:
                    has_alpha = alpha_from_stream(container, stream, kind)
                except Exception:                   # noqa: BLE001
                    has_alpha = False
            try:
                duration = float(stream.duration) * float(stream.time_base) \
                    if stream.duration else 0.0
            except Exception:                       # noqa: BLE001
                duration = 0.0
            if duration <= 0:
                try:
                    duration = float(container.duration) / 1_000_000.0 if container.duration else 0.0
                except Exception:                   # noqa: BLE001
                    duration = 0.0
            arr = _pick_video_frame(container, stream, duration, bool(has_alpha))

        if arr is None:
            return None
        # ★ 长边上限：`resize_bgra` 只按宽度缩，竖屏素材会仍然很高 ⇒ 先按长边折算目标宽。
        if max_side is not None and int(max_side) > 0:
            h0, w0 = arr.shape[:2]
            long_edge = max(h0, w0)
            if long_edge > int(max_side):
                max_w = min(int(max_w) if max_w else int(max_side),
                            max(1, int(round(w0 * float(max_side) / float(long_edge)))))
        arr = resize_bgra(arr, max_w)
        return encode_png_bgra(arr)
    except Exception:                               # noqa: BLE001
        return None
    finally:
        try:
            container.close()
        except Exception:                           # noqa: BLE001
            pass


def selftest() -> bool:
    """无素材自检：PNG 编码往返 + 缓存键稳定性。"""
    arr = np.zeros((8, 8, 4), dtype=np.uint8)
    arr[..., 3] = 128
    png = encode_png_bgra(arr)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 30
    k1, k2 = cache_key(__file__), cache_key(__file__)
    assert k1 == k2 and len(k1) == 16
    assert make_thumb("__nope__.mp4") is None
    print("[thumbs] 自检通过 ✓  PNG 编码 / 缓存键稳定 / 坏路径返回 None")
    return True


if __name__ == "__main__":
    selftest()
