# -*- coding: utf-8 -*-
"""★ T04 **独立验证**（software-engineer-5-2-2 · fresh eyes）。

⚠ 与 owner 的 `tools/_t24_t04_verify.py` **互不复用**：本脚本自己造 mp4、自己**独立推算**
   几何期望值（不动用被测的 `_crop_frame()` 作为"期望"来源），只把被测实现当黑盒。

覆盖（对照 设计 §#16 / §D16 / §#10 / §C10）：
  A. 进入裁切 ⇒ 显示图像矩形四角/四边确有**把手像素**（按**独立算出的** box 位置采样）。
  B. 拖**角把手** br ⇒ 图像重裁 + crop 值 = **我独立预测**的值（16:9 源 + 16:9 码表 ⇒ cover==contain==box）。
  C. 松手恰好 1 次 emit；**预览不改模型**；随后经 **真 MainWindow** 落模型 + `undo()` 一步回退。
  D. 拖动期间 `set_runtime` 调用 = **0**；compose p50 / 预览 fps 对照基线(3.5ms / 30.3)不退化。
  E. #10 点空白（框外 / contain 黑边）⇒ `selection_cleared`；点图像 ⇒ 不取消。
  F. 边界：把手拖出画面 / 拖到最小尺寸 ⇒ **钳制**（0..1 且边长≥CROP_MIN）。

跑法：  venv\\Scripts\\python.exe tools\\_t24_t04_verify_indep.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback
import warnings

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["ERCUE_NO_AUTOSAVE"] = "1"
warnings.filterwarnings("ignore", category=DeprecationWarning)

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import av                                                          # noqa: E402
import numpy as np                                                 # noqa: E402

from PySide6.QtCore import QEvent, QPointF, Qt                    # noqa: E402
from PySide6.QtGui import QColor, QImage, QMouseEvent              # noqa: E402
from PySide6.QtWidgets import QApplication                        # noqa: E402

from ercue.model.cuesheet import (CompLayer, CueItem, CueSheet,    # noqa: E402
                                  Identity, MediaRef, TrackEntry)
from ercue.model.timeline import CueRuntime                        # noqa: E402
from ercue.ui import theme                                         # noqa: E402
from ercue.ui.preview import CROP_MIN, PreviewCanvas, crop_apply   # noqa: E402

RES = []
SV = 320        # 源宽
SH = 180        # 源高（16:9）
BASE_COMPOSE_P50_MS = 3.5
BASE_FPS = 30.3


def _chk(tag, ok, detail):
    RES.append((tag, bool(ok), detail))
    print("  [%s] %s\n        %s" % ("PASS" if ok else "FAIL", tag, detail))


def _make_mp4(path, frames=90, fps=30, w=SV, h=SH):
    c = av.open(path, "w")
    st = c.add_stream("mpeg4", rate=fps)
    st.width, st.height = w, h
    st.pix_fmt = "yuv420p"
    for i in range(frames):
        img = np.zeros((h, w, 3), np.uint8)
        img[:, :, 2] = 170
        x = (i * 9) % max(1, w - 40)
        img[:, x:x + 40, 1] = 250
        fr = av.VideoFrame.from_ndarray(img, format="rgb24")
        for pk in st.encode(fr):
            c.mux(pk)
    for pk in st.encode():
        c.mux(pk)
    c.close()


def _rgb(img):
    if hasattr(img, "toImage"):
        img = img.toImage()
    img = img.convertToFormat(QImage.Format_RGB32)
    w, h = img.width(), img.height()
    bpl = img.bytesPerLine()
    a = np.frombuffer(bytes(img.constBits()), np.uint8, count=bpl * h).reshape(h, bpl)
    return a[:, :w * 4].reshape(h, w, 4)[:, :, 2::-1].copy()


def _accent_near(rgb, x, y, rad=6):
    """点 (x,y) 半径 rad 内与主题 accent 色**相等**的像素数（把手填充色）。"""
    acc = np.array(QColor(theme.c("accent")).getRgb()[:3], np.uint8)
    h, w = rgb.shape[:2]
    x0, x1 = max(0, int(x) - rad), min(w, int(x) + rad + 1)
    y0, y1 = max(0, int(y) - rad), min(h, int(y) + rad + 1)
    if x0 >= x1 or y0 >= y1:
        return 0
    sub = rgb[y0:y1, x0:x1]
    return int(np.sum(np.all(sub == acc, axis=2)))


def _ev(t, x, y, btn, btns):
    return QMouseEvent(t, QPointF(x, y), btn, btns, Qt.NoModifier)


def _rt(mp4):
    sheet = CueSheet(title="t", res_w=1920, res_h=1080)     # 16:9 ⇒ 源 16:9 时 cover==contain==box
    te = TrackEntry(track_key="k",
                    identity=Identity(title="P", duration_sec=60.0, bpm=120.0))
    ly = CompLayer(lyr_id="L1", name="L1", z=0)
    it = CueItem(item_id="i1", clip_id="c1", at_sec=0.0, end_sec=10.0,
                 trans_in="cut", trans_out="cut")
    ly.items.append(it)
    te.composition.layers.append(ly)
    sheet.media.append(MediaRef(clip_id="c1", name="a.mp4", duration_sec=30.0))
    sheet.tracks.append(te)
    return CueRuntime(sheet, te, media_paths={"c1": mp4}), ly, it


def _canvas(mp4, w=800, h=450):
    rt, ly, it = _rt(mp4)
    v = PreviewCanvas()
    v.resize(w, h)
    v.set_runtime(rt, 120.0)
    v.set_selection("L1", "i1")
    v.set_pos(1.0)
    v.set_active(True)
    v._dirty = True
    v._compose()
    return v, ly, it


# ==========================================================================
def part_A_handles(mp4):
    print("\n--- A. 进入裁切 ⇒ 8 把手出现（独立采样 box 位置）---")
    v, _ly, _it = _canvas(mp4)
    app = QApplication.instance()
    box = v._box_rect()
    # 独立期望：源 16:9 + 码表 16:9 + 默认 xform ⇒ 显示图像矩形 == 比例框 box
    bx, by, bw, bh = box.left(), box.top(), box.width(), box.height()
    spots = {
        "tl": (bx, by), "t": (bx + bw / 2, by), "tr": (bx + bw, by),
        "l": (bx, by + bh / 2), "r": (bx + bw, by + bh / 2),
        "bl": (bx, by + bh), "b": (bx + bw / 2, by + bh), "br": (bx + bw, by + bh),
    }
    v._crop_mode = False
    v.update(); app.processEvents()
    pre = _rgb(v.grab())
    pre_hits = {k: _accent_near(pre, x, y) for k, (x, y) in spots.items()}
    v._crop_mode = True
    v.update(); app.processEvents()
    post = _rgb(v.grab())
    hits = {k: _accent_near(post, x, y) for k, (x, y) in spots.items()}
    found = sum(1 for k in spots if hits[k] > 0 and pre_hits[k] == 0)
    _chk("A. 8 把手像素（进入前无、进入后有）", found == 8,
         "命中把手 %d/8；逐点 accent(before→after)=%s"
         % (found, {k: (pre_hits[k], hits[k]) for k in spots}))
    return v


def part_B_corner_drag(mp4):
    print("\n--- B. 拖角把手 br ⇒ 图像重裁 + crop=独立预测值 ---")
    v, _ly, it = _canvas(mp4)
    app = QApplication.instance()
    v._crop_mode = True
    box = v._box_rect()
    before = _rgb(v.grab())
    commits = []
    v.xform_committed.connect(lambda a, b, xf, c: commits.append((a, b, dict(xf), list(c) if c else None)))
    rtcalls = []
    _orig = v.set_runtime
    v.set_runtime = lambda *a, **k: (rtcalls.append(1), _orig(*a, **k))[1]
    # br 角：显示图像矩形右下角 == box 右下角（16:9/16:9）
    bx, by, bw, bh = box.left(), box.top(), box.width(), box.height()
    brx, bry = bx + bw, by + bh
    dx, dy = -0.15 * bw, -0.20 * bh          # 独立预测：crop = [0,0,1-0.15,1-0.20]
    exp_w, exp_h = 1.0 + dx / bw, 1.0 + dy / bh
    v.mousePressEvent(_ev(QEvent.MouseButtonPress, brx, bry, Qt.LeftButton, Qt.LeftButton))
    hit = v._crop_drag is not None and v._crop_drag["handle"] == "br"
    for i in range(1, 6):
        v.mouseMoveEvent(_ev(QEvent.MouseMove, brx + dx * i / 5.0, bry + dy * i / 5.0,
                             Qt.NoButton, Qt.LeftButton))
        v._compose()
    v.update(); app.processEvents()
    drag = _rgb(v.grab())
    inner = (int(by) + 3, int(by + bh) - 3, int(bx) + 3, int(bx + bw) - 3)
    dimg = int(np.sum(np.any(drag[inner[0]:inner[1], inner[2]:inner[3]]
                             != before[inner[0]:inner[1], inner[2]:inner[3]], axis=2)))
    ov = list(v._override[3]) if v._override else None
    _chk("B1. 命中 br 角把手 + 图像即时重裁", hit and ov is not None and dimg > 50,
         "命中=%s；override.crop=%s；框内像素差分=%d"
         % (hit, [round(z, 4) for z in ov] if ov else None, dimg))
    _chk("B2. crop 值 == 独立预测", ov is not None
         and abs(ov[2] - exp_w) < 2e-3 and abs(ov[3] - exp_h) < 2e-3,
         "实测=[%.4f,%.4f,%.4f,%.4f]  预测 w=%.4f h=%.4f" % (ov[0], ov[1], ov[2], ov[3], exp_w, exp_h))
    _chk("B3. 拖动中 set_runtime=0 / emit=0（性能铁律）",
         len(rtcalls) == 0 and len(commits) == 0,
         "set_runtime=%d；emit=%d" % (len(rtcalls), len(commits)))
    # 松手
    v.mouseReleaseEvent(_ev(QEvent.MouseButtonRelease, brx + dx, bry + dy, Qt.LeftButton, Qt.NoButton))
    v.set_runtime = _orig
    _chk("B4. 松手恰好 1 次 emit + 提交 crop；预览不改模型",
         len(commits) == 1 and commits[0][3] is not None and it.crop is None,
         "emit=%d；提交 crop=%s；模型 it.crop=%s"
         % (len(commits), [round(z, 4) for z in commits[0][3]] if commits else None, it.crop))
    return v


def part_C_integration(mp4):
    print("\n--- C. 真 MainWindow：松手落模型 + undo() 一步回退 ---")
    try:
        from ercue.ui.window import MainWindow
    except Exception as e:                                     # noqa: BLE001
        _chk("C. MainWindow 可用", False, "import 失败: %r" % e)
        return
    try:
        w = MainWindow(restore_workspace=False, autosave=False)
    except Exception as e:                                     # noqa: BLE001
        _chk("C. MainWindow 构造", False, "构造失败: %r" % e)
        return
    w._interactive = False
    w.setAttribute(Qt.WA_DontShowOnScreen, True)
    w.resize(1256, 850)
    w.show()
    for _ in range(6):
        app = QApplication.instance()
        app.processEvents()
    # 建码表(16:9) + 素材 + 片段
    w.new_sheet("T04indep", 1920, 1080)
    ids = w.add_media_paths([mp4])
    w.add_track("t")
    ly = w._layers()[0]
    w.add_cue(ly.lyr_id, ids[0], 0.0)
    for _ in range(6):
        QApplication.instance().processEvents()
    # 选中片段（喂给预览）
    it = ly.items[0]
    w.timeline.set_selected(ly.lyr_id, it.item_id)
    w._sync_props_selection()
    pv = w.preview
    pv._crop_mode = True
    pv.set_pos(1.0)
    pv.set_active(True)
    pv._dirty = True
    pv._compose()
    QApplication.instance().processEvents()
    box = pv._box_rect()
    bx, by, bw, bh = box.left(), box.top(), box.width(), box.height()
    brx, bry = bx + bw, by + bh
    crop_before = it.crop
    pv.mousePressEvent(_ev(QEvent.MouseButtonPress, brx, bry, Qt.LeftButton, Qt.LeftButton))
    pv.mouseMoveEvent(_ev(QEvent.MouseMove, brx - 0.2 * bw, bry - 0.2 * bh, Qt.NoButton, Qt.LeftButton))
    pv.mouseReleaseEvent(_ev(QEvent.MouseButtonRelease, brx - 0.2 * bw, bry - 0.2 * bh,
                             Qt.LeftButton, Qt.NoButton))
    for _ in range(4):
        QApplication.instance().processEvents()
    ly2, it2 = w._find_item(ly.lyr_id, it.item_id)
    landed = (it2.crop is not None and len(it2.crop) == 4)
    _chk("C1. 松手后 crop 落进片段（真窗集成）", landed,
         "松手前 crop=%s → 松手后 it.crop=%s"
         % (crop_before, [round(z, 4) for z in it2.crop] if it2.crop else None))
    # undo 一步回退
    w.undo()
    for _ in range(4):
        QApplication.instance().processEvents()
    ly3, it3 = w._find_item(ly.lyr_id, it.item_id)
    _chk("C2. undo() 一步回退 crop", it3.crop is None,
         "undo 后 it.crop=%s（应回 None）" % (it3.crop,))
    try:
        w.shutdown()
    except Exception:                                          # noqa: BLE001
        pass


def part_D_perf(mp4):
    print("\n--- D. 帧率/延迟不退化（基线 compose p50≈%.1fms / fps≈%.1f）---" % (BASE_COMPOSE_P50_MS, BASE_FPS))
    v, _ly, it = _canvas(mp4)
    app = QApplication.instance()
    v._crop_mode = True
    box = v._box_rect()
    brx, bry = box.left() + box.width(), box.top() + box.height()
    v.mousePressEvent(_ev(QEvent.MouseButtonPress, brx, bry, Qt.LeftButton, Qt.LeftButton))
    lat = []
    for i in range(120):
        v.mouseMoveEvent(_ev(QEvent.MouseMove, brx - (i % 50), bry, Qt.NoButton, Qt.LeftButton))
        t0 = time.monotonic()
        v._compose()
        lat.append((time.monotonic() - t0) * 1000.0)
    v.mouseReleaseEvent(_ev(QEvent.MouseButtonRelease, brx, bry, Qt.LeftButton, Qt.NoButton))
    s = sorted(lat)
    p50 = s[len(s) // 2] if s else 0.0
    _chk("D1. 拖动期 compose p50 不退化", p50 <= BASE_COMPOSE_P50_MS + 2.0,
         "p50=%.2fms（n=%d，上限 %.2f）" % (p50, len(lat), BASE_COMPOSE_P50_MS + 2.0))
    # 端到端 fps
    v.set_selection("", "")
    v.set_active(True)
    v.set_playing(True)
    v.set_pos(0.0)
    cnt = [0]
    _oc = v._compose
    v._compose = lambda: (cnt.__setitem__(0, cnt[0] + 1), _oc())[1]
    t0 = time.monotonic()
    while time.monotonic() - t0 < 1.0:
        app.processEvents()
        time.sleep(0.001)
    fps = cnt[0] / max(0.001, time.monotonic() - t0)
    v._compose = _oc
    _chk("D2. 预览 fps 不退化", fps >= BASE_FPS * 0.8,
         "实测 %.1f fps（判据 ≥ %.1f）" % (fps, BASE_FPS * 0.8))


def part_E_blank(mp4):
    print("\n--- E. #10 点空白 ⇒ selection_cleared ---")
    v, _ly, it = _canvas(mp4)
    cleared = []
    v.selection_cleared.connect(lambda: cleared.append(1))
    box = v._box_rect()
    # (1) 框外（右侧黑边）
    v.mousePressEvent(_ev(QEvent.MouseButtonPress, int(box.right()) + 3,
                          int(box.center().y()), Qt.LeftButton, Qt.LeftButton))
    ok1 = len(cleared) == 1 and v._drag is None
    cleared.clear()
    # (2) contain 黑边（缩小 scale ⇒ 图像小于框 ⇒ 框内非图像）
    it.xform = {"scale": 0.3, "x": 0.0, "y": 0.0}
    v.mousePressEvent(_ev(QEvent.MouseButtonPress, int(box.left()) + 3,
                          int(box.top()) + 3, Qt.LeftButton, Qt.LeftButton))
    ok2 = len(cleared) == 1
    cleared.clear()
    # (3) 点图像（cover：中心）⇒ 不取消、进入位移拖动
    it.xform = {"scale": 1.0, "x": 0.0, "y": 0.0}
    v.mousePressEvent(_ev(QEvent.MouseButtonPress, int(box.center().x()),
                          int(box.center().y()), Qt.LeftButton, Qt.LeftButton))
    ok3 = len(cleared) == 0 and v._drag is not None
    v.mouseReleaseEvent(_ev(QEvent.MouseButtonRelease, int(box.center().x()),
                            int(box.center().y()), Qt.LeftButton, Qt.NoButton))
    _chk("E1. 框外点空 ⇒ cleared", ok1, "次数=%d（含无位移拖动=%s）" % (1 if ok1 else 0, v._drag is None))
    _chk("E2. contain 黑边点空 ⇒ cleared", ok2, "次数=%d" % (1 if ok2 else 0))
    _chk("E3. 点图像 ⇒ 不取消 + 位移拖动", ok3, "cleared=%d；_drag=%s" % (0 if ok3 else 1, v._drag is not None))


def part_F_bounds():
    print("\n--- F. 边界：拖出画面 / 最小尺寸 ⇒ 钳制 ---")
    # 独立算：把左把手一路右拖（超出右边界）⇒ 顶到 right-CROP_MIN
    c = crop_apply([0.0, 0.0, 1.0, 1.0], "l", 9.0, 0.0)
    okA = abs(c[2] - CROP_MIN) < 1e-9 and abs(c[0] + c[2] - 1.0) < 1e-9
    # 上把手向上狂拖（负）⇒ 上边扩到 0（y=0，h=0.7）
    c2 = crop_apply([0.2, 0.2, 0.5, 0.5], "t", 0.0, -9.0)
    okB = abs(c2[1] - 0.0) < 1e-9 and abs(c2[3] - 0.7) < 1e-9
    # 上把手向下狂拖（正）⇒ 上边顶到 b-mn（h=CROP_MIN）
    c2b = crop_apply([0.2, 0.2, 0.5, 0.5], "t", 0.0, 9.0)
    okB = okB and abs(c2b[3] - CROP_MIN) < 1e-9
    # 下把手向下狂拖（正）⇒ 下边扩到 1（b=1，h=0.8）
    c2c = crop_apply([0.2, 0.2, 0.5, 0.5], "b", 0.0, 9.0)
    okB = okB and abs(c2c[1] + c2c[3] - 1.0) < 1e-9 and abs(c2c[3] - 0.8) < 1e-9
    # br 角向外拖出 ⇒ 顶到 1.0
    c3 = crop_apply([0.1, 0.1, 0.5, 0.5], "br", 9.0, 9.0)
    okC = abs(c3[0] + c3[2] - 1.0) < 1e-9 and abs(c3[1] + c3[3] - 1.0) < 1e-9
    # 所有把手 × 极端增量的通用不变量
    allok = True
    for hnd in ("l", "r", "t", "b", "tl", "tr", "bl", "br"):
        for d in (12.0, -12.0):
            cc = crop_apply([0.25, 0.35, 0.4, 0.3], hnd, d, -d)
            allok = allok and (0.0 <= cc[0] <= 1.0 and 0.0 <= cc[1] <= 1.0
                               and cc[2] >= CROP_MIN - 1e-9 and cc[3] >= CROP_MIN - 1e-9
                               and cc[0] + cc[2] <= 1.0 + 1e-9 and cc[1] + cc[3] <= 1.0 + 1e-9)
    _chk("F. 钳制：最小边长 / 0..1 边界", okA and okB and okC and allok,
         "左顶到 min=%s；上/下边界钳制=%s；br 顶到 1=%s；8把手×±12 不变量=%s"
         % (okA, okB, okC, allok))


def main() -> int:
    app = QApplication.instance() or QApplication([])
    tmp = tempfile.mkdtemp(prefix="ercue_t04indep_")
    mp4 = os.path.join(tmp, "a.mp4")
    _make_mp4(mp4)
    tests = [
        ("A 把手出现", lambda: part_A_handles(mp4)),
        ("B 角把手拖动", lambda: part_B_corner_drag(mp4)),
        ("C 真窗集成+undo", lambda: part_C_integration(mp4)),
        ("D 性能", lambda: part_D_perf(mp4)),
        ("E 点空白", lambda: part_E_blank(mp4)),
        ("F 边界", part_F_bounds),
    ]
    for name, fn in tests:
        try:
            fn()
        except Exception as e:                                 # noqa: BLE001
            _chk("%s（异常）" % name, False, "%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()))
    passed = sum(1 for _t, ok, _d in RES if ok)
    print("\n" + "=" * 72)
    print("T04 独立验证：%d 通过 / %d 失败" % (passed, len(RES) - passed))
    for tag, ok, _d in RES:
        print("   %s %s" % ("✓" if ok else "✗", tag))
    print("=" * 72)
    print("INDEP_OK" if passed == len(RES) else "INDEP_FAIL")
    return 0 if passed == len(RES) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:                                          # noqa: BLE001
        traceback.print_exc()
        print("INDEP_FAIL")
        raise SystemExit(1)
