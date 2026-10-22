# -*- coding: utf-8 -*-
"""★ BugFix A 组复现 / 验证（需求：用户报 3 条 bug，其中 2 条在 timeline_widget.py）。

跑法：<Cue>\\venv\\Scripts\\python.exe -u <Cue>\\tools\\_bugfix_a_verify.py [OUT_JSON]

三条：
  Bug1 双击裁剪时时间轴全部消失（松手才回来）
  Bug2 拖动视频素材时其素材音轨波形不跟手
  Bug3 变速后时长不重算（10s 素材 0.5× → 20s；调回 1× 仍 20s，只播前 10s）

输出 JSON 含每条 bug 的**量化指标** + `bug`/`fixed` 判定；修复前后各跑一次对比。
⚠ 只读探测 + 事件驱动，不改产品代码。
"""
import json
import os
import sys
import tempfile
import traceback

CUE = r"P:\ERAVJ\EasyRealityCue"
sys.path.insert(0, os.path.join(CUE, "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import logging                                    # noqa: E402
logging.disable(logging.CRITICAL)

import numpy as np                                 # noqa: E402
from PySide6.QtCore import QPoint, Qt              # noqa: E402
from PySide6.QtGui import QImage                   # noqa: E402
from PySide6.QtTest import QTest                   # noqa: E402
from PySide6.QtWidgets import QApplication         # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(CUE, "tools", "_bugfix_a_verify.json")
AUD = r"P:\ERAVJ\brand\ui_audit"

res = {}
app = QApplication.instance() or QApplication([])

# 捕获 paintEvent 里逃逸的异常（PySide 会把 Python 异常交给 sys.excepthook）
_paint_errors = []
_orig_hook = sys.excepthook


def _hook(etype, evalue, tb):
    _paint_errors.append("%s: %s" % (etype.__name__, evalue))
    _orig_hook(etype, evalue, tb)


sys.excepthook = _hook


def _make_mp4(path, frames=100, fps=10, w=320, h=180):
    """10s 素材（10fps × 100 帧）。"""
    import av
    c = av.open(path, "w")
    st = c.add_stream("mpeg4", rate=fps)
    st.width, st.height = w, h
    st.pix_fmt = "yuv420p"
    for i in range(frames):
        img = np.zeros((h, w, 3), np.uint8)
        img[:, :, 2] = 180
        img[:, (i * 12) % w:((i * 12) % w) + 24, 1] = 255
        for pk in st.encode(av.VideoFrame.from_ndarray(img, format="rgb24")):
            c.mux(pk)
    for pk in st.encode():
        c.mux(pk)
    c.close()


tmp = tempfile.mkdtemp(prefix="ercue_bugfixa_")
mp4 = os.path.join(tmp, "clip10s.mp4")
_make_mp4(mp4)

from ercue.ui.window import MainWindow             # noqa: E402
from ercue.audio.waveform import WaveformData      # noqa: E402
from ercue.ui import theme                         # noqa: E402

BG = theme.c("bg")


def _rgb(img):
    if hasattr(img, "toImage"):
        img = img.toImage()
    im = img.convertToFormat(QImage.Format_ARGB32)
    ww, hh = im.width(), im.height()
    bpl = im.bytesPerLine()
    a = np.frombuffer(bytes(im.constBits()), dtype=np.uint8, count=bpl * hh).reshape(hh, bpl)
    return a[:, :ww * 4].reshape(hh, ww, 4)[:, :, :3].astype(np.int16)


def _content_px(img):
    """非背景像素数（bg=#1e1e1e，任一通道差 >24 即算"有东西"）。"""
    a = _rgb(img)
    d = np.abs(a - np.array([int(BG[1:3], 16), int(BG[3:5], 16), int(BG[5:7], 16)],
                            dtype=np.int16)).max(axis=2)
    return int((d > 24).sum())


def _new_window():
    w = MainWindow()
    w.thumbs_enabled = False
    w._interactive = False
    w.setAttribute(Qt.WA_DontShowOnScreen, True)
    w.resize(1280, 860)
    w.show()
    for _ in range(6):
        app.processEvents()
    return w


def _setup_clip(at=4.0):
    w = _new_window()
    w.new_sheet("BugFixA", 1920, 1080)
    ids = w.add_media_paths([mp4])
    cid = ids[0]
    w.add_track("曲", "a", 300.0, 120.0)
    ly = w._layers()[0]
    it = w.add_cue(ly.lyr_id, cid, at)
    tl = w.timeline
    tl.resize(1100, 260)
    tl.set_view(0.0, 30.0)
    for _ in range(6):
        app.processEvents()
    return w, cid, ly, it, tl


def _row_y(tl, row=0):
    return int(theme.RULER_H + tl.row_h * row + tl.row_h / 2)


# ==========================================================================
# Bug 1 · 双击裁剪时时间轴全部消失
# ==========================================================================
def bug1():
    w, cid, ly, it, tl = _setup_clip()
    try:
        y = _row_y(tl)
        xmid = int((tl._x(float(it.at_sec)) + tl._x(float(it.end_sec))) / 2)
        QTest.mouseDClick(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xmid, y))
        for _ in range(4):
            app.processEvents()
        normal = _content_px(tl.grab())                    # 剪辑态、未拖动（基准）
        # 按下左把手（**不移动、不松手**）⇒ 用户此刻应看到时间轴照常显示
        QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier,
                         QPoint(int(tl._x(float(it.at_sec))) + 2, y))
        for _ in range(2):
            app.processEvents()
        _paint_errors.clear()
        during_grab_err = None
        try:
            during = _content_px(tl.grab())                # 拖拽中第一帧（应正常重绘）
        except Exception as ex:                            # noqa: BLE001
            during = -1
            during_grab_err = "%s: %s" % (type(ex).__name__, ex)
        errs = list(_paint_errors)
        pv_now = tl._trim.get("preview") if tl._trim else "(no trim)"
        QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier,
                           QPoint(int(tl._x(float(it.at_sec))) + 2, y))
        for _ in range(3):
            app.processEvents()
        after_release = _content_px(tl.grab())
        r = {"normal_content_px": normal, "during_drag_content_px": during,
             "after_release_content_px": after_release,
             "paint_exceptions": errs[:3], "during_grab_error": during_grab_err,
             "trim_preview_at_press": str(pv_now),
             "content_loss_ratio": (None if during < 0
                                    else round(1.0 - during / max(1, normal), 4))}
        r["bug"] = bool(errs) or bool(during_grab_err) or (during < 0) or (during < normal * 0.6)
        res["bug1_trim_drag_blank"] = r
    finally:
        try:
            w.shutdown()
        except Exception:                                  # noqa: BLE001
            pass


