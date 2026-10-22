# -*- coding: utf-8 -*-
"""★ 第五轮**独立复验**（software-engineer-5-2-2 · fresh eyes · 只读 + 自建脚本）。

不复用任何 owner 的验证脚本/判据：自造 mp4、自己合成鼠标事件、自己读模型。
隔离：`LOCALAPPDATA` 指到临时目录（**不污染**真实 config / autosave）。

覆盖 6 条用户反馈：
  ③ ★ 再制的用户场景：`左键选中 → 拖动 → 拖动中按右键 → 松手` ⇒ **原件 at 不动** + **产出副本**。
  ① 自动吸附开关（可见 / 默认关 / 开=真吸且邻居不动 / 关=不吸 / 持久化）
  ② 再制：右键已选中片段 ⇒ +1 副本（鼠标落点）、原件不动、可撤销一步
  ④ 属性面板「大小」= 滑条 + 数字框双向同步，双双落模型 `xform.scale`
  ⑤ 混合/不透明度去「继承本层」入口 + 旧数据（""/None）显示生效值且**改别字段不物化**
  ⑥ 标尺点击/拖动 ⇒ 播放头动、**不做平移**
附带：三处波形指纹（ast+sha256 口径）+ 三条自检（A1/A2/A3）走查由外层脚本另跑。

跑法：  venv\\Scripts\\python.exe tools\\_fix5_indep_verify.py
"""
from __future__ import annotations

import ast
import hashlib
import os
import sys
import tempfile
import time
import traceback
import warnings

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("ERCUE_PROF_MAIN", "0")
# ★ 隔离：config / autosave 都落到临时目录（必须在 import ercue.* 之前）
_TMPROOT = tempfile.mkdtemp(prefix="ercue_fix5_")
os.environ["LOCALAPPDATA"] = _TMPROOT
os.environ["ERCUE_NO_AUTOSAVE"] = "1"
warnings.filterwarnings("ignore", category=DeprecationWarning)

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import av                                                          # noqa: E402
import numpy as np                                                 # noqa: E402

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt            # noqa: E402
from PySide6.QtGui import QContextMenuEvent, QMouseEvent           # noqa: E402
from PySide6.QtWidgets import QApplication, QCheckBox              # noqa: E402

RES = []
MASK = "?"
TARGET_MD5 = {}


def chk(tag, ok, detail):
    RES.append((tag, bool(ok), detail))
    print("  [%s] %s\n        %s" % ("PASS" if ok else "FAIL", tag, detail))


def pump(app, n=8):
    for _ in range(n):
        app.processEvents()


def _make_mp4(path, frames=90, fps=30, w=320, h=180):
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


def mk_window(app, mp4):
    from ercue.ui.window import MainWindow
    w = MainWindow(restore_workspace=False, autosave=False)
    w._interactive = False
    w.setAttribute(Qt.WA_DontShowOnScreen, True)
    w.resize(1300, 860)
    w.show()
    pump(app)
    w.new_sheet("fix5", 1920, 1080)
    ids = w.add_media_paths([mp4])
    w.add_track("t")
    pump(app)
    return w, ids[0]


def total_items(w):
    return sum(len(l.items) for l in w._layers())


def ev(t, x, y, btn, btns):
    return QMouseEvent(t, QPointF(float(x), float(y)), btn, btns, Qt.NoModifier)


def ctx(x, y):
    return QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(int(x), int(y)),
                             QPoint(int(x), int(y)))


def tl_xy(tl, at_sec, row=0):
    return (int(round(tl._x(at_sec))), int(round(tl._row_rect(row).center().y())))


