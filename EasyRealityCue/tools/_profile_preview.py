# -*- coding: utf-8 -*-
"""★ 预览卡顿 profile（**先测，别猜**）。

跑法（Windows）：
    P:\\ERAVJ\\EasyRealityCue\\venv\\Scripts\\python.exe  tools/_profile_preview.py
可选环境变量：
    ERCUE_PROF_MEDIA  真实 DXV 目录（默认 `Y:\\EasyRealityAutoVJ\\素材库`）
    ERCUE_PROF_WIDTH  预览控件宽度（默认依次 960 / 1280 / 1600）

量什么（全部真实运行：真开解码器、真跑 `CueRuntime`、真跑 `compose()`）：
  1) 画布尺寸（= 预览控件宽度 × 9/16）
  2) 端到端单 tick：`_compose()`（取帧 + 合成）耗时（1 层 / 3 层，分 DXV / 小 mp4）
  3) **拆解**：把取帧换成"预解好的缓存帧" ⇒ 量**纯合成**耗时（不含解码）
  4) `frame_play()` 单次耗时：前进 1 帧 / 前进 1.0s（最坏）/ 回退 seek
  5) **当前实际能达到的 fps**（连续跑 N tick 计时）
  6) 绘制（把合成结果画进控件）耗时

输出每个环节的**毫秒数字**，据此判定瓶颈。
"""

from __future__ import annotations

import os
import statistics
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import numpy as np                                                        # noqa: E402
from PySide6.QtCore import QEventLoop, QTimer                            # noqa: E402
from PySide6.QtWidgets import QApplication                               # noqa: E402

from ercue.model.cuesheet import (CompLayer, CueItem, CueSheet, Identity,  # noqa: E402
                                    MediaRef, TrackEntry)
from ercue.model.timeline import CueRuntime                              # noqa: E402
from ercue.ui.preview import PreviewCanvas                               # noqa: E402

APP = None
MEDIA_DIR = os.environ.get("ERCUE_PROF_MEDIA", r"Y:\EasyRealityAutoVJ\素材库")
WIDTHS = [int(x) for x in os.environ.get("ERCUE_PROF_WIDTH", "960,1280,1600").split(",")]


def _pct(vals, q):
    if not vals:
        return 0.0
    s = sorted(vals)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


def _stats(vals):
    return {"mean": statistics.fmean(vals), "p50": _pct(vals, 0.5),
            "p95": _pct(vals, 0.95), "max": max(vals)}


def _fmt(vals):
    s = _stats(vals)
    return "mean=%.1f p50=%.1f p95=%.1f max=%.1f ms" % (
        s["mean"], s["p50"], s["p95"], s["max"])


# ==========================================================================
# 夹具
# ==========================================================================
def _make_mp4(path, n=150, fps=30, w=320, h=180):
    """小 mp4（每帧颜色不同）。"""
    import av
    c = av.open(path, mode="w")
    st = c.add_stream("libx264", rate=fps)
    st.width, st.height, st.pix_fmt = w, h, "yuv420p"
    try:
        st.options = {"g": "30", "keyint_min": "30", "preset": "ultrafast"}
    except Exception:                                                     # noqa: BLE001
        pass
    for i in range(n):
        arr = np.zeros((h, w, 3), dtype=np.uint8)
        arr[:, :, 0] = (i * 7) % 256
        arr[:, :, 1] = (i * 13) % 256
        arr[:, :, 2] = (i * 29) % 256
        for pkt in st.encode(av.VideoFrame.from_ndarray(arr, format="rgb24")):
            c.mux(pkt)
    for pkt in st.encode(None):
        c.mux(pkt)
    c.close()
    return path


def _fixtures():
    tmp = tempfile.mkdtemp(prefix="ercue_prof_")
    small = _make_mp4(os.path.join(tmp, "small.mp4"))
    dxv = []
    for name in ("#20_AMP_VU_METER_INSIDE_CUBEROOM_ZDEPTH.mov", "001.mov", "002.mov"):
        p = os.path.join(MEDIA_DIR, name)
        if os.path.isfile(p):
            dxv.append(p)
    # 4K（3840×2160）大素材
    dxv4k = []
    for name in ("Balls visual@110 (19).mov", "Balls visual@110 (40).mov",
                 "Balls visual@110 (57).mov"):
        p = os.path.join(MEDIA_DIR, name)
        if os.path.isfile(p):
            dxv4k.append(p)
    return {"small": small, "dxv": dxv, "dxv4k": dxv4k}


