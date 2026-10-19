# -*- coding: utf-8 -*-
"""`_draw_layer` 的**像素回归守卫**（S2 前置门禁）—— 证明"补 crop 之后，无 crop 场景逐像素零变化"。

═══ 它解决什么问题 ═══
S2 要在**热路径函数** `_draw_layer` 上加 `crop=None` 形参，并改动**旋转**与 **glitch** 两个绘制分支。
改错了会**所有普通图层**一起遭殃（画面错位/变暗/花屏），而这种错"看起来能跑"。
所以必须有一条"改造前 vs 改造后逐像素一致"的机器判据。

═══ 三个必须做对的点（架构师复核指出，缺一不可）═══
**(A) 确定性**：`_draw_layer` 里有**两处时间依赖** ——
    · glitch 分支 `rng = random.Random(int(now * 1000))`（`engine.py:3263-3264`）
    · `now = time.perf_counter()`（`engine.py:3200`）
    ⇒ 不冻结时钟，glitch 场景**每次都不同**（要么排除该分支 = 等于没验，要么永远红）。
    本工具用 `frozen_clock()` **只在该次绘制期间**替换 `time.perf_counter`，退出即还原；
    同时把 `pulse`（= `snap["bass"]` 派生）、`silent`、`cfg["auto"]["intensity"]` 全部**写死**。
    ⚠ `_draw_layer` 本身不读 `snap`/`silent`/`cfg`（它们在 `_compose` 里），
      但为了让场景"语义完整、以后能直接搬进 `_compose` 级对比"，这里仍把它们固定住并记录。

**(B) 场景矩阵**：覆盖 `_draw_layer` 的**每一个分支**（见 `SCENES`）：
    cut/默认 · cover · contain(custom) · **rot≠0 旋转** · fade(prev+cur) · slide · zoom ·
    **glitch** · **use_alpha=False 扁平化** · pulse≠1 · drift≠0。
    ⚠ `subtract` **不是** `_draw_layer` 的分支（它在合成后由 `_apply_subtract` 处理），
      这里只记一条说明，不伪装成"已覆盖"。

**(C) 旋转分支也要切源矩形**：`engine.py:3229-3237` 的旋转分支同样是 `drawImage(_tgt, image)`。
    本工具里有一个 `rot` 场景 ⇒ 改造后如果只给非旋转分支加了 `src_rect`，这里会立刻红。

═══ 用法 ═══
```
# 1) 先证明"守卫自己是确定的"（同一份 engine.py 跑两次必须 0 差异）
python tools/cue_pixel_diff_guard.py --self-check

# 2) 改造前录基准（存档到 tools/_cue_pixbaseline/）
python tools/cue_pixel_diff_guard.py --record

# 3) 改造后比对（0 差异才算过）
python tools/cue_pixel_diff_guard.py
python tools/cue_pixel_diff_guard.py --report tools/_cue_pixdiff_report.md
```

退出码：`0` = 全一致（或已录基准/自证通过） · `1` = 发现差异 · `2` = 环境/基准缺失。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time as _time
from contextlib import contextmanager

HERE = os.path.dirname(os.path.abspath(__file__))
# `CUE_GUARD_SRC` 可指向"另一份 src"（用于变异验证：把 engine.py 拷出去改坏一处，
# 再让守卫去测它 ⇒ 证明守卫**真的能抓到**渲染变化，而不是永远绿）。
SRC = os.path.abspath(os.environ.get("CUE_GUARD_SRC")
                      or os.path.join(HERE, "..", "src"))
DEFAULT_BASE = os.path.join(HERE, "_cue_pixbaseline")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from PySide6.QtCore import Qt, QRectF                       # noqa: E402
from PySide6.QtGui import QImage, QPainter, QColor, QLinearGradient   # noqa: E402

import engine                                                # noqa: E402

DRAW = engine.AutoVJEngine._draw_layer                       # 未绑定方法：它不读 self


# ==========================================================================
# (A) 确定性：只在该次绘制期间冻结时钟，退出即还原
# ==========================================================================
@contextmanager
def frozen_clock(value):
    """把 `time.perf_counter` 临时换成常量。

    ⚠ `engine.py` 里是 `import time` ⇒ `engine.time` 就是**同一个模块对象**，
      所以只能替换模块属性（没有别的注入点）。这里**进出一对**，异常也还原，
      保证不影响本工具自己的计时与后续调用。
    """
    orig = _time.perf_counter
    _time.perf_counter = lambda: float(value)
    try:
        yield
    finally:
        _time.perf_counter = orig


# 与真实 `_compose` 一致的固定输入（不固定 ⇒ 每次画面不同，diff 不可信）
FIXED = {
    "snap_bass": 0.62,          # → pulse = 1 + bass*0.08*intensity
    "silent": False,
    "intensity": 1.0,           # cfg["auto"]["intensity"] 的系数（[0.6,1.0,1.4,2.0] 之一）
    "opacity": 0.9,
}
PULSE = 1.0 + FIXED["snap_bass"] * 0.08 * FIXED["intensity"]


# ==========================================================================
# 桩对象（只提供 `_draw_layer` 真正读的那几个属性）
# ==========================================================================
class StubPlayer:
    def __init__(self, img):
        self._img = img

    def current(self):
        return self._img


class StubItem:
    """`_draw_layer` 通过 `_t(item)` 读：`img_scale / img_x / img_y / img_rot`。"""

    def __init__(self, scale=1.0, x=0.0, y=0.0, rot=0.0):
        self.img_scale = scale
        self.img_x = x
        self.img_y = y
        self.img_rot = rot


class StubLayer:
    """`_draw_layer` 真正读取的属性（逐个对齐，别多别少）。"""

    def __init__(self, opacity=1.0, use_alpha=True, prev_img=None, cur_item=None,
                 prev_item=None, trans_active=False, lock_motion=False, is_kv=False):
        self.opacity = float(opacity)
        self.use_alpha = bool(use_alpha)
        self.is_kv = bool(is_kv)
        self.lock_motion = bool(lock_motion)
        self._alpha_key = None
        self._alpha_img = None
        self.prev_player = StubPlayer(prev_img) if prev_img is not None else None
        self.clips = [cur_item or StubItem(), prev_item or StubItem()]
        self.cur = 0
        self.prev_idx = 1
        self.trans_active = bool(trans_active)


# ==========================================================================
# 确定性测试图（只用几何绘制，不用 text/随机/系统字体 ⇒ 跨次稳定）
# ==========================================================================
def make_image(w, h, alpha=False, tag=0):
    """造一张结构化的测试图：不同色块 + 渐变 + 斜纹 + 不对称标记。

    刻意做得**不对称**（左上角小方块、右下角红角），这样：
      · 缩放/裁切/旋转/镜像 一旦错一点，diff 立刻能看出来；
      · 纯色图会让"错位"变成"看起来一样"，是最差的测试图。
    """
    img = QImage(int(w), int(h), QImage.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 0) if alpha else QColor(20, 24, 32, 255))
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, False)
    # 渐变底（横向 + 纵向各一层，带位置信息）
    g = QLinearGradient(0, 0, float(w), float(h))
    g.setColorAt(0.0, QColor(30, 90, 200, 255 if not alpha else 160))
    g.setColorAt(0.5, QColor(40, 200, 140, 255 if not alpha else 200))
    g.setColorAt(1.0, QColor(220, 60, 60, 255 if not alpha else 120))
    p.fillRect(0, 0, int(w), int(h), g)
    # 左上角标记（用来判"是否上下/左右翻"）
    p.fillRect(0, 0, max(4, int(w * 0.10)), max(4, int(h * 0.10)), QColor(255, 255, 0, 255))
    # 右下角标记
    p.fillRect(int(w * 0.88), int(h * 0.88), int(w * 0.12), int(h * 0.12),
               QColor(0, 255, 255, 255))
    # 斜纹（高频细节：缩放/插值/裁切错一点就变）
    pen_step = max(6, int(min(w, h) / 64))
    p.setPen(QColor(250, 250, 250, 200 if alpha else 255))
    for x in range(-int(h), int(w), pen_step * 3):
        p.drawLine(x, 0, x + int(h), int(h))
    # 一圈细边框（判裁切边界）
    p.setPen(QColor(255, 0, 255, 255))
    p.drawRect(0, 0, int(w) - 1, int(h) - 1)
    # 不同 tag 让 cur/prev 两张图不同（fade/slide/zoom 才能看出混合比例）
    if tag:
        p.fillRect(int(w * 0.30), int(h * 0.30), int(w * 0.22), int(h * 0.22),
                   QColor(255, 140, 0, 230))
    p.end()
    return img


def _pixels(img):
    """图像 → 原始像素字节（ARGB32，非预乘），用于**逐字节**比较。"""
    conv = img.convertToFormat(QImage.Format_ARGB32)
    bits = conv.constBits()
    try:
        return bytes(bits)
    except TypeError:                        # 某些 PySide 版本给的是 sip.voidptr
        return bits.tobytes()


def _md5_bytes(b):
    return hashlib.md5(b).hexdigest()


# ==========================================================================
# (B) 场景矩阵
# ==========================================================================
def build_scenes(frames=3):
    """每个场景 = 一次 `_draw_layer` 调用参数。`frames` 控每场景帧数（prog/时钟不同）。"""
    S = []

    def add(name, note, W, H, img, tr="cut", progs=(1.0,), pulse=PULSE,
            drift=(0.0, 0.0), layer=None, prev_img=None, prev_item=None,
            now0=1000.0, now_step=0.1):
        S.append({
            "name": name, "note": note, "W": int(W), "H": int(H),
            "img": img, "tr": tr, "progs": list(progs)[:frames],
            "pulse": float(pulse), "drift": drift,
            "layer": layer, "prev_img": prev_img, "prev_item": prev_item,
            "now0": float(now0), "now_step": float(now_step),
        })

    big = make_image(1920, 1080, tag=0)          # 16:9 源
    big2 = make_image(1920, 1080, tag=1)         # 与 big 内容不同（当 prev）
    tall = make_image(1440, 1080, tag=0)         # 4:3 源
    alpha_img = make_image(800, 600, alpha=True, tag=0)

    # 1) 默认/硬切：xform 全默认 ⇒ **cover**（铺满裁切）
    add("cut_cover", "tr=cut, xform 默认 ⇒ cover(铺满裁切)；1440x1080 画布 + 1920x1080 源",
        1440, 1080, big, tr="cut")
    # 2) contain：任一变换 ⇒ **完整显示**（min 缩放）
    add("cut_contain", "tr=cut, img_scale=0.8 ⇒ contain(完整显示)",
        1440, 1080, big, tr="cut",
        layer=StubLayer(opacity=FIXED["opacity"], cur_item=StubItem(scale=0.8)))
    # 3) ★ 旋转分支（323-3237）：crop 改造时必须切 src_rect，这条专抓它
    add("rot17", "tr=cut, img_rot=17° ⇒ 走旋转分支 drawImage(_tgt,image)",
        1920, 1080, tall, tr="cut",
        layer=StubLayer(opacity=FIXED["opacity"], cur_item=StubItem(rot=17.0)))
    # 4) fade（prev + cur 两张不同图，alpha*prog）
    add("fade", "tr=fade, prog=0.25/0.5/0.75/1.0；prev 与 cur 内容不同",
        1440, 1080, big2, tr="fade", progs=(0.25, 0.5, 0.75, 1.0),
        layer=StubLayer(opacity=FIXED["opacity"], prev_img=make_image(1920, 1080, tag=1),
                        trans_active=True), prev_img=big)
    # 5) slide（旧左移出 + 新右移入）
    add("slide", "tr=slide, prog=0.3/0.6", 1440, 1080, big2, tr="slide",
        progs=(0.3, 0.6), layer=StubLayer(opacity=FIXED["opacity"], prev_img=big,
                                          trans_active=True))
    # 6) zoom（旧放大淡出 + 新放大）
    add("zoom", "tr=zoom, prog=0.4/0.7", 1440, 1080, big2, tr="zoom",
        progs=(0.4, 0.7), layer=StubLayer(opacity=FIXED["opacity"], prev_img=big,
                                          trans_active=True))
    # 7) ★ glitch（随机行错位，靠冻结时钟才有确定性）
    add("glitch", "tr=glitch：rng=Random(int(now*1000)) ⇒ **必须冻结时钟**",
        1440, 1080, big2, tr="glitch", progs=(0.5, 0.5, 0.5),
        layer=StubLayer(opacity=FIXED["opacity"], prev_img=big, trans_active=True),
        now0=1000.0, now_step=0.017)
    # 8) ★ use_alpha=False 扁平化（3189-3198，会走 _alpha_img 缓存分支）
    add("alpha_flat", "use_alpha=False + 带 alpha 的图 ⇒ 透明区填黑（_alpha_img 缓存）",
        1440, 1080, alpha_img, tr="cut",
        layer=StubLayer(opacity=FIXED["opacity"], use_alpha=False))
    # 8b) 同一场景跑第二遍：命中 `_alpha_key == key` 的**缓存命中**路径
    S.append(dict(S[-1], name="alpha_flat_cached", warm=True,
                  note="use_alpha=False 第二次（先自预热 ⇒ 命中 _alpha_key 缓存复用分支，"
                       "与执行顺序无关）"))
    # 9) pulse≠1（能量脉冲缩放）
    add("pulse", "pulse=%.3f（bass=%.2f, intensity=%.1f）" % (PULSE, FIXED["snap_bass"],
                                                             FIXED["intensity"]),
        1440, 1080, big, tr="cut", pulse=PULSE)
    # 10) drift≠0（漂移相位按 lay.cur 不同）
    add("drift", "drift=(18,-12) ⇒ dx=drift_x*(0.5+(cur%3)*0.25)，dy 同理",
        1440, 1080, big, tr="cut", drift=(18.0, -12.0))
    return S


# ==========================================================================
# 渲染
# ==========================================================================
def render_frame(scene, i):
    """渲染场景第 i 帧 → (QImage, 触发条件说明)。"""
    layer = scene["layer"] or StubLayer(opacity=FIXED["opacity"])
    img = scene["img"]
    prev_img = scene["prev_img"]
    if prev_img is not None:
        layer.prev_player = StubPlayer(prev_img)
    prog = scene["progs"][i] if i < len(scene["progs"]) else scene["progs"][-1]
    now = scene["now0"] + i * scene["now_step"]
    if scene.get("warm"):
        # 预热：先画一次把 `_alpha_key/_alpha_img` 缓存填上（本场景自己负责，不依赖顺序）
        _warm = QImage(2, 2, QImage.Format_ARGB32)
        _warm.fill(Qt.transparent)
        _wp = QPainter(_warm)
        with frozen_clock(now):
            DRAW(None, _wp, layer, img, scene["W"], scene["H"], prog, scene["tr"],
                 scene["pulse"], {}, scene["drift"][0], scene["drift"][1])
        _wp.end()
    canvas = QImage(scene["W"], scene["H"], QImage.Format_ARGB32)
    canvas.fill(Qt.transparent)                 # 透明底：alpha 合成也参与比较
    p = QPainter(canvas)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    with frozen_clock(now):
        DRAW(None, p, layer, img, scene["W"], scene["H"], prog, scene["tr"],
             scene["pulse"], {}, scene["drift"][0], scene["drift"][1])
    p.end()
    return canvas, {"prog": prog, "now": now}


# ==========================================================================
# 基准：录 / 比
# ==========================================================================
def _engine_fingerprint():
    """记录 engine.py 与 `_draw_layer` 的指纹（比对时提示"基准来自哪个版本"）。"""
    path = os.path.join(SRC, "engine.py")
    raw = open(path, "rb").read()
    src = raw.decode("utf-8", "replace")
    i = src.index("    def _draw_layer(")
    j = src.index("    # ---------------- 手动干预 ----------------", i)
    return {
        "engine_md5": hashlib.md5(raw).hexdigest(),
        "engine_bytes": len(raw),
        "draw_layer_md5": hashlib.md5(src[i:j].encode("utf-8")).hexdigest(),
        "draw_layer_lines": src[i:j].count("\n"),
    }


def record(base_dir, scenes):
    os.makedirs(base_dir, exist_ok=True)
    meta = {
        "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        "fixed": FIXED, "pulse": PULSE,
        "python": sys.version.split()[0],
        "qt": __import__("PySide6").__version__,
        "engine": _engine_fingerprint(),
        "scenes": [],
    }
    total = 0
    for sc in scenes:
        d = os.path.join(base_dir, sc["name"])
        os.makedirs(d, exist_ok=True)
        frames = []
        for i in range(len(sc["progs"])):
            img, cond = render_frame(sc, i)
            png = os.path.join(d, "%02d.png" % i)
            if not img.save(png, "PNG"):
                raise RuntimeError("写 PNG 失败：%s" % png)
            frames.append({"i": i, "cond": cond, "png": os.path.relpath(png, base_dir),
                           "sha": _md5_bytes(_pixels(img))})
            total += 1
        meta["scenes"].append({
            "name": sc["name"], "note": sc["note"], "W": sc["W"], "H": sc["H"],
            "tr": sc["tr"], "pulse": sc["pulse"], "drift": sc["drift"],
            "frames": frames,
        })
    with open(os.path.join(base_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    return meta, total


def compare(base_dir, scenes):
    """逐场景逐帧比较：基准 PNG 解码成 ARGB32 原始字节 vs 现渲染原始字节。"""
    meta_path = os.path.join(base_dir, "meta.json")
    if not os.path.isfile(meta_path):
        raise SystemExit("没有基准：先跑 `--record`（目录 %s）" % base_dir)
    meta = json.load(open(meta_path, encoding="utf-8"))
    by_name = {s["name"]: s for s in meta["scenes"]}
    rows, bad = [], 0
    for sc in scenes:
        b = by_name.get(sc["name"])
        if b is None:
            rows.append({"scene": sc["name"], "status": "缺基准", "frames": 0,
                         "diff_frames": [], "max_delta": None, "first_xy": None})
            bad += 1
            continue
        diff_frames, max_delta, first_xy = [], 0, None
        n = min(len(sc["progs"]), len(b["frames"]))
        for i in range(n):
            ref_png = os.path.join(base_dir, b["frames"][i]["png"])
            ref = QImage(ref_png)
            if ref.isNull():
                raise SystemExit("基准 PNG 读不出来：%s" % ref_png)
            ref_b = _pixels(ref)
            cur_img, _cond = render_frame(sc, i)
            cur_b = _pixels(cur_img)
            if len(ref_b) != len(cur_b):
                diff_frames.append((i, -1, None))
                bad += 1
                continue
            if ref_b != cur_b:
                npx = 0
                dmax = 0
                for k in range(0, len(ref_b), 4):
                    if ref_b[k:k + 4] != cur_b[k:k + 4]:
                        npx += 1
                        for c in range(4):
                            d = abs(ref_b[k + c] - cur_b[k + c])
                            if d > dmax:
                                dmax = d
                        if first_xy is None:
                            w = cur_img.width()
                            first_xy = ((k // 4) % w, (k // 4) // w)
                diff_frames.append((i, npx, dmax))
                max_delta = max(max_delta, dmax)
                bad += 1
        rows.append({"scene": sc["name"], "status": "PASS" if not diff_frames else "FAIL",
                     "frames": n, "diff_frames": diff_frames,
                     "max_delta": max_delta, "first_xy": first_xy})
    return meta, rows, bad


def self_check(scenes, rounds=2):
    """同一进程内渲染两轮：**不依赖基准文件**证明"守卫自身确定性"。"""
    first = {}
    for sc in scenes:
        for i in range(len(sc["progs"])):
            img, _c = render_frame(sc, i)
            first[(sc["name"], i)] = _pixels(img)
    diffs = []
    for sc in scenes:
        for i in range(len(sc["progs"])):
            img, _c = render_frame(sc, i)
            b = _pixels(img)
            if b != first[(sc["name"], i)]:
                n = sum(1 for k in range(0, min(len(b), len(first[(sc["name"], i)])), 4)
                        if b[k:k + 4] != first[(sc["name"], i)][k:k + 4])
                diffs.append((sc["name"], i, n))
    return diffs, sum(len(sc["progs"]) for sc in scenes)


# ==========================================================================
# 报告
# ==========================================================================
def print_record(meta, total, base_dir):
    e = meta["engine"]
    print("已录基准：%d 个场景 / %d 帧 → %s" % (len(meta["scenes"]), total, base_dir))
    print("  engine.py md5=%s（%d 字节）" % (e["engine_md5"], e["engine_bytes"]))
    print("  _draw_layer md5=%s（%d 行）" % (e["draw_layer_md5"], e["draw_layer_lines"]))
    print("  固定量：bass=%.2f intensity=%.1f silent=%s pulse=%.3f"
          % (FIXED["snap_bass"], FIXED["intensity"], FIXED["silent"], PULSE))
    print("")
    print("  %-20s %-5s %-6s %s" % ("场景", "帧", "画布", "覆盖"))
    for s in meta["scenes"]:
        print("  %-20s %-5d %dx%d %s" % (s["name"], len(s["frames"]), s["W"], s["H"],
                                         s["note"][:60]))


def print_compare(meta, rows, bad, base_dir):
    e = meta["engine"]
    cur = _engine_fingerprint()
    print("基准：%s（录于 %s）" % (base_dir, meta.get("created_at", "?")))
    print("  基准 engine.py md5=%s；当前=%s%s"
          % (e.get("engine_md5"), cur["engine_md5"],
             "（**engine.py 已改动**，正是本次要比的）"
             if e.get("engine_md5") != cur["engine_md5"] else "（同一份 engine.py）"))
    print("")
    print("  %-20s %-6s %-4s %-8s %-8s %s"
          % ("场景", "结论", "帧", "差异帧", "最大通道差", "首个差异像素"))
    for r in rows:
        df = r["diff_frames"]
        detail = ""
        if df:
            detail = "帧#" + ",".join(str(x[0]) for x in df)
            if df[0][1] and df[0][1] > 0:
                detail += "（首帧 %d 像素差）" % df[0][1]
        print("  %-20s %-6s %-4d %-8s %-8s %s"
              % (r["scene"], r["status"], r["frames"], detail,
                 ("%d" % r["max_delta"]) if r["max_delta"] else "-",
                 ("%s" % (r["first_xy"],)) if r["first_xy"] else "-"))
    print("")
    if bad == 0:
        print("PIXEL_OK  全部 %d 个场景逐字节一致（0 字节差异）" % len(rows))
    else:
        print("PIXEL_FAIL  %d 个场景存在差异（见上表）" % bad)


def write_report(path, base_dir, meta, rows, bad, self_diffs=None):
    lines = ["# `_draw_layer` 像素回归报告", "",
             "- 基准目录：`%s`" % base_dir,
             "- 基准录制时间：%s" % meta.get("created_at", "?"),
             "- 固定量：bass=%.2f · intensity=%.1f · silent=%s · pulse=%.3f"
             % (FIXED["snap_bass"], FIXED["intensity"], FIXED["silent"], PULSE),
             "- 基准 engine.py md5：`%s`" % meta["engine"].get("engine_md5"),
             "- 当前 engine.py md5：`%s`" % _engine_fingerprint()["engine_md5"], ""]
    if self_diffs is not None:
        lines.append("## 守卫自证（同进程两轮）")
        lines.append("")
        lines.append("0 处不一致 ✓" if not self_diffs else "**有 %d 处不一致**" % len(self_diffs))
        lines.append("")
    lines.append("## 场景结果")
    lines.append("")
    lines.append("| 场景 | 结论 | 帧数 | 差异帧 | 最大通道差 | 首个差异像素 |")
    lines.append("|---|---|---|---|---|---|")
    for r in rows:
        lines.append("| %s | %s | %d | %s | %s | %s |"
                     % (r["scene"], r["status"], r["frames"],
                        ",".join(str(x[0]) for x in r["diff_frames"]) or "-",
                        r["max_delta"] or "-", r["first_xy"] or "-"))
    lines.append("")
    lines.append("结论：**%s**" % ("PIXEL_OK（0 字节差异）" if bad == 0
                                   else "PIXEL_FAIL（%d 个场景有差异）" % bad))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path



# ==========================================================================
# crop 验证（S2 本体：不靠基准，靠**几何事实**断言）
# ==========================================================================
def make_quad_image(w, h):
    """四象限纯色图（左上红 / 右上绿 / 左下蓝 / 右下黄）。

    用来**从像素反推几何**：颜色边界的位置能直接告诉我们
    "裁切是否正确""缩放基准是整幅还是裁切后尺寸""有没有被拉伸"。
    """
    img = QImage(int(w), int(h), QImage.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 255))
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, False)
    p.fillRect(0, 0, int(w) // 2, int(h) // 2, QColor(220, 20, 20, 255))          # TL 红
    p.fillRect(int(w) // 2, 0, int(w) - int(w) // 2, int(h) // 2,
               QColor(20, 200, 20, 255))                                          # TR 绿
    p.fillRect(0, int(h) // 2, int(w) // 2, int(h) - int(h) // 2,
               QColor(30, 30, 220, 255))                                          # BL 蓝
    p.fillRect(int(w) // 2, int(h) // 2, int(w) - int(w) // 2,
               int(h) - int(h) // 2, QColor(230, 220, 30, 255))                   # BR 黄
    p.end()
    return img


def _classify(c):
    """把颜色归到四象限色之一（容忍缩放插值带来的偏差）。"""
    r, g, b = c.red(), c.green(), c.blue()
    if r > 100 and g < 110 and b < 110:
        return "TL"
    if g > 100 and r < 110 and b < 110:
        return "TR"
    if b > 100 and r < 110 and g < 110:
        return "BL"
    if r > 100 and g > 100 and b < 120:
        return "BR"
    return "?"


def _render_with(img, W, H, crop, *, scale=1.0, x=0.0, rot=0.0, tr="cut", prog=1.0):
    lay = StubLayer(opacity=1.0, cur_item=StubItem(scale=scale, x=x, rot=rot))
    canvas = QImage(W, H, QImage.Format_ARGB32)
    canvas.fill(Qt.transparent)
    p = QPainter(canvas)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    with frozen_clock(1000.0):
        DRAW(None, p, lay, img, W, H, prog, tr, 1.0, {}, 0.0, 0.0, crop)
    p.end()
    return canvas


def verify_crop():
    """返回 [(检查项, 期望, 实测, 是否通过)]。

    三个几何事实（**不看基准图**，直接算）：
      1. **裁切生效**：可见区域内只出现裁切窗口里的内容；
      2. **基准 = 裁切后尺寸**：cover 缩放 = max(W/cw, H/ch)，而不是 max(W/iw, H/ih)；
      3. **不拉伸**：横线/竖线的颜色边界在画面上下/左右**位置一致**（等比，无单向拉伸）。
    """
    rows = []

    def rec(name, want, got, ok):
        rows.append((name, want, got, ok))

    src = make_quad_image(1000, 1000)
    W, H = 1440, 1080
    crop = [0.1, 0.1, 0.6, 0.6]        # ⇒ src=(100,100,600,600)

    # ---- case A：cover（xform 全默认）----
    A = _render_with(src, W, H, crop)
    # cover: s = max(1440/600, 1080/600) = 2.4 ⇒ 画 1440x1440，y 偏移 -180
    s = max(W / 600.0, H / 600.0)
    y_off = (H - 600 * s) / 2.0
    x_off = (W - 600 * s) / 2.0
    # 源 x=500（红/绿分界）→ 输出 x = (500-100)*s + x_off
    bx = int((500 - 100) * s + x_off)
    # 源 y=500（上/下分界）→ 输出 y = (500-100)*s + y_off
    by = int((500 - 100) * s + y_off)

    rec("A1 左上=红(裁切窗口内)", "TL", _classify(A.pixelColor(20, max(0, by - 40))),
        _classify(A.pixelColor(20, max(0, by - 40))) == "TL")
    rec("A2 右上=绿", "TR", _classify(A.pixelColor(min(W - 1, bx + 40), max(0, by - 40))),
        _classify(A.pixelColor(min(W - 1, bx + 40), max(0, by - 40))) == "TR")
    rec("A3 左下=蓝", "BL", _classify(A.pixelColor(20, min(H - 1, by + 40))),
        _classify(A.pixelColor(20, min(H - 1, by + 40))) == "BL")
    rec("A4 右下=黄", "BR", _classify(A.pixelColor(min(W - 1, bx + 40), min(H - 1, by + 40))),
        _classify(A.pixelColor(min(W - 1, bx + 40), min(H - 1, by + 40))) == "BR")
    # 分界线位置（用于"不拉伸"与"基准=裁后尺寸"）
    def boundary_x(img, y):
        """找**两个象限色之间**的竖直分界（忽略 "?"=透明/过渡色，否则会误取画面左边界）。"""
        prev = _classify(img.pixelColor(0, y))
        for x in range(1, img.width()):
            cur = _classify(img.pixelColor(x, y))
            if cur != prev and cur != "?" and prev != "?":
                return x, prev, cur
            if cur != "?":
                prev = cur
        return None, prev, None
    bx1, l1, r1 = boundary_x(A, max(0, by - 60))
    bx2, l2, r2 = boundary_x(A, min(H - 1, by + 60))
    rec("A5 竖分界实测位置", "≈%d（max(W/cw,H/ch)基准）" % bx, "%s（%s→%s）" % (bx1, l1, r1),
        bx1 is not None and abs(bx1 - bx) <= 3)
    rec("A6 上下两行竖分界一致（不拉伸）", "两行差 ≤2px",
        "上=%s 下=%s 差=%s" % (bx1, bx2, None if (bx1 is None or bx2 is None) else abs(bx1 - bx2)),
        bx1 is not None and bx2 is not None and abs(bx1 - bx2) <= 2)

    # ---- case B：contain（有任一变换 ⇒ 完整显示；基准仍是裁后尺寸）----
    B = _render_with(src, W, H, crop, x=0.5)     # img_x≠0 ⇒ custom ⇒ contain
    s2 = min(W / 600.0, H / 600.0)               # 1.8
    drawn_w = 600 * s2                            # 1080
    ux_px = W * 0.5 / 100.0                       # img_x=0.5% ⇒ 7.2px（_t() 里 W*ux/100）
    left = (W - drawn_w) / 2.0 + ux_px            # 180 + 7.2
    rec("B1 contain 左边界外=透明", "透明/黑",
        "rgba=%s" % (B.pixelColor(int(left) - 20, 540).getRgb(),),
        B.pixelColor(int(left) - 20, 540).alpha() == 0)
    rec("B2 contain 左边界内=可见", "非透明",
        "alpha=%d" % B.pixelColor(int(left) + 20, 540).alpha(),
        B.pixelColor(int(left) + 20, 540).alpha() > 0)
    bxB = int((500 - 100) * s2 + left)
    rec("B3 contain 竖分界位置", "≈%d（min()基准）" % bxB,
        "%s" % (boundary_x(B, 540)[0],), abs((boundary_x(B, 540)[0] or -999) - bxB) <= 3)

    # ---- case C：crop 的边界语义（**与 Cue 端 `_crop_src_rect` 同规则**）----
    #   `max(1, round(w*fw))` ⇒ w=0 得到 **1×1**（不是"不裁"）；这与 Cue 端逐字一致，
    #   所以这里断言的是"1×1"，不是"等于不裁"（第一版我写错了判据）。
    r0 = engine._crop_src_rect(src, [0.1, 0.1, 0.0, 0.0])
    got = None if r0 is None else (int(r0.x()), int(r0.y()), int(r0.width()), int(r0.height()))
    rec("C1 crop 宽高=0 ⇒ 1×1（Cue 同规则 max(1,·)）", "(100,100,1,1)", "%s" % (got,),
        got == (100, 100, 1, 1))
    C2 = _render_with(src, W, H, None)
    C3 = _render_with(src, W, H, ["x", 0, 1, 1])
    rec("C2 crop 非数值视为不裁（逐字节）", "一致", "一致" if _pixels(C3) == _pixels(C2) else "不一致",
        _pixels(C3) == _pixels(C2))
    # ---- case D：越界 crop 被钳住（不崩、非空）----
    D = _render_with(src, W, H, [0.9, 0.9, 0.5, 0.5])
    nz = sum(1 for yy in range(0, H, 40) for xx in range(0, W, 40)
             if D.pixelColor(xx, yy).alpha() > 0)
    rec("D1 越界 crop 仍出画（钳位不崩）", ">0 个不透明采样点", "%d" % nz, nz > 0)
    # ---- case E：crop + glitch（重写分支）不崩且非空 ----
    E = _render_with(src, W, H, crop, tr="glitch", prog=0.5)
    nzE = sum(1 for yy in range(0, H, 60) for xx in range(0, W, 60)
              if E.pixelColor(xx, yy).alpha() > 0)
    rec("E1 crop+glitch 出画（不崩）", ">0 个不透明采样点", "%d" % nzE, nzE > 0)
    # ---- case F：crop + 旋转（重写分支）不崩且非空 ----
    F = _render_with(src, W, H, crop, rot=17.0)
    nzF = sum(1 for yy in range(0, H, 60) for xx in range(0, W, 60)
              if F.pixelColor(xx, yy).alpha() > 0)
    rec("F1 crop+rot 出画（不崩）", ">0 个不透明采样点", "%d" % nzF, nzF > 0)
    return rows


def print_crop(rows):
    print("crop 几何验证（不依赖基准图，直接用颜色边界反推）")
    bad = 0
    print("  %-30s %-26s %-34s %s" % ("检查项", "期望", "实测", "结论"))
    for name, want, got, ok in rows:
        if not ok:
            bad += 1
        print("  %-30s %-26s %-34s %s" % (name, want, got, "PASS" if ok else "FAIL"))
    print("")
    if bad == 0:
        print("CROP_OK  裁切正确、基准=裁切后尺寸、无拉伸、非法/越界/旋转/glitch 均不崩")
    else:
        print("CROP_FAIL  %d 项不通过" % bad)
    return bad


# ==========================================================================
# 微基准（只测 `_draw_layer` 本身：这就是被改的那个热路径函数）
# ==========================================================================
def bench(scenes, n=200):
    """只测 `_draw_layer` 本身：**复用同一张画布**（每帧新建 6MB 图会主导耗时并抬高噪声）。"""
    out = {}
    for sc in scenes:
        frames = len(sc["progs"])
        t_all = []
        canvas = QImage(sc["W"], sc["H"], QImage.Format_ARGB32)   # 复用，不在计时区内分配
        for k in range(n):
            i = k % frames
            prog = sc["progs"][i] if i < len(sc["progs"]) else sc["progs"][-1]
            layer = sc["layer"] or StubLayer(opacity=FIXED["opacity"])
            if sc["prev_img"] is not None:
                layer.prev_player = StubPlayer(sc["prev_img"])
            now = sc["now0"] + i * sc["now_step"]
            t0 = _time.perf_counter()
            canvas.fill(Qt.transparent)
            p = QPainter(canvas)
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            with frozen_clock(now):
                DRAW(None, p, layer, sc["img"], sc["W"], sc["H"], prog, sc["tr"],
                     sc["pulse"], {}, sc["drift"][0], sc["drift"][1])
            p.end()
            t_all.append((_time.perf_counter() - t0) * 1000.0)
        t_all.sort()
        out[sc["name"]] = {
            "p50_ms": t_all[len(t_all) // 2],
            "p95_ms": t_all[min(len(t_all) - 1, int(len(t_all) * 0.95))],
            "mean_ms": sum(t_all) / len(t_all),
            "n": n, "frames": frames,
        }
    return out


def print_bench(res, json_path=""):
    print("_draw_layer 微基准（每场景 %d 次，单位 ms/次）" % (list(res.values())[0]["n"]
                                                       if res else 0))
    print("  %-20s %8s %8s %8s" % ("场景", "p50", "p95", "均值"))
    tot = 0.0
    for name, r in res.items():
        print("  %-20s %8.3f %8.3f %8.3f" % (name, r["p50_ms"], r["p95_ms"], r["mean_ms"]))
        tot += r["mean_ms"]
    print("  %-20s %8s %8s %8.3f  ← 全场景均值之和" % ("合计", "", "", tot))
    if json_path:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
        print("  已写：%s" % json_path)


def compare_bench(before_path, after):
    before = json.load(open(before_path, encoding="utf-8"))
    print("fps 微基准对比（改前 = %s）" % before_path)
    print("  %-20s %10s %10s %9s %s" % ("场景", "改前均值", "改后均值", "退化", "结论"))
    worst = 0.0
    bad = 0
    for name, a in after.items():
        b = before.get(name)
        if not b:
            print("  %-20s %10s %10.3f %9s 缺改前数据" % (name, "-", a["mean_ms"], "-"))
            continue
        deg = (a["mean_ms"] - b["mean_ms"]) / b["mean_ms"] * 100.0 if b["mean_ms"] else 0.0
        worst = max(worst, deg)
        ok = deg < 1.0
        if not ok:
            bad += 1
        print("  %-20s %10.3f %10.3f %8.2f%% %s"
              % (name, b["mean_ms"], a["mean_ms"], deg, "PASS" if ok else "**FAIL**"))
    print("")
    print("  最大退化 %.2f%%（门槛 <1%%）" % worst)
    print("BENCH_OK  改动未造成可测退化" if bad == 0 else "BENCH_FAIL  %d 个场景退化 ≥1%%" % bad)
    return bad

# ==========================================================================
# 换机器 / 首次使用：**必须先录基准**
# ==========================================================================
#   python tools/cue_pixel_diff_guard.py --record
#     ⇒ 在当前这份 engine.py 上生成 tools/_cue_pixbaseline/（17 张 PNG + meta.json，
#        meta 里记 engine.py md5 / _draw_layer md5 / Qt 版本，便于日后判断基准来自哪版）。
#   然后：
#   python tools/cue_pixel_diff_guard.py             # 比对（0 字节差异 ⇒ exit 0）
#   python tools/cue_pixel_diff_guard.py --self-check  # 守卫自身确定性（同进程两轮）
#   python tools/cue_pixel_diff_guard.py --verify-crop # crop 几何（不依赖基准图）
#   python tools/cue_pixel_diff_guard.py --bench 400   # 微基准（复用画布，可 --bench-compare）
#
#   ⚠ 录基准必须在**你打算当作'改前'的那份 engine.py** 上做；改完代码再录就失去意义。
#   ⚠ `tools/_cue_pixbaseline/` 约 14MB，**不入库**（本地工具用；`.gitignore` 未覆盖，
#      是否提交由团队定）。换机器时按上面第一条命令自己生成即可。


# ==========================================================================
# 入口
# ==========================================================================
def main(argv=None):
    ap = argparse.ArgumentParser(description="_draw_layer 像素回归守卫（S2 门禁）")
    ap.add_argument("--record", action="store_true", help="录基准（改造前跑一次）")
    ap.add_argument("--self-check", action="store_true", help="同进程两轮自证确定性")
    ap.add_argument("--base", default=DEFAULT_BASE, help="基准目录")
    ap.add_argument("--frames", type=int, default=3, help="每场景帧数上限")
    ap.add_argument("--scenes", default="", help="只跑这些场景（逗号分隔）")
    ap.add_argument("--report", default="", help="把结果写成 markdown")
    ap.add_argument("--verify-crop", action="store_true", help="crop 几何验证（不依赖基准）")
    ap.add_argument("--bench", type=int, default=0, metavar="N", help="微基准：每场景跑 N 次")
    ap.add_argument("--bench-json", default="", help="把微基准结果写成 JSON")
    ap.add_argument("--bench-compare", default="", help="与改前的基准 JSON 对比（退化 <1%）")
    args = ap.parse_args(argv)

    scenes = build_scenes(frames=args.frames)
    if args.scenes:
        want = [s.strip() for s in args.scenes.split(",") if s.strip()]
        scenes = [s for s in scenes if s["name"] in want]
        if not scenes:
            raise SystemExit("没有匹配的场景：%s" % args.scenes)

    if args.self_check:
        diffs, n = self_check(scenes)
        print("守卫自证：%d 个场景 / %d 帧，同进程渲染两轮" % (len(scenes), n))
        if diffs:
            for name, i, npx in diffs:
                print("  [FAIL] %s 帧#%d 有 %d 像素不同" % (name, i, npx))
            print("\nSELFCHECK_FAIL  守卫不确定 ⇒ 后面的 diff 全部不可信")
            return 1
        print("SELFCHECK_OK  两轮逐字节一致（0 字节差异）⇒ 时钟冻结与固定量生效")
        return 0

    if args.verify_crop:
        bad = print_crop(verify_crop())
        return 0 if bad == 0 else 1

    if args.bench:
        res = bench(scenes, n=args.bench)
        print_bench(res, args.bench_json)
        bad = compare_bench(args.bench_compare, res) if args.bench_compare else 0
        return 0 if bad == 0 else 1

    if args.record:
        meta, total = record(args.base, scenes)
        print_record(meta, total, args.base)
        print("")
        print("RECORD_OK  基准已存档（%s）" % args.base)
        return 0

    meta, rows, bad = compare(args.base, scenes)
    print_compare(meta, rows, bad, args.base)
    if args.report:
        p = write_report(args.report, args.base, meta, rows, bad)
        print("\n报告已写：%s" % p)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
