# -*- coding: utf-8 -*-
"""素材探测：时长 / fps / 分辨率 / 编码 / 像素格式 / **是否真带 alpha**。

零 Qt。只依赖 `av`(PyAV) 与标准库 —— 图片也用 PyAV 解，本项目不引入 Pillow/cv2。

★★ 两个必须踩住的坑（技术选型 §A2 / R3，规格书 §6.3）：

1) **`av.open` 必须带 `metadata_errors="replace"`**。
   素材库里大量 `.mov`（实测本机 279 个里绝大多数）元数据是 GBK 编码，不带这个参数
   会抛 `UnicodeDecodeError` —— 探测直接失败，素材被判成"打不开"。参考实现
   `media_manager.py:946` 与 §A2 都点明了这一点。

2) **`has_alpha` 不能只看 `pix_fmt`**。
   DXV 的解码器**一律**输出 `rgba` / `yuva420p`，但其中绝大多数素材（DXT1）的 alpha
   通道其实是**全 255（完全不透明）**。只用 `pix_fmt` 判断，会把整个 DXV 素材库都当成
   带 alpha，导致缩略图/预览白白走"带 alpha"这条更贵的通路，并且画面语义也是错的。
   实测本机 `Y:\\EasyRealityAutoVJ\\素材库` 的 279 个 `.mov`：
       仅看 pix_fmt 会判为带 alpha = 247
       实解帧验证后真带 alpha   =  18
   所以本模块的 alpha 判定是**两层**（复刻 `media_manager.py:871 _av_probe_alpha`）：
       第 1 层：`codec` / `pix_fmt` 是否**有可能**带 alpha（快速否掉 32 个 yuv420p）。
       第 2 层：确认有可能后，**实际顺序解若干帧**，看 alpha 通道是否出现 `< 255`。
   （只查第一帧不够 —— 透明可能只出现在中段，故采样到第 24 帧。）

容错原则（编排工具里一个坏素材不该中断整个导入）：打不开 / 探测失败的素材，
返回一个 `error` 非空的 `ProbeResult`，**绝不抛异常**。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

# ★ 2026-10-19 审计修复：`video_size()` 的 except 分支调用 `log.exception()`，
#   但模块里从未定义 `log` ⇒ 异常路径抛 NameError（docstring 承诺"绝不抛"）。
log = logging.getLogger("ercue.media.probe")

# --------------------------------------------------------------------------
# 扩展名分类
# --------------------------------------------------------------------------
VIDEO_EXT = (".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v", ".gif")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff")

# ★ 需求 4：**图片素材的默认摆放时长（秒）** —— **唯一来源**，别在别处再写 5.0 字面量。
#   语义：图片本身"没有时长"（`probe().duration_sec` 仍为 0.0，那是**文件属性**）；
#   这里的 5.0 是**放到时间轴上的默认时长**（摆放策略）⇒ UI 侧（`window._clip_duration_sec`）
#   用它算出 `end_sec = at + IMAGE_DEFAULT_SEC`。
IMAGE_DEFAULT_SEC = 5.0

# GIF 扩展名（需求 4 的"循环"标记；`kind_of` 把 GIF 归到 video，见其 docstring）。
GIF_EXT = (".gif",)

# pix_fmt 里出现这些标记 = "**有可能**带 alpha"（第 1 层判定）。
# 注意：这只是必要条件，不是充分条件 —— 必须再过第 2 层实解帧。
ALPHA_PIXFMT_MARKS = ("rgba", "bgra", "yuva", "argb", "abgr", "ya8", "pal8")

# 第 2 层深度探测：顺序解码最多 24 帧，每 4 帧查一次 alpha（命中即收工）。
ALPHA_SCAN_FRAMES = 24
ALPHA_SCAN_STEP = 4


def kind_of(path: str) -> str:
    """按扩展名判定素材种类：`"video"` / `"image"`；无法识别返回 `""`。

    GIF 归到 `video`（PyAV 按多帧视频流解），不做单独分支。
    ⚠ 但"**是否需要循环环绕**"要看 `is_gif()`（需求 4）—— 二者是不同问题。
    """
    e = (path or "").lower()
    if e.endswith(VIDEO_EXT):
        return "video"
    if e.endswith(IMAGE_EXT):
        return "image"
    return ""


def is_gif(path: str) -> bool:
    """是否 GIF 素材（按扩展名，需求 4）。

    ★ GIF 的运行时约束（**必须遵守**）：GIF 片段**不要设 `src_out_sec`**（保持 `None`）。
      理由：模型里"到达出点即冻结"（A2）会让 GIF 只播一轮就定住；而 GIF 的循环靠
      `media/frames.py` 对 `play_sec` 做"按一轮时长取模环绕"实现 ⇒ 只要不设 `src_out`，
      **时间轴多长就循环多久**。见 `docs/需求_12条_设计.md` 需求 4 与 §9。
    """
    return (path or "").lower().endswith(GIF_EXT)


def gif_src_out_conflict(name: str, src_out_sec) -> bool:
    """★ GIF 运行时约束的**检查器**（需求 4）：返回 True = **违反**约束。

    约束：**GIF 片段必须不设 `src_out_sec`**（否则"到达出点即冻结"会让 GIF 只播一轮，
    循环失效）。供 UI（加片段时）与预检使用；零 Qt，可单测。
    """
    return bool(is_gif(name)) and (src_out_sec is not None)


@dataclass
class ProbeResult:
    """探测结果。

    成功时 `error == ""`；失败时 `error` 为可读原因，其余字段取默认值/已知值。
    永远**不抛异常**，失败也返回本对象。
    """
    path: str = ""
    kind: str = "video"          # "video" | "image"
    error: str = ""              # 非空 = 探测失败
    duration_sec: float = 0.0    # 图片为 0.0
    fps: float = 0.0
    width: int = 0
    height: int = 0
    has_alpha: bool = False      # 实解帧验证后的结论（不是 pix_fmt 猜测）
    codec: str = ""
    pix_fmt: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def to_state_dict(self) -> dict:
        """给 `MediaRef` / 缓存用的纯数据形态。"""
        return {"path": self.path, "kind": self.kind, "error": self.error,
                "duration_sec": self.duration_sec, "fps": self.fps,
                "width": self.width, "height": self.height,
                "has_alpha": self.has_alpha, "codec": self.codec, "pix_fmt": self.pix_fmt}


def _deep_probe_alpha(container, stream) -> bool:
    """第 2 层：实解若干帧，看 alpha 通道是否真的出现过 `< 255`。

    返回 True = 真带 alpha。解不出帧 / 解码报错 → 保守返回 False（维持"无 alpha"，
    绝不凭空改变画面）。注意 DXV 软解在个别帧上会抛 `av.error.OSError`（无帧素材），
    这里必须**逐帧 try**，单帧失败不能废掉整个探测。
    """
    k = 0
    try:
        for fr in container.decode(video=0):
            if k % ALPHA_SCAN_STEP == 0:            # 查第 1、5、9 … 帧
                try:
                    arr = fr.to_ndarray(format="rgba")
                    if (arr.ndim == 3 and arr.shape[2] >= 4
                            and int(arr[:, :, 3].min()) < 255):
                        return True                 # 发现真透明 → 立即收工
                except Exception:                   # noqa: BLE001 —— 单帧解码失败跳过
                    pass
            k += 1
            if k >= ALPHA_SCAN_FRAMES:
                break
    except Exception:                               # noqa: BLE001
        return False
    return False


def _probe_image_alpha(container) -> bool:
    """图片：解第一帧（也只有一帧）看 alpha 是否 < 255。"""
    try:
        fr = next(iter(container.decode(video=0)), None)
        if fr is None:
            return False
        arr = fr.to_ndarray(format="rgba")
        return bool(arr.ndim == 3 and arr.shape[2] >= 4 and int(arr[:, :, 3].min()) < 255)
    except Exception:                               # noqa: BLE001
        return False


def alpha_from_stream(container, stream, kind: str = "video") -> bool:
    """对一个**已打开**的容器/流做两层 alpha 判定。

    供 `probe()` 与 `thumbs.make_thumb()` 复用（避免为了判 alpha 再开一次文件）。
    第 1 层快否（pix_fmt 不含 alpha 标记 → 直接 False），第 2 层实解帧确认。
    """
    if kind == "image":
        return _probe_image_alpha(container)
    pf = (getattr(stream, "pix_fmt", "") or "").lower()
    if any(m in pf for m in ALPHA_PIXFMT_MARKS):
        return _deep_probe_alpha(container, stream)
    return False


def _duration_of(stream, container) -> float:
    """时长（秒）：优先流时长，回落到容器时长。都拿不到返回 0.0。"""
    try:
        if stream is not None and stream.duration:
            return max(0.0, float(stream.duration) * float(stream.time_base))
    except Exception:                               # noqa: BLE001
        pass
    try:
        if container.duration:
            return max(0.0, float(container.duration) / 1_000_000.0)   # AV_TIME_BASE
    except Exception:                               # noqa: BLE001
        pass
    return 0.0


def probe(path: str) -> ProbeResult:
    """探测一个素材文件。**绝不抛异常**，失败返回带 `error` 的结果。

    字段：`duration_sec / fps / width / height / has_alpha / codec / pix_fmt / kind`。
    """
    path = str(path or "")
    res = ProbeResult(path=path, kind=kind_of(path))
    if not path or not os.path.isfile(path):
        res.error = "文件不存在"
        return res

    try:
        import av
    except Exception as e:                          # noqa: BLE001
        res.error = "PyAV 不可用: %r" % (e,)
        return res

    try:
        # ★ metadata_errors="replace" 不能省（GBK 元数据 .mov 会抛 UnicodeDecodeError）。
        container = av.open(path, metadata_errors="replace")
    except Exception as e:                          # noqa: BLE001
        res.error = "打开失败: %r" % (e,)
        return res

    try:
        try:
            stream = container.streams.video[0]
        except Exception:                           # noqa: BLE001
            res.error = "无视频流"
            return res

        cc = stream.codec_context
        res.codec = str(cc.name or "")
        res.pix_fmt = str(stream.pix_fmt or "")
        try:
            res.width = int(cc.width or 0)
            res.height = int(cc.height or 0)
        except Exception:                           # noqa: BLE001
            pass

        if res.kind == "":
            # 扩展名认不出：能解出视频流就当视频
            res.kind = "video"

        if res.kind == "image":
            res.duration_sec = 0.0
            res.fps = 0.0
            res.has_alpha = _probe_image_alpha(container)
        else:
            res.duration_sec = _duration_of(stream, container)
            try:
                rate = stream.average_rate or stream.guessed_rate
                res.fps = float(rate) if rate else 0.0
            except Exception:                       # noqa: BLE001
                res.fps = 0.0
            # 两层 alpha 判定（复刻 _av_probe_alpha）
            res.has_alpha = alpha_from_stream(container, stream, "video")

        return res
    except Exception as e:                          # noqa: BLE001
        res.error = "探测异常: %r" % (e,)
        return res
    finally:
        try:
            container.close()
        except Exception:                           # noqa: BLE001
            pass


def video_size(path):
    """视频/图片的**像素尺寸** `(w, h)`；失败 / 无视频流 ⇒ `None`。**绝不抛**。

    用途：#16（PS 式裁切把手）需要 `fw,fh` 做"预览像素 ↔ 源归一化"的映射。

    ⚠ **用临时容器探测**（`with av.open(...)`）—— **绝不复用别处的容器**：同一容器上反复
      demux/seek 会互相打架（`media/frames.py` 的 `_try_gpu` 就吃过这个亏）。
    """
    p = str(path or "")
    if not p or not os.path.isfile(p):
        return None
    try:
        import av
    except Exception:                               # noqa: BLE001
        return None
    try:
        with av.open(p, metadata_errors="replace") as c:
            try:
                st = c.streams.video[0]
            except Exception:                       # noqa: BLE001
                return None
            cc = st.codec_context
            w = int(getattr(cc, "width", 0) or 0)
            h = int(getattr(cc, "height", 0) or 0)
            if w <= 0 or h <= 0:
                return None
            return (w, h)
    except Exception:                               # noqa: BLE001
        log.exception("读素材像素尺寸失败: %s", p)
        return None


def selftest() -> bool:
    """无素材也能跑的自检：坏路径必须返回 error 且不抛。"""
    r = probe("__definitely_not_exist__.mp4")
    assert isinstance(r, ProbeResult) and not r.ok and r.error, r
    r2 = probe("")
    assert not r2.ok
    assert kind_of("a.MOV") == "video" and kind_of("b.PNG") == "image" and kind_of("c") == ""
    # ★ #16：像素尺寸探测（坏路径安全）
    assert video_size("__nope__.mp4") is None and video_size("") is None
    # ★ 需求 4：GIF 标记 + 图片默认摆放时长（唯一来源）
    assert is_gif("x.GIF") and is_gif("y.gif") and not is_gif("z.png")
    assert kind_of("x.gif") == "video", "GIF 仍按视频解（循环由 frames 侧环绕）"
    assert IMAGE_DEFAULT_SEC == 5.0, "图片默认时长常量被改动（需求 4 定为 5 秒）"
    # GIF 必须不设 src_out（否则到出点冻结、循环失效）
    assert not gif_src_out_conflict("a.gif", None), "GIF + src_out=None 合规"
    assert gif_src_out_conflict("a.gif", 12.0), "GIF 设了 src_out 应判为违规"
    assert not gif_src_out_conflict("a.mp4", 12.0), "非 GIF 可设 src_out"
    print("[probe] 自检通过 ✓  坏路径返回 error 且不抛；扩展名分类 OK；is_gif / IMAGE_DEFAULT_SEC / GIF 约束 OK")
    return True


if __name__ == "__main__":
    selftest()
