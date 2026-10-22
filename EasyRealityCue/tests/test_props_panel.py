# -*- coding: utf-8 -*-
"""T05 + 第五轮④⑤ · 片段属性面板。

覆盖：
  需求 7（变速）：滑条 + 输入框双向同步；范围对照输入框 min/max（0.1~3.0 → 10~300）。
  **第五轮④（大小）**：同样"滑条 + 输入框双向同步"；范围对照 `sp_scale`（0.05~8.0 → 5~800）。
  **第五轮⑤（去继承 UI）**：混合下拉不再有「（继承本层）」、不透明度无勾选框；
  底层 `item.blend==""` / `item.opacity is None` = 继承本层 的**数据契约不变**：
    · 打开面板按 `effective_blend/effective_opacity` 的**实际生效值**显示；
    · **只有用户真正动了该项**才写显式值；改别的字段**不**物化继承（老数据不改坏）。
  通用：防自激（一次操作恰好 1 次 `changed`）；B2 守卫（置位在动控件之前、≥2 处复位、
        回填不发信号、守卫期间 `commit()` 拒绝写回）；选中态语义（选中写 item / 未选写 layer）。

跑法（无头）：
    set QT_QPA_PLATFORM=offscreen
    <Cue>\\venv\\Scripts\\python.exe tests\\test_props_panel.py
"""

from __future__ import annotations

import os
import sys
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from PySide6.QtWidgets import QApplication                            # noqa: E402

from ercue.model.cuesheet import CompLayer, CueItem                    # noqa: E402
from ercue.ui.props_panel import (                                     # noqa: E402
    LBL_BLEND_ITEM, LBL_BLEND_LAYER, LBL_OP_ITEM, LBL_OP_LAYER,
    SCALE_MAX, SCALE_MIN, SCALE_SLIDER_SCALE,
    SPEED_MAX, SPEED_MIN, SPEED_SLIDER_SCALE, PropsPanel,
)

APP = None


def _panel(layer, item):
    p = PropsPanel()
    p.set_selection(layer, item)
    return p


def _item_with_layer(blend="normal", opacity=1.0):
    ly = CompLayer(lyr_id="L1", name="层1", z=0, blend=blend, opacity=opacity)
    it = CueItem(item_id="i1", clip_id="c1", at_sec=0.0)
    ly.items.append(it)
    return ly, it


# ==========================================================================
# 1. 滑条范围对照输入框 min/max（变速 + 大小）
# ==========================================================================
def test_slider_range_matches_spin():
    p = PropsPanel()
    assert abs(p.sp_speed.minimum() - SPEED_MIN) < 1e-12, p.sp_speed.minimum()
    assert abs(p.sp_speed.maximum() - SPEED_MAX) < 1e-12, p.sp_speed.maximum()
    assert p.sld_speed.minimum() == int(round(p.sp_speed.minimum() * SPEED_SLIDER_SCALE)) == 10
    assert p.sld_speed.maximum() == int(round(p.sp_speed.maximum() * SPEED_SLIDER_SCALE)) == 300
    print("  ✓ 变速滑条 %d~%d 对照输入框 %.2f~%.2f"
          % (p.sld_speed.minimum(), p.sld_speed.maximum(),
             p.sp_speed.minimum(), p.sp_speed.maximum()))


def test_scale_slider_range_matches_spin():
    p = PropsPanel()
    assert abs(p.sp_scale.minimum() - SCALE_MIN) < 1e-12, p.sp_scale.minimum()
    assert abs(p.sp_scale.maximum() - SCALE_MAX) < 1e-12, p.sp_scale.maximum()
    # 派生自输入框 min/max（×100），不是独立魔数
    assert p.sld_scale.minimum() == int(round(p.sp_scale.minimum() * SCALE_SLIDER_SCALE)) == 5
    assert p.sld_scale.maximum() == int(round(p.sp_scale.maximum() * SCALE_SLIDER_SCALE)) == 800
    print("  ✓ 大小滑条 %d~%d 对照输入框 %.2f~%.2f（×%d）"
          % (p.sld_scale.minimum(), p.sld_scale.maximum(),
             p.sp_scale.minimum(), p.sp_scale.maximum(), SCALE_SLIDER_SCALE))


