# -*- coding: utf-8 -*-
"""**端到端闸门**：真音频 → 真认歌循环 → 真电荷条 → 真码表绑定。

用户场景的完整链路，本地一条命令验证：
   放歌 → 认出（合成 id / 曲库 id）→ 码表自动装表 + 绑曲目 → 出拍位
"""
from __future__ import annotations
import json, os, sys, tempfile, threading, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
os.environ["LOCALAPPDATA"] = os.path.join(tempfile.gettempdir(), "e2e_fixed")
os.makedirs(os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ"), exist_ok=True)

import numpy as np, soundfile as sf
import audio_engine as AE
import fp as fpvj
import cue_fp_pack as C
import engine as E
from charge_engine import ChargeBarEngine
from cue_position import CuePositionTracker

SR, CHUNK, PRE, WAIT = 48000, 1440, 12.0, 30.0
REAL_CFG = r"C:/Users/Administrator/AppData/Local/AutoVJ/config.json"
REAL_DB = r"C:/Users/Administrator/AppData/Local/AutoVJ/fingerprints.db"


class FakeCfg(dict):
    def save(self): pass


def to48k(path):
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    mono = np.ascontiguousarray(data.mean(axis=1), dtype=np.float32)
    if sr != SR:
        x = np.linspace(0, len(mono) - 1, max(1, int(len(mono) * SR / sr)))
        mono = np.interp(x, np.arange(len(mono)), mono).astype(np.float32)
    return mono


def build(mono, cfg, db):
    # ① 音频引擎骨架（挂真的 _recognize_loop / _rec_diag）
    ae = object.__new__(AE.AudioEngine)
    ae._fp_db = db
    ae._recognizer_engine = ChargeBarEngine()
    ae._rate_est = None
    for n in dir(AE):
        if "Rate" in n and isinstance(getattr(AE, n), type):
            ae._rate_est = getattr(AE, n)()
            break
    ae.state = AE.AudioState(); ae.state.running = True
    ae._genre_lock = threading.Lock(); ae._genre_stop = threading.Event()
    ae._fp_sr = SR; ae._fp_buf = AE._AudioRing(int(SR * 12))
    ae._fp_buf.write(mono[: int(PRE * SR)])
    for attr, val in (("_last_match", None), ("_last_match_t", 0.0),
                      ("_last_sig", (-1, -1.0)), ("_last_adv", 0),
                      ("_eng_state", (0, -1, 0, 0)), ("_rec_err_t", 0.0),
                      ("_rec_diag_t", 0.0), ("_rec_hash_t", 0.0)):
        setattr(ae, attr, val)

    # ② 引擎骨架（挂真的 cue 方法）
    ns = {}
    for m in dir(E.AutoVJEngine):
        if m.startswith("_cue") or m.startswith("cue_") or m in ("set_cue_sheet", "clear_cue_sheet"):
            f = getattr(E.AutoVJEngine, m)
            if callable(f):
                ns[m] = staticmethod(f) if m == "_cue_song_id_of" else f
    eng = type("FakeEngine", (), ns)()
    # ★★★ 2026-10-17：**必须与生产接线一致** —— 生产里
    #   `AutoVJEngine(cfg, self.audio.state, lib)` 传的是 **AudioState**，
    #   所以 `engine.audio` 是状态对象；音频引擎本体走 `set_audio_engine()`。
    #   这里原来写 `eng.audio = ae`（完整 AudioEngine，自带 .state），于是 engine.py 里
    #   `getattr(self.audio, "state", None)` 的错写法在这个夹具下**恰好能work** ⇒
    #   测试全绿而现场 `sid=-1` 永远不接管。夹具失真比没有测试更危险。
    eng.audio = ae.state
    eng._audio_engine = ae
    eng.cfg = cfg
    eng._cue_sheet = None; eng._cue_track = None; eng._cue_rt = None
    eng._cue_media = {}; eng._cue_pos_tracker = CuePositionTracker()
    eng._cue_pos_song = -1; eng._cue_pos_src = ""; eng._cue_pos_beats = None
    eng._cue_manual_clock = False; eng._cue_pos_next_t = 0.0; eng._cue_auto_sheet = ""
    eng._cue_bound_cache = None; eng._cue_bad_song_n = 0; eng._cue_bad_song_t = 0.0
    eng._health_log = None; eng._cue_index = None; eng._cue_id_cache = None
    eng._cue_diag_t = 0.0; eng._cue_rebind_t = 0.0
    eng._pos_log = []
    eng.set_cue_position = lambda pb, src: eng._pos_log.append((pb, src))
    # 一个可见的码表图层（`cuesheet_wanted` 需要）
    lay = type("L", (), {})()
    lay.is_cuesheet = True; lay.visible = True; lay.name = "码表图层"
    eng.layers = [lay]
    return ae, eng


def run_case(db, cfg, name, path, want_sid, want_sheet):
    mono = to48k(path)
    ae, eng = build(mono, cfg, db)
    stop = threading.Event()

    def feeder():
        pos = int(PRE * SR); t0 = time.perf_counter()
        while not stop.is_set() and pos < len(mono):
            delay = (t0 + (pos - int(PRE * SR)) / SR) - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            ae._fp_buf.write(mono[pos:pos + CHUNK]); pos += CHUNK

    threading.Thread(target=feeder, daemon=True).start()
    # ★★★ 2026-10-17：**必须校验「引擎看见了什么」**，而不只是「状态里有什么」。
    #   真实故障：`state.recognized_song_id` 正确（`[R] cur=346225955`），但
    #   `_cue_auto_position()` 因为读错对象（`self.audio.state` ⇒ None）恒读 -1
    #   ⇒ 码表永不接管。只断言 state 的测试**全绿也发现不了**。
    #   这里给 `_cue_diag` 装探针，把它**实际读到的** sid 记下来。
    seen = []
    _orig_diag = eng._cue_diag

    def _spy(sid, votes, off, status, pb, now):
        seen.append((int(sid), int(votes), float(off), str(status)))
        return _orig_diag(sid, votes, off, status, pb, now)

    eng._cue_diag = _spy
    threading.Thread(target=AE.AudioEngine._recognize_loop, args=(ae,), daemon=True).start()

    t0 = time.perf_counter(); got, bound, t_done = -1, None, None
    while time.perf_counter() - t0 < WAIT:
        time.sleep(0.2)
        try:
            eng._cue_auto_position()
        except Exception as e:                                   # noqa: BLE001
            print("   _cue_auto_position 抛异常:", e)
        sid = int(getattr(ae.state, "recognized_song_id", -1) or -1)
        if sid >= 0:
            got = sid
            if eng._cue_rt is not None:
                bound = getattr(eng._cue_rt, "n_items", 0); t_done = time.perf_counter() - t0
                break
            # 没有码表的歌：认出即算完成（不必等满 WAIT）
            if not want_sheet:
                t_done = time.perf_counter() - t0
                break
    stop.set(); ae._genre_stop.set()
    # ★ 引擎侧实际读到的最大 sid / 是否出现 ok·switch / 是否有过非零 off
    seen_sid = max([s[0] for s in seen], default=-1)
    seen_ok = any(s[3] in ("ok", "switch") for s in seen)
    seen_off = max([s[2] for s in seen], default=-1.0)
    # want_sheet=False 的歌（曲库里有、但没有码表）应"认出来但不装表"——这也是正确结果
    ok = (got == want_sid
          and ((bound and bound > 0) if want_sheet else (eng._cue_sheet is None))
          # ★★ 关键：引擎**自己**必须读到这首歌（旧写法恒 -1 ⇒ 这里必红）
          and seen_sid == want_sid
          and seen_ok
          and seen_off > 0.0)
    print("  %-30s sid=%-11s %s | 表=%-9s items=%s  %.1fs  %s  [引擎侧 sid=%s ok=%s off=%.1f/%d样本]"
          % (name[:30], got,
             "✅" if got == want_sid else "❌(期望 %s)" % want_sid,
             ("是" if eng._cue_sheet else "否"), bound, t_done or -1,
             "✅" if ok else "❌", seen_sid, seen_ok, seen_off, len(seen)))
    return ok


def main():
    import shutil
    tmp = os.path.join(tempfile.mkdtemp(), "fp.db"); shutil.copyfile(REAL_DB, tmp)
    db = fpvj.FingerprintDB(tmp)
    cfg = FakeCfg(json.load(open(REAL_CFG, encoding="utf-8")))
    CASES = [
        ("揽月（码表包）", r"Y:/EasyRealityAutoVJ/码表素材/揽月 - Dreamira.flac",
         C.fp_song_id("fp2-617615efb8f43911")),
        ("Nimbus（码表包）", r"Y:/EasyRealityAutoVJ/码表素材/Nimbus Spark 1999 - Supa7onyz.flac",
         C.fp_song_id("fp2-f6ebed7d34af40e3")),
        ("Cosmic String（曲库·无表）",
         r"W:/临时结界相关/2of/tofu/Rolling Contact - Cosmic String.flac", "NO_SHEET"),
    ]
    ok = n = 0
    for name, path, want in CASES:
        if not os.path.isfile(path):
            print("  %-30s （文件不存在）" % name); continue
        # "NO_SHEET" = 曲库里有这首歌、但没有对应的码表 ⇒ 正确结果是"认出来但不装表"
        expect_sheet = (want != "NO_SHEET")
        if want is None or want == "NO_SHEET":
            base = os.path.basename(path)
            p = next((q for q in (cfg.get("music_library") or [])
                      if os.path.basename(q) == base), path)
            import hashlib
            want = int(hashlib.md5(p.encode("utf-8")).hexdigest()[:8], 16)
        n += 1
        ok += 1 if run_case(db, cfg, name, path, want, expect_sheet) else 0
    db.close()
    print("\n通过 %d / %d" % (ok, n))
    print("日志：", os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ", "cue_health.log"))
    return 0 if ok == n else 1


if __name__ == "__main__":
    sys.exit(main())