def _build_runtime(paths, crop=None, offsets=None):
    """paths: [(clip_id, path)] → CueRuntime（每层 1 段，整段激活、无过渡）。

    `crop`（可选）`[x,y,w,h]` 归一化：给每段设裁切（用于模拟"图层内再放大"的画质场景）。
    `offsets`（可选）：每层的 `src_in_sec` —— 让各层 `play_sec` 不同（否则同一素材多层会被
      预览的 memo 去重成"只解一次"，量不出真实多层负载）。
    """
    sheet = CueSheet(title="profile")
    te = TrackEntry(track_key="k", identity=Identity(title="P", artist="A",
                                                     duration_sec=600.0, bpm=120.0))
    te.identity.ensure_meta_id()
    mp = {}
    for i, (cid, p) in enumerate(paths):
        ly = CompLayer(lyr_id="L%d" % i, name="L%d" % i, z=i)
        off = float(offsets[i]) if offsets and i < len(offsets) else 0.0
        ly.items.append(CueItem(item_id="i%d" % i, clip_id=cid, at_sec=0.0,
                                end_sec=600.0, trans_in="cut", trans_out="cut",
                                src_in_sec=off,
                                crop=(list(crop) if crop else None)).normalized())
        te.composition.layers.append(ly)
        sheet.media.append(MediaRef(clip_id=cid, name=os.path.basename(p)))
        mp[cid] = p
    sheet.tracks.append(te)
    return CueRuntime(sheet, te, media_paths=mp)


# ==========================================================================
# 量：单次 _compose（含解码）
# ==========================================================================
def _measure_compose(paths, width, n=60, dt=1.0 / 30.0):
    cv = PreviewCanvas()
    cv.resize(width, int(width * 9 / 16))
    cv.set_runtime(_build_runtime(paths), 0.0)
    W, H = cv._canvas_size()
    cv.set_pos(0.0)
    cv._compose()                                    # 预热（含 seek/开容器）
    ts = []
    pos = 0.0
    for _ in range(n):
        pos += dt
        cv.set_pos(pos)
        t0 = time.perf_counter()
        cv._compose()
        ts.append((time.perf_counter() - t0) * 1000.0)
    cv.shutdown()
    return W, H, ts


# ==========================================================================
# 量：纯合成（取帧换成预解好的缓存帧，不含解码）
# ==========================================================================
def _measure_compose_nolookup(paths, width, n=40, dt=1.0 / 30.0):
    cv = PreviewCanvas()
    cv.resize(width, int(width * 9 / 16))
    cv.set_runtime(_build_runtime(paths), 0.0)
    # 预解一帧（DXV 带 alpha 的 BGRA），之后 lookup 直接返回它（零解码）
    from ercue.media.frames import CueFrameSource
    src = CueFrameSource()
    fr = src.frame_exact(paths[0][1], 0.0)
    src.close()
    if fr is None:
        cv.shutdown()
        return None
    cv._frame_lookup = lambda clip_id, play_sec=0.0: fr      # noqa: ARG005
    cv.set_pos(0.0)
    cv._compose()
    ts = []
    pos = 0.0
    for _ in range(n):
        pos += dt
        cv.set_pos(pos)
        t0 = time.perf_counter()
        cv._compose()
        ts.append((time.perf_counter() - t0) * 1000.0)
    cv.shutdown()
    return ts


# ==========================================================================
# 量：连续跑 _tick 的实际 fps（含 runtime.update + 合成）
# ==========================================================================
def _measure_fps(paths, width, seconds=3.0, dt=1.0 / 30.0):
    cv = PreviewCanvas()
    cv.resize(width, int(width * 9 / 16))
    cv.set_runtime(_build_runtime(paths), 0.0)
    cv.set_pos(0.0)
    cv.set_playing(True)
    cv._compose()                                    # 预热
    pos = 0.0
    n = 0
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < seconds:
        pos += dt
        cv.set_pos(pos)
        cv._tick()
        n += 1
    el = time.perf_counter() - t0
    cv.shutdown()
    return n / max(1e-9, el)