# ==========================================================================
# ③ ★ 再制用户场景（最高优先）
# ==========================================================================
def part3_dup_user_scenario(app, mp4):
    print("\n--- ③ 再制用户场景：左键选中→拖动→拖动中右键→松手 ---")
    w, cid = mk_window(app, mp4)
    ly = w._layers()[0]
    a = w.add_cue(ly.lyr_id, cid, 0.0)     # 被拖的那个
    _b = w.add_cue(ly.lyr_id, cid, 10.0)   # 邻居
    pump(app)
    tl = w.timeline
    tl.set_selected(ly.lyr_id, a.item_id)
    w._sync_props_selection()
    pump(app)
    a_id = a.item_id
    at0 = float(a.at_sec)
    n0 = total_items(w)
    mv, dp = [], []
    tl.moved.connect(lambda *x: mv.append(x))
    tl.duplicate_requested.connect(lambda *x: dp.append(x))
    # 左键按在 a 体内
    px, py = tl_xy(tl, at0 + 0.5, 0)
    tl.mousePressEvent(ev(QEvent.MouseButtonPress, px, py, Qt.LeftButton, Qt.LeftButton))
    dx = tl._drag["dx"] if tl._drag else 0.0
    # 拖动
    mx = int(round(tl._x(6.0) - dx))
    tl.mouseMoveEvent(ev(QEvent.MouseMove, mx, py, Qt.NoButton, Qt.LeftButton))
    dragging = tl._drag is not None
    # ★ 拖动中按右键
    tl.contextMenuEvent(ctx(mx, py))
    pump(app)
    # 松手
    tl.mouseReleaseEvent(ev(QEvent.MouseButtonRelease, mx, py, Qt.LeftButton, Qt.NoButton))
    pump(app)
    ly2, a2 = w._find_item(ly.lyr_id, a_id)
    at_after = float(a2.at_sec)
    n1 = total_items(w)
    chk("③1 原件 at 未动（拖动中右键）", abs(at_after - at0) < 1e-6,
        "at %.4f → %.4f（应不变）；拖到过 x=%d" % (at0, at_after, mx))
    chk("③2 产出一份副本（片段数 +1）", n1 == n0 + 1,
        "items %d → %d；duplicate_requested 次数=%d" % (n0, n1, len(dp)))
    chk("③3 拖动中右键 ⇒ 不发 moved（原件不被挪）", len(mv) == 0,
        "moved 次数=%d（应 0）；进入过拖动=%s" % (len(mv), dragging))
    # 对照组：同样的拖动但**不按右键** ⇒ 应发 moved（证明"右键在起作用"）
    w2, cid2 = mk_window(app, mp4)
    lyx = w2._layers()[0]
    aA = w2.add_cue(lyx.lyr_id, cid2, 0.0)
    pump(app)
    tlx = w2.timeline
    tlx.set_selected(lyx.lyr_id, aA.item_id)
    mv2 = []
    tlx.moved.connect(lambda *x: mv2.append(x))
    px2, py2 = tl_xy(tlx, 0.5, 0)
    tlx.mousePressEvent(ev(QEvent.MouseButtonPress, px2, py2, Qt.LeftButton, Qt.LeftButton))
    dx2 = tlx._drag["dx"] if tlx._drag else 0.0
    tlx.mouseMoveEvent(ev(QEvent.MouseMove, int(round(tlx._x(6.0) - dx2)), py2,
                          Qt.NoButton, Qt.LeftButton))
    tlx.mouseReleaseEvent(ev(QEvent.MouseButtonRelease, int(round(tlx._x(6.0) - dx2)), py2,
                             Qt.LeftButton, Qt.NoButton))
    pump(app)
    chk("③4 对照：不按右键的拖动 ⇒ 会发 moved（反证右键=拦截）", len(mv2) >= 1,
        "对照组 moved 次数=%d（应 ≥1）" % len(mv2))
    try:
        w.shutdown(); w2.shutdown()
    except Exception:                                          # noqa: BLE001
        pass