# ==========================================================================
# 2. 双向同步（变速 + 大小）
# ==========================================================================
def test_bidirectional_sync():
    ly, it = _item_with_layer()
    p = _panel(ly, it)
    p.sld_speed.setValue(250)
    assert abs(p.sp_speed.value() - 2.50) < 1e-9, p.sp_speed.value()
    p.sld_speed.setValue(35)
    assert abs(p.sp_speed.value() - 0.35) < 1e-9, p.sp_speed.value()
    p.sp_speed.setValue(2.1)
    assert p.sld_speed.value() == 210, p.sld_speed.value()
    p.sp_speed.setValue(0.15)
    assert p.sld_speed.value() == 15, p.sld_speed.value()
    print("  ✓ 变速双向同步：滑条 250→×2.50/35→×0.35；输入框 ×2.1→210/×0.15→15")


def test_scale_bidirectional_sync():
    ly, it = _item_with_layer()
    p = _panel(ly, it)
    # 滑条 → 输入框
    p.sld_scale.setValue(250)
    assert abs(p.sp_scale.value() - 2.50) < 1e-9, p.sp_scale.value()
    p.sld_scale.setValue(50)
    assert abs(p.sp_scale.value() - 0.50) < 1e-9, p.sp_scale.value()
    # 输入框 → 滑条
    p.sp_scale.setValue(1.25)
    assert p.sld_scale.value() == 125, p.sld_scale.value()
    p.sp_scale.setValue(8.0)
    assert p.sld_scale.value() == 800, p.sld_scale.value()
    print("  ✓ 大小双向同步：滑条 250→2.50/50→0.50；输入框 1.25→125/8.00→800")


# ==========================================================================
# 3. 防自激：一次用户操作 = 恰好 1 次 changed
# ==========================================================================
def test_no_signal_storm():
    ly, it = _item_with_layer()
    p = _panel(ly, it)
    hits = []
    p.changed.connect(lambda: hits.append(1))

    for tag, fn, _expect in (
            ("变速·拖滑条", lambda: p.sld_speed.setValue(180), 1.8),
            ("变速·改输入框", lambda: p.sp_speed.setValue(1.2), None),
            ("大小·拖滑条", lambda: p.sld_scale.setValue(260), 2.6),
            ("大小·改输入框", lambda: p.sp_scale.setValue(0.9), None)):
        hits.clear()
        fn()
        assert len(hits) == 1, "%s 应恰好发 1 次 changed，实际 %d" % (tag, len(hits))
    assert abs(p.sp_speed.value() - 1.2) < 1e-9 and p.sld_speed.value() == 120
    assert abs(p.sp_scale.value() - 0.9) < 1e-9 and p.sld_scale.value() == 90

    hits.clear()
    p.sld_speed.setValue(90)
    p.sld_speed.setValue(90)                      # 同值重复设 → Qt 不发信号
    assert len(hits) == 1, "重复设同一值不应再发信号：%d" % len(hits)
    print("  ✓ 防自激：变速/大小 拖滑条/改输入框 各恰好 1 次 changed；同值重复设不再发信号")


# ==========================================================================
# 4. B2 守卫：≥2 处复位；回填不发信号；守卫期间 commit 拒绝
# ==========================================================================
def test_guard_reset_and_no_storm_on_refresh():
    ly, it = _item_with_layer()
    it.speed = 1.7
    it.xform = {"scale": 2.5, "x": 0.0, "y": 0.0}
    p = _panel(ly, it)
    hits = []
    p.changed.connect(lambda: hits.append(1))
    n0 = p.commit_calls
    hits.clear()
    for _ in range(50):
        p.refresh()
    assert p._loading is False, "refresh() 后守卫必须复位"
    assert len(hits) == 0, "回填期间不该发 changed（发了 %d 次）" % len(hits)
    assert p.commit_calls == n0, "回填期间 commit 不该被调用"
    # 回填后滑条/输入框都按权威值对齐（变速 + 大小）
    assert p.sld_speed.value() == 170 and abs(p.sp_speed.value() - 1.7) < 1e-9
    assert p.sld_scale.value() == 250 and abs(p.sp_scale.value() - 2.5) < 1e-9
    p._loading = True
    try:
        assert p.commit() is False
    finally:
        p._loading = False
    print("  ✓ B2：refresh()×50 发 0 changed、守复位；守卫期间 commit=False；滑条/框对齐")