def _measure_delivered_fps(paths, width, seconds=3.0, fps_override=None):
    """**用户实际看到的 fps**：起真实定时器 + 真实事件循环，用 30Hz 位置推进模拟 `_play_tick`。"""
    saved = PreviewCanvas.FPS
    if fps_override is not None:
        PreviewCanvas.FPS = fps_override
    cv = PreviewCanvas()
    cv.resize(width, int(width * 9 / 16))
    cv.set_runtime(_build_runtime(paths), 0.0)
    count = {"n": 0}
    orig = cv._compose

    def counting():
        count["n"] += 1
        orig()

    cv._compose = counting
    cv.set_pos(0.0)
    cv.set_active(True)
    cv.set_playing(True)
    pos = {"p": 0.0}
    drv = QTimer()
    drv.setInterval(33)                          # ≈ window._play_timer（30Hz）
    drv.timeout.connect(lambda: (pos.__setitem__("p", pos["p"] + 1 / 30.0),
                                 cv.set_pos(pos["p"])))
    drv.start()
    loop = QEventLoop()
    QTimer.singleShot(int(seconds * 1000), loop.quit)
    t0 = time.perf_counter()
    loop.exec()                                  # ★ 真实事件循环（定时器精度才对）
    el = time.perf_counter() - t0
    drv.stop()
    fps = count["n"] / max(1e-9, el)
    cv.shutdown()
    PreviewCanvas.FPS = saved
    return fps


def _measure_paint(paths, width, n=20):
    cv = PreviewCanvas()
    cv.resize(width, int(width * 9 / 16))
    cv.set_runtime(_build_runtime(paths), 0.0)
    cv.set_pos(0.0)
    cv._compose()
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        cv.grab()                                    # 真渲染控件（含 paintEvent 整幅画）
        ts.append((time.perf_counter() - t0) * 1000.0)
    cv.shutdown()
    return ts


# ==========================================================================
# 量：frame_play 单次耗时（前进 1 帧 / 前进 1s / 回退 seek）
# ==========================================================================
def _measure_frame_play(path, fps_hint=30.0, n=40):
    from ercue.media.frames import CueFrameSource
    src = CueFrameSource()
    src.frame_play(path, 0.0)                        # 预热
    adv = []
    t = 0.0
    for _ in range(n):
        t += 1.0 / fps_hint
        t0 = time.perf_counter()
        src.frame_play(path, round(t, 4))
        adv.append((time.perf_counter() - t0) * 1000.0)
    big = []
    for k in range(10):
        t0 = time.perf_counter()
        src.frame_play(path, 3.0 + k * 1.0 + 0.5)    # 一次前进 1.0s（最坏）
        big.append((time.perf_counter() - t0) * 1000.0)
    back = []
    for k in range(10):
        t0 = time.perf_counter()
        src.frame_play(path, 1.0 + k * 0.2)          # 回退 ⇒ seek
        back.append((time.perf_counter() - t0) * 1000.0)
    src.close()
    return adv, big, back


# ==========================================================================
# ★ Task F：4K 取帧**四步拆解**（解码 / 转 BGRA / 合成 / 绘制）+ 端到端 fps + 画质对比
# ==========================================================================
def _fit_box(fw, fh, out_size):
    """保持宽高比、绝不放大：`out_size`（目标盒）→ 实际尺寸；不需缩小返回 None。"""
    if not out_size or fw <= 0 or fh <= 0:
        return None
    k = min(out_size[0] / float(fw), out_size[1] / float(fh))
    if k >= 0.999:
        return None
    return (max(1, int(round(fw * k))), max(1, int(round(fh * k))))


def _stage_decode(path, thread_type, n=120, thread_count=None):
    """第 1 步「解码」：只 `next(frame)`，**不转 ndarray**。"""
    import av
    c = av.open(path, metadata_errors="replace")
    s = c.streams.video[0]
    if thread_type is not None:
        s.thread_type = thread_type
    if thread_count:
        s.thread_count = int(thread_count)
    gen = c.decode(video=0)
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        next(gen)
        ts.append((time.perf_counter() - t0) * 1000.0)
    c.close()
    return ts


