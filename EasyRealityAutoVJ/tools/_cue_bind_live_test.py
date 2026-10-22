# -*- coding: utf-8 -*-
"""本地复现「认出歌之后，码表到底有没有装上」。

用**真实** cfg（副本）+ **真实** 169MB 包 + **真的** AutoVJEngine 方法，
只把"已认出哪首歌"直接注入，然后调 `_cue_auto_position()` 看 `_cue_rt/_cue_track`。
"""
import json, os, shutil, sys, tempfile
sys.path.insert(0, "src")
import numpy as np

CFG = os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ", "config.json")

class FakeCfg(dict):
    """Config 是包装对象；这里用 dict 子类即可（引擎只用 __getitem__ / save）。"""
    def save(self):
        pass

def main():
    d = json.load(open(CFG, encoding="utf-8"))
    cfg = FakeCfg(d)
    print("cue.sheets 条目:", len((d.get("cue") or {}).get("sheets") or []))
    for e in ((d.get("cue") or {}).get("sheets") or []):
        print("   path=%s" % e.get("path"))
        print("   tracks=%d" % len(e.get("tracks") or []))
    print("cue.fp_songs:", list(((d.get("cue") or {}).get("fp_songs") or {}).keys()))

    import engine as E
    # ⚠ AutoVJEngine 是 QObject 子类，不能 object.__new__ ⇒ 把要测的方法挂到普通类上
    # 把引擎里所有 _cue_* / cue_* / set_clear 的方法原样挂到普通类上（少了哪个都会 AttributeError）
    ns = {}
    for m in dir(E.AutoVJEngine):
        if not (m.startswith("_cue") or m.startswith("cue_")
                or m in ("set_cue_sheet", "clear_cue_sheet")):
            continue
        f = getattr(E.AutoVJEngine, m)
        if not callable(f):
            continue
        ns[m] = staticmethod(f) if m == "_cue_song_id_of" else f
    eng = type("FakeEngine", (), ns)()
    from cue_position import CuePositionTracker
    eng.cfg = cfg
    eng.audio = type("A", (), {"state": type("S", (), {})()})()
    eng.layers = []
    eng._cue_sheet = None; eng._cue_track = None; eng._cue_rt = None
    eng._cue_media = {}; eng._cue_pos_tracker = CuePositionTracker()
    eng._cue_pos_song = -1; eng._cue_pos_src = ""; eng._cue_pos_beats = None
    eng._cue_manual_clock = False; eng._cue_pos_next_t = 0.0
    eng._cue_auto_sheet = ""; eng._cue_bound_cache = None
    eng._cue_bad_song_n = 0; eng._cue_bad_song_t = 0.0
    eng._health_log = None
    eng._cue_index = None; eng._cue_id_cache = None
    eng._cue_diag_t = 0.0
    eng._last_match = None
    # 位置落地由渲染层消费，这里只记录（不是被测对象）
    eng._pos_log = []
    eng.set_cue_position = lambda pb, src: eng._pos_log.append((pb, src))

    SID = 346225955   # 揽月（合成 id）
    st = eng.audio.state
    st.recognized_song_id = SID
    st.recognized_offset = 120.0
    st.recognized_votes = 200
    st.recognized_rate = 1.0
    st.silent_sec = 0.0

    print("\n--- 认歌参数 ---")
    q = eng._cue_track_query_by_sid(SID)
    print("  by_sid →", {k: (v[:20] if isinstance(v, str) else v) for k, v in (q or {}).items()})
    print("\n--- 候选表 ---")
    hits = eng.cue_sheets_for_track(**(q or {}))
    print("  cue_sheets_for_track →", [(os.path.basename(h.get("path","")), lv) for h, lv in hits])

    print("\n--- 调 _cue_auto_position ---")
    eng._cue_pos_next_t = 0.0
    for i in range(3):
        r = eng._cue_auto_position()
        print("  第%d次 → %s  | sheet=%s track=%s rt=%s items=%s"
              % (i + 1, r,
                 bool(eng._cue_sheet),
                 getattr(getattr(eng._cue_track, "identity", None), "title", None),
                 bool(eng._cue_rt), getattr(eng._cue_rt, "n_items", 0)))
        st.recognized_offset += 0.5

if __name__ == "__main__":
    main()