# ==========================================================================
# Bug 2 · 拖动素材时其音频波形不跟手
# ==========================================================================
def _green_spike_x(img, tl):
    """第 0 行内"绿色波形"像素最多的列 x（= 尖峰列）；无则 -1。"""
    a = _rgb(img)
    y0, y1 = theme.RULER_H, theme.RULER_H + int(tl.row_h)
    band = a[y0:y1, :, :]
    g = band[:, :, 1]
    rr = band[:, :, 0]
    bb = band[:, :, 2]
    green = (g > 60) & (g > rr + 30) & (g > bb + 30)
    counts = green.sum(axis=0)
    if counts.max() <= 0:
        return -1
    return int(np.argmax(counts))


def bug2():
    w, cid, ly, it, tl = _setup_clip()
    try:
        # 注入假波形：尖峰落在**片段可见源窗口**内（片段 10s ⇒ 可见源 = 0~10s ⇒ 列 0~50）
        n = 300
        amp = np.full(n, 0.02, dtype=np.float32)
        amp[30] = 1.0                                   # 源 6.0s 处尖峰（可见窗口内）
        amp[45] = 0.1                                   # 弱峰，保证 argmax 恒为 30 列
        wf = WaveformData(columns=n, amp=amp,
                          r=np.zeros(n, np.float32), g=np.ones(n, np.float32),
                          b=np.zeros(n, np.float32), duration_sec=60.0)
        tl.set_clip_waves({cid: wf})
        tl.set_show_clip_audio(True)
        for _ in range(4):
            app.processEvents()

        y = _row_y(tl)
        x0 = _green_spike_x(tl.grab(), tl)                 # 静止时尖峰列
        cx = int((tl._x(float(it.at_sec)) + tl._x(float(it.end_sec))) / 2)
        QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(cx, y))
        QTest.mouseMove(tl, QPoint(cx + 150, y))           # 向右拖 150px（不松手）
        for _ in range(3):
            app.processEvents()
        dx = float(tl._preview_dx)
        x1 = _green_spike_x(tl.grab(), tl)                 # 拖动中尖峰列
        QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(cx + 150, y))
        for _ in range(3):
            app.processEvents()
        shift = (x1 - x0) if (x0 >= 0 and x1 >= 0) else None
        r = {"spike_x_rest": x0, "spike_x_during_drag": x1,
             "block_shift_px": round(dx, 1), "wave_shift_px": shift,
             "tracked": (shift is not None and abs(shift - dx) <= max(6.0, abs(dx) * 0.25))}
        r["bug"] = (shift is None) or (abs(shift - dx) > max(6.0, abs(dx) * 0.25))
        res["bug2_clip_audio_not_following"] = r
    finally:
        try:
            w.shutdown()
        except Exception:                                  # noqa: BLE001
            pass


