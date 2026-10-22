# -*- coding: utf-8 -*-
"""S5 自检：**码表真的出画**（真窗口截图 + 像素级 α²/crop/z 序 + fps）。

为什么这步必须"看图"：前几步验的是"没坏"，这步验的是"**有东西出来**"。
所以本脚本同时给两类证据：
  · **真窗口截图**（`QWidget.grab()`，落盘 PNG）：出画一张 / `clear_cue_sheet()` 后一张；
  · **像素级数字**：α² 三个采样点、crop 四象限与边界位置、z 序遮挡关系、compose 耗时。

运行：`python tools/cue_draw_test.py`（exit 0 = 全过）；截图落在 `tools/_cueshots/`。
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
SHOT_DIR = os.path.join(HERE, "_cueshots")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from PySide6.QtCore import Qt                                    # noqa: E402
from PySide6.QtGui import QImage, QPainter, QColor, QPixmap       # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel                # noqa: E402

ENGINE_SRC = os.path.abspath(os.environ.get("CUE_ENGINE_SRC") or SRC)
if ENGINE_SRC != SRC and ENGINE_SRC not in sys.path:
    sys.path.insert(0, ENGINE_SRC)          # 让 import engine 命中"另一份"
import engine                                                     # noqa: E402
import cuesheet                                                   # noqa: E402
import cuesheet_runtime as crt                                    # noqa: E402

W, H = 960, 540          # 画布用小一点，像素断言好读、截图也清楚
BPM = 120.0
BL = 60.0 / BPM          # 0.5s/拍


# ==========================================================================
# 假播放器（按路径返回固定图）
# ==========================================================================
class FakePlayer:
    def __init__(self, img):
        self._img = img
        self.active = False
        self.warmed = False
        self.speed = 1.0
        self.closed = False

    def current(self):
        return self._img

    def set_active(self, a):
        self.active = bool(a)

    def warm_up(self):
        self.warmed = True

    def set_max_size(self, w, h):
        pass

    def set_speed(self, s):
        self.speed = float(s)

    def position(self):
        return 0.0

    def seek(self, sec):
        return True

    def close(self):
        self.closed = True


def solid(w, h, rgba):
    img = QImage(w, h, QImage.Format_ARGB32)
    img.fill(QColor(*rgba))
    return img


def quad(w, h):
    """四象限纯色（用于 crop 几何反推）：TL 红 / TR 绿 / BL 蓝 / BR 黄。"""
    img = QImage(w, h, QImage.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 255))
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, False)
    p.fillRect(0, 0, w // 2, h // 2, QColor(220, 20, 20, 255))
    p.fillRect(w // 2, 0, w - w // 2, h // 2, QColor(20, 200, 20, 255))
    p.fillRect(0, h // 2, w // 2, h - h // 2, QColor(30, 30, 220, 255))
    p.fillRect(w // 2, h // 2, w - w // 2, h - h // 2, QColor(230, 220, 30, 255))
    p.end()
    return img


def blue_with_hole(w, h):
    """中间透明、四周蓝（用来测 z 序：顶层只在边缘不透明）。"""
    img = QImage(w, h, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.fillRect(0, 0, w, h, QColor(20, 40, 220, 255))
    # ★ 挖空要用 Source 模式：SourceOver 下画 alpha=0 等于"什么都没画"（第一版踩了这个坑）
    p.setCompositionMode(QPainter.CompositionMode_Source)
    p.fillRect(w // 4, h // 4, w // 2, h // 2, QColor(0, 0, 0, 0))
    p.setCompositionMode(QPainter.CompositionMode_SourceOver)
    p.end()
    return img


# ==========================================================================
# 引擎桩（只装 `_compose` 用到的东西）
# ==========================================================================
def new_engine(canvas, layers, images):
    eng = engine.AutoVJEngine.__new__(engine.AutoVJEngine)
    eng.canvas = canvas
    eng.layers = layers
    eng.mode = "normal"
    eng.blackout = False
    eng.freeze = False
    eng.cfg = {"auto": {"intensity": 1, "transition": None},
               "mode": {"transition": ""},
               "color": {}, "postfx": {}}
    eng._silent_now_debounced = False
    eng._health_log = None
    eng._cue_tick_fail = 0
    eng._cue_err_at = 0.0
    eng._cue_pos_beats = None
    eng._cue_pos_src = ""
    eng._cue_sheet = None
    eng._cue_rt = None
    eng._cue_media = {}
    eng._cue_jumped = False
    eng._cue_closed_last = 0
    eng._images = images
    return eng


def fake_make_player(item, cue=False):
    p = FakePlayer(None)
    p._img = None
    return p


# ==========================================================================
# 场景搭建
# ==========================================================================
def build_scene(canvas_w=W, canvas_h=H, alpha_case=False, crop_case=False,
                z_case=False):
    """返回 (engine, cue_layer)。图与层按用例组合。"""
    images = {}          # 磁盘路径 -> QImage（给假播放器用）
    media_paths = {}     # clip_id  -> 磁盘路径（给 CueRuntime 用）
    sheet = cuesheet.new_sheet("S5 自检", res_w=canvas_w, res_h=canvas_h)
    te = cuesheet.new_track("T", "A", 300.0, BPM)
    L = cuesheet.CompLayer(lyr_id="L1", name="码表子层", z=0, blend="normal", opacity=1.0)

    def add_clip(cid, path, img):
        images[path] = img
        media_paths[cid] = path
        sheet.media.append(cuesheet.MediaRef(clip_id=cid, name=os.path.basename(path)))
        return cid

    if alpha_case:
        cid = add_clip("c1", "P:/x/white.png", solid(canvas_w, canvas_h, (255, 255, 255, 255)))
        L.items.append(cuesheet.CueItem(item_id="i1", clip_id=cid, at_beats=8.0,
                                        trans_in="fade", trans_in_beats=2.0,
                                        trans_out="cut", trans_out_beats=0.0).normalized())
    elif crop_case:
        cid = add_clip("c1", "P:/x/quad.png", quad(1000, 1000))
        L.items.append(cuesheet.CueItem(item_id="i1", clip_id=cid, at_beats=0.0,
                                        crop=[0.1, 0.1, 0.6, 0.6],
                                        trans_in="cut", trans_out="cut").normalized())
    else:
        cid = add_clip("c1", "P:/x/green.png", solid(canvas_w, canvas_h, (30, 200, 90, 255)))
        L.items.append(cuesheet.CueItem(item_id="i1", clip_id=cid, at_beats=0.0,
                                        trans_in="cut", trans_out="cut").normalized())
    te.layers.append(L)
    sheet.tracks.append(te)

    canvas = QImage(canvas_w, canvas_h, QImage.Format_ARGB32)
    canvas.fill(Qt.black)

    cue_layer = engine.Layer("码表层")
    cue_layer.is_cuesheet = True
    cue_layer.cuesheet_id = "S5"
    normal_bottom = engine.Layer("底(红)")
    normal_bottom.player = FakePlayer(solid(canvas_w, canvas_h, (220, 30, 30, 255)))
    normal_top = engine.Layer("顶(蓝挖空)")
    normal_top.player = FakePlayer(blue_with_hole(canvas_w, canvas_h))

    if alpha_case or crop_case:
        layers = [cue_layer]                      # 只有码表层，便于纯像素测量
    elif z_case:
        layers = [normal_top, cue_layer, normal_bottom]   # index0=最上
    else:
        layers = [normal_top, cue_layer, normal_bottom]

    eng = new_engine(canvas, layers, images)
    eng._cue_sheet = sheet
    eng._cue_track = te
    eng._cue_rt = crt.CueRuntime(sheet, te, media_paths=media_paths)
    return eng, cue_layer, images


# ==========================================================================
# 截图
# ==========================================================================
def shot(app, eng, path, title=""):
    os.makedirs(SHOT_DIR, exist_ok=True)
    pm = QPixmap.fromImage(eng.canvas).scaled(480, 270, Qt.KeepAspectRatio,
                                              Qt.SmoothTransformation)
    lbl = QLabel()
    lbl.setWindowTitle(title or "cue shot")
    lbl.setPixmap(pm)
    lbl.setFixedSize(pm.size())
    lbl.show()
    app.processEvents()
    time.sleep(0.05)
    app.processEvents()
    ok = lbl.grab().save(path, "PNG")
    lbl.close()
    app.processEvents()
    return ok, path


# ==========================================================================
# 主流程
# ==========================================================================
def dump_normal(path):
    """把"只用普通图层"的 `_compose` 结果存 PNG（用来跨版本逐字节比对）。"""
    canvas = QImage(W, H, QImage.Format_ARGB32)
    layers = []
    bottom = engine.Layer("底(红)")
    bottom.player = FakePlayer(solid(W, H, (210, 20, 20, 255)))
    mid = engine.Layer("中(绿,减色)")
    mid.blend = "subtract"
    mid.player = FakePlayer(solid(W, H, (0, 180, 60, 255)))
    top = engine.Layer("顶(蓝挖空)")
    top.player = FakePlayer(blue_with_hole(W, H))
    trans = engine.Layer("过渡层(fade)")
    trans.player = FakePlayer(solid(W, H, (200, 200, 20, 255)))
    trans.prev_player = FakePlayer(solid(W, H, (20, 20, 200, 255)))
    trans.trans_active = True
    trans.trans_start = time.perf_counter() - 0.10        # 过渡进行到一半左右
    trans.trans_ms = 400
    hidden = engine.Layer("静音隐藏")
    hidden.hide_silent = True
    hidden.player = FakePlayer(solid(W, H, (255, 255, 255, 255)))
    layers = [top, trans, mid, hidden, bottom]            # index0=最上
    eng = new_engine(canvas, layers, {})
    eng._compose({"bass": 0.3})
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    eng.canvas.save(path, "PNG")
    return path


def main():
    fails = []

    def check(name, cond, detail=""):
        print("  [%s] %s%s" % ("ok  " if cond else "FAIL", name,
                               "" if cond else "   ← " + str(detail)))
        if not cond:
            fails.append(name)

    if len(sys.argv) > 2 and sys.argv[1] == "--dump-normal":
        print("dump:", dump_normal(sys.argv[2]))
        return 0

    app = QApplication.instance() or QApplication(sys.argv)
    real_make = engine.make_player

    def _make(item, cue=False):
        p = FakePlayer(None)
        p._img = ENG_IMAGES.get(getattr(item, "path", ""), None)
        p.cue_flag = bool(cue)
        return p

    ENG_IMAGES = {}

    def _bind(eng, images):
        nonlocal ENG_IMAGES
        ENG_IMAGES = images

    # ---------------- ① 出画 + 卸载后消失（真窗口截图）----------------
    print("① 真窗口截图：出画 / clear 之后")
    eng, cue_lay, images = build_scene()
    _bind(eng, images)
    engine.make_player = _make
    try:
        eng._cuesheet_tick()
        eng.set_cue_position(5.0)
        eng._cuesheet_tick()
        eng._compose({"bass": 0.2})
        px_center = eng.canvas.pixelColor(480, 270)
        px_edge = eng.canvas.pixelColor(10, 10)
        ok1, p1 = shot(app, eng, os.path.join(SHOT_DIR, "s5_out.png"), "S5 出画")
        check("出画：中心像素是码表的绿（不是底层的红）",
              px_center.green() > 120 and px_center.red() < 120, px_center.getRgb())
        check("z 序：边缘被顶层蓝盖住", px_edge.blue() > 120 and px_edge.red() < 120,
              px_edge.getRgb())
        check("截图已落盘 s5_out.png", ok1, p1)

        eng.clear_cue_sheet()
        eng._compose({"bass": 0.2})
        px_center2 = eng.canvas.pixelColor(480, 270)
        ok2, p2 = shot(app, eng, os.path.join(SHOT_DIR, "s5_cleared.png"), "S5 卸载后")
        check("卸载后：中心回到普通图层（红）",
              px_center2.red() > 120 and px_center2.green() < 120, px_center2.getRgb())
        check("截图已落盘 s5_cleared.png", ok2, p2)
        check("两张截图不是同一张", _md5(p1) != _md5(p2), (_md5(p1), _md5(p2)))
    finally:
        engine.make_player = real_make

    # ---------------- ② α²：0 / 0.25 / 1.0 ----------------
    print("\n② α² 像素级验证（白图叠在黑底上：px = 255 × α²）")
    eng, cue_lay, images = build_scene(alpha_case=True)
    _bind(eng, images)
    engine.make_player = _make
    try:
        got = []
        for beats_off, expect in ((0.0, 0.0), (1.0, 0.25), (2.0, 1.0)):
            eng.set_cue_position(8.0 + beats_off)
            eng._cuesheet_tick()
            eng._compose({"bass": 0.0})
            c = eng.canvas.pixelColor(480, 270)
            got.append(c.red())
            want = int(round(255 * expect))
            check("prog=%.2f ⇒ α²=%.2f ⇒ 期望≈%d 实测=%d"
                  % (beats_off / 2.0, expect, want, c.red()),
                  abs(c.red() - want) <= 4, c.getRgb())
        check("三个采样点单调递增 0 < mid < 255",
              got[0] < got[1] < got[2], got)
    finally:
        engine.make_player = real_make

    # ---------------- ③ crop 生效（不拉伸）----------------
    print("\n③ crop 生效：四象限 + 分界位置（基准=裁后尺寸）")
    eng, cue_lay, images = build_scene(crop_case=True)
    _bind(eng, images)
    engine.make_player = _make
    try:
        eng.set_cue_position(1.0)
        eng._cuesheet_tick()
        eng._compose({"bass": 0.0})
        cv = eng.canvas
        # 源 1000×1000，crop=[0.1,0.1,0.6,0.6] ⇒ src=(100,100,600,600)
        # 画布 960×540，cover: s=max(960/600,540/600)=1.6 ⇒ 画 960×960，y 偏移 -210
        s = max(W / 600.0, H / 600.0)
        y_off = (H - 600 * s) / 2.0
        bx = int((500 - 100) * s)
        by = int((500 - 100) * s + y_off)
        print("      期望竖分界 x=%d / 横分界 y=%d（cover 基准=裁后 600）" % (bx, by))
        c1 = cv.pixelColor(max(0, bx - 40), max(0, by - 40))
        c2 = cv.pixelColor(min(W - 1, bx + 40), max(0, by - 40))
        c3 = cv.pixelColor(max(0, bx - 40), min(H - 1, by + 40))
        c4 = cv.pixelColor(min(W - 1, bx + 40), min(H - 1, by + 40))
        check("左上=红", c1.red() > 120 and c1.green() < 120, c1.getRgb())
        check("右上=绿", c2.green() > 120 and c2.red() < 120, c2.getRgb())
        check("左下=蓝", c3.blue() > 120 and c3.red() < 120, c3.getRgb())
        check("右下=黄", c4.red() > 120 and c4.green() > 120 and c4.blue() < 120,
              c4.getRgb())
        # 不拉伸：上下两行分界位置一致
        def boundary_x(img, y):
            prev = None
            for x in range(2, img.width()):
                c = img.pixelColor(x, y)
                left_red = c.red() > 120 and c.green() < 120
                right_green = c.green() > 120 and c.red() < 120
                if left_red:
                    prev = "red"
                elif right_green and prev == "red":
                    return x
            return None
        # 取同一色带（上半个带里红→绿）的两行：by-60 与 by-160
        b1 = boundary_x(cv, max(0, by - 60))
        b2 = boundary_x(cv, max(0, by - 160))
        check("上下两行竖分界一致（不拉伸）", b1 is not None and b2 is not None
              and abs(b1 - b2) <= 2, (b1, b2, bx))
    finally:
        engine.make_player = real_make

    # ---------------- ④ z 序（夹在两层普通图层之间）----------------
    print("\n④ z 序：码表层夹在两层普通图层之间")
    eng, cue_lay, images = build_scene(z_case=True)
    _bind(eng, images)
    engine.make_player = _make
    try:
        eng.set_cue_position(5.0)
        eng._cuesheet_tick()
        eng._compose({"bass": 0.0})
        center = eng.canvas.pixelColor(480, 270)     # 顶层挖空 ⇒ 看到码表绿
        edge = eng.canvas.pixelColor(10, 10)         # 顶层不透明 ⇒ 蓝
        check("中心=绿（码表盖住底层红）", center.green() > 120 and center.red() < 120,
              center.getRgb())
        check("边缘=蓝（顶层盖住码表）", edge.blue() > 120 and edge.green() < 120,
              edge.getRgb())
        # 码表层不可见 ⇒ 中心变红（底层）
        cue_lay.visible = False
        eng._cuesheet_tick()
        eng._compose({"bass": 0.0})
        center2 = eng.canvas.pixelColor(480, 270)
        check("码表层隐藏 ⇒ 中心回到红", center2.red() > 120 and center2.green() < 120,
              center2.getRgb())
        cue_lay.visible = True
    finally:
        engine.make_player = real_make

    # ---------------- ⑤ fps：码表路径 vs 等价普通图层（比"机制开销"）----------------
    print("\n⑤ fps：同一画面，码表路径 vs 等价普通图层（比的是「机制开销」）")
    eng, cue_lay, images = build_scene()
    _bind(eng, images)
    engine.make_player = _make
    try:
        eng.set_cue_position(5.0)
        eng._cuesheet_tick()

        def bench_compose(n=200):
            for _ in range(20):
                eng._compose({"bass": 0.1})
            t0 = time.perf_counter()
            for _ in range(n):
                eng._compose({"bass": 0.1})
            return (time.perf_counter() - t0) / n * 1000.0

        t_cue = bench_compose()

        # 等价场景：把码表层换成"一层普通图层"，画同一张绿图（同样的全屏 blit 量）
        eng2, cue2, images2 = build_scene()
        _bind(eng2, images2)
        eq = engine.Layer("等价普通层")
        eq.player = FakePlayer(solid(W, H, (30, 200, 90, 255)))
        eng2.layers = [l for l in eng2.layers if not getattr(l, "is_cuesheet", False)]
        eng2.layers.insert(1, eq)          # 同样的位置（夹在两层之间）
        for _ in range(20):
            eng2._compose({"bass": 0.1})
        t0 = time.perf_counter()
        for _ in range(200):
            eng2._compose({"bass": 0.1})
        t_norm = (time.perf_counter() - t0) / 200 * 1000.0
        deg = (t_cue - t_norm) / t_norm * 100.0 if t_norm else 0.0
        print("      码表路径 %.3f ms/帧 ；等价普通图层 %.3f ms/帧 ；差 %+.2f%%"
              % (t_cue, t_norm, deg))
        check("码表路径的机制开销 < 1%（同等绘制量）", deg < 1.0, "%.2f%%" % deg)
        print("      （注：码表「多出来的画面」本身要花一次全屏 blit，"
              "这是内容成本、不是机制开销 —— 上面这条比的是机制）")

        # ---- 3 子层（生产形态）vs 3 普通图层：同样 3 次全屏绘制 ----
        sheet3, te3 = cuesheet.new_sheet("S5-3"), cuesheet.new_track("T", "A", 300.0, BPM)
        mp3, img3 = {}, {}
        for k, col in enumerate(((30, 200, 90, 255), (200, 120, 30, 255), (120, 40, 200, 255))):
            cid, path = "c%d" % k, "P:/x/c%d.png" % k
            mp3[cid] = path
            img3[path] = solid(W, H, col)
            sheet3.media.append(cuesheet.MediaRef(clip_id=cid, name="c%d.png" % k))
            Lk = cuesheet.CompLayer(lyr_id="L%d" % k, name="s%d" % k, z=k * 10)
            Lk.items.append(cuesheet.CueItem(item_id="i%d" % k, clip_id=cid, at_beats=0.0,
                                            trans_in="cut", trans_out="cut").normalized())
            te3.layers.append(Lk)
        sheet3.tracks.append(te3)
        layers3 = [engine.Layer("码表3")]
        layers3[0].is_cuesheet = True
        bays = [engine.Layer("普%d" % k) for k in range(3)]
        for k, ly in enumerate(bays):
            ly.player = FakePlayer(solid(W, H, (60 + k * 40, 90, 200 - k * 50, 255)))
        eng3 = new_engine(QImage(W, H, QImage.Format_ARGB32), layers3, img3)
        eng3._cue_sheet, eng3._cue_track = sheet3, te3
        eng3._cue_rt = crt.CueRuntime(sheet3, te3, media_paths=mp3)
        ENG_IMAGES = img3
        eng3.set_cue_position(1.0)
        eng3._cuesheet_tick()
        n_sub = len(layers3[0].cs_states)

        def bench(e, n=200):
            for _ in range(20):
                e._compose({"bass": 0.1})
            t0 = time.perf_counter()
            for _ in range(n):
                e._compose({"bass": 0.1})
            return (time.perf_counter() - t0) / n * 1000.0

        t3_cue = bench(eng3)
        eng4 = new_engine(QImage(W, H, QImage.Format_ARGB32), bays, img3)
        t3_norm = bench(eng4)
        d3 = (t3_cue - t3_norm) / t3_norm * 100.0 if t3_norm else 0.0
        print("      3 子层：码表 %.3f ms/帧（%d 个子层激活）；3 普通图层 %.3f ms/帧；差 %+.2f%%"
              % (t3_cue, n_sub, t3_norm, d3))
        check("3 子层时机制开销 < 1%%（同等 3 次绘制）", d3 < 1.0, "%.2f%%" % d3)
    finally:
        engine.make_player = real_make

    print("")
    if fails:
        print("DRAW_FAIL  %d 项不通过：%s" % (len(fails), "、".join(fails)))
        return 1
    print("DRAW_OK  出画 / 卸载消失 / α² / crop / z 序 / fps 全过（截图在 %s）" % SHOT_DIR)
    return 0


def _md5(path):
    import hashlib
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


if __name__ == "__main__":
    sys.exit(main())
