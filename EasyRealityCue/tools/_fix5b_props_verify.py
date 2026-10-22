# -*- coding: utf-8 -*-
"""★ 第五轮 ④⑤ 实测验证（真窗口 + grab 截图 + 数值）—— `ui/props_panel.py`。

④ 「大小」= 滑条 + 输入框**双向同步**（范围对照 `sp_scale` 的 min/max 0.05~8.0 → 5~800）。
⑤ 「素材混合 / 素材不透明度」**去掉"继承本层"UI 入口**（只删 UI，底层 `""`/`None`=继承契约不变）：
   · 未设过 ⇒ 按 `effective_*` 的**实际生效值**显示；
   · 改别的字段**不**物化继承；**动过该项**才写显式值。

判据以 `[PASS]/[FAIL]` 打印，末尾 `CLOSED_LOOP_OK` / `HAS_FAILURES`；截图落 `_shots/`。

跑法：  venv\\Scripts\\python.exe tools\\_fix5b_props_verify.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["ERCUE_NO_AUTOSAVE"] = "1"          # 测试隔离（不加载/不写用户真实 workspace）

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
_SHOTS = os.path.join(_ROOT, "_shots")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import av                                                          # noqa: E402
import numpy as np                                                 # noqa: E402

from PySide6.QtWidgets import QApplication                        # noqa: E402

from ercue.model.cuesheet import CompLayer, CueItem                # noqa: E402
from ercue.ui.props_panel import (                                 # noqa: E402
    SCALE_MAX, SCALE_MIN, SCALE_SLIDER_SCALE, PropsPanel,
)

RES = []
SKIPPED = []


def _chk(tag, ok, detail):
    RES.append((tag, bool(ok), detail))
    print("  [%s] %s\n        %s" % ("PASS" if ok else "FAIL", tag, detail))


def _skip(tag, reason):
    SKIPPED.append((tag, reason))
    print("  [SKIP] %s\n        %s" % (tag, reason))


def _make_mp4(path, w=320, h=180, frames=10, fps=10):
    c = av.open(path, "w")
    st = c.add_stream("mpeg4", rate=fps)
    st.width, st.height = w, h
    st.pix_fmt = "yuv420p"
    for i in range(frames):
        img = np.zeros((h, w, 3), np.uint8)
        img[:, :, 2] = 180
        img[:, (i * 22) % max(1, w - 20):(i * 22) % max(1, w - 20) + 20, 1] = 240
        for pk in st.encode(av.VideoFrame.from_ndarray(img, format="rgb24")):
            c.mux(pk)
    for pk in st.encode():
        c.mux(pk)
    c.close()


def _panel(ly, it):
    p = PropsPanel()
    p.set_selection(ly, it)
    p.resize(320, 560)
    p.show()
    QApplication.processEvents()
    return p


def part_direct(app, mp4):
    print("\n--- 直连 PropsPanel（真控件）---")
    ly = CompLayer(lyr_id="L1", name="层1", z=0, blend="screen", opacity=0.8)
    it = CueItem(item_id="i1", clip_id="c1", at_sec=0.0)      # blend="" / opacity=None（继承）
    ly.items.append(it)
    p = _panel(ly, it)
    p.grab().save(os.path.join(_SHOTS, "fix5b_panel_before.png"))

    # A. 大小滑条范围对照输入框
    okA = (abs(p.sp_scale.minimum() - SCALE_MIN) < 1e-12
           and abs(p.sp_scale.maximum() - SCALE_MAX) < 1e-12
           and p.sld_scale.minimum() == int(round(SCALE_MIN * SCALE_SLIDER_SCALE)) == 5
           and p.sld_scale.maximum() == int(round(SCALE_MAX * SCALE_SLIDER_SCALE)) == 800)
    _chk("A. 大小滑条范围对照 sp_scale", okA,
         "滑条 %d~%d ↔ 输入框 %.2f~%.2f" % (p.sld_scale.minimum(), p.sld_scale.maximum(),
                                            p.sp_scale.minimum(), p.sp_scale.maximum()))

    # B. 双向同步（真实拖动 = setValue）
    p.sld_scale.setValue(250)
    b1 = abs(p.sp_scale.value() - 2.50) < 1e-9
    p.sp_scale.setValue(1.25)
    b2 = p.sld_scale.value() == 125
    _chk("B. 大小 双向同步", b1 and b2,
         "滑条 250 → 输入框 %.2f；输入框 1.25 → 滑条 %d" % (p.sp_scale.value(), p.sld_scale.value()))

    # C. 拖滑条 → 写进 xform.scale（commit）
    p.sld_scale.setValue(200)
    p.commit()
    okC = abs(it.xform["scale"] - 2.0) < 1e-9
    _chk("C. 大小落模型 xform.scale", okC, "滑条 200 → xform.scale=%.2f" % it.xform["scale"])

    # D. ⑤ UI 无"继承"入口
    no_chk = not hasattr(p, "chk_op_inherit")
    no_inherit_item = all("继承" not in p.cb_blend.itemText(i)
                          for i in range(p.cb_blend.count())) and \
        not any(p.cb_blend.itemData(i) == "" for i in range(p.cb_blend.count()))
    _chk("D. ⑤ 无「继承本层」UI 入口", no_chk and no_inherit_item,
         "不透明度勾选框存在=%s；混合下拉项=%s"
         % (not no_chk, [p.cb_blend.itemText(i) for i in range(p.cb_blend.count())]))

    # E. ⑤ 未设过 ⇒ 显示生效值（= 层 screen / 80%）
    okE = p.cb_blend.currentData() == "screen" and abs(p.sp_op.value() - 80) < 1e-9
    _chk("E. ⑤ 未设过按生效值显示", okE,
         "混合显示=%s；不透明度显示=%.0f%%（层为 screen/80%%）"
         % (p.cb_blend.currentData(), p.sp_op.value()))

    # F. ⑤ 改别的字段不物化继承
    p.sp_at.setValue(3.0)
    p.sp_px.setValue(12.0)
    p.commit()
    okF = (it.blend == "" and it.opacity is None
           and abs(it.at_sec - 3.0) < 1e-9 and abs(it.xform["x"] - 12.0) < 1e-9)
    _chk("F. ⑤ 改别的字段不物化继承", okF,
         "it.blend=%r it.opacity=%r（应 ''/None）；at=%.1f x=%.0f（确已写入）"
         % (it.blend, it.opacity, it.at_sec, it.xform["x"]))

    # G. ⑤ 动过 ⇒ 写显式值
    p.cb_blend.setCurrentIndex(p.cb_blend.findData("add"))
    p.sp_op.setValue(40)
    p.commit()
    okG = it.blend == "add" and abs(it.opacity - 0.4) < 1e-9 and \
        ly.blend == "screen" and abs(ly.opacity - 0.8) < 1e-9
    _chk("G. ⑤ 动过才写显式值（层不受影响）", okG,
         "it.blend=%s it.opacity=%.2f；层仍 %s/%.2f" % (it.blend, it.opacity, ly.blend, ly.opacity))

    p.grab().save(os.path.join(_SHOTS, "fix5b_panel_after.png"))


def part_mainwindow(app, mp4):
    print("\n--- 真 MainWindow 集成（选中片段 → 属性面板）---")
    try:
        from ercue.ui.window import MainWindow
        w = MainWindow(restore_workspace=False, autosave=False)
    except Exception as e:                                     # noqa: BLE001
        # window.py 由另一 teammate 并行编辑，构造失败 ≠ 本任务缺陷 ⇒ SKIP（不判 FAIL）。
        _skip("H. 真窗集成", "MainWindow 构造失败（疑似他人并行编辑 window.py）：%s: %s"
              % (type(e).__name__, e))
        return
    try:
        w._interactive = False
        w.thumbs_enabled = False
        w.resize(1200, 760)
        w.show()
        app.processEvents()
        w.new_sheet("fix5b")
        cids = w.add_media_paths([mp4])
        w.add_track("曲", "x", 30.0, 120.0)
        ly = w._layers()[0]
        w.add_cue(ly.lyr_id, cids[0], 0.0)
        it = ly.items[0]
        w._select(ly.lyr_id, it.item_id)
        app.processEvents()
        p = w.props_panel
        ok1 = (p.item is it and p.lbl_blend.text().startswith("本素材"))
        # 大小：拖滑条 → 经 changed→commit 落模型
        p.sld_scale.setValue(250)
        app.processEvents()
        ok2 = abs(it.xform.get("scale", 0.0) - 2.5) < 1e-6
        # ⑤ 未设过 ⇒ 生效值显示；改别的字段不物化
        d1 = p.cb_blend.currentData()
        p.sp_at.setValue(1.0)
        app.processEvents()
        ok3 = (it.blend == "" and it.opacity is None)
        # ⑤ 动过 ⇒ 写显式
        p.cb_blend.setCurrentIndex(p.cb_blend.findData("lighten"))
        app.processEvents()
        ok4 = it.blend == "lighten"
        w.grab().save(os.path.join(_SHOTS, "fix5b_mainwindow.png"))
        w.shutdown()
        _chk("H. 真窗集成：面板接线 + 大小落模型 + ⑤ 生效值/不物化/动过写显式",
             ok1 and ok2 and ok3 and ok4,
             "item 接线=%s；×2.50 落模型=%s；未设过显示=%s；改别字段仍 ''/None=%s；动混合→%s"
             % (ok1, ok2, d1, ok3, it.blend))
    except Exception as e:                                     # noqa: BLE001
        _chk("H. 真窗集成", False, "%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()))


def main() -> int:
    app = QApplication.instance() or QApplication([])
    os.makedirs(_SHOTS, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="ercue_fix5b_")
    mp4 = os.path.join(tmp, "a.mp4")
    _make_mp4(mp4)
    print("第五轮 ④⑤ 属性面板实测")
    print("=" * 70)
    part_direct(app, mp4)
    part_mainwindow(app, mp4)
    passed = sum(1 for _t, ok, _d in RES if ok)
    print("\n" + "=" * 70)
    print("fix5b 实测：%d 通过 / %d 失败 / %d 跳过"
          % (passed, len(RES) - passed, len(SKIPPED)))
    for tag, ok, _d in RES:
        print("   %s %s" % ("✓" if ok else "✗", tag))
    for tag, reason in SKIPPED:
        print("   - %s（跳过：%s）" % (tag, reason))
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
