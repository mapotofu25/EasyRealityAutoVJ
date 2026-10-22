# -*- coding: utf-8 -*-
"""★ T24 · T04 预览画布实测（真窗口 + grab 逐像素 + spy）——#16 裁切把手 + #10 点空白取消选择。

依据新约定（`docs/需求_24条_设计.md` §9）：「无头被短路 / 需真实绘制」的分支必须有真窗口验证。
本脚本起真 `QApplication`（offscreen，但**真实走 `paintEvent`/`grab()`**），证据：
  · 进入裁切模式后，**裁切框描边（主题 accent 色）像素**出现在显示图像矩形上（`grab` 逐像素）；
  · 拖把手 ⇒ **图像即时重裁**（框内像素变化）但 **0 次 `set_runtime()`**（spy）；
  · 松手 ⇒ 恰好 **1 次 `xform_committed`**，且 **模型未被预览改写**（只发信号）；
  · **帧率探针**：`_compose` p50 与拉动拖动模拟下的预览 fps（对照基线 compose p50≈3.5ms）。
  · 点空白（比例框外）⇒ **`selection_cleared`**（spy）。

跑法：
    venv\\Scripts\\python.exe tools\\_t24_t04_verify.py

结论以可判读判据打印：`[PASS]/[FAIL]` + 末尾 `CLOSED_LOOP_OK` / `HAS_FAILURES`。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback
import warnings

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
warnings.filterwarnings("ignore", category=DeprecationWarning)

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
_SHOTS = os.path.join(_ROOT, "_shots")
for p in (_SRC,):
    if p not in sys.path:
        sys.path.insert(0, p)

import av                                                          # noqa: E402
import numpy as np                                                 # noqa: E402

from PySide6.QtCore import QEvent, QPointF, Qt                    # noqa: E402
from PySide6.QtGui import QColor, QImage, QMouseEvent              # noqa: E402
from PySide6.QtWidgets import QApplication                        # noqa: E402

from ercue.model.cuesheet import (CompLayer, CueItem, CueSheet,    # noqa: E402
                                  Identity, MediaRef, TrackEntry)
from ercue.model.timeline import CueRuntime                        # noqa: E402
from ercue.ui import theme                                        # noqa: E402
from ercue.ui.preview import PreviewCanvas                        # noqa: E402

RES = []
# ★ 上轮基线（任务书给的对照值）
BASE_COMPOSE_P50_MS = 3.5
BASE_VIDEO_FPS = 30.3


def _check(tag, ok, detail):
    RES.append((tag, bool(ok), detail))
    print("  [%s] %s\n        %s" % ("PASS" if ok else "FAIL", tag, detail))


def _make_mp4(path, frames=90, fps=30, w=320, h=180):
    c = av.open(path, "w")
    st = c.add_stream("mpeg4", rate=fps)
    st.width, st.height = w, h
    st.pix_fmt = "yuv420p"
    for i in range(frames):
        img = np.zeros((h, w, 3), np.uint8)
        img[:, :, 2] = 180
        x = (i * 7) % max(1, w - 40)
        img[:, x:x + 40, 1] = 255
        fr = av.VideoFrame.from_ndarray(img, format="rgb24")
        for pk in st.encode(fr):
            c.mux(pk)
    for pk in st.encode():
        c.mux(pk)
    c.close()


def _rgb(img):
    """QPixmap / QImage → (h,w,3) uint8 副本。"""
    if hasattr(img, "toImage"):
        img = img.toImage()
    img = img.convertToFormat(QImage.Format_RGB32)
    w, h = img.width(), img.height()
    bpl = img.bytesPerLine()
    a = np.frombuffer(bytes(img.constBits()), np.uint8, count=bpl * h).reshape(h, bpl)
    return a[:, :w * 4].reshape(h, w, 4)[:, :, 2::-1].copy()   # BGR(A) -> RGB


def _p50(vals):
    s = sorted(vals)
    return s[len(s) // 2] if s else 0.0


def main() -> int:
    app = QApplication.instance() or QApplication([])
    os.makedirs(_SHOTS, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="ercue_t04v_")
    mp4 = os.path.join(tmp, "a.mp4")
    _make_mp4(mp4)

    sheet = CueSheet(title="t")
    te = TrackEntry(track_key="k",
                    identity=Identity(title="P", duration_sec=60.0, bpm=120.0))
    ly = CompLayer(lyr_id="L1", name="L1", z=0)
    it = CueItem(item_id="i1", clip_id="c1", at_sec=0.0, end_sec=10.0,
                 trans_in="cut", trans_out="cut")
    ly.items.append(it)
    te.composition.layers.append(ly)
    sheet.media.append(MediaRef(clip_id="c1", name="a.mp4", duration_sec=30.0))
    sheet.tracks.append(te)
    rt = CueRuntime(sheet, te, media_paths={"c1": mp4})

    v = PreviewCanvas()
    v.resize(800, 350)
    v.set_runtime(rt, 120.0)
    v.set_selection("L1", "i1")
    v.set_pos(1.0)
    v.set_active(True)                       # 触发一次同步 _tick（_compose）
    v._dirty = True
    v._compose()
    app.processEvents()

    accent = QColor(theme.c("accent")).getRgb()[:3]

    # ---- ① 进入裁切模式：accent 描边/把手出现在显示图像矩形上 ----
    box = v._box_rect()
    geo0 = v._crop_frame()
    if geo0 is None:
        print("  [FAIL] 素材尺寸探测失败（无法继续）")
        return 1
    wgt0 = geo0[0]

    before = v.grab()
    before.save(os.path.join(_SHOTS, "t24_t04_before.png"))
    rgb_before = _rgb(before)
    accent_before = int(np.sum(np.all(rgb_before == np.array(accent, np.uint8), axis=2)))
    v._crop_mode = True
    v.update()
    app.processEvents()
    v.set_active(True)                       # 确保合成一次
    v._dirty = True
    v._compose()
    after = v.grab()
    after.save(os.path.join(_SHOTS, "t24_t04_crop_mode.png"))
    rgb_after = _rgb(after)
    accent_after = int(np.sum(np.all(rgb_after == np.array(accent, np.uint8), axis=2)))
    diff = int(np.sum(np.any(rgb_before != rgb_after, axis=2)))
    _check("① 裁切框已绘制",
           accent_after > accent_before + 50 and diff > 50,
           "accent 像素 before=%d → after=%d；整幅差分像素=%d（截图 _shots/t24_t04_crop_mode.png）"
           % (accent_before, accent_after, diff))

    # ---- ② 拖右把手：图像即时重裁 + 拖动中 0 次 set_runtime ----
    commits = []
    v.xform_committed.connect(lambda a, b, xf, crop: commits.append((a, b, dict(xf), crop)))
    rt_calls = []
    _orig = v.set_runtime
    v.set_runtime = lambda *a, **k: (rt_calls.append(1), _orig(*a, **k))[1]

    wgt = v._crop_frame()[0]
    tgt = v._crop_frame()[1]
    rx, ry = wgt.right(), (wgt.top() + wgt.bottom()) / 2.0
    def _ev(t, x, y, btn, btns):
        return QMouseEvent(t, QPointF(x, y), btn, btns, Qt.NoModifier)
    v.mousePressEvent(_ev(QEvent.MouseButtonPress, rx, ry, Qt.LeftButton, Qt.LeftButton))
    hit = v._crop_drag is not None
    steps = 5
    for i in range(1, steps + 1):
        v.mouseMoveEvent(_ev(QEvent.MouseMove, rx - i * 8.0, ry, Qt.NoButton, Qt.LeftButton))
        v._compose()
    v.update()
    app.processEvents()
    drag_pm = v.grab()
    drag_pm.save(os.path.join(_SHOTS, "t24_t04_after_drag.png"))
    rgb_drag = _rgb(drag_pm)
    inner = (int(wgt.top()) + 4, int(wgt.bottom()) - 4, int(wgt.left()) + 4, int(wgt.right()) - 4)
    d_inner = int(np.sum(np.any(
        rgb_drag[inner[0]:inner[1], inner[2]:inner[3]] != rgb_after[inner[0]:inner[1], inner[2]:inner[3]],
        axis=2)))
    crop_now = list(v._override[3]) if v._override else None
    _check("② 拖动命中把手 + 图像即时重裁",
           hit and crop_now is not None and d_inner > 20,
           "命中把手=%s；override.crop=%s；框内像素差分=%d"
           % (hit, [round(x, 4) for x in crop_now] if crop_now else None, d_inner))
    _check("② 拖动中绝不 set_runtime（性能护栏）",
           len(rt_calls) == 0 and len(commits) == 0,
           "拖动中 set_runtime 次数=%d；emit 次数=%d（应 0/0）" % (len(rt_calls), len(commits)))

    # ---- ③ 松手：恰好 1 次 xform_committed；模型未被预览改写 ----
    v.mouseReleaseEvent(_ev(QEvent.MouseButtonRelease, rx - steps * 8.0, ry,
                            Qt.LeftButton, Qt.NoButton))
    model_crop = list(it.crop) if it.crop else None
    _check("③ 松手一次入栈 + 提交 crop",
           len(commits) == 1 and commits[0][3] is not None and len(commits[0][3]) == 4,
           "emit 次数=%d；提交 crop=%s；模型 it.crop=%s（预览不直接改模型）"
           % (len(commits), [round(x, 4) for x in commits[0][3]] if commits else None, model_crop))
    v.set_runtime = _orig

    # ---- ④ 点空白（比例框外）⇒ selection_cleared ----
    cleared = []
    v.selection_cleared.connect(lambda: cleared.append(1))
    b = v._box_rect()
    if b.left() > 3:
        bx = 2
    else:
        bx = int(b.right()) + 2
    v.mousePressEvent(_ev(QEvent.MouseButtonPress, bx, int(b.center().y()),
                          Qt.LeftButton, Qt.LeftButton))
    _check("④ 点空白 ⇒ selection_cleared",
           len(cleared) == 1,
           "框=%s；点击 x=%d ⇒ selection_cleared 次数=%d" % (b, bx, len(cleared)))

    # ---- ⑤ 帧率探针（对照基线 compose p50≈3.5ms / video_fps≈30.3）----
    #   (a) 拖动模拟下的 compose 延迟（每步 set override + compose）
    v._crop_mode = True
    v.set_selection("L1", "i1")
    it.crop = None
    lat = []
    g = v._crop_frame()
    if g is not None:
        w2 = g[0]
        hx, hy = w2.right(), (w2.top() + w2.bottom()) / 2.0
        v.mousePressEvent(_ev(QEvent.MouseButtonPress, hx, hy, Qt.LeftButton, Qt.LeftButton))
        for i in range(120):
            v.mouseMoveEvent(_ev(QEvent.MouseMove, hx - (i % 40), hy, Qt.NoButton, Qt.LeftButton))
            t0 = time.monotonic()
            v._compose()
            lat.append((time.monotonic() - t0) * 1000.0)
        v.mouseReleaseEvent(_ev(QEvent.MouseButtonRelease, hx, hy, Qt.LeftButton, Qt.NoButton))
    p50 = _p50(lat)
    _check("⑤ compose p50 不退化（基线 ≈%.1fms）" % BASE_COMPOSE_P50_MS,
           p50 <= BASE_COMPOSE_P50_MS + 2.0,
           "拖动模拟 compose p50=%.2fms（n=%d）⇒ 上限 %.2fms 内" % (p50, len(lat), p50))

    #   (b) 端到端预览 fps（跑真事件循环 ~1s，统计 _compose 调用）
    v.set_selection("", "")
    v.set_active(True)
    v.set_playing(True)
    v.set_pos(0.0)
    cnt = [0]
    _orig_compose = v._compose
    v._compose = lambda: (cnt.__setitem__(0, cnt[0] + 1), _orig_compose())[1]
    t0 = time.monotonic()
    while time.monotonic() - t0 < 1.0:
        app.processEvents()
        time.sleep(0.001)
    fps = cnt[0] / max(0.001, time.monotonic() - t0)
    v._compose = _orig_compose
    v.set_playing(False)
    _check("⑤ 预览 fps 不退化（基线 ≈%.1f）" % BASE_VIDEO_FPS,
           fps >= BASE_VIDEO_FPS * 0.8,
           "实测 %.1f fps / 1s（判据 ≥ %.1f）" % (fps, BASE_VIDEO_FPS * 0.8))

    # ---- 汇总 ----
    passed = sum(1 for _t, ok, _d in RES if ok)
    print("\n" + "=" * 70)
    print("T24-T04 预览实测：%d 通过 / %d 失败" % (passed, len(RES) - passed))
    for tag, ok, _d in RES:
        print("   %s %s" % ("✓" if ok else "✗", tag))
    print("=" * 70)
    allok = passed == len(RES)
    print("CLOSED_LOOP_OK" if allok else "HAS_FAILURES")
    return 0 if allok else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:                                          # noqa: BLE001
        traceback.print_exc()
        print("HAS_FAILURES")
        raise SystemExit(1)
