# -*- coding: utf-8 -*-
"""歌曲封面：从音频文件里取封面，缩成小图缓存到本地。

## 封面从哪来（2026-10-01 实测本机 67 首曲库）
| 来源 | 命中 | 说明 |
|---|---|---|
| **音频内嵌**（FLAC PICTURE 块 / MP3 APIC / MP4 covr） | **66 / 67** | 全部 640×640；PyAV 取一张 ~16ms |
| 同目录图片（cover.jpg / folder.jpg / front.jpg …） | 6 / 16 目录 | 少数无内嵌的靠它 |
| 都没有 | 1 首 | 列表里显示占位图 |

## ⚠ 两条铁律（跟本项目其它缩略图一致）
1. **主线程只准读缓存**（`cached()`），**绝不准提取**（`extract()` 要开容器 + 解码 + 写盘）。
   提取发生在：① 曲库扫描的 worker 线程（离线建库，见 `ui_main.MusicScanThread`）；
   ② 后台补图线程。UI 里拿不到就先用占位图，补好了再刷新。
2. **缓存键含 mtime + size + 尺寸 + 版本号**（`COVER_VER`），换文件/改规则都会自动失效。

## 为什么不用 mutagen
项目已依赖 PyAV（`requirements.txt` 里有 `av`），它能直接读内嵌封面流，
不必为封面多引一个第三方库。⚠ 但 PyAV 的 `to_image()` 要 PIL，本项目没有 PIL
⇒ 走 `to_ndarray()` + OpenCV 缩放，与其它缩略图一致。
"""

import hashlib
import os
import threading

# 缓存目录：复用素材缩略图的目录（`thumbs/`），单独放一个子目录，方便整体清理
try:
    from media_manager import THUMB_DIR
except Exception:                                              # noqa: BLE001
    from config import app_base_dir
    THUMB_DIR = os.path.join(app_base_dir(), "thumbs")

COVER_SUBDIR = "covers"
COVER_SIZE = 144          # 缓存边长（列表 icon 用 56~72，2x 屏也够）
COVER_VER = 1             # ★ 改动提取规则时 +1，旧缓存自动失效

# 同目录图片的优先名字（按顺序找，命中即用）
FOLDER_NAMES = ("cover", "folder", "front", "album", "albumart", "albumarts",
                "artwork", "thumb", "封面")
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp")

_dir_lock = threading.Lock()
_dir_ready = False


def cover_dir():
    """封面缓存目录（首次调用时创建）。"""
    global _dir_ready
    d = os.path.join(THUMB_DIR, COVER_SUBDIR)
    if not _dir_ready:
        with _dir_lock:
            if not _dir_ready:
                try:
                    os.makedirs(d, exist_ok=True)
                except Exception:                              # noqa: BLE001
                    pass
                _dir_ready = True
    return d


def _key(path, size):
    """缓存键：路径 + mtime + 文件大小 + 目标尺寸 + 规则版本。"""
    try:
        st = os.stat(path)
        stamp = "%d|%d" % (int(st.st_mtime), int(st.st_size))
    except Exception:                                          # noqa: BLE001
        stamp = "nostat"
    raw = "%s|%s|%d|%d" % (os.path.abspath(path), stamp, int(size), COVER_VER)
    return hashlib.md5(raw.encode("utf-8", "replace")).hexdigest()[:16]


def cover_path(path, size=COVER_SIZE):
    """该曲目的封面缓存文件路径（**不保证存在**）。"""
    return os.path.join(cover_dir(), _key(path, size) + ".jpg")


def cached(path, size=COVER_SIZE):
    """**只查缓存**（主线程安全）：命中返回路径，否则 None。不读盘、不解码。"""
    try:
        p = cover_path(path, size)
        return p if os.path.isfile(p) else None
    except Exception:                                          # noqa: BLE001
        return None


# ---------------------------------------------------------------- 图片工具
def _imdecode_uni(fs_path):
    """读图片，**支持非 ASCII 路径**（cv2.imread 在中文路径下会返回 None）。"""
    import cv2
    import numpy as np
    try:
        buf = np.fromfile(fs_path, dtype=np.uint8)
        if buf.size == 0:
            return None
        return cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except Exception:                                          # noqa: BLE001
        return None


