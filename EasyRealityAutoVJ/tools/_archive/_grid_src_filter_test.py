# -*- coding: utf-8 -*-
"""阻塞项 2 实测：_inject_song_grids 是否只放行 src=="vdj" 的网格。

用法： P:\\AutoVJ\\venv\\Scripts\\python.exe tools/_grid_src_filter_test.py
"""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import ui_main  # noqa: E402


class EngineStub:
    def __init__(self):
        self.mapping = None

    def set_song_grids(self, mapping):
        self.mapping = mapping


def make_self(meta):
    s = types.SimpleNamespace()
    s.cfg = {"music_meta": meta}
    s.engine = EngineStub()
    s._inject_song_grids = types.MethodType(ui_main.MainWindow._inject_song_grids, s)
    return s


def g(src, conf, bl=0.5, anchor=1.0, bpm=120.0):
    return {"bpm": bpm, "beat_len": bl, "anchor": anchor, "conf": conf,
            "src": src, "dur": 300.0}


META = {
    "/a/vdj_ok.flac": {"song_id": 1, "grid": g("vdj", 0.95)},
    "/a/local_conf095.flac": {"song_id": 2, "grid": g("local", 0.95)},
    "/a/local_conf050.flac": {"song_id": 3, "grid": g("local", 0.50)},
    "/a/vdj_lowconf.flac": {"song_id": 4, "grid": g("vdj", 0.50)},
    "/a/vdj_badbl.flac": {"song_id": 5, "grid": g("vdj", 0.95, bl=8.0)},
    "/a/nosrc.flac": {"song_id": 6, "grid": {"bpm": 120.0, "beat_len": 0.5, "anchor": 1.0, "conf": 0.95}},
    "/a/nogrid.flac": {"song_id": 7},
}

s = make_self(META)
n = s._inject_song_grids()
got = set(s.engine.mapping.keys())
print("注入数量 = %d（期望 1）" % n)
print("注入的 song_id = %s（期望 {1}）" % sorted(got))
exp = {1}
ok = (got == exp)
print("[%s] 仅 VDJ 来源进入网格：vdj_ok 放行；local(0.95)/local(0.50)/vdj_lowconf/vdj_badbl/无src/无grid 全部剔除"
      % ("PASS" if ok else "FAIL"))
sys.exit(0 if ok else 1)