def _stage_decode_convert(path, thread_type, out_size=None, n=120, thread_count=None):
    """第 2/3 步「解码 + 转 BGRA（+ 可选一步缩放）」；`out_size=None` ⇒ 原尺寸。

    ★ 缩放器**缓存复用**（`VideoReformatter`）——否则 `frame.reformat()` 每个调用都新建
      swscale 上下文，比转全尺寸还慢（见报告）。
    """
    import av
    from av.video.reformatter import VideoReformatter
    c = av.open(path, metadata_errors="replace")
    s = c.streams.video[0]
    if thread_type is not None:
        s.thread_type = thread_type
    if thread_count:
        s.thread_count = int(thread_count)
    gen = c.decode(video=0)
    box = None
    if out_size:
        first = next(gen)                                     # 用首帧定 fit（保持宽高比）
        box = _fit_box(first.width, first.height, out_size)
    sc = VideoReformatter() if box else None
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        fr = next(gen)
        if box:
            ff = sc.reformat(fr, width=box[0], height=box[1], format="bgra",
                             interpolation="BILINEAR")
            _ = np.ascontiguousarray(ff.to_ndarray())
        else:
            _ = np.ascontiguousarray(fr.to_ndarray(format="bgra"))
        ts.append((time.perf_counter() - t0) * 1000.0)
    c.close()
    return ts


def _frame_play_stat(path, out_size, thread_type, thread_count=None, n=60):
    """团队判据①：`CueFrameSource().frame_play(p, i/30)` 连续 60 帧的耗时。"""
    import ercue.media.frames as F
    F.DECODE_THREAD_TYPE = "" if thread_type is None else thread_type
    F.DECODE_THREAD_COUNT = thread_count
    src = F.CueFrameSource()
    src.frame_play(path, 0.0, out_size=out_size)              # 预热（开容器 + 定位）
    ts = []
    for i in range(n):
        t0 = time.perf_counter()
        src.frame_play(path, i / 30.0, out_size=out_size)
        ts.append((time.perf_counter() - t0) * 1000.0)
    src.close()
    return ts


def _canvas_4k(paths, width, out_size, thread_type, crop=None, sec=0.0, offsets=None,
               thread_count=None):
    """按指定 `out_size` / `thread_type` 建一个已预热的预览画布（供合成/绘制测量）。"""
    import ercue.media.frames as F
    F.DECODE_THREAD_TYPE = "" if thread_type is None else thread_type
    F.DECODE_THREAD_COUNT = thread_count
    cv = PreviewCanvas()
    cv.resize(width, int(width * 9 / 16))
    cv.set_runtime(_build_runtime(paths, crop=crop, offsets=offsets), 0.0)
    if out_size is None:
        cv._out_size = lambda: None                # 模拟"改前：不缩放"
    cv.set_pos(sec)
    cv._compose()
    return cv


def _measure_compose_4k(paths, width, out_size, thread_type, crop=None, n=40, offsets=None,
                        thread_count=None):
    """第 4 步「合成」：把取帧换成**预解好的缓存帧** ⇒ 纯合成耗时（不含解码）。"""
    import ercue.media.frames as F
    from ercue.media.frames import CueFrameSource
    src = CueFrameSource()
    box = None
    if out_size is not None:
        box = (int(round(width * 1.5)), int(round(width * 9 / 16 * 1.5)))
    fr = src.frame_exact(paths[0][1], 0.0, out_size=box)
    src.close()
    if fr is None:
        return None
    F.DECODE_THREAD_TYPE = "" if thread_type is None else thread_type
    F.DECODE_THREAD_COUNT = thread_count
    cv = PreviewCanvas()
    cv.resize(width, int(width * 9 / 16))
    cv.set_runtime(_build_runtime(paths, crop=crop, offsets=offsets), 0.0)
    cv._frame_lookup = lambda clip_id, play_sec=0.0: fr        # noqa: ARG005
    cv.set_pos(0.0)
    cv._compose()
    ts = []
    pos = 0.0
    for _ in range(n):
        pos += 1.0 / 30.0
        cv.set_pos(pos)
        t0 = time.perf_counter()
        cv._compose()
        ts.append((time.perf_counter() - t0) * 1000.0)
    cv.shutdown()
    return ts