# ==========================================================================
# ① 自动吸附
# ==========================================================================
def part1_snap(app, mp4):
    print("\n--- ① 自动吸附开关 + 功能 + 持久化 ---")
    w, cid = mk_window(app, mp4)
    chk_widget = getattr(w, "chk_snap", None)
    chk("①1 工具条上有「自动吸附」开关", isinstance(chk_widget, QCheckBox)
        and "自动吸附" in chk_widget.text() and chk_widget.isVisibleTo(w),
        "控件=%r 文本=%r 可见=%s"
        % (type(chk_widget).__name__ if chk_widget is not None else None,
           chk_widget.text() if chk_widget is not None else None,
           chk_widget.isVisibleTo(w) if chk_widget is not None else None))
    chk("①2 默认关（设计 §9）", w.chk_snap.isChecked() is False
        and w.timeline.snap_enabled() is False,
        "chk=%s  snap_enabled=%s" % (w.chk_snap.isChecked(), w.timeline.snap_enabled()))
    # 开 ⇒ 真吸附
    ly = w._layers()[0]
    a = w.add_cue(ly.lyr_id, cid, 0.0)
    b = w.add_cue(ly.lyr_id, cid, 10.0)
    pump(app)
    tl = w.timeline
    w.chk_snap.setChecked(True)                 # 触发 toggled → set_snap + cfg 持久化
    pump(app)
    on_ok = (w.timeline.snap_enabled() is True)
    tl.set_selected(ly.lyr_id, a.item_id)
    w._sync_props_selection()
    px, py = tl_xy(tl, 0.5, 0)
    tl.mousePressEvent(ev(QEvent.MouseButtonPress, px, py, Qt.LeftButton, Qt.LeftButton))
    dx = tl._drag["dx"] if tl._drag else 0.0
    # 拖到 ~9.97（离 B 的 10.0 仅约 4px ⇒ 在 8px 吸附阈值内 ⇒ 应吸附到 10.0）
    tl.mouseMoveEvent(ev(QEvent.MouseMove, int(round(tl._x(9.97) - dx)), py,
                         Qt.NoButton, Qt.LeftButton))
    guide = tl._snap_guide
    tl.mouseReleaseEvent(ev(QEvent.MouseButtonRelease, int(round(tl._x(9.97) - dx)), py,
                            Qt.LeftButton, Qt.NoButton))
    pump(app)
    _ly, a3 = w._find_item(ly.lyr_id, a.item_id)
    _ly, b3 = w._find_item(ly.lyr_id, b.item_id)
    chk("①3 开 ⇒ snap_enabled True + 落点吸附到邻居端点",
        on_ok and guide is not None and abs(float(a3.at_sec) - 10.0) < 0.2,
        "snap_enabled=%s；导引线=%s；落点 at=%.4f（应≈10）" % (on_ok, guide, float(a3.at_sec)))
    chk("①4 吸附时邻居不动", abs(float(b3.at_sec) - 10.0) < 1e-6,
        "邻居 B.at=%.4f（应 10）" % float(b3.at_sec))
    # 关 ⇒ 不吸附
    w.chk_snap.setChecked(False)
    pump(app)
    tl.set_selected(ly.lyr_id, a.item_id)
    w._sync_props_selection()
    # 先把 A 挪回 0 附近（直接改模型 + 重建，避免继续叠加）
    _ly, a4 = w._find_item(ly.lyr_id, a.item_id)
    a4.at_sec = 0.0
    w._rebuild_runtime()
    pump(app)
    px, py = tl_xy(tl, 0.5, 0)
    tl.mousePressEvent(ev(QEvent.MouseButtonPress, px, py, Qt.LeftButton, Qt.LeftButton))
    dx = tl._drag["dx"] if tl._drag else 0.0
    tl.mouseMoveEvent(ev(QEvent.MouseMove, int(round(tl._x(9.9) - dx)), py,
                         Qt.NoButton, Qt.LeftButton))
    tl.mouseReleaseEvent(ev(QEvent.MouseButtonRelease, int(round(tl._x(9.9) - dx)), py,
                            Qt.LeftButton, Qt.NoButton))
    pump(app)
    _ly, a5 = w._find_item(ly.lyr_id, a.item_id)
    chk("①5 关 ⇒ 不吸附（落点≈9.9，不贴 10）",
        w.timeline.snap_enabled() is False and abs(float(a5.at_sec) - 9.9) < 0.3,
        "落点 at=%.4f（应≈9.9）" % float(a5.at_sec))
    # 持久化：config.json 磁盘上应有 snap=true（先开一次再读盘）
    w.chk_snap.setChecked(True)
    pump(app)
    cfgp = None
    try:
        cfgp = w.cfg.path if hasattr(w.cfg, "path") else None
    except Exception:                                          # noqa: BLE001
        pass
    disk_snap = None
    try:
        import json
        p = cfgp or os.path.join(_TMPROOT, "EasyRealityCue", "config.json")
        disk_snap = json.load(open(p, encoding="utf-8")).get("snap")
    except Exception as e:                                     # noqa: BLE001
        disk_snap = "读取失败:%r" % e
    w2, _ = mk_window(app, mp4)
    chk("①6 重启（新窗口）后状态被记住（cfg 持久化）",
        disk_snap is True and w2.chk_snap.isChecked() is True,
        "磁盘 config snap=%r；新窗口 chk=%s" % (disk_snap, w2.chk_snap.isChecked()))
    # 复原（避免影响后续）
    try:
        w.chk_snap.setChecked(False); w2.chk_snap.setChecked(False)
    except Exception:                                          # noqa: BLE001
        pass
    try:
        w.shutdown(); w2.shutdown()
    except Exception:                                          # noqa: BLE001
        pass