# ==========================================================================
# 5. 第五轮⑤：UI 无"继承"入口 + 显示生效值 + 不物化继承 + 动过才写显式
# ==========================================================================
def test_inherit_ui_removed_and_effective_display():
    ly, it = _item_with_layer(blend="screen", opacity=0.8)     # it: blend="" opacity=None（继承）
    p = _panel(ly, it)
    # (A) UI 里**没有**"继承"入口
    assert not hasattr(p, "chk_op_inherit"), "不透明度勾选框应已删除"
    assert all("继承" not in p.cb_blend.itemText(i) for i in range(p.cb_blend.count())), \
        "混合下拉不应再含「（继承本层）」"
    assert not any(p.cb_blend.itemData(i) == "" for i in range(p.cb_blend.count())), \
        "混合下拉不应再有 data=='' 的继承项"
    # (B) 未设过 ⇒ 显示**实际生效值**（= 层值）
    assert p.cb_blend.currentData() == "screen", p.cb_blend.currentData()
    assert abs(p.sp_op.value() - 80) < 1e-9, p.sp_op.value()
    print("  ✓ ⑤ UI：无「继承」入口；未设过按生效值显示（混合=%s / 不透明度=%.0f%%）"
          % (p.cb_blend.currentData(), p.sp_op.value()))


def test_editing_other_fields_does_not_materialize_inherit():
    ly, it = _item_with_layer(blend="darken", opacity=0.5)
    p = _panel(ly, it)
    # 改一堆**别的**字段 + commit ⇒ 继承态（""/None）必须原封不动
    p.sp_at.setValue(1.0)
    p.sp_len.setValue(2.0)
    p.sp_speed.setValue(1.5)
    p.sld_scale.setValue(150)
    p.sp_px.setValue(10.0)
    p.sp_crop[0].setValue(0.1)
    assert p.commit() is True
    assert it.blend == "" and it.opacity is None, \
        "改别的字段不得物化继承：blend=%r opacity=%r" % (it.blend, it.opacity)
    # 老数据契约：状态快照往返仍是 ""/None
    d = it.to_state_dict()
    assert d["blend"] == "" and d["opacity"] is None, d
    back = CueItem.from_state_dict(d)
    assert back.blend == "" and back.opacity is None
    assert abs(it.xform["scale"] - 1.5) < 1e-9, it.xform     # 大小确实写进去了
    print("  ✓ ⑤ 只改别的字段：继承态 ''/None 原封不动；快照往返仍是继承；大小已写 1.5")


def test_touching_blend_or_opacity_writes_explicit():
    ly, it = _item_with_layer(blend="screen", opacity=0.8)
    p = _panel(ly, it)
    # 用户动混合 ⇒ 写显式值（层不受影响）
    p.cb_blend.setCurrentIndex(p.cb_blend.findData("add"))
    assert p.commit() is True
    assert it.blend == "add", it.blend
    # 用户动不透明度 ⇒ 写显式值
    p.sp_op.setValue(40)
    assert p.commit() is True
    assert abs(it.opacity - 0.4) < 1e-9, it.opacity
    assert ly.blend == "screen" and abs(ly.opacity - 0.8) < 1e-9, "选中片段时层不该被改"
    print("  ✓ ⑤ 动过才写显式：混合→%s、不透明度→%.2f；层不受影响" % (it.blend, it.opacity))