def _imwrite_uni(fs_path, bgr):
    """写 JPEG，**支持非 ASCII 路径**（cv2.imwrite 同理）。"""
    import cv2
    try:
        ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
        if not ok:
            return False
        buf.tofile(fs_path)
        return True
    except Exception:                                          # noqa: BLE001
        return False


def _square_resize(bgr, size):
    """居中裁成正方形再缩放（保持封面比例，不拉变形）。"""
    import cv2
    h, w = bgr.shape[:2]
    if h <= 0 or w <= 0:
        return None
    s = min(h, w)
    y0, x0 = (h - s) // 2, (w - s) // 2
    crop = bgr[y0:y0 + s, x0:x0 + s]
    interp = cv2.INTER_AREA if s > size else cv2.INTER_LINEAR
    return cv2.resize(crop, (int(size), int(size)), interpolation=interp)


# ---------------------------------------------------------------- 提取
def _embedded_bgr(path):
    """取音频内嵌封面（PyAV）。返回 BGR ndarray 或 None。"""
    try:
        import av
    except Exception:                                          # noqa: BLE001
        return None
    try:
        with av.open(path, metadata_errors="ignore") as c:
            for s in c.streams:
                try:
                    if not (s.disposition and s.disposition.attached_pic):
                        continue
                except Exception:                              # noqa: BLE001
                    continue
                for fr in c.decode(s):
                    try:
                        arr = fr.to_ndarray(format="rgb24")
                    except Exception:                          # noqa: BLE001
                        continue
                    if arr is None or getattr(arr, "size", 0) == 0:
                        continue
                    import cv2
                    return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    except Exception:                                          # noqa: BLE001
        return None
    return None


def _folder_image(path):
    """同目录里的封面图片路径（优先 cover/folder/front… 这类名字）。"""
    try:
        d = os.path.dirname(path)
        names = os.listdir(d)
    except Exception:                                          # noqa: BLE001
        return None
    imgs = [n for n in names if n.lower().endswith(IMG_EXT)]
    if not imgs:
        return None
    def rank(n):
        stem = os.path.splitext(n)[0].lower()
        try:
            return (FOLDER_NAMES.index(stem), len(n))
        except ValueError:
            return (len(FOLDER_NAMES), len(n))
    imgs.sort(key=rank)
    return os.path.join(d, imgs[0])


def extract(path, size=COVER_SIZE):
    """**在后台线程调用**：提取封面 → 缩成 size×size 的 JPEG 进缓存。

    返回缓存文件路径；没有任何封面来源时返回 None。
    已缓存则直接返回（不做重复解码）。
    """
    hit = cached(path, size)
    if hit:
        return hit
    try:
        if not os.path.isfile(path):
            return None
    except Exception:                                          # noqa: BLE001
        return None

    bgr = None
    try:
        bgr = _embedded_bgr(path)          # ① 内嵌（覆盖 98%）
    except Exception:                                          # noqa: BLE001
        bgr = None
    if bgr is None:
        try:
            fp = _folder_image(path)       # ② 同目录图片
            if fp:
                bgr = _imdecode_uni(fp)
        except Exception:                                      # noqa: BLE001
            bgr = None
    if bgr is None or getattr(bgr, "size", 0) == 0:
        return None
    try:
        small = _square_resize(bgr, size)
        if small is None:
            return None
        out = cover_path(path, size)
        if not _imwrite_uni(out, small):
            return None
        return out
    except Exception:                                          # noqa: BLE001
        return None


def size_of(path):
    """缓存文件的字节数（诊断用）。"""
    try:
        return os.path.getsize(path)
    except Exception:                                          # noqa: BLE001
        return 0


def clear_cache():
    """清空封面缓存。返回删除的文件数。"""
    n = 0
    try:
        d = cover_dir()
        for f in os.listdir(d):
            if f.lower().endswith(".jpg"):
                try:
                    os.remove(os.path.join(d, f))
                    n += 1
                except Exception:                              # noqa: BLE001
                    pass
    except Exception:                                          # noqa: BLE001
        pass
    return n
