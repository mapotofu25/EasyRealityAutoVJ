# -*- coding: utf-8 -*-
"""T03 自测：演出端详情页读写 music_meta.grid_manual + engine 分段/多锚消费。

1) engine._grid_segment_of：分段/多锚/no-manual 三种情况 offset→(beat_len,anchor,segkey)
2) GridClock.reanchor：跨段硬重锚后拍位按新段 (anchor,beat_len) 重算
3) music_detail：headless 打开 → 标八拍/分段 → 保存 → 重开读回（写 music_meta["grid_manual"]）
"""
import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import beatgrid as _bg
from engine import AutoVJEngine


def test_grid_segment_of():
    print("== 1) engine._grid_segment_of ==")
    seg = {"bpm": 128.0, "beat_len": 60.0 / 128, "anchor": 0.0, "src": "manual",
           "segments": [
               {"t0": 0.0, "bpm": 128.0, "beat_len": 60.0 / 128, "anchor": 0.0},
               {"t0": 120.0, "bpm": 155.0, "beat_len": 60.0 / 155, "anchor": 120.03}]}
    bl, anc, sk = AutoVJEngine._grid_segment_of(seg, 50.0)
    assert abs(bl - 60.0 / 128) < 1e-6 and abs(anc - 0.0) < 1e-6 and sk is not None, (bl, anc, sk)
    print("  seg off=50  -> beat_len=%.4f anchor=%.3f (段1:128BPM)" % (bl, anc))
    bl, anc, sk2 = AutoVJEngine._grid_segment_of(seg, 130.0)
    assert abs(bl - 60.0 / 155) < 1e-6 and abs(anc - 120.03) < 1e-6 and sk2 != sk, (bl, anc)
    print("  seg off=130 -> beat_len=%.4f anchor=%.3f (段2:155BPM)" % (bl, anc))

    anc_g = {"bpm": 140.0, "beat_len": 60.0 / 140, "anchor": 12.4, "src": "manual",
             "anchors": [12.4, 28.8]}
    _, a20, _ = AutoVJEngine._grid_segment_of(anc_g, 20.0)
    _, a30, _ = AutoVJEngine._grid_segment_of(anc_g, 30.0)
    assert abs(a20 - 12.4) < 1e-6 and abs(a30 - 28.8) < 1e-6, (a20, a30)
    print("  多锚 off=20 -> anchor=%.2f ; off=30 -> anchor=%.2f" % (a20, a30))

    plain = {"bpm": 140.0, "beat_len": 60.0 / 140, "anchor": 12.4, "src": "bt", "conf": 0.9}
    bl, anc, sk = AutoVJEngine._grid_segment_of(plain, 100.0)
    assert abs(bl - 60.0 / 140) < 1e-9 and abs(anc - 12.4) < 1e-9 and sk is None
    print("  无 manual off=100 -> beat_len=%.4f anchor=%.2f segkey=None（零回归）" % (bl, anc))


def test_reanchor():
    print("== 2) GridClock.reanchor 跨段硬重锚 ==")
    clock = _bg.GridClock()
    clock.update(1, 60.0 / 128, 0.0, 50.0, 1.0, 100.0, strong=True)
    p1 = clock.pos(100.0)
    exp1 = 50.0 / (60.0 / 128)
    assert abs(p1 - exp1) < 0.02, (p1, exp1)
    clock.reanchor(60.0 / 155, 120.03, 130.0, 1.0, 200.0)
    p2 = clock.pos(200.0)
    exp2 = (130.0 - 120.03) / (60.0 / 155)
    assert abs(p2 - exp2) < 0.02, (p2, exp2)
    print("  段1 pos@off50 = %.2f（期望 %.2f）; 重锚后 段2 pos@off130 = %.2f（期望 %.2f）"
          % (p1, exp1, p2, exp2))