# ==========================================================================
# ② 再制（右键已选中片段）
# ==========================================================================
def part2_duplicate(app, mp4):
    print("\n--- ② 再制：右键已选中片段 ---")
    w, cid = mk_window(app, mp4)
    ly = w._layers()[0]
    a = w.add_cue(ly.lyr_id, cid, 0.0)
    pump(app)
    tl = w.timeline
    tl.set_selected(ly.lyr_id, a.item_id)
    w._sync_props_selection()
    pump(app)
    a_id, at0, n0 = a.item_id, float(a.at_sec), total_items(w)
    px, py = tl_xy(tl, 1.0, 0)          # 鼠标位置 = 1.0s 处（**在片段体内**：片段＝素材全长≈3s）
    tl.contextMenuEvent(ctx(px, py))
    pump(app)
    items = ly.items
    n1 = total_items(w)
    _ly, a2 = w._find_item(ly.lyr_id, a_id)
    dup = [x for x in items if x.item_id != a_id]
    chk("②1 右键已选中 ⇒ +1 副本", n1 == n0 + 1 and len(dup) == 1,
        "items %d → %d；副本=%s" % (n0, n1, [x.item_id for x in dup]))
    chk("②2 原件不动", abs(float(a2.at_sec) - at0) < 1e-6,
        "原件 at %.4f → %.4f" % (at0, float(a2.at_sec)))
    if dup:
        exp_at = tl._sec(px)
        chk("②3 副本落在鼠标位置", abs(float(dup[0].at_sec) - exp_at) < 0.5,
            "副本 at=%.4f；鼠标处=%.4f" % (float(dup[0].at_sec), exp_at))
    # 撤销一步
    w.undo()
    pump(app)
    chk("②4 可撤销一步（回到 +0）", total_items(w) == n0,
        "undo 后 items=%d（应 %d）" % (total_items(w), n0))
    try:
        w.shutdown()
    except Exception:                                          # noqa: BLE001
        pass


# ==========================================================================
# ④ 属性面板「大小」滑条 + 数字框
# ==========================================================================
def part4_scale(app, mp4):
    print("\n--- ④ 属性面板「大小」= 滑条 + 数字框 双向同步 ---")
    w, cid = mk_window(app, mp4)
    ly = w._layers()[0]
    a = w.add_cue(ly.lyr_id, cid, 0.0)
    pump(app)
    w.timeline.set_selected(ly.lyr_id, a.item_id)
    w._sync_props_selection()
    pump(app)
    p = w.props_panel
    has = hasattr(p, "sld_scale") and hasattr(p, "sp_scale")
    chk("④1 有滑条 + 数字框", has, "sld_scale=%s sp_scale=%s"
        % (hasattr(p, "sld_scale"), hasattr(p, "sp_scale")))
    if not has:
        return
    # 拖滑条 ⇒ 数字框跟随 + 落模型
    p.sld_scale.setValue(int(round(2.0 * 100)))
    pump(app)
    spin_v = p.sp_scale.value()
    _ly, a2 = w._find_item(ly.lyr_id, a.item_id)
    model_v = float((a2.xform or {}).get("scale", 1.0) or 1.0)
    chk("④2 滑条 → 数字框跟随 + 落模型", abs(spin_v - 2.0) < 0.06 and abs(model_v - 2.0) < 0.06,
        "spin=%.3f  model.scale=%.3f" % (spin_v, model_v))
    # 改数字 ⇒ 滑条跟随 + 落模型
    p.sp_scale.setValue(3.50)
    pump(app)
    slider_v = p.sld_scale.value() / 100.0
    _ly, a3 = w._find_item(ly.lyr_id, a.item_id)
    model_v2 = float((a3.xform or {}).get("scale", 1.0) or 1.0)
    chk("④3 数字框 → 滑条跟随 + 落模型", abs(slider_v - 3.5) < 0.06 and abs(model_v2 - 3.5) < 0.06,
        "slider=%.3f  model.scale=%.3f" % (slider_v, model_v2))
    try:
        w.shutdown()
    except Exception:                                          # noqa: BLE001
        pass


