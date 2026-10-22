# -*- coding: utf-8 -*-
"""**预览合成器**：把 `timeline.LayerDrawState` 列表合成成一幅 `QImage`。

技术路线（`docs/技术选型.md` §5.1 / §B7，均为本机实测结论）：

  · **一律先合成到离屏 `QImage(Format_ARGB32)`**，画完再由 UI 整幅画到控件。
    （`QPainter` 的 CompositionMode 只在光栅设备上语义完整；绝不直接画到 widget。）
  · **7 种混合**：6 种 Qt 原生，`subtract` Qt 无原生 → numpy 原地饱和减补上。
  · **A4 适配**（★ 2026-10-01 修订，对齐演出端）：与 `engine._draw_layer` **同一判据** ——
    xform 全默认 ⇒ **cover**（`max(W/iw,H/ih)`，等比铺满 + 裁切）；改过任一变换 ⇒ **contain**
    （`min(...) * 用户 scale`）。crop 后以**裁切尺寸**为新基准再算适配。
    （旧注释把演出端的 `max()` 铺满误判为"拉变形"——`max()` 是等比 cover，不变形；
      `compositor` 原先**永远**用 contain，导致非 16:9 素材的预览与现场构图不一致。）
  · **A5 fade 曲线**：`fade` 过渡与演出端一致，`opacity × alpha × trans_p`
    （演出端 `engine.py:2835` 的 `alpha * prog`；`alpha` 已含淡入量 ⇒ 淡入是 α² 曲线，
     评审确认后按两端一致处理）。slide/zoom/glitch/cut 不额外乘 `trans_p`。
  · **D2**：`opacity × alpha` 的基础项只乘一次；`trans_p` 仅对 `fade` 追加（见 A5）。
  · **crop**：用 `drawImage(tgt_rect, img, src_rect)` 源矩形重载，**不先 `copy()` 再画**。
  · **scratch 缓冲按尺寸缓存复用，绝不在每帧分配**
    （旧实现每帧 `QImage(W,H)+fill()` = 1.78 ms/帧；3 层旧写法实测 264 ms/帧）。
  · `SmoothPixmapTransform`。

本模块**不碰解码**：`frame_lookup(clip_id, play_sec) -> np.ndarray(BGRA, HxWx4) | None`
由调用方注入。

★ `frame_lookup` 的**签名契约（M4 变更）**：`frame_lookup(clip_id, play_sec)`。
  旧签名只按 `clip_id` 取帧 ⇒ **同一素材在同一帧被两层以不同 `play_sec` 使用时，
  两层会拿到同一帧（后者覆盖前者）**，预览错误 —— 而"同一个视频既做背景又做前景、
  时间点不同"是编排里很常见的用法。现在把 `play_sec` 一起传下去，每层各自取自己该有的帧。
  ⚠ **向后兼容**：若传入的 callable 只接受 1 个参数（M3 的旧回调），用 `inspect`
  判出后按单参数调用，**绝不因签名变更而崩**（见 `_wants_two_args`）。
"""

from __future__ import annotations

import inspect
import math

import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter

# 混合模式 → Qt 合成模式（`subtract` 不在其中，见 `_apply_subtract`）
BLEND_QT = {
    "normal": QPainter.CompositionMode_SourceOver,
    "add": QPainter.CompositionMode_Plus,
    "screen": QPainter.CompositionMode_Screen,
    "multiply": QPainter.CompositionMode_Multiply,
    "lighten": QPainter.CompositionMode_Lighten,
    "darken": QPainter.CompositionMode_Darken,
}
SUBTRACT = "subtract"

_BLACK = QColor(0, 0, 0)

# ---- scratch 复用（按尺寸缓存，绝不在每帧分配）----
_CANVAS = {}      # (w,h) -> QImage(ARGB32)，合成画布
_SUB_IMG = {}     # (w,h) -> QImage(ARGB32)，subtract 图层的暂存图
_SUB_MIN = {}     # (h,w) -> ndarray(uint8, (h,w,3))，饱和减的临时 min
_OP_LUT = {}      # op -> ndarray(uint8, 256)，透明度缩放查表

# glitch 过渡的画布内最大横向错位（像素）
_GLITCH_MAX_OFF = 30.0