def test_inject():
    print("== 4) ui_main._inject_song_grids（effective_grid 注入）==")
    from PySide6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication([])
    from ui_main import MainWindow

    class FakeCfg(dict):
        def save(self):
            pass

    class FakeEngine:
        def __init__(self):
            self.last = None

        def set_song_grids(self, m):
            self.last = m

    meta = {
        # A：manual 有分段，但算法 src=local（正常会被 gate 掉）→ manual 应 bypass 注入
        "/tmp/A.flac": {
            "song_id": 1,
            "grid": {"bpm": 128.0, "beat_len": 60.0 / 128, "anchor": 0.0,
                     "src": "local", "conf": 0.9, "dur": 300.0},
            "grid_manual": {"bpm_manual": 0.0, "anchors": [], "segments": [
                {"t0": 0.0, "bpm": 128.0, "beat_len": 60.0 / 128, "anchor": 0.0},
                {"t0": 120.0, "bpm": 155.0, "beat_len": 60.0 / 155, "anchor": 120.03}]}},
        # B：无 manual、src=local → 被 gate 掉
        "/tmp/B.flac": {
            "song_id": 2,
            "grid": {"bpm": 128.0, "beat_len": 60.0 / 128, "anchor": 0.0,
                     "src": "local", "conf": 0.9, "dur": 300.0}},
        # C：无 manual、src=vdj → 正常注入（单锚）
        "/tmp/C.flac": {
            "song_id": 3,
            "grid": {"bpm": 140.0, "beat_len": 60.0 / 140, "anchor": 12.4,
                     "src": "vdj", "conf": 0.9, "dur": 300.0}},
    }
    fake = type("FakeMain", (), {})()
    fake.cfg = FakeCfg({"music_meta": meta})
    fake._grid_path_by_sid = {}
    fake.engine = FakeEngine()
    MainWindow._inject_song_grids(fake)
    m = fake.engine.last or {}
    assert 1 in m and m[1].get("segments"), ("A 未被注入", m.get(1))
    assert 2 not in m, ("B 应被 gate 掉", m.get(2))
    assert 3 in m and abs(m[3]["anchor"] - 12.4) < 1e-6, ("C 注入异常", m.get(3))
    print("  A(manual分段+local) 注入 %d 段 ✓ ; B(local无manual) 被 gate ✓ ; C(vdj) anchor=%.1f ✓"
          % (len(m[1]["segments"]), m[3]["anchor"]))


def test_detail_roundtrip():
    print("== 3) music_detail 详情页 headless 读写 ==")
    from PySide6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication([])
    import music_detail as MD

    cfgp = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "config.json")
    cj = json.load(open(cfgp, encoding="utf-8"))
    lib = [x for x in (cj.get("music_library") or []) if os.path.isfile(x)]
    assert lib, "music_library 无可读文件"
    path = lib[0]

    class FakeCfg(dict):
        def save(self):
            pass

    cfg = FakeCfg({"music_meta": {}})
    cfg["music_meta"][path] = {
        "song_id": 424242, "title": "T03 test",
        "grid": {"bpm": 128.0, "beat_len": 60.0 / 128, "anchor": 0.0,
                 "src": "vdj", "conf": 0.9, "dur": 300.0}}

    dlg = MD.MusicDetailDialog(cfg, path)
    print("  打开详情：波形 %s ; 自动BPM=%.1f"
          % ("有" if dlg._wf else "无", dlg._auto_bpm))
    dlg.wave.set_mode("anchor")
    dlg.wave.set_anchors([10.0, 20.0])
    dlg.wave.add_segment(0.0, 60.0, 128.0, default_bpm=128.0)
    dlg.wave.add_segment(60.0, 300.0, 155.0, default_bpm=128.0)
    manual = dlg._effective_manual()
    assert manual and manual["anchors"] == [10.0, 20.0], manual
    assert len(manual["segments"]) >= 2, manual["segments"]
    dlg._write_manual(manual)
    got = cfg["music_meta"][path].get("grid_manual")
    assert got and got["anchors"] == [10.0, 20.0] and len(got["segments"]) >= 2, got
    print("  保存后 music_meta[path].grid_manual = %s"
          % json.dumps(got, ensure_ascii=False))

    dlg2 = MD.MusicDetailDialog(cfg, path)
    assert dlg2.wave.anchors() == [10.0, 20.0], dlg2.wave.anchors()
    assert len(dlg2.wave.segments()) >= 2, dlg2.wave.segments()
    print("  重开读回：八拍=%s 分段=%d 段" % (dlg2.wave.anchors(), len(dlg2.wave.segments())))

    # 回退自动
    dlg2._write_manual(None)
    assert cfg["music_meta"][path].get("grid_manual") is None
    print("  回退自动：grid_manual 已清空 ✓")


def main():
    test_grid_segment_of()
    test_reanchor()
    test_inject()
    test_detail_roundtrip()
    print("\n[T03] 全部通过 ✓")


if __name__ == "__main__":
    main()