# ==========================================================================
# Bug 3 · 变速后时长不重算
# ==========================================================================
def bug3():
    w, cid, ly, it, tl = _setup_clip()
    try:
        # 让片段 = 满素材 10s：src_in=0 / src_out=10 / speed=1 / 时长=10
        w.timeline.set_selected(ly.lyr_id, it.item_id)
        w._sync_props_selection()
        pp = w.props_panel
        pp.sp_src_in.setValue(0.0)
        pp.sp_src_out.setValue(10.0)
        pp.sp_speed.setValue(1.0)
        pp.sp_len.setValue(10.0)
        for _ in range(3):
            app.processEvents()
        len_1x = float(it.end_sec) - float(it.at_sec)

        # 用户操作：变速 0.5× ⇒ 时间轴长度应≈20s（10/0.5）
        pp.sp_speed.setValue(0.5)
        for _ in range(3):
            app.processEvents()
        len_05x = float(it.end_sec) - float(it.at_sec)
        # 用户把长度拉到 20s（等价于拖右把手到 20s）
        pp.sp_len.setValue(20.0)
        for _ in range(3):
            app.processEvents()
        len_20 = float(it.end_sec) - float(it.at_sec)

        # ★ 关键：变速调回 1× ⇒ 时间轴长度应随 src 长度(10s)/speed(1) 重算 = 10s
        pp.sp_speed.setValue(1.0)
        for _ in range(3):
            app.processEvents()
        len_back = float(it.end_sec) - float(it.at_sec)
        src_span = (float(it.src_out_sec) - float(it.src_in_sec)) if it.src_out_sec else None
        expected = (src_span / float(it.speed)) if src_span else None

        r = {"len_at_1x": round(len_1x, 3), "len_at_0.5x": round(len_05x, 3),
             "len_after_pull_20s": round(len_20, 3), "len_after_speed_back_1x": round(len_back, 3),
             "src_span": src_span, "speed_after": float(it.speed),
             "expected_len_after": (round(expected, 3) if expected is not None else None),
             "playable_sec": (round(min(len_back, expected), 3) if expected else None)}
        r["bug"] = (expected is not None and abs(len_back - expected) > 0.5)
        res["bug3_speed_length_not_recomputed"] = r
    finally:
        try:
            w.shutdown()
        except Exception:                                  # noqa: BLE001
            pass


# ==========================================================================
for fn in (bug1, bug2, bug3):
    try:
        fn()
    except Exception:                                      # noqa: BLE001
        res[fn.__name__ + "_CRASH"] = traceback.format_exc()

bugs = {k: v.get("bug") for k, v in res.items() if isinstance(v, dict) and "bug" in v}
res["_summary"] = bugs
res["_verdict"] = "ALL_FIXED" if (bugs and not any(bugs.values())) else "BUGS_PRESENT"

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(res, f, ensure_ascii=False, indent=1)
print(json.dumps(res, ensure_ascii=False, indent=1))
print("DONE")