def _wants_two_args(fn) -> bool:
    """判断 `frame_lookup` 是否接受 `(clip_id, play_sec)` 两个参数（**向后兼容**）。

    新契约是双参数；但为兼容只接受一个参数的旧回调（M3 的 `lambda cid: frames.get(cid)`），
    用 `inspect` 判参数个数（含 `*args` 也视为接受双参）。判不出（C 函数/内建）时**按双参**处理。
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return True
    n = 0
    for p in sig.parameters.values():
        if p.kind == p.VAR_POSITIONAL:                 # *args ⇒ 能吃两个参数
            return True
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
            n += 1
    return n >= 2


def _clamp01(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if f < 0.0 else (1.0 if f > 1.0 else f)


def _op_lut(op: float) -> np.ndarray:
    """透明度 → 256 项 uint8 查表（缓存；subtract 路径专用）。"""
    key = round(op, 6)
    lut = _OP_LUT.get(key)
    if lut is None:
        lut = np.clip(np.round(np.arange(256, dtype=np.float64) * key), 0, 255).astype(np.uint8)
        _OP_LUT[key] = lut
    return lut


def _canvas(w: int, h: int) -> QImage:
    img = _CANVAS.get((w, h))
    if img is None:
        img = QImage(w, h, QImage.Format_ARGB32)
        _CANVAS[(w, h)] = img
    return img


def _sub_image(w: int, h: int) -> QImage:
    img = _SUB_IMG.get((w, h))
    if img is None:
        img = QImage(w, h, QImage.Format_ARGB32)
        _SUB_IMG[(w, h)] = img
    return img


def _sub_min(h: int, w: int) -> np.ndarray:
    a = _SUB_MIN.get((h, w))
    if a is None:
        a = np.empty((h, w, 3), np.uint8)
        _SUB_MIN[(h, w)] = a
    return a


def _np_view(img: QImage) -> np.ndarray:
    """QImage(ARGB32) → 可写 numpy 视图 `(h, w, 4)`，通道序 **BGRA**。"""
    w, h = img.width(), img.height()
    bpl = img.bytesPerLine()
    arr = np.frombuffer(img.bits(), dtype=np.uint8, count=bpl * h).reshape(h, bpl)
    return arr[:, :w * 4].reshape(h, w, 4)


def _bgra_array(frame):
    """把 `frame_lookup` 的返回值归一化成连续 BGRA uint8 `(h, w, 4)`；不合法返回 None。

    ⚠ 可能返回**新分配的副本**（源非连续时）。`QImage` 不持有 numpy 引用 ⇒
    调用方必须把返回的数组**保持存活到绘制结束**（`Compositor.compose` 内部有 keepalive）。
    """
    if frame is None:
        return None
    a = frame if isinstance(frame, np.ndarray) else np.asarray(frame)
    if a.ndim != 3 or a.shape[2] != 4:
        return None
    if a.dtype != np.uint8:
        a = a.astype(np.uint8, copy=False)
    if not a.flags["C_CONTIGUOUS"]:
        a = np.ascontiguousarray(a)
    return a


def _qimage(a: np.ndarray) -> QImage:
    """连续 BGRA 数组 → `QImage(ARGB32)`（**零拷贝**，见 `_bgra_array` 的生命周期注意）。"""
    h, w = int(a.shape[0]), int(a.shape[1])
    return QImage(a.data, w, h, 4 * w, QImage.Format_ARGB32)


# --------------------------------------------------------------------------
# 几何：contain 适配 + 用户 scale/位移 + 过渡的空间变换
# --------------------------------------------------------------------------
def _xform_values(st):
    """从 `st.xform` 取 `(scale, x, y, rot)`（缺省 1/0/0/0）。"""
    xf = getattr(st, "xform", None) or {}
    us = float(xf.get("scale", 1.0) or 1.0)
    ux = float(xf.get("x", 0.0) or 0.0)
    uy = float(xf.get("y", 0.0) or 0.0)
    rot = float(xf.get("rot", 0.0) or 0.0)
    return us, ux, uy, rot


def _is_custom(xf_scale, xf_x, xf_y, xf_rot) -> bool:
    """是否做过用户变换（★ A4：判据**逐字照抄演出端** `engine.py:2774`）。

    演出端 `_draw_layer._t(item)`：
        `custom = (|us-1|>1e-3 or |ux|>1e-3 or |uy|>1e-3 or |rot|>1e-3)`
    全默认 ⇒ cover（铺满+裁切）；任一非默认 ⇒ contain（完整显示+用户缩放）。
    """
    return (abs(xf_scale - 1.0) > 1e-3 or abs(xf_x) > 1e-3
            or abs(xf_y) > 1e-3 or abs(xf_rot) > 1e-3)


def _crop_src_rect(crop, fw: int, fh: int):
    """归一化 `crop=[x,y,w,h]` → 源图像素矩形（**照抄演出端** `engine.py:2786-2798` 的取整/钳位）。

    取不到 / 非法 → None（表示不裁切）。
    """
    if not crop or len(crop) != 4:
        return None
    try:
        cw = max(1, int(round(float(crop[2]) * fw)))
        ch = max(1, int(round(float(crop[3]) * fh)))
        cx = max(0, min(fw - 1, int(round(float(crop[0]) * fw))))
        cy = max(0, min(fh - 1, int(round(float(crop[1]) * fh))))
        cw = min(cw, fw - cx)
        ch = min(ch, fh - cy)
    except (TypeError, ValueError, IndexError):
        return None
    if cw <= 0 or ch <= 0:
        return None
    return QRectF(cx, cy, cw, ch)


def _target_src(st, fw: int, fh: int, w: int, h: int):
    """返回 `(tgt_rect, src_rect)`。**几何与演出端 `engine._draw_layer` 完全一致**：

    · **A4 适配判据**（照抄 `engine.py:2774/2799-2802`）：
        xform 全默认（scale/x/y/rot 均≈默认值）⇒ **cover**（`max(W/iw,H/ih)`，等比铺满 + 裁切）；
        改过任一变换               ⇒ **contain**（`min(W/iw,H/ih) * 用户 scale`，完整显示）。
      ⚠ 改前本函数**永远**用 contain，导致非 16:9 素材的预览构图与现场不符。
    · **A4 crop 基准**：裁切后以**裁切尺寸**为新基准再算适配（`engine.py:2796 iw, ih = cw, ch`），
      而不是用整幅尺寸算框再把裁切区拉伸进去（那样裁切会被放大变形）。
    · 过渡空间变换：slide 从右侧滑入、zoom 从 70% 放大到 100%（fade/cut 无空间变换）。
    """
    us, ux, uy, rot = _xform_values(st)
    custom = _is_custom(us, ux, uy, rot)

    src = _crop_src_rect(getattr(st, "crop", None), fw, fh)
    if src is None:
        src = QRectF(0.0, 0.0, float(fw), float(fh))
    iw, ih = src.width(), src.height()                   # ★ 以裁切后尺寸为适配基准

    if custom:
        k = min(w / iw, h / ih) * us                     # contain + 用户缩放
    else:
        k = max(w / iw, h / ih)                          # cover（铺满 + 裁切）
    tw, th = iw * k, ih * k

    cx = w / 2.0 + ux / 100.0 * w
    cy = h / 2.0 + uy / 100.0 * h
    dx = 0.0
    trans = getattr(st, "trans", None) or "fade"
    tp = _clamp01(getattr(st, "trans_p", 1.0))
    if trans == "slide":
        dx = (1.0 - tp) * w                              # 从画布右侧滑入
    elif trans == "zoom":
        f = 0.7 + 0.3 * tp
        tw, th = tw * f, th * f
    tgt = QRectF(cx - tw / 2.0 + dx, cy - th / 2.0, tw, th)
    return tgt, src


def _glitch_offset(tp: float, n: int) -> float:
    """deterministic（不依赖随机种子）的横向错位；`tp→1` 时收敛到 0。"""
    v = (math.sin(tp * 97.0 + n * 12.9898) * 43758.5453) % 1.0
    if v >= 0.35:
        return 0.0
    return (v * 2.0 - 1.0) * _GLITCH_MAX_OFF * (1.0 - tp)


def _draw_image(painter: QPainter, img: QImage, tgt: QRectF, src: QRectF,
                trans: str, tp: float) -> None:
    """按过渡类型把 `img` 画到 `tgt`（`src` 为源矩形，做 crop）。"""
    if trans == "glitch" and tp < 0.999:
        height = tgt.height()
        if height <= 0.0:
            painter.drawImage(tgt, img, src)
            return
        step = max(8.0, height / 24.0)
        n = 0
        y = tgt.y()
        y_end = tgt.y() + height
        while y < y_end - 0.5:
            band = min(step, y_end - y)
            sy = src.y() + (y - tgt.y()) / height * src.height()
            sh = band / height * src.height()
            painter.drawImage(QRectF(tgt.x() + _glitch_offset(tp, n), y, tgt.width(), band),
                              img, QRectF(src.x(), sy, src.width(), sh))
            y += band
            n += 1
        return
    painter.drawImage(tgt, img, src)


# --------------------------------------------------------------------------
# subtract（Qt 无原生）：numpy 原地饱和减
# --------------------------------------------------------------------------
def _apply_subtract(canvas: QImage, w: int, h: int, jobs) -> None:
    """对每个 `(scratch_img, opacity)` 做 `dst = max(0, dst - src*opacity)`（RGB，饱和）。

    ⚠⚠ 防回归：正确的"减少"混合就是 **`max(0, dst − src)`**（分量饱和减）。
      **不要**改成 `np.maximum(dst, 255 − src)` —— 那**不是相减**，是 `技术选型.md` §B7
      当次**为测速度临时写的近似式**（原文自己标了"近似 subtract（仅测速度）"），
      已在 `docs/架构方案.md` §5.1 勘误。反例：dst=40、src=200 ⇒ 正确 `max(0, 40−200)=0`，
      而错误公式给 `max(40, 55)=55`。
      本式与旧引擎一致 —— AutoVJ `src/engine.py:2968 _apply_subtract` docstring 原文：
      「「减少」混合：画布 RGB 减去图层 RGB（饱和到 0，alpha 不动）」。

    实现用 `dst − min(dst, src)` 等价式（`min` 后再减不会下溢），避免 `astype(int16)`
    的临时大数组（实测朴素写法 1080p 49.6 ms/帧，本写法只走两次 uint8 ufunc）。
    """
    cvs = _np_view(canvas)
    if cvs.shape[0] != h or cvs.shape[1] != w or not cvs.flags["C_CONTIGUOUS"]:
        return
    cvs_rgb = cvs[:, :, :3]
    m = _sub_min(h, w)
    for img, op in jobs:
        try:
            src = _np_view(img)
            if src.shape[0] != h or src.shape[1] != w:
                continue
            s = src[:, :, :3]
            if op < 0.999:
                lut = _op_lut(op)
                np.copyto(s, lut[s])          # 透明度缩放（原地，写回 stride 视图）
            np.minimum(cvs_rgb, s, out=m)     # m = min(dst, src)
            np.subtract(cvs_rgb, m, out=cvs_rgb)   # dst -= m ⇒ 饱和减，不回绕
        except Exception:                                  # noqa: BLE001
            continue                                       # 单层失败不影响整帧


# --------------------------------------------------------------------------
# 合成器
# --------------------------------------------------------------------------
class Compositor:
    """持有按尺寸复用的离屏画布与 scratch 缓冲。

    ⚠ `compose()` 返回的是**内部复用的画布对象**：调用方必须**立即整幅画走 / 读走**，
    不可长期持有（下次 `compose()` 会改写它）。预览场景正是"画完即用"。
    """

    def compose(self, size, states, frame_lookup) -> QImage:
        """把 `states`（**须按 z 升序**）合成到离屏画布并返回。

        `size`: `(w, h)`；
        `frame_lookup(clip_id, play_sec) -> numpy BGRA | QImage | None`。
        ⚠ 向后兼容：只接受 1 个参数的旧回调也能用（见 `_wants_two_args`）。
        """
        w, h = int(size[0]), int(size[1])
        if w <= 0 or h <= 0:
            raise ValueError("size 必须为正：(w, h) = %r" % (size,))
        two_args = _wants_two_args(frame_lookup)
        canvas = _canvas(w, h)
        canvas.fill(_BLACK)

        keep = []          # 保住 QImage 借用的 numpy 缓冲，防止绘制期间被 GC
        normal_states = []
        subtract_jobs = []
        for st in states:
            if st is None or not getattr(st, "active", True):
                continue
            if getattr(st, "missing", False):
                continue                                   # E3：缺素材跳过本层
            op = _clamp01(getattr(st, "opacity", 1.0)) * _clamp01(getattr(st, "alpha", 1.0))
            if op <= 0.0:                                  # D2：基础项 opacity × alpha 只乘一次
                continue
            # ★ A5：fade 过渡与演出端 `engine._draw_layer` 对齐 —— 额外乘一次 `trans_p`。
            #   演出端 `paint_cover(p, img, cur_item, pulse, dx, dy, alpha * prog, crop)`
            #   （engine.py:2835），其中 alpha = base*opacity*ps.alpha 已含淡入量，故 fade
            #   实际是 α² 曲线。cut（trans_p 恒 1.0）、slide/zoom/glitch（演出端不额外乘 prog）
            #   均**不**走这一支。两端曲线据此一致。
            if (getattr(st, "trans", "fade") or "fade") == "fade":
                op *= _clamp01(getattr(st, "trans_p", 1.0))
                if op <= 0.0:
                    continue
            clip_id = getattr(st, "clip_id", "")
            play_sec = getattr(st, "play_sec", 0.0)
            # ★ A2：素材出点防御性钳位 —— 即便调用方未走 timeline，也不越过 src_out_sec。
            so = getattr(st, "src_out_sec", None)
            if so is not None:
                try:
                    play_sec = min(float(play_sec), float(so))
                except (TypeError, ValueError):
                    pass
            # ★ 把 play_sec 一起交给回调：同一素材不同 play_sec 的两层各取各的帧
            frame = frame_lookup(clip_id, play_sec) if two_args else frame_lookup(clip_id)
            if isinstance(frame, QImage):
                img = frame
            else:
                a = _bgra_array(frame)
                if a is None:
                    continue                               # 取不到帧 → 跳过，不黑整幅
                keep.append(a)
                img = _qimage(a)
            if img is None or img.isNull() or img.width() <= 0 or img.height() <= 0:
                continue
            blend = getattr(st, "blend", "normal") or "normal"
            if blend == SUBTRACT:
                subtract_jobs.append((st, img, op))
            else:
                normal_states.append((st, img, op))

        if normal_states:
            p = QPainter(canvas)
            try:
                p.setRenderHint(QPainter.SmoothPixmapTransform)
                for st, img, op in normal_states:
                    tgt, src = _target_src(st, img.width(), img.height(), w, h)
                    p.setOpacity(op)                       # D2：alpha 只乘一次
                    p.setCompositionMode(BLEND_QT.get(
                        getattr(st, "blend", "normal"), QPainter.CompositionMode_SourceOver))
                    _draw_image(p, img, tgt, src, getattr(st, "trans", "fade"),
                                _clamp01(getattr(st, "trans_p", 1.0)))
                p.setCompositionMode(QPainter.CompositionMode_SourceOver)
            finally:
                p.end()

        if subtract_jobs:
            # ⚠ 逐层渲染到同一块 scratch 并**立即**施加，不能先攒起来再统一施加 ——
            #    否则多个 subtract 层的 job 会全部指向同一块 scratch，只剩最后一层生效。
            scratch = _sub_image(w, h)
            for st, img, op in subtract_jobs:
                scratch.fill(Qt.transparent)
                p = QPainter(scratch)
                try:
                    p.setRenderHint(QPainter.SmoothPixmapTransform)
                    p.setOpacity(1.0)                      # 透明度改由 numpy 施加
                    p.setCompositionMode(QPainter.CompositionMode_SourceOver)
                    tgt, src = _target_src(st, img.width(), img.height(), w, h)
                    _draw_image(p, img, tgt, src, getattr(st, "trans", "fade"),
                                _clamp01(getattr(st, "trans_p", 1.0)))
                finally:
                    p.end()
                _apply_subtract(canvas, w, h, [(scratch, op)])

        return canvas


_DEFAULT = Compositor()


def compose(size, states, frame_lookup) -> QImage:
    """模块级便捷入口（内部复用同一个 `Compositor` 的 scratch 缓冲）。

    `frame_lookup(clip_id, play_sec)`（旧单参回调亦兼容，见 `_wants_two_args`）。
    ⚠ 返回值是复用的画布对象，**立即用**（见 `Compositor.compose`）。
    """
    return _DEFAULT.compose(size, states, frame_lookup)