# ==========================================================================
# ⑤ 混合/不透明度 去「继承本层」
# ==========================================================================
def part5_inherit(app, mp4):
    print("\n--- ⑤ 混合/不透明度 去「继承本层」+ 旧数据不物化 ---")
    w, cid = mk_window(app, mp4)
    ly = w._layers()[0]
    ly.blend = "screen"          # 层的混合
    ly.opacity = 0.8             # 层的不透明度
    a = w.add_cue(ly.lyr_id, cid, 0.0)
    a.blend = ""                 # 旧数据：继承本层
    a.opacity = None             # 旧数据：继承本层
    pump(app)
    w._rebuild_runtime()
    w.timeline.set_selected(ly.lyr_id, a.item_id)
    w._sync_props_selection()
    w.props_panel.refresh()
    pump(app)
    p = w.props_panel
    datas = [p.cb_blend.itemData(i) for i in range(p.cb_blend.count())]
    chk("⑤1 混合下拉无「继承本层」项", "" not in datas and None not in datas,
        "下拉 data=%s" % (datas,))
    chk("⑤2 不透明度无「继承本层」勾选", not hasattr(p, "chk_op_inherit"),
        "has chk_op_inherit=%s" % hasattr(p, "chk_op_inherit"))
    eff_blend = p.cb_blend.currentData()
    eff_op = p.sp_op.value()
    chk("⑤3 旧片段(blend=''/opacity=None) 显示**生效值**（=层）",
        eff_blend == "screen" and abs(eff_op - 80) < 1,
        "显示 blend=%r opacity=%d%%（层=screen/80%%）" % (eff_blend, int(eff_op)))
    # 只改别的字段（起点）⇒ blend/opacity 不得被物化
    p.sp_at.setValue(4.0)
    pump(app)
    _ly, a2 = w._find_item(ly.lyr_id, a.item_id)
    chk("⑤4 只改起点 ⇒ blend 仍 ''、opacity 仍 None（不物化）",
        a2.blend == "" and a2.opacity is None,
        "blend=%r opacity=%r" % (a2.blend, a2.opacity))
    # 真改混合/不透明度 ⇒ 才写显式值
    p.cb_blend.setCurrentIndex(1)          # 选一个具体模式
    pump(app)
    p.sp_op.setValue(55)
    pump(app)
    _ly, a3 = w._find_item(ly.lyr_id, a.item_id)
    chk("⑤5 真改了才写显式值", isinstance(a3.blend, str) and a3.blend != ""
        and a3.opacity is not None and abs(float(a3.opacity) - 0.55) < 1e-6,
        "blend=%r opacity=%r" % (a3.blend, a3.opacity))
    try:
        w.shutdown()
    except Exception:                                          # noqa: BLE001
        pass