# ==========================================================================
# 6. 选中态语义（选中写 item / 未选写 layer；标签切换）
# ==========================================================================
def test_selection_semantics_item_vs_layer():
    ly, it = _item_with_layer(blend="screen", opacity=0.8)
    p = _panel(ly, it)

    assert p.lbl_blend.text() == LBL_BLEND_ITEM, p.lbl_blend.text()
    assert p.lbl_op.text() == LBL_OP_ITEM, p.lbl_op.text()

    # (D) 未选中：标签 = 本层…；写层，片段不受影响
    p.set_selection(ly, None)
    p.refresh()
    assert p.lbl_blend.text() == LBL_BLEND_LAYER, p.lbl_blend.text()
    assert p.lbl_op.text() == LBL_OP_LAYER, p.lbl_op.text()
    assert p.cb_blend.currentData() == "screen"
    p.cb_blend.setCurrentIndex(p.cb_blend.findData("multiply"))
    p.sp_op.setValue(30)
    assert p.commit() is True
    assert ly.blend == "multiply" and abs(ly.opacity - 0.3) < 1e-9, (ly.blend, ly.opacity)
    assert it.blend == "" and it.opacity is None, "未选中时**片段**不该被改"

    # 回到选中：标签切回「本素材…」
    p.set_selection(ly, it)
    p.refresh()
    assert p.lbl_blend.text() == LBL_BLEND_ITEM and p.lbl_op.text() == LBL_OP_ITEM
    print("  ✓ 选中态语义：选中写 item（层不动）/ 未选写 layer（片段不动）；标签切换正确")


# ==========================================================================
# 7. 变速 / 大小 落模型（纯 UI 之上不变）
# ==========================================================================
def test_speed_commits_to_item():
    ly, it = _item_with_layer()
    p = _panel(ly, it)
    p.sld_speed.setValue(275)
    assert p.commit() is True
    assert abs(it.speed - 2.75) < 1e-9, it.speed
    v = it.speed
    p.sp_speed.setValue(3.0)
    assert p.commit() is True and abs(it.speed - 3.0) < 1e-9
    print("  ✓ 变速落模型：滑条 275 → it.speed=%.2f；边界 ×3.00 正常" % v)


def test_scale_commits_to_item():
    ly, it = _item_with_layer()
    p = _panel(ly, it)
    p.sld_scale.setValue(250)
    assert p.commit() is True
    assert abs(it.xform["scale"] - 2.5) < 1e-9, it.xform
    sc = it.xform["scale"]
    p.sp_scale.setValue(0.5)
    assert p.commit() is True and abs(it.xform["scale"] - 0.5) < 1e-9
    print("  ✓ 大小落模型：滑条 250 → xform.scale=%.2f；输入框 0.5 → 0.50" % sc)


# ==========================================================================
def main() -> int:
    global APP
    APP = QApplication.instance() or QApplication([])
    tests = [
        ("变速滑条范围对照输入框", test_slider_range_matches_spin),
        ("大小滑条范围对照输入框", test_scale_slider_range_matches_spin),
        ("变速双向同步", test_bidirectional_sync),
        ("大小双向同步", test_scale_bidirectional_sync),
        ("防自激（信号风暴）", test_no_signal_storm),
        ("B2 守卫复位/回填不发信号", test_guard_reset_and_no_storm_on_refresh),
        ("⑤ UI 无继承入口 + 生效值显示", test_inherit_ui_removed_and_effective_display),
        ("⑤ 改别的字段不物化继承", test_editing_other_fields_does_not_materialize_inherit),
        ("⑤ 动过才写显式值", test_touching_blend_or_opacity_writes_explicit),
        ("选中态语义（item vs layer）", test_selection_semantics_item_vs_layer),
        ("变速落模型", test_speed_commits_to_item),
        ("大小落模型", test_scale_commits_to_item),
    ]
    ok, passed = True, 0
    for name, fn in tests:
        print("\n--- %s ---" % name)
        try:
            fn()
            passed += 1
        except Exception as e:                                  # noqa: BLE001
            ok = False
            print("  ✗ %s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()))
    print("\n" + "=" * 68)
    print("结果：%d 通过 / %d 失败" % (passed, len(tests) - passed))
    print("汇总：%s" % ("全部通过 ✓" if ok else "有失败 ✗"))
    print("=" * 68)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