def _measure_paint_4k(paths, width, out_size, thread_type, crop=None, n=15, offsets=None,
                      thread_count=None):
    """第 5 步「绘制」：`grab()` 真渲染控件（含 paintEvent 整幅画）。"""
    cv = _canvas_4k(paths, width, out_size, thread_type, crop=crop, offsets=offsets,
                    thread_count=thread_count)
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        cv.grab()
        ts.append((time.perf_counter() - t0) * 1000.0)
    cv.shutdown()
    return ts


def _delivered_fps_4k(paths, width, out_size, thread_type, seconds=10.0, crop=None,
                      offsets=None, thread_count=None):
    """端到端**实际交付 fps**：真实定时器 + 真实事件循环，用 30Hz 推进位置。"""
    import ercue.media.frames as F
    F.DECODE_THREAD_TYPE = "" if thread_type is None else thread_type
    F.DECODE_THREAD_COUNT = thread_count
    cv = PreviewCanvas()
    cv.resize(width, int(width * 9 / 16))
    cv.set_runtime(_build_runtime(paths, crop=crop, offsets=offsets), 0.0)
    if out_size is None:
        cv._out_size = lambda: None
    count = {"n": 0}
    orig = cv._compose

    def counting():
        count["n"] += 1
        orig()

    cv._compose = counting
    cv.set_pos(0.0)
    cv.set_active(True)
    cv.set_playing(True)
    pos = {"p": 0.0}
    drv = QTimer()
    drv.setInterval(33)
    drv.timeout.connect(lambda: (pos.__setitem__("p", pos["p"] + 1 / 30.0),
                                 cv.set_pos(pos["p"])))
    drv.start()
    loop = QEventLoop()
    QTimer.singleShot(int(seconds * 1000), loop.quit)
    t0 = time.perf_counter()
    loop.exec()
    el = time.perf_counter() - t0
    drv.stop()
    fps = count["n"] / max(1e-9, el)
    cv.shutdown()
    return fps


def _screenshot_compare(path, width, out_path):
    """★ 画质对比：同一帧"改前(原尺寸取帧)" vs "改后(1.5×取帧)"，含 crop 放大场景。

    上排 = 全幅；下排 = crop=[0.25,0.25,0.5,0.5]（放大 2×，最能暴露"取帧缩太小 ⇒ 糊"）。
    返回 (全幅平均像素差, crop 平均像素差)（0 = 完全一致）。
    """
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QColor, QImage, QPainter, QFont
    box = (int(round(width * 1.5)), int(round(width * 9 / 16 * 1.5)))
    shots = []
    diffs = []
    for crop, tag in ((None, "全幅"), ([0.25, 0.25, 0.5, 0.5], "crop 放大 2×")):
        cvs = []
        for out_size, lab in ((None, "改前·原尺寸取帧"), (box, "改后·1.5×取帧")):
            cv = _canvas_4k([("c0", path)], width, out_size, "AUTO", crop=crop, sec=3.0)
            pix = cv.grab()
            cv.shutdown()
            cvs.append((pix.toImage(), lab))
        # 像素差（改后 vs 改前，同尺寸）
        a0 = _qimg_np(cvs[0][0])
        a1 = _qimg_np(cvs[1][0])
        if a0 is not None and a1 is not None and a0.shape == a1.shape:
            diff = float(np.mean(np.abs(a0.astype(np.int16) - a1.astype(np.int16))))
        else:
            diff = -1.0
        diffs.append(diff)
        shots.append((tag, cvs, diff))
    # 拼图
    pad, labh = 12, 22
    one_w = max(im.width() for _, cvs, _ in shots for im, _ in cvs)
    one_h = max(im.height() for _, cvs, _ in shots for im, _ in cvs)
    two_w = one_w * 2 + pad * 3
    two_h = (one_h + labh) * len(shots) + pad * (len(shots) + 1) + labh
    canvas = QImage(two_w, two_h, QImage.Format_ARGB32)
    canvas.fill(QColor("#1a1a1a"))
    p = QPainter(canvas)
    try:
        f = QFont()
        f.setPointSize(10)
        p.setFont(f)
        p.setPen(QColor("#e0e0e0"))
        p.drawText(QRectF(pad, 2, two_w, labh), 0,
                   "预览画质对比（%s）  平均像素差：全幅=%.2f / crop=%.2f（0=完全一致）"
                   % (os.path.basename(path), diffs[0], diffs[1]))
        y = labh + pad
        for tag, cvs, diff in shots:
            p.setPen(QColor("#ffd479"))
            p.drawText(QRectF(pad, y, two_w, labh), 0,
                       "%s    平均像素差 = %.2f" % (tag, diff))
            y += labh
            x = pad
            for im, lab in cvs:
                p.drawImage(x, y, im)
                p.setPen(QColor("#8fd0ff"))
                p.drawText(QRectF(x, y + one_h - 18, one_w, 18), 0, lab)
                p.setPen(QColor("#e0e0e0"))
                x += one_w + pad
            y += one_h + pad
    finally:
        p.end()
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    canvas.save(out_path)
    return diffs


