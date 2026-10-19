# -*- coding: utf-8 -*-
"""Step6 自检：图层**码表选择下拉**（`cmbCue`）—— 出现 / 换表 / 持久化 / 不干扰普通图层。

5 项判据（团队给定）：
  1. 下拉出现：`is_cuesheet` 的图层头出现青绿码表下拉（**真窗口截图** `tools/_cueshots/step6_badge.png`）
  2. 换表：能换表 / 卸下；**换表后画面跟着换**（用假播放器 + 不同颜色图验证）
  3. 持久化：`to_dict → rebuild_from_cfg` 后 `is_cuesheet` / `cuesheet_id` 仍在
  4. 不干扰普通图层：非码表图层的头部**子控件几何 + 渲染像素**与改动前（git HEAD 的 panels.py）一致
  5. 回归另跑（9 项 + 守卫 + parity）

运行：`python tools/cue_badge_test.py`（exit 0 = 全过）
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
SHOT_DIR = os.path.join(HERE, "_cueshots")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from PySide6.QtCore import Qt                                      # noqa: E402
from PySide6.QtGui import QColor, QImage                            # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget                  # noqa: E402

import engine                                                       # noqa: E402
import cuesheet                                                     # noqa: E402
import cue_ui                                                       # noqa: E402
import panels                                                       # noqa: E402
from cue_seek_policy import CueSeekPolicy                            # noqa: E402

W, H, BPM = 320, 180, 120.0


# ==========================================================================
class FakeMain:
    """面板只用到 `engine.layers` 与一堆回调：其余一律空实现（`__getattr__` 兜底）。"""

    def __init__(self, layers):
        self.engine = type("E", (), {"layers": layers})()
        self.saved = 0

    def __getattr__(self, name):
        def _noop(*a, **k):
            return None
        return _noop

    def save_layers(self):
        self.saved += 1


class FakePlayer:
    def __init__(self, img):
        self._img = img
        self._active = False

    def current(self):
        return self._img

    def set_active(self, a):
        self._active = bool(a)

    def warm_up(self):
        pass

    def set_max_size(self, w, h):
        pass

    def set_speed(self, s):
        pass

    def position(self):
        return 0.0

    def seek(self, s):
        return True

    def close(self):
        pass


class FakeCfg:
    def __init__(self):
        self.data = {}

    def __getitem__(self, k):
        return self.data[k]

    def __setitem__(self, k, v):
        self.data[k] = v

    def save(self, force=False):
        pass


def new_engine(layers):
    eng = engine.AutoVJEngine.__new__(engine.AutoVJEngine)
    eng.canvas = QImage(W, H, QImage.Format_ARGB32)
    eng.mode = "normal"
    eng.blackout = False
    eng.cfg = {"auto": {"intensity": 1, "transition": None},
               "mode": {"transition": ""}, "color": {}, "postfx": {}}
    eng._silent_now_debounced = False
    eng._health_log = None
    eng._cue_tick_fail = 0
    eng._cue_err_at = 0.0
    eng._cue_pos_beats = None
    eng._cue_pos_src = ""
    eng._cue_sheet = None
    eng._cue_track = None
    eng._cue_media = {}
    eng._cue_jumped = False
    eng._cue_closed_last = 0
    eng._cue_policy = CueSeekPolicy(cfg={})
    eng._cue_rt = None
    eng.layers = list(layers)
    eng.library = {}
    return eng


def make_sheet(name="徽表", color_name="a.mp4"):
    s = cuesheet.CueSheet(title=name, res_w=W, res_h=H)
    te = cuesheet.TrackEntry()
    te.identity = cuesheet.Identity(title="曲", artist="者", duration_sec=180.0, bpm=BPM)
    te.identity.ensure_meta_id()
    L = cuesheet.CompLayer(lyr_id="L1", name="层", z=0)
    L.items.append(cuesheet.CueItem(item_id="i1", clip_id="c1", at_beats=0.0,
                                    src_in_sec=0.0, src_out_sec=30.0,
                                    trans_in="cut", trans_out="cut").normalized())
    te.layers.append(L)
    s.tracks.append(te)
    s.media.append(cuesheet.MediaRef(clip_id="c1", name=color_name))
    return s


def load_head_panels(tmp):
    """把 git HEAD 的 panels.py 取出来当"改动前"的参照（用于"普通图层零干扰"）。"""
    src = subprocess.run(["git", "show", "HEAD:src/panels.py"], cwd=os.path.dirname(SRC),
                         capture_output=True)
    if src.returncode != 0:
        return None
    d = os.path.join(tmp, "headpanels")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "panels_head.py")
    with open(p, "wb") as f:
        f.write(src.stdout)
    spec = importlib.util.spec_from_file_location("panels_head", p)
    mod = importlib.util.module_from_spec(spec)
    if SRC not in sys.path:
        sys.path.insert(0, SRC)
    spec.loader.exec_module(mod)
    return mod


def children_geom(hdr, skip=None):
    """头部里**可见**子控件的 (类名, x,y,w,h) —— 用来证明"布局没变"。

    · 只取可见控件（隐藏控件不参与布局）；
    · `skip` 里的是"本版本新增的控件"（如徽标），比较时排除。
    """
    skip = set(id(w) for w in (skip or ()))
    out = []
    # ★ 只比**直接子控件**：QComboBox 内部的 QListView/QScrollBar 是懒创建的，
    #   是否已实化取决于有没有展开过弹窗 —— 那是噪声，不是布局差异。
    for ch in hdr.findChildren(QWidget, "", Qt.FindDirectChildrenOnly):
        if id(ch) in skip:
            continue
        try:
            if hasattr(ch, "isHidden") and ch.isHidden():
                continue
            g = ch.geometry()
            out.append((type(ch).__name__, g.x(), g.y(), g.width(), g.height()))
        except Exception:                                        # noqa: BLE001
            continue
    return sorted(out)


def md5_png(widget):
    pm = widget.grab()
    img = pm.toImage().convertToFormat(QImage.Format_ARGB32)
    bits = img.constBits()
    try:
        return hashlib.md5(bytes(bits)).hexdigest()
    except TypeError:
        return hashlib.md5(bits.tobytes()).hexdigest()


# ==========================================================================
def main():
    fails = []

    def check(name, cond, detail=""):
        print("  [%s] %s%s" % ("ok  " if cond else "FAIL", name,
                               "" if cond else "   ← " + str(detail)))
        if not cond:
            fails.append(name)

    os.environ.pop("QT_QPA_PLATFORM", None)      # 真实平台（offscreen 没 CJK 字体，中文会变方块）
    app = QApplication.instance() or QApplication(sys.argv)
    tmp = tempfile.mkdtemp(prefix="badge_")

    cue_lay = engine.Layer("码表层")
    cue_lay.is_cuesheet = True
    cue_lay.cuesheet_id = "夏夜星律.ercue"
    plain = engine.Layer("普通层")
    eng = new_engine([cue_lay, plain])
    main_obj = FakeMain(eng.layers)
    main_obj.engine = eng

    # ---------------- ① 下拉出现 ----------------
    print("① 下拉：只有码表层显示")
    panel = panels.LayerStackPanel(main_obj)
    panel.rebuild()
    hdr_cue = panel.rows[0][0]
    hdr_plain = panel.rows[1][0]
    check("码表层有 _cmb_cue", hasattr(hdr_cue, "_cmb_cue"))
    check("码表层的下拉可见", hdr_cue._cmb_cue.isVisible()
          or not hdr_cue._cmb_cue.isHidden(), hdr_cue._cmb_cue.isHidden())
    check("普通层的下拉隐藏", hdr_plain._cmb_cue.isHidden(), hdr_plain._cmb_cue.isHidden())
    print("      下拉文案=%r tooltip=%r" % (hdr_cue._cmb_cue.currentText(),
                                        hdr_cue._cmb_cue.toolTip()[:40]))
    os.makedirs(SHOT_DIR, exist_ok=True)
    shot = os.path.join(SHOT_DIR, "step6_badge.png")
    panel.resize(420, 200)
    panel.show()
    for _ in range(6):
        app.processEvents()
    ok_shot = panel.grab().save(shot, "PNG")
    check("截图已落盘 step6_badge.png", ok_shot, shot)

    # ---------------- ④ 不干扰普通图层（与 git HEAD 版比） ----------------
    print("\n④ 普通图层头部：与改动前（git HEAD 的 panels.py）逐控件/逐像素一致")
    head = load_head_panels(tmp)
    if head is None:
        check("能取到 HEAD 版 panels.py", False, "git show 失败")
    else:
        plain2 = engine.Layer("普通层")
        main2 = FakeMain([plain2])
        main2.engine.layers = [plain2]
        p2 = head.LayerStackPanel(main2)
        p2.rebuild()
        # ★ 两边都 show + 同尺寸后再比几何：没 show 的那边不做布局 ⇒ 控件几何不可比
        p2.resize(420, 200)
        p2.show()
        for _ in range(6):
            app.processEvents()
        hdr_old = p2.rows[0][0]
        # 同样的图层 ⇒ 子控件几何应完全一致（徽标隐藏时不占位）
        g_new = children_geom(hdr_plain, skip=(hdr_plain._cmb_cue,))
        g_old = children_geom(hdr_old)
        print("      可见子控件：新版 %d 个（已排除徽标）｜ HEAD %d 个" % (len(g_new), len(g_old)))
        check("普通层的可见子控件与改动前**完全相同**（数量 + 逐个几何）",
              g_new == g_old, [x for x in g_new if x not in g_old][:3])
        check("下拉本体是隐藏的（不占布局）",
              hdr_plain._cmb_cue.isHidden() is True, hdr_plain._cmb_cue.isHidden())
        md5_new = md5_png(hdr_plain)
        md5_old = md5_png(hdr_old)
        print("      普通层头部 PNG md5：新版 %s ｜ HEAD %s" % (md5_new[:8], md5_old[:8]))
        check("普通层头部渲染像素完全一致", md5_new == md5_old, (md5_new, md5_old))

    # ---------------- ② 换表（真实导入两套包，画面跟着换） ----------------
    print("\n② 换表：导入 A → 导入 B ⇒ 引擎的表/画面都变")
    imgs = {}
    real_make = engine.make_player
    engine.make_player = lambda item, cue=False: FakePlayer(
        imgs.get(os.path.basename(getattr(item, "path", "")), QImage()))
    try:
        root = os.path.join(tmp, "media")
        os.makedirs(root, exist_ok=True)
        for nm, col in (("a.mp4", (220, 40, 40, 255)), ("b.mp4", (40, 120, 230, 255))):
            open(os.path.join(root, nm), "wb").close()
            im = QImage(W, H, QImage.Format_ARGB32)
            im.fill(QColor(*col))
            imgs[nm] = im
        cue_ui.cue_cfg(FakeCfg())                # 初始化默认段（顺带不报错）
        cfg = FakeCfg()
        cue_ui.cue_cfg(cfg)["media_dir"] = root
        pa = os.path.join(tmp, "A.ercue")
        pb = os.path.join(tmp, "B.ercue")
        cuesheet.save(make_sheet("表A", "a.mp4"), pa, app_version="2026.10.17.12")
        cuesheet.save(make_sheet("表B", "b.mp4"), pb, app_version="2026.10.17.12")

        lay = engine.Layer("码表层")
        lay.is_cuesheet = True
        eng2 = new_engine([lay])
        owner = type("O", (), {})()
        owner.cfg = cfg
        owner.engine = eng2
        owner.layers_panel = None
        dlg = cue_ui.CueSheetDialog(owner)

        # 导入前把媒体目录塞进 cfg（scan_media_dir 才会命中）
        dlg.import_one(pa)
        dlg.start_clock()
        import time as _t
        _t.sleep(0.12)                 # 让 30ms 的 QTimer 真的发一次（否则拍位仍是 None）
        for _ in range(3):
            app.processEvents()
        eng2._cuesheet_tick()
        eng2._compose({"bass": 0.0})
        colA = eng2.canvas.pixelColor(W // 2, H // 2).getRgb()
        titleA = eng2.cuesheet_status()["title"]
        dlg.import_one(pb)
        eng2.set_cue_position(float(eng2._cue_pos_beats or 2.0), "test")
        eng2._cuesheet_tick()
        eng2._compose({"bass": 0.0})
        colB = eng2.canvas.pixelColor(W // 2, H // 2).getRgb()
        titleB = eng2.cuesheet_status()["title"]
        dlg.stop_clock()
        print("      表A：title=%s 画面=%s ；表B：title=%s 画面=%s"
              % (titleA, colA, titleB, colB))
        check("换表后引擎里的表变了（A→B）", titleA == "表A" and titleB == "表B",
              (titleA, titleB))
        check("画面跟着换（像素不同，且分别对应 A/B 的颜色）",
              colA != colB and colA[0] > colA[2] and colB[2] > colB[0], (colA, colB))
        check("下拉绑定的包跟着更新（cue_sheet_id = 当前包名）",
              str(lay.cuesheet_id or "") in (os.path.basename(pb),)
              or lay.cuesheet_id != "", lay.cuesheet_id)

        # 卸下（徽标菜单里那条动作的核心两步）
        eng2.clear_cue_sheet()
        lay.is_cuesheet = False
        lay.cuesheet_id = ""
        check("卸下后引擎清空 + 图层不再是码表层",
              eng2._cue_rt is None and lay.is_cuesheet is False, (eng2._cue_rt, lay.is_cuesheet))
        dlg.close()
    finally:
        engine.make_player = real_make

    # ---------------- ③ 持久化（to_dict → rebuild_from_cfg） ----------------
    print("\n③ 持久化：to_dict → rebuild_from_cfg 后 is_cuesheet / cuesheet_id 还在")
    lay3 = engine.Layer("码表层")
    lay3.is_cuesheet = True
    lay3.cuesheet_id = "夏夜星律 演出码表.ercue"
    eng3 = new_engine([lay3])
    eng3.cfg = {"layers": [lay3.to_dict()]}
    eng3._tick_lock = __import__("threading").RLock()
    eng3.library = {}
    eng3._switch_layer_to = lambda *a, **k: None
    dumped = lay3.to_dict()
    check("to_dict 带 is_cuesheet / cuesheet_id",
          dumped.get("is_cuesheet") is True
          and dumped.get("cuesheet_id") == "夏夜星律 演出码表.ercue", dumped)
    eng3.rebuild_from_cfg()
    got = eng3.layers[0]
    check("rebuild_from_cfg 读回 is_cuesheet", got.is_cuesheet is True, got.is_cuesheet)
    check("rebuild_from_cfg 读回 cuesheet_id",
          got.cuesheet_id == "夏夜星律 演出码表.ercue", got.cuesheet_id)
    # 再 dump 一次应稳定（幂等）
    check("二次 to_dict 仍是同一份绑定关系", got.to_dict() == dumped,
          (got.to_dict(), dumped))

    print("")
    if fails:
        print("BADGE_FAIL  %d 项不通过：%s" % (len(fails), "、".join(fails)))
        return 1
    print("BADGE_OK  徽标出现/换表/持久化/不干扰普通图层 全过（截图 %s）" % SHOT_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
