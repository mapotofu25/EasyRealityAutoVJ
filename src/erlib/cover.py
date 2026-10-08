# -*- coding: utf-8 -*-
r"""erlib **专用**的轻量封面提取 —— 不依赖 OpenCV / FFmpeg。

## 为什么不用主程序那个 `music_cover`
主程序的 `music_cover` 依赖 **`cv2`（97 MB）** 和 **`av`（63 MB 的 FFmpeg）**。
对主程序是值得的 —— 它还要给**视频素材**取封面、做更复杂的图像处理。
但 erlib 只需要一件事：**从音频文件里抠出封面图，缩成 200px**。
用 `mutagen`（本来就要带，读标签用）+ `PIL`（本来就要带）就够了。

⇒ 砍掉这 160 MB，erlib 从 387 MB 降到 ~227 MB。

## 覆盖三条封面来源（与原实现等价）
1. **内嵌**：FLAC 的 picture / MP3 的 APIC / MP4 的 covr
2. **同目录图片**：cover.jpg / folder.jpg / front.jpg

（主程序还有一条走 `av` 的解码路径用于视频素材 —— erlib 只处理音频，不需要。）
"""

import io
import os

SIZE = 200

# ⚠ Pillow 10+ 把重采样常量挪进了 `Image.Resampling`，旧的 `Image.LANCZOS`
#   仍能用但会告警。这样取一次，两个版本都安全。
_LANCZOS = getattr(getattr(__import__("PIL.Image", fromlist=["Image"]),
                           "Resampling", None), "LANCZOS", None)


def _embedded_bytes(path):
    """从音频标签里抠出内嵌封面字节；没有返回 None。**任何异常都吞掉**。"""
    low = path.lower()
    try:
        if low.endswith(".flac"):
            from mutagen.flac import FLAC
            a = FLAC(path)
            if a.pictures:
                return bytes(a.pictures[0].data)
        elif low.endswith(".mp3"):
            from mutagen.id3 import ID3
            a = ID3(path)
            for k in list(a.keys()):
                if str(k).startswith("APIC"):
                    return bytes(a[k].data)
        elif low.endswith((".m4a", ".mp4", ".aac", ".aiff", ".aif")):
            from mutagen.mp4 import MP4
            a = MP4(path)
            tags = getattr(a, "tags", None)
            if tags:
                cov = tags.get("covr")
                if cov:
                    return bytes(cov[0])
        # 其它格式尝试通用标签接口
        else:
            import mutagen
            m = mutagen.File(path)
            if m is not None and getattr(m, "tags", None):
                pics = m.tags.get("pictures") if hasattr(m.tags, "get") else None
                if pics:
                    return bytes(pics[0].data)
    except Exception:                                            # noqa: BLE001
        pass
    return None


def _folder_bytes(path):
    """同目录图片兜底。"""
    try:
        d = os.path.dirname(os.path.abspath(path))
        for n in ("cover.jpg", "Cover.jpg", "folder.jpg", "Folder.jpg",
                  "front.jpg", "cover.png", "Cover.png", "folder.png"):
            p = os.path.join(d, n)
            if os.path.isfile(p):
                with open(p, "rb") as f:
                    return f.read()
    except Exception:                                            # noqa: BLE001
        pass
    return None


def cover_bytes(path, size=SIZE):
    """返回**居中裁成正方形、缩到 size×size 的 JPEG 字节**；没有封面来源时 None。

    ⚠ 与主程序 `music_cover` 的产物**语义一致**（正方形 200px JPEG），
      这样导入到演出机后显示效果一样；但实现完全独立（不引 cv2/av）。
    """
    raw = None
    try:
        raw = _embedded_bytes(path)
    except Exception:                                            # noqa: BLE001
        raw = None
    if not raw:
        raw = _folder_bytes(path)
    if not raw:
        return None
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw))
        im = im.convert("RGB")
        w, h = im.size
        if w <= 0 or h <= 0:
            return None
        s = min(w, h)
        box = ((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s)
        im = im.crop(box)
        if _LANCZOS is not None:
            im = im.resize((int(size), int(size)), _LANCZOS)
        else:
            im = im.resize((int(size), int(size)))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=88)
        return buf.getvalue()
    except Exception:                                            # noqa: BLE001
        return None