def _qimg_np(img):
    """QImage → numpy (h,w,4) BGRA（只读比较用）。"""
    from PySide6.QtGui import QImage
    try:
        img = img.convertToFormat(QImage.Format_ARGB32)
        w, h = img.width(), img.height()
        bpl = img.bytesPerLine()
        buf = bytes(img.constBits())
        a = np.frombuffer(buf, dtype=np.uint8, count=bpl * h).reshape(h, bpl)
        return a[:, :w * 4].reshape(h, w, 4).copy()
    except Exception:                                                         # noqa: BLE001
        return None


def _task_f_section():
    """Task F：4K 四步拆解（改前/改后）+ 端到端 fps + 画质对比。"""
    print("\n" + "#" * 78)
    print("# ★ Task F：4K 素材专项（四步拆解 / 端到端 fps / 画质对比）")
    print("#" * 78)
    p4k = None
    for name in ("Balls visual@110 (19).mov", "Balls visual@110 (40).mov"):
        cand = os.path.join(MEDIA_DIR, name)
        if os.path.isfile(cand):
            p4k = cand
            break
    if p4k is None:
        print("（未找到 4K 素材，跳过）")
        return
    width = 960
    H = int(width * 9 / 16)
    box = (int(round(width * 1.5)), int(round(H * 1.5)))
    import ercue.media.frames as F
    tc_new = F.DECODE_THREAD_COUNT                    # 出货配置的绑定线程数（默认 2）
    print("素材 = %s" % os.path.basename(p4k))
    print("画布 = %d×%d ；取帧目标盒(1.5×) = %d×%d" % (width, H, box[0], box[1]))
    print("出货配置：thread_type=%r  thread_count=%r" % (F.DECODE_THREAD_TYPE, tc_new))

    # ---- 判据①：4K / HD 取帧 P95（团队原口径：frame_play(p, i/30) 连续 60 帧）----
    print("\n--- 判据①：取帧耗时（`CueFrameSource.frame_play(p, i/30)`，连续 60 帧）---")
    hd_paths = [os.path.join(MEDIA_DIR, n) for n in
                ("Balls visual@110 (40).mov", "#20_AMP_VU_METER_INSIDE_CUBEROOM_ZDEPTH.mov")
                if os.path.isfile(os.path.join(MEDIA_DIR, n))]
    for pth, tag in [(p4k, "4K  Balls visual@110 (19).mov")] + \
                    [(p, "HD  %s" % os.path.basename(p)) for p in hd_paths[:1]]:
        ts_b = _frame_play_stat(pth, None, None, None)            # 改前：原尺寸 + 不改多线程
        ts_a = _frame_play_stat(pth, box, "AUTO", tc_new)         # 改后：1.5× + AUTO/2
        print("[%s]" % tag)
        print("      改前（原尺寸取帧）      : %s" % _fmt(ts_b))
        print("      改后（1.5×取帧 + AUTO/%s）: %s   ⇒ P95 ≤ 20ms ? %s"
              % (tc_new, _fmt(ts_a), "✓" if _stats(ts_a)["p95"] <= 20.0 else "✗"))

    print("\n--- 四步拆解（改前 = 默认多线程 + 原尺寸取帧；改后 = AUTO/%s + 1.5× 取帧）---" % tc_new)
    for label, tt, osz, tcn in (("改前", None, None, None), ("改后", "AUTO", box, tc_new)):
        dec = _stage_decode(p4k, tt, thread_count=tcn)
        conv = _stage_decode_convert(p4k, tt, None, thread_count=tcn)
        scl = _stage_decode_convert(p4k, tt, osz, thread_count=tcn) if osz else None
        k_dec = _stats(dec)["p50"]
        print("[%s] ① 解码（纯，不转 ndarray）        : %s" % (label, _fmt(dec)))
        print("[%s] ①+② 解码 + 转BGRA（原尺寸）      : %s   ⇒ ② 转BGRA ≈ %.2f ms"
              % (label, _fmt(conv), _stats(conv)["p50"] - k_dec))
        if scl:
            print("[%s] ①+③ 解码 + 缩放到 1.5× + BGRA  : %s   ⇒ ③ 缩放并转BGRA ≈ %.2f ms"
                  % (label, _fmt(scl), _stats(scl)["p50"] - k_dec))
        else:
            print("[%s] ③ 缩放                          : —（原尺寸取帧，交给合成器缩放）" % label)
        cr = _measure_compose_4k([("c0", p4k)], width, osz, tt, thread_count=tcn)
        pt = _measure_paint_4k([("c0", p4k)], width, osz, tt, thread_count=tcn)
        if cr:
            print("[%s] ④ 合成（纯，预解帧，1 层）      : %s" % (label, _fmt(cr)))
        print("[%s] ⑤ 绘制（grab）                   : %s" % (label, _fmt(pt)))
        if scl:
            tick = _stats(scl)["p50"] + (_stats(cr)["p50"] if cr else 0) \
                + _stats(pt)["p50"]
            print("[%s] 合计单 tick ≈ ③+④+⑤ = %.2f ms（30fps 预算 33.3ms）" % (label, tick))
        print()

    print("--- 端到端播放（真实定时器 + 事件循环，10s；多层用不同入点避免被去重）---")
    hd = [os.path.join(MEDIA_DIR, n) for n in
          ("Balls visual@110 (40).mov", "Balls visual@110 (57).mov")
          if os.path.isfile(os.path.join(MEDIA_DIR, n))]
    cases = [
        ("1 层 4K",                    [("c0", p4k)], [0.0]),
        ("2 层 4K 同入点(叠层)",        [("c0", p4k), ("c1", p4k)], [0.0, 0.0]),
        ("3 层 4K 同入点(叠层)",        [("c%d" % i, p4k) for i in range(3)], [0.0, 0.0, 0.0]),
        ("3 层 4K 异入点(同一素材)",    [("c%d" % i, p4k) for i in range(3)],
         [0.0, 0.7, 1.4]),
    ]
    if len(hd) >= 2:
        cases.append(("3 层混合(4K+2×HD)",
                      [("c0", p4k), ("c1", hd[0]), ("c2", hd[1])], [0.0, 0.3, 0.6]))
    for tag, paths, offs in cases:
        before = _delivered_fps_4k(paths, width, None, None, seconds=10.0, offsets=offs)
        after = _delivered_fps_4k(paths, width, box, "AUTO", seconds=10.0, offsets=offs,
                                  thread_count=tc_new)
        print("[%-18s] 交付 fps：改前=%.1f  →  改后=%.1f（目标 ≥24）%s"
              % (tag, before, after, "  ✓" if after >= 24 else "  ✗"))

    print("\n--- 画质对比（截图 side-by-side）---")
    shot = os.path.join(_ROOT, "_shots", "m7_scale_compare.png")
    diffs = _screenshot_compare(p4k, 960, shot)
    print("已存：%s" % shot)
    print("平均像素差：全幅=%.2f / crop 放大 2×=%.2f（0 = 与「原尺寸取帧」完全一致）"
          % (diffs[0], diffs[1]))


