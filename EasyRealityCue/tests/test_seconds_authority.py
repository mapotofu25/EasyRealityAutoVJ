# -*- coding: utf-8 -*-
"""★ 读包时**时间以「秒」为权威**（`at_sec`），拍（`at_beats`）只是派生 —— 这是"按指纹对齐"的落点。

为什么（等式）：
    演出端位置：`pos_beats = offset_sec / beat_len`（`offset_sec` = 指纹算出的原曲秒数）
    片段坐标  ：`at_beats  = at_sec    / beat_len`（读包时由秒反推）
    触发判据  ：`at_beats <= pos_beats` ⟺ `at_sec <= offset_sec`
⇒ 以秒为权威后 **bpm 在比较里结构性抵消**：填错 / 留空 / 手改包里的 bpm / 一首歌多段变速，
  都影响不到"什么时候触发"。反之若信任 `at_beats`，只要 bpm 与写包时不一致，整张表的
  秒坐标会被整体缩放 —— 那才是真会错位的情形。

跑法：<Cue>\\venv\\Scripts\\python.exe tests\\test_seconds_authority.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from ercue.model import ercue_io, units                             # noqa: E402
from ercue.model.cuesheet import (CompLayer, CueItem, CueSheet,     # noqa: E402
                                  Identity, TrackEntry)

BPM_LIST = (0.0, 120.0, 999.0)


def _filedict(at_sec=50.0, at_beats=250.0, bpm=150.0,
              include_at_sec=True, include_end=False, end_sec=None):
    """造一份「`at_beats` 与 `at_sec` 故意不自洽」的包内容（模拟手改/第三方生成）。"""
    item = {"item_id": "i1", "clip_id": "c1", "at_beats": at_beats, "at_sec": at_sec,
            "end_beats": None, "src_in_sec": 0.0, "src_out_sec": None, "speed": 1.0,
            "crop": None, "xform": {"scale": 1.0, "x": 0.0, "y": 0.0},
            "trans_in": "fade", "trans_in_beats": 0.0,
            "trans_out": "fade", "trans_out_beats": 0.0,
            "snap": "off", "note": ""}
    if not include_at_sec:
        item.pop("at_sec", None)
    if include_end:
        item["end_sec"] = end_sec if end_sec is not None else at_sec + 10.0
    return {"format_version": 1, "title": "秒权威", "notes": "",
            "media": [{"clip_id": "c1", "name": "a.mp4"}],
            "tracks": [{"track_key": "k", "identity": {"bpm": bpm, "title": "T",
                                                      "artist": "A", "duration_sec": 180.0},
                        "composition": {"layers": [{"lyr_id": "L1", "name": "层1", "z": 0,
                                                    "items": [item]}]}}]}


def _read_item(fd):
    s = ercue_io.sheet_from_file_dict(fd)
    return s.tracks[0].layers[0].items[0]


# ---------------------------------------------------------------- 1
def test_at_sec_wins_over_at_beats():
    """① `at_beats` 故意写错（250）而 `at_sec=50` ⇒ 读回必须是 **50**；换个 bpm 也不许变。"""
    base = _read_item(_filedict(at_sec=50.0, at_beats=250.0, bpm=150.0))
    assert abs(base.at_sec - 50.0) < 1e-9, ("应信任 at_sec=50", base.at_sec)
    for bpm in BPM_LIST:
        it = _read_item(_filedict(at_sec=50.0, at_beats=250.0, bpm=bpm))
        assert abs(it.at_sec - 50.0) < 1e-9, ("改 bpm=%s 后 at_sec 变了" % bpm, it.at_sec)
    print("  ✓ at_beats=250 / at_sec=50 ⇒ 读回 50.0；bpm 0 / 120 / 999 三次都不变")


# ---------------------------------------------------------------- 2
def test_legacy_fallback_without_at_sec():
    """② 兼容：**删掉 `at_sec` 键**（老包）⇒ 回落 `at_beats` 换算，值与改动前逐值一致。"""
    for bpm in (0.0, 150.0, 155.03):
        fd = _filedict(at_sec=50.0, at_beats=250.0, bpm=bpm, include_at_sec=False)
        it = _read_item(fd)
        want = units.beats_to_sec(250.0, bpm)          # 旧口径（= 改动前的行为）
        assert abs(it.at_sec - want) < 1e-9, (bpm, it.at_sec, want)
    print("  ✓ 缺 at_sec 时回落 at_beats 换算（bpm 0/150/155.03 与旧口径逐值一致）")


# ---------------------------------------------------------------- 3
def test_zero_sec_is_respected():
    """③ `at_sec == 0.0` 是合法坐标，**不能当成"缺字段"**（判"有没有"看键不看值）。"""
    it = _read_item(_filedict(at_sec=0.0, at_beats=999.0, bpm=150.0))
    assert abs(it.at_sec) < 1e-12, ("at_sec=0 应被尊重，不该回落 at_beats=999", it.at_sec)
    print("  ✓ at_sec=0.0 被尊重（没有回落成 at_beats=999）")


# ---------------------------------------------------------------- 4
def test_reverse_trusting_beats_fails():
    """④ 反向复现：把判据改回"信任 at_beats" ⇒ ① 必须挂（证明 ① 不是空断言）。"""
    fd = _filedict(at_sec=50.0, at_beats=250.0, bpm=150.0)
    real = ercue_io._item_from_file

    def _legacy_trust_beats(d, bpm):
        """旧口径垫片：照旧用 `at_beats` 反算秒（= 忽略 at_sec）。"""
        it = real(d, bpm)
        it.at_sec = units.beats_to_sec(d.get("at_beats"), bpm)
        return it

    ercue_io._item_from_file = _legacy_trust_beats
    try:
        got = _read_item(fd).at_sec
    finally:
        ercue_io._item_from_file = real
    legacy_expect = units.beats_to_sec(250.0, 150.0)         # = 250 × (60/150) = 100 秒
    assert abs(got - legacy_expect) < 1e-9, ("旧口径应得 %.3f 秒" % legacy_expect, got)
    caught = not (abs(got - 50.0) < 1e-9)
    assert caught, "旧口径居然也得到 50 —— ① 的断言无效！"
    # 恢复后必须回到 50
    assert abs(_read_item(fd).at_sec - 50.0) < 1e-9, "还原后应回到 50"
    print("  ✓ 改回「信任 at_beats」⇒ 读到 %.3f 秒（≠50，按 250 拍 × 0.4s 放大）"
          "⇒ ① 会挂；还原后回到 50.0" % got)


# ---------------------------------------------------------------- 5
def test_roundtrip_seconds_exact():
    """⑤ 往返：模型里秒=12.345 / 98.765 → `save()` → `load()` ⇒ 秒**逐字不变**。"""
    tmp = tempfile.mkdtemp(prefix="ercue_sec_")
    for bpm in (0.0, 150.0, 155.03):
        s = CueSheet(title="往返")
        te = TrackEntry(track_key="k1", identity=Identity(title="T", artist="A",
                                                          duration_sec=180.0, bpm=bpm))
        ly = CompLayer(lyr_id="L1", name="层1", z=0)
        ly.items.append(CueItem(item_id="i1", clip_id="c1", at_sec=12.345).normalized())
        ly.items.append(CueItem(item_id="i2", clip_id="c1", at_sec=98.765).normalized())
        te.composition.layers.append(ly)
        s.tracks.append(te)
        p = os.path.join(tmp, "rt%.2f.ercue" % bpm)
        ercue_io.save(s, p, app_version="2026.10.18.01")
        back, _info = ercue_io.load(p)
        got = [round(float(it.at_sec), 12) for it in back.tracks[0].layers[0].items]
        assert got == [12.345, 98.765], ("往返后秒坐标变了", bpm, got)
    print("  ✓ 往返（bpm 0/150/155.03）：12.345 / 98.765 逐字不变")


# ---------------------------------------------------------------- 6
def test_end_sec_authority():
    """⑥ 附加：`end_sec` 同样以秒为权威（且被夹到 >= at_sec，防手改出负时长）。"""
    it = _read_item(_filedict(at_sec=50.0, at_beats=250.0, bpm=150.0,
                              include_end=True, end_sec=60.0))
    assert abs(it.end_sec - 60.0) < 1e-9, it.end_sec
    bad = _read_item(_filedict(at_sec=50.0, at_beats=250.0, bpm=150.0,
                               include_end=True, end_sec=10.0))
    assert abs(bad.end_sec - 50.0) < 1e-9, ("end_sec < at_sec 应被夹到 at_sec", bad.end_sec)
    print("  ✓ end_sec 也以秒为权威；end_sec < at_sec 时夹到 at_sec")


def main() -> int:
    tests = [
        ("at_sec 压过 at_beats（改 bpm 不影响）", test_at_sec_wins_over_at_beats),
        ("老包（无 at_sec）回落 at_beats", test_legacy_fallback_without_at_sec),
        ("at_sec == 0 被尊重", test_zero_sec_is_respected),
        ("反向复现：信任 at_beats 会挂", test_reverse_trusting_beats_fails),
        ("save/load 往返秒坐标逐字不变", test_roundtrip_seconds_exact),
        ("end_sec 也以秒为权威", test_end_sec_authority),
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