# ==========================================================================
# ⑥ 标尺点击 + 拖动播放头
# ==========================================================================
def part6_ruler(app, mp4):
    print("\n--- ⑥ 标尺点击/拖动 ⇒ 播放头动、不平移 ---")
    w, cid = mk_window(app, mp4)
    ly = w._layers()[0]
    w.add_cue(ly.lyr_id, cid, 0.0)
    pump(app)
    tl = w.timeline
    seeks = []
    tl.seek_requested.connect(lambda s: seeks.append(float(s)))
    v0 = tl.view0
    # 点击标尺（y < RULER_H）
    tx = int(round(tl._x(5.0)))
    p0 = float(w._pos)
    tl.mousePressEvent(ev(QEvent.MouseButtonPress, tx, 5, Qt.LeftButton, Qt.LeftButton))
    tl.mouseReleaseEvent(ev(QEvent.MouseButtonRelease, tx, 5, Qt.LeftButton, Qt.NoButton))
    pump(app)
    chk("⑥1 点击标尺 ⇒ 播放头到该处 + 不平移",
        abs(float(w._pos) - 5.0) < 0.3 and abs(tl.view0 - v0) < 1e-6
        and not tl._ruler_drag and tl._pan is None,
        "pos %.3f→%.3f；view0 %.1f→%.1f；seeks=%d" % (p0, float(w._pos), v0, tl.view0, len(seeks)))
    # 拖动标尺 ⇒ 连续 seek
    n_before = len(seeks)
    tl.mousePressEvent(ev(QEvent.MouseButtonPress, int(tl._x(2.0)), 5, Qt.LeftButton, Qt.LeftButton))
    for sec in (3.0, 5.0, 7.0, 9.0):
        tl.mouseMoveEvent(ev(QEvent.MouseMove, int(round(tl._x(sec))), 5,
                             Qt.NoButton, Qt.LeftButton))
        time.sleep(0.035)              # 越过 30ms 节流
    tl.mouseReleaseEvent(ev(QEvent.MouseButtonRelease, int(round(tl._x(9.0))), 5,
                            Qt.LeftButton, Qt.NoButton))
    pump(app)
    chk("⑥2 拖动标尺 ⇒ 连续 seek、松手定稿、不崩、不平移",
        (len(seeks) - n_before) >= 2 and abs(float(w._pos) - 9.0) < 0.3
        and abs(tl.view0 - v0) < 1e-6,
        "新增 seek=%d；末 pos=%.3f；view0=%.1f（未平移）" % (len(seeks) - n_before, float(w._pos), tl.view0))
    try:
        w.shutdown()
    except Exception:                                          # noqa: BLE001
        pass


# ==========================================================================
# 附带：波形指纹（ast + sha256[:16]）
# ==========================================================================
def part_fingerprint():
    print("\n--- 附带：三处波形指纹（ast + sha256[:16]）---")
    TL = os.path.join(_SRC, "ercue", "ui", "timeline_widget.py")
    src = open(TL, encoding="utf-8").read()
    tree = ast.parse(src)
    L = src.splitlines(keepends=True)

    def fh(name):
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
                return hashlib.sha256("".join(L[n.lineno - 1:n.end_lineno]).encode()).hexdigest()[:16]
        return ""
    exp = {"_paint_waveform": "6eb426e2b00ac03f", "_paint_ruler": "1ed3d8f25b876d96",
           "wave_sample": "d52290429389c6f8"}
    got = {k: fh(k) for k in exp}
    chk("附带 三处波形指纹一致", got == exp, "got=%s exp=%s" % (got, exp))


def main():
    app = QApplication.instance() or QApplication([])
    mp4 = os.path.join(_TMPROOT, "a.mp4")
    _make_mp4(mp4)
    for f in (part3_dup_user_scenario, part1_snap, part2_duplicate,
              part4_scale, part5_inherit, part6_ruler):
        try:
            f(app, mp4)
        except Exception as e:                                 # noqa: BLE001
            chk("%s（异常）" % f.__name__, False, "%s: %s\n%s" % (type(e).__name__, e,
                                                                 traceback.format_exc()))
    try:
        part_fingerprint()
    except Exception as e:                                     # noqa: BLE001
        chk("附带 指纹（异常）", False, repr(e))
    npass = sum(1 for _t, ok, _d in RES if ok)
    print("\n" + "=" * 74)
    print("第五轮独立复验：PASS %d / FAIL %d" % (npass, len(RES) - npass))
    for t, ok, _d in RES:
        print("   %s %s" % ("✓" if ok else "✗", t))
    print("=" * 74)
    print("FIX5_INDEP_OK" if npass == len(RES) else "FIX5_INDEP_FAIL")
    return 0 if npass == len(RES) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:                                          # noqa: BLE001
        traceback.print_exc()
        print("FIX5_INDEP_FAIL")
        raise SystemExit(1)
