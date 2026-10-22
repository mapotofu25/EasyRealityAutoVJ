# -*- coding: utf-8 -*-
"""★ 不变量：**模型坐标是秒 ⇒ 改 BPM 不应该移动任何片段**（外加 BPM 文案口径）。

为什么这条比文案重要：
    编排端模型内部是**秒**（`CueItem.at_sec` 权威），导出 `at_beats = at_sec × bpm/60`；
    演出端算位置 `pos_beats = offset_sec / beat_len`，选片段 `(pos_beats − at_beats) × beat_len`
    ⇒ 比较回的是**秒差**（同一个 bpm 系数互相抵消）。
    ⇒ BPM 填错 / 留空 / 一首歌多段 BPM，**都不会让画面对不上**；它只影响「按小节查看/对齐」。
    ⇒ 所以「改 BPM」绝不能顺手按比例缩放 `at_sec`（看起来"更自然"，其实会把用户排好的
      时间点整体挪走，并且与演出端的秒差比较二次错位）。

跑法：<Cue>\\venv\\Scripts\\python.exe tests\\test_bpm_invariant.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["ERCUE_NO_AUTOSAVE"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from ercue.model import ercue_io, units                            # noqa: E402
from ercue.model.cuesheet import (CompLayer, CueItem, CueSheet,    # noqa: E402
                                  Identity, TrackEntry)

SECS = (30.0, 60.0, 90.0)


def _sheet(bpm=0.0):
    """造一张码表：1 首曲目 / 1 层 / 3 个片段（at_sec = 30 / 60 / 90）。"""
    s = CueSheet(title="BPM 不变量")
    te = TrackEntry(track_key="k1", identity=Identity(title="T", artist="A",
                                                      duration_sec=180.0, bpm=bpm))
    ly = CompLayer(lyr_id="L1", name="层1", z=0)
    for i, sec in enumerate(SECS):
        ly.items.append(CueItem(item_id="i%d" % i, clip_id="c1", at_sec=sec).normalized())
    te.composition.layers.append(ly)
    s.tracks.append(te)
    return s


def _secs(sheet):
    return [float(it.at_sec) for it in sheet.tracks[0].layers[0].items]


def _assert_same(got, want, tag):
    assert len(got) == len(want), (tag, got, want)
    for a, b in zip(got, want):
        assert abs(a - b) < 1e-9, ("%s：片段被移动了" % tag, a, b, got)


def _guard_no_item_moved(before, sheet, tag):
    """不变量守卫：改造 `before` 快照与当前 `at_sec` 必须逐值相同（否则抛）。"""
    _assert_same(_secs(sheet), before, tag)
    return True


# ---------------------------------------------------------------- 1
def test_model_bpm_change_keeps_seconds():
    """① 纯模型：把 bpm 从 0 改成 150（再 174.5、再回 0），三个 at_sec 一个都不许变。"""
    s = _sheet(bpm=0.0)
    before = _secs(s)
    _assert_same(before, list(SECS), "初始")
    for bpm in (150.0, 174.5, 0.0):
        s.tracks[0].identity.bpm = bpm
        _guard_no_item_moved(before, s, "改成 %.1f BPM" % bpm)
    print("  ✓ 模型层：bpm 0→150→174.5→0，三个 at_sec 始终是 %s" % (before,))


# ---------------------------------------------------------------- 2
def test_ui_bpm_edit_keeps_seconds():
    """② 真实编辑路径：面板上改 BPM → `_on_track_edited()` → at_sec 不动。"""
    from PySide6.QtWidgets import QApplication
    from ercue.ui.window import MainWindow
    app = QApplication.instance() or QApplication([])             # noqa: F841
    win = MainWindow()
    win._interactive = False
    win.thumbs_enabled = False
    try:
        win.new_sheet("UI BPM 不变量")
        idx = win.add_track("T", "A", 180.0, 0.0)
        win._ti = max(0, int(idx))
        ly = win._layers()[0]
        for i, sec in enumerate(SECS):
            ly.items.append(CueItem(item_id="i%d" % i, clip_id="c1", at_sec=sec).normalized())
        before = _secs(win._sheet())

        win.track_panel.sp_bpm.setValue(150.0)
        win._on_track_edited()
        assert abs(float(win._track().identity.bpm) - 150.0) < 1e-9, win._track().identity.bpm
        _guard_no_item_moved(before, win._sheet(), "UI 改成 150")
        print("  ✓ UI 编辑路径：面板改 150 BPM，at_sec 仍为 %s" % (before,))
    finally:
        try:
            win.shutdown()
        except Exception:                                        # noqa: BLE001
            pass


# ---------------------------------------------------------------- 3
def test_export_roundtrip_keeps_seconds():
    """③ 导出 → 读回：`at_beats` 与 `beats_to_sec` 互逆 ⇒ at_sec 不变（0 / 150 / 155.03）。"""
    tmp = tempfile.mkdtemp(prefix="ercue_bpm_inv_")
    for bpm in (0.0, 150.0, 155.03):
        s = _sheet(bpm=bpm)
        before = _secs(s)
        p = os.path.join(tmp, "b%.2f.ercue" % bpm)
        ercue_io.save(s, p, app_version="2026.10.18.01")
        back, _info = ercue_io.load(p)
        _guard_no_item_moved(before, back, "bpm=%.2f 往返" % bpm)
        # 拍坐标也必须自洽：at_beats == units.sec_to_beats(at_sec, bpm)
        fd = ercue_io.sheet_to_file_dict(back)
        items = fd["tracks"][0]["composition"]["layers"][0]["items"]
        for it, sec in zip(items, before):
            want = units.sec_to_beats(sec, bpm)
            assert abs(float(it["at_beats"]) - want) < 1e-9, (bpm, it["at_beats"], want)
        note = "（bpm=0 退化：at_beats == at_sec）" if bpm <= 0 else ""
        print("  ✓ bpm=%-7.2f 往返后 at_sec 不变%s" % (bpm, note))


# ---------------------------------------------------------------- 4
def test_reverse_wrong_scaling_is_caught():
    """④ 反向复现：如果有人"顺手"按比例缩放 at_sec，守卫必须抓住（证明 ①②③ 不是空断言）。"""
    s = _sheet(bpm=0.0)
    before = _secs(s)
    # 模拟那个"看起来更自然"的错误实现：改 BPM 时把秒坐标按比例缩放
    k = 150.0 / 100.0
    for it in s.tracks[0].layers[0].items:
        it.at_sec = float(it.at_sec) * k
    caught = False
    try:
        _guard_no_item_moved(before, s, "错误实现（按比例缩放 at_sec）")
    except AssertionError as e:
        caught = True
        print("  ✓ 守卫抓住了错误实现：%s" % (e,))
    assert caught, "守卫没能发现 at_sec 被缩放 —— 不变量断言是空的！"
    print("  ✓ 反向复现成立：缩放式实现会被这条断言拦住")


# ---------------------------------------------------------------- 5
def test_bpm_missing_is_not_fatal_and_wording():
    """⑤ 没填 BPM 不是错误：`bpm_warn_text` 只提示、`_warn_missing_bpm` 永不阻止导出。"""
    from ercue.ui import window as win_mod
    from PySide6.QtWidgets import QApplication
    from ercue.ui.window import MainWindow

    assert win_mod.bpm_warn_text([]) == "", "空列表应返回空串"
    bad = [("Nimbus Spark 1999", "Supa7onyz"), ("揽月", "")]
    text = win_mod.bpm_warn_text(bad)
    assert "不影响演出" in text, text
    for banned in ("不对拍", "仍要导出吗", "会废"):
        assert banned not in text, ("提示里不该出现吓人/错误的措辞：%r" % banned, text)
    assert "按小节" in text, text
    print("  ✓ bpm_warn_text 口径正确（不影响演出 / 只是没法按小节对齐）")

    # 判据不变：bpm<=0 与 bpm=1e-9 都算"没填"
    s = _sheet(bpm=0.0)
    assert win_mod.tracks_without_bpm(s) == [("T", "A")], win_mod.tracks_without_bpm(s)
    s.tracks[0].identity.bpm = 1e-9
    assert win_mod.tracks_without_bpm(s) == [("T", "A")], "极小正数仍走退化态"
    s.tracks[0].identity.bpm = 140.0
    assert win_mod.tracks_without_bpm(s) == [], "正常 BPM 不该被列出"
    print("  ✓ tracks_without_bpm 判据未变（bpm<=0 与 1e-9 都算没填，140 不算）")

    app = QApplication.instance() or QApplication([])             # noqa: F841
    win = MainWindow()
    win._interactive = False                                     # 无头：只记日志
    win.thumbs_enabled = False
    try:
        win.new_sheet("没填 BPM")
        win.add_track("T", "A", 180.0, 0.0)
        assert win._warn_missing_bpm() is True, "无头模式下必须继续导出"
        # 无头下导出也必须能成功（BPM 为空不再阻止导出）
        p = os.path.join(tempfile.mkdtemp(prefix="ercue_bpm_exp_"), "no_bpm.ercue")
        assert win.export_sheet(p, force=False, embed=False) is True, "缺 BPM 不该阻止导出"
        back, _info = ercue_io.load(p)
        assert abs(float(back.tracks[0].identity.bpm)) < 1e-9, back.tracks[0].identity.bpm
        print("  ✓ 缺 BPM 仍能导出成功（identity.bpm 存 0.0）")
        # 面板文案也不能再吓唬人
        tip = win.track_panel.sp_bpm.toolTip()
        assert "不影响演出" in tip and "不会对拍" not in tip, tip
        # ★ 用户可见的 Qt 文案里**不能残留 Markdown 星号**（QLabel/tooltip 只认 HTML，
        #   会原样显示 `**`——上一轮说明文字就踩过这个坑）
        assert "**" not in tip, ("BPM tooltip 里有字面 Markdown 星号", tip)
        src_tip = win._bpm_source_text(145.0, "audio",
                                       {"peaks": [(145.0, 3.4), (160.0, 3.0)], "score": 3.4})
        assert "**" not in src_tip, ("来源说明里有字面 Markdown 星号", src_tip)
        assert "<b>" in src_tip, src_tip
        print("  ✓ sp_bpm tooltip 口径正确（且无字面 Markdown 星号）")
    finally:
        try:
            win.shutdown()
        except Exception:                                        # noqa: BLE001
            pass


def main() -> int:
    tests = [
        ("改 BPM 不移动片段（模型层）", test_model_bpm_change_keeps_seconds),
        ("改 BPM 不移动片段（UI 编辑路径）", test_ui_bpm_edit_keeps_seconds),
        ("导出/读回保持秒坐标", test_export_roundtrip_keeps_seconds),
        ("反向复现：缩放式实现会被抓住", test_reverse_wrong_scaling_is_caught),
        ("没填 BPM 不致命 + 文案口径", test_bpm_missing_is_not_fatal_and_wording),
    ]
    ok = True
    passed = 0
    for name, fn in tests:
        print("\n--- %s ---" % name)
        try:
            fn()
            passed += 1
        except Exception as e:                                   # noqa: BLE001
            ok = False
            print("  ✗ %s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()))
    print("\n" + "=" * 68)
    print("结果：%d 通过 / %d 失败" % (passed, len(tests) - passed))
    print("汇总：%s" % ("全部通过 ✓" if ok else "有失败 ✗"))
    print("=" * 68)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