# ==========================================================================
def main() -> int:
    global APP
    APP = QApplication.instance() or QApplication([])
    fx = _fixtures()
    dxv = fx["dxv"]
    print("=" * 78)
    print("预览 profile   DXV 目录=%s（找到 %d 个真实素材）" % (MEDIA_DIR, len(dxv)))
    print("小 mp4 = %s" % fx["small"])
    print("=" * 78)

    # ---- 素材：DXV 单层 / 3 层 / 小 mp4 ----
    if dxv:
        one = [("c0", dxv[0])]
        three = [("c%d" % i, dxv[i % len(dxv)]) for i in range(3)]
    else:
        one = three = [("c0", fx["small"])]
    small4 = [("c%d" % i, fx["small"]) for i in range(3)]

    for width in (WIDTHS if os.environ.get("ERCUE_PROF_MAIN", "1") != "0" else []):
        print("\n" + "#" * 78)
        print("# 预览控件宽度 = %d px" % width)
        print("#" * 78)

        W, H, ts = _measure_compose(one, width)
        print("\n[画布] %d×%d（= 宽度 × 9/16）" % (W, H))

        print("\n[1 层 DXV] _compose（含取帧）: %s" % _fmt(ts))
        tl = _measure_compose_nolookup(one, width)
        if tl:
            print("[1 层 DXV] _compose（纯合成，无取帧）: %s" % _fmt(tl))

        if len(dxv) >= 1:
            _, _, ts3 = _measure_compose(three, width)
            print("[3 层 DXV] _compose（含取帧）: %s" % _fmt(ts3))
            tl3 = _measure_compose_nolookup(three, width)
            if tl3:
                print("[3 层 DXV] _compose（纯合成，无取帧）: %s" % _fmt(tl3))

        _, _, ts_small = _measure_compose(small4, width)
        print("[3 层 小mp4] _compose（含取帧）: %s" % _fmt(ts_small))

        if fx["dxv4k"]:
            one4k = [("c0", fx["dxv4k"][0])]
            three4k = [("c%d" % i, fx["dxv4k"][i % len(fx["dxv4k"])]) for i in range(3)]
            _, _, ts4 = _measure_compose(one4k, width, n=30)
            print("[1 层 4K ] _compose（含取帧）: %s" % _fmt(ts4))
            _, _, ts4b = _measure_compose(three4k, width, n=30)
            print("[3 层 4K ] _compose（含取帧）: %s" % _fmt(ts4b))
            tl4 = _measure_compose_nolookup(one4k, width)
            if tl4:
                print("[1 层 4K ] _compose（纯合成，无取帧）: %s" % _fmt(tl4))

        fps = _measure_fps((three if dxv else small4), width, seconds=3.0)
        print("[3 层] 连续 _tick 实际 fps = %.1f（30fps 目标）" % fps)

        # ★ 用户实际看到的 fps：真实定时器 + 真实事件循环（改前 FPS=10 / 改后 FPS=30）
        dfps_old = _measure_delivered_fps((three if dxv else small4), width,
                                          seconds=3.0, fps_override=10)
        dfps_new = _measure_delivered_fps((three if dxv else small4), width,
                                          seconds=3.0, fps_override=30)
        print("[3 层] 端到端交付 fps（真实定时器+事件循环）: 改前(FPS=10)=%.1f  →  改后(FPS=30)=%.1f"
              % (dfps_old, dfps_new))

        pt = _measure_paint((three if dxv else small4), width, n=15)
        print("[3 层] 控件整幅绘制（grab）: %s" % _fmt(pt))

    # ---- frame_play 单次耗时 ----
    print("\n" + "#" * 78)
    print("# frame_play 单次耗时（素材 = %s）"
          % (os.path.basename(dxv[0]) if dxv else os.path.basename(fx["small"])))
    print("#" * 78)
    target = dxv[0] if dxv else fx["small"]
    adv, big, back = _measure_frame_play(target)
    print("前进 1 帧(33ms)     : %s" % _fmt(adv))
    print("一次前进 1.0s（最坏）: %s   ← PLAY_FWD_MAX_SEC=1.0 下的顺序解码量" % _fmt(big))
    print("回退 seek           : %s" % _fmt(back))

    # ★ Task F：4K 四步拆解 / 端到端 fps / 画质对比（`ERCUE_PROF_TASKF=0` 可跳过）
    if os.environ.get("ERCUE_PROF_TASKF", "1") != "0":
        _task_f_section()

    print("\n完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
