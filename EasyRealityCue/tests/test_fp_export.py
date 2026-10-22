# -*- coding: utf-8 -*-
"""★ C3/C4 回归：导出时把**曲目指纹**带进包（`fp/<fp_id>.bin`）—— 走**真实窗口路径**。

**背景（用户原话）**
    「这首歌在 erc 里做的码表包啊，理论来讲 era 导入就应该要识别到」

  `.ercue` 里只有 `identity.fp_id`（整首哈希序列的 sha1 前 16 hex = **16 字符摘要**），
  演出端拿它认不出音频。现在导出时把**完整哈希序列**写进包，演出端导入后灌进指纹库
  ⇒ 歌不在那台机器上也能认出来。

**本测试专门盯"窗口那一层"的接线**（格式本身由 `tools/_fp_pack_test.py` 验）：
  1. 身份扫描缓存命中 ⇒ 导出直接 `pack_hashes`，包里出现 `fp/<fp_id>.bin`，内容可解回原序列；
  2. 缓存未命中（用户没点过身份扫描）⇒ 导出时**后台补算**（无头模式下同步），照样带上；
  3. 音频被换过（现算签名 ≠ 码表里的 `fp_id`）⇒ **不写**该成员的指纹（否则演出端会认错歌），
     且**导出照常成功**（绝不阻断）；
  4. `fp_id` 为空的曲目 ⇒ 跳过（包里没有它的成员），导出照常成功；
  5. 缓存只属于"当前这张表"：`new_sheet()` 之后缓存清空（换表/关窗同一套清理）；
  6. 多曲目时**只带该带的**（有 fp_id 且有音频的那首），不会张冠李戴。

跑法（无头）：
    set QT_QPA_PLATFORM=offscreen
    venv\\Scripts\\python.exe tests\\test_fp_export.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback
import zipfile

# ★ 测试隔离（与 test_ui_smoke 同一约定）：关掉自动保存，避免污染真实工作区
os.environ["ERCUE_NO_AUTOSAVE"] = "1"
os.environ.setdefault("ERCUE_AUTOSAVE_DIR",
                      os.path.join(tempfile.gettempdir(), "ercue_autosave_test_fp_export"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import numpy as np                                             # noqa: E402
import wave                                                    # noqa: E402

from PySide6.QtWidgets import QApplication                     # noqa: E402

from ercue.model import ercue_io, fp_pack                    # noqa: E402
from ercue.ui import identity_scan                             # noqa: E402
from ercue.ui.window import MainWindow                         # noqa: E402


# ==========================================================================
# 夹具
# ==========================================================================
def _make_song_wav(path, sec=20.0, sr=44100, seed=20261018):
    """造一段"有明确节拍峰"的 wav（tone + 每 0.5 s 一次宽带瞬态）⇒ 指纹非空。"""
    rng = np.random.default_rng(seed)
    n = int(sr * sec)
    t = np.arange(n, dtype=np.float64) / float(sr)
    left = 0.30 * np.sin(2 * np.pi * 220.0 * t) + 0.20 * np.sin(2 * np.pi * 1320.0 * t)
    right = 0.30 * np.sin(2 * np.pi * 330.0 * t) + 0.20 * np.sin(2 * np.pi * 1760.0 * t)
    click = np.zeros(n, dtype=np.float64)
    for k in range(0, n, max(1, int(sr * 0.5))):
        m = min(n - k, int(sr * 0.05))
        click[k:k + m] += np.exp(-np.linspace(0.0, 6.0, m)) * float(rng.uniform(0.6, 1.0))
    data = np.stack([left + click, right + click], axis=1)
    data += rng.standard_normal(data.shape) * 0.002
    pcm = np.round(np.clip(data, -1.0, 1.0) * 32767.0).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm.reshape(-1).tobytes())
    return path


def _new_window():
    """无头窗口：不弹任何对话框、不起缩略图线程（与 test_ui_smoke 同一约定）。"""
    w = MainWindow()
    w.thumbs_enabled = False
    w._interactive = False
    return w


def _prepare_song(w, path, title="指纹测试曲"):
    """加一首曲目并把音频**直接挂到 session**（不动 `import_track_audio`，避免起扫描线程）。"""
    idx = w.add_track(title, "unit-test", 20.0, 128.0)
    s = w.session
    s.track_audio[int(idx)] = os.path.abspath(path)
    return idx, s.sheet.tracks[int(idx)]


def _fp_members(pkg):
    return ercue_io.list_fp_members(pkg)


class _Fixture:
    """一次解码，供多个用例共用（解 44100×20s 只要几十毫秒）。"""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="ercue_fp_export_")
        self.song = _make_song_wav(os.path.join(self.dir, "song.wav"))
        self.hashes, self.sig = identity_scan.compute_hashes(self.song)
        assert self.hashes and self.sig, "夹具音频算不出指纹（测试前提不成立）"

    def pkg(self, name):
        return os.path.join(self.dir, name + ercue_io.EXT)


# ==========================================================================
# 用例
# ==========================================================================
def test_cached_hashes_are_written(fx):
    """① 缓存命中 ⇒ 导出直接打包，包里有 fp/<fp_id>.bin 且内容可解回原序列。"""
    w = _new_window()
    try:
        w.new_sheet("缓存命中")
        _idx, te = _prepare_song(w, fx.song)
        te.identity.fp_id = fx.sig
        assert identity_scan.put_cached_hashes(fx.song, fx.sig, fx.hashes), "缓存没存进去"
        out = fx.pkg("cached")
        assert w.export_sheet(out, force=True), "导出失败"
        members = _fp_members(out)
        name = "fp/%s.bin" % fx.sig
        assert members == [name], ("成员不对", members)
        data = ercue_io.read_fp_pack(out, fx.sig)
        assert data and fp_pack.unpack_hashes(data) == fx.hashes, "包里的指纹与原始哈希序列不一致"
        assert len(data) < 1.5 * 1024 * 1024, "指纹成员过大：%.1f KB" % (len(data) / 1024.0)
        with zipfile.ZipFile(out) as z:
            assert not any("\\" in n for n in z.namelist()), "成员名不能含反斜杠"
        print("  ✓ 缓存命中：带上 %s（%.1f KB / %d 条）"
              % (name, len(data) / 1024.0, len(fx.hashes)))
    finally:
        w.shutdown()


def test_uncached_recomputed_on_export(fx):
    """② 缓存未命中 ⇒ 导出时补算（无头模式同步），照样带上。"""
    identity_scan.clear_hash_cache()
    w = _new_window()
    try:
        w.new_sheet("补算")
        _idx, te = _prepare_song(w, fx.song)
        te.identity.fp_id = fx.sig
        out = fx.pkg("recomputed")
        assert w.export_sheet(out, force=True), "导出失败"
        assert _fp_members(out) == ["fp/%s.bin" % fx.sig], ("补算没写进包", _fp_members(out))
        data = ercue_io.read_fp_pack(out, fx.sig)
        assert fp_pack.unpack_hashes(data) == fx.hashes, "补算结果与身份扫描口径不一致"
        print("  ✓ 缓存未命中：导出时补算成功（%d 条）" % len(fx.hashes))
    finally:
        w.shutdown()


def test_signature_mismatch_not_written(fx):
    """③ 音频被换过（现算签名 ≠ 码表 fp_id）⇒ 不写该成员，且导出照常成功。"""
    identity_scan.clear_hash_cache()
    w = _new_window()
    try:
        w.new_sheet("签名不符")
        _idx, te = _prepare_song(w, fx.song)
        te.identity.fp_id = "fp2-0000000000000000"      # 与现算签名必然不符
        out = fx.pkg("mismatch")
        assert w.export_sheet(out, force=True), "签名不符**不该**阻断导出"
        assert _fp_members(out) == [], ("签名不符却写了指纹", _fp_members(out))
        print("  ✓ 签名不符：不写指纹但导出成功（避免演出端认错歌）")
    finally:
        w.shutdown()


def test_no_fp_id_skipped(fx):
    """④ `fp_id` 为空的曲目 ⇒ 跳过（不写成员），导出照常成功。"""
    identity_scan.clear_hash_cache()
    w = _new_window()
    try:
        w.new_sheet("无 fp_id")
        _idx, te = _prepare_song(w, fx.song)
        te.identity.fp_id = ""
        out = fx.pkg("noid")
        assert w.export_sheet(out, force=True), "没有 fp_id **不该**阻断导出"
        assert _fp_members(out) == [], ("没有 fp_id 却写了指纹", _fp_members(out))
        print("  ✓ fp_id 为空：跳过该曲目，导出成功")
    finally:
        w.shutdown()


def test_multi_track_only_the_right_one(fx):
    """⑤ 多曲目：只带"有 fp_id 且有音频"的那首，不张冠李戴。"""
    identity_scan.clear_hash_cache()
    w = _new_window()
    try:
        w.new_sheet("多曲目")
        idx0, te0 = _prepare_song(w, fx.song, "有指纹的")
        te0.identity.fp_id = fx.sig
        idx1 = w.add_track("没有指纹的", "unit-test", 0.0, 0.0)
        w.session.track_audio[int(idx1)] = ""          # 第二首没有音频
        out = fx.pkg("multi")
        assert w.export_sheet(out, force=True), "导出失败"
        assert _fp_members(out) == ["fp/%s.bin" % fx.sig], ("只该带一首", _fp_members(out))
        print("  ✓ 多曲目：只带第 1 首（%d 条），第 2 首无 fp_id 被跳过" % len(fx.hashes))
    finally:
        w.shutdown()


def test_cache_cleared_on_sheet_switch(fx):
    """⑥ 缓存只属于"当前这张表"：新建/换表后必须清空（关窗同样清）。"""
    w = _new_window()
    try:
        w.new_sheet("A 表")
        _idx, te = _prepare_song(w, fx.song)
        te.identity.fp_id = fx.sig
        assert identity_scan.put_cached_hashes(fx.song, fx.sig, fx.hashes)
        assert identity_scan.HASH_CACHE.stats()["entries"] == 1, "缓存没进去"
        w.new_sheet("B 表")
        assert identity_scan.HASH_CACHE.stats()["entries"] == 0, "新建码表后缓存没清空"
        assert identity_scan.cached_hashes(fx.song, fx.sig) is None
        print("  ✓ 缓存生命周期：新建码表后清空（换表/关窗同一套清理）")
    finally:
        w.shutdown()


# ==========================================================================
# 跑
# ==========================================================================
def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    print("=" * 70)
    print("导出带曲目指纹（fp/<fp_id>.bin）· 窗口路径回归")
    print("=" * 70)
    fx = _Fixture()
    print("夹具：%s（%d 条哈希，签名 %s）\n" % (fx.song, len(fx.hashes), fx.sig))

    tests = [
        ("缓存命中 ⇒ 写入包", test_cached_hashes_are_written),
        ("未命中 ⇒ 导出时补算", test_uncached_recomputed_on_export),
        ("签名不符 ⇒ 不写（导出不失败）", test_signature_mismatch_not_written),
        ("无 fp_id ⇒ 跳过", test_no_fp_id_skipped),
        ("多曲目 ⇒ 只带对的那首", test_multi_track_only_the_right_one),
        ("缓存随换表清空", test_cache_cleared_on_sheet_switch),
    ]
    ok_all = True
    for name, fn in tests:
        print("--- %s ---" % name)
        try:
            fn(fx)
        except Exception as e:                                  # noqa: BLE001
            ok_all = False
            print("  ✗ %s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()))
    print("\n" + "=" * 70)
    print("汇总：%s" % ("全部通过 ✓" if ok_all else "有失败 ✗"))
    print("=" * 70)
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
