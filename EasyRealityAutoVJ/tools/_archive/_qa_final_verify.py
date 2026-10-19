# -*- coding: utf-8 -*-
"""终验独立复核（QA Edward）。

独立于工程师的 _gridfix5_accept.py / _gridfix7_sens.py：
  · 自己实现「驱动现役 src/engine.py `_beat_pos`」的闭环重放（不 import 前者）；
  · 自己实现窗口直方图采集（HOP 可配），以对比 **生产 0.5s 窗口**与 **harness 1.0s 窗口**。

用途：
  A) 用现有 1.0s 数据（tools/_fix3_hint_seq.json）复测 dt∈{3,4,5,6} 的 220 例指标；
  B) 用 **0.5s**（生产线）数据复测同一批 220 例指标（独立验证「dt 语义在生产是否成立」）；
  C) 稀窗场景（故意把窗间隔拉到 5s/8s）测「是否永远锁不上」。

用法：
  collect0.5 : python tools/_qa_final_verify.py collect0.5     # 采集 0.5s 序列（重，后台跑）
  all        : python tools/_qa_final_verify.py all            # 跑 A + (若有) B + C
"""
import importlib.util
import json
import os
import statistics
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import fp as FP          # noqa: E402
import engine as E       # noqa: E402

DT = 1.0 / 60.0
HOP_SIZE = FP.HOP_SIZE
SR = FP.SAMPLE_RATE
SEQ_1S = os.path.join(ROOT, "tools", "_fix3_hint_seq.json")
SEQ_HALF = os.path.join(ROOT, "tools", "_qa_final_seq_half.json")
GRIDS = os.path.join(ROOT, "tools", "_gridfix4_grids.json")
TOFU = r"W:\临时结界相关\2of\tofu"
LA = os.environ.get("LOCALAPPDATA", "")
CFG = os.path.join(LA, "AutoVJ", "config.json")
SRC_DB = os.path.join(LA, "AutoVJ", "fingerprints.db")


class FT:
    t = 0.0

    @staticmethod
    def perf_counter():
        return FT.t


E.time = FT


# ------------------------------------------------------------------ 独立驱动
def make_self(exp, bl, anchor, dt_cap):
    s = types.SimpleNamespace()
    s._pos_ref = None
    s.switch_pos = 0.0
    s._next_due = None
    s._due_off = 0.0
    s._bar_off_user = None
    s.mode = "normal"
    s.cfg = {"auto": {"beat_grid": True}, "mode": {"sensitivity": 1}}
    s._grid_clock = E.GridClock()
    s._grid_active_sid = None
    s._grid_coord_delta = 0.0
    s._grid_last_pos = 0.0
    s._grid_last_out = None
    s._grid_pending = None
    s._grid_seen = None
    s._song_grid_lookup = {exp: {"beat_len": bl, "anchor": anchor, "bpm": 120.0, "src": "vdj"}}
    s.audio = types.SimpleNamespace(grid_pred_orig=-1.0, grid_pred_w=None)
    for nm in ("_beat_pos", "_beat_pos_grid", "_grid_observe", "_grid_state"):
        setattr(s, nm, types.MethodType(getattr(E.AutoVJEngine, nm), s))
    s.GRID_MIN_VOTES = E.AutoVJEngine.GRID_MIN_VOTES
    s.GRID_STRONG_VOTES = E.AutoVJEngine.GRID_STRONG_VOTES
    s.GRID_CONSIST_LO = E.AutoVJEngine.GRID_CONSIST_LO
    s.GRID_CONSIST_HI = E.AutoVJEngine.GRID_CONSIST_HI
    s.GRID_CONSIST_MAX_DT = dt_cap
    return s


def _snap(sid, off_end, votes, ref_t, t):
    return {"bpm": 128.0, "beat_active": True, "beat_count": int(t / 0.5),
            "beat_phase": (t / 0.5) % 1.0, "downbeat_off": 0.0, "energy": 0.5,
            "energy_trend": 0.0, "recognized_song_id": sid,
            "recognized_offset": off_end, "recognized_rate": 1.0,
            "recognized_t": ref_t, "recognized_votes": votes}


def _pick(s, w):
    hint_f = None
    hint_w = None
    p = s.audio.grid_pred_orig
    if p is not None and p >= 0.0:
        hint_f = p * SR / HOP_SIZE
        pw = s.audio.grid_pred_w
        if pw is not None and float(pw) > 0.0:
            hint_w = float(pw)
    hist = {(c[0], c[1]): c[2] for c in w["cand"]}
    picked = FP.FingerprintDB._pick(hist, hint_f, hint_w)
    if picked is None:
        return None
    (sid, delta), votes = picked
    return {"sid": int(sid), "off": delta * HOP_SIZE / SR, "votes": int(votes)}


def replay(wins, exp, bl, anchor, start_t, dt_cap):
    """闭环重放，返回 (errs=[(t,err)], lock_t)。err 为网格原始拍位 vs 真值。"""
    s = make_self(exp, bl, anchor, dt_cap)
    win_by = {w["t"]: w for w in wins}
    t_end = wins[-1]["t"]
    t = float(start_t)
    cur = None
    cur_ref = 1000.0 + t
    errs = []
    lock_t = None
    while t <= t_end + DT:
        w = win_by.get(round(t, 3))
        if w is not None:
            cur = _pick(s, w)
            cur_ref = 1000.0 + t
        FT.t = 1000.0 + t
        sid = cur["sid"] if cur is not None else -1
        off_end = (float(cur["off"]) + 4.5) if cur is not None else -1.0
        votes = int(cur["votes"]) if cur is not None else 0
        s._beat_pos(_snap(sid, off_end, votes, cur_ref, t))
        if s._grid_active_sid is not None:
            if lock_t is None:
                lock_t = t
            errs.append((t, s._grid_last_pos - (t + 4.5 - anchor) / bl))
        t += DT
    return errs, lock_t


def bad_dur(errs, thr=1.0):
    return sum(DT for _, e in errs if abs(e) > thr)


def load(path):
    seq = json.load(open(path, encoding="utf-8"))
    grids = json.load(open(GRIDS, encoding="utf-8"))
    out = []
    for d in seq:
        if int(d.get("exp", -1)) < 0:
            continue
        g = grids.get(d["song"])
        if not g or not g.get("beat_len"):
            continue
        out.append({"song": d["song"], "exp": int(d["exp"]), "wins": d["wins"],
                    "bl": float(g["beat_len"]), "anchor": float(g["anchor"] or 0.0),
                    "bpm": float(g.get("bpm") or 0.0)})
    return out


def scan(data, starts, dt_cap, tag=""):
    tot = mx = 0.0
    n5 = 0
    locks = []
    for d in data:
        for st in starts:
            errs, lt = replay(d["wins"], d["exp"], d["bl"], d["anchor"], st, dt_cap)
            bd = bad_dur(errs)
            tot += bd
            mx = max(mx, bd)
            if bd > 5.0:
                n5 += 1
            locks.append(lt)
    got = [x for x in locks if x is not None]
    med = statistics.median(got) if got else float("nan")
    mxlock = max(got) if got else float("nan")
    print("  %-8s %8.0f %10.1f %5d %12.1f %10.1f %8s   %s" % (
        "%.1f" % dt_cap, tot, mx, n5, med, mxlock,
        "%d/%d" % (len(got), len(locks)), tag))
    return tot, mx, n5, med, mxlock, len(got), len(locks)


def t0_bad(data, sub, dt_cap):
    for d in data:
        if sub.lower() in d["song"].lower():
            errs, _ = replay(d["wins"], d["exp"], d["bl"], d["anchor"], 0.0, dt_cap)
            return d["song"], bad_dur(errs)
    return None


# ------------------------------------------------------------------ 采集 0.5s 序列
def collect_half(hop=0.5, win=4.5, maxsec=150):
    import glob
    import shutil
    import tempfile
    import numpy as np
    import soundfile as sf

    tmp = tempfile.mkdtemp(prefix="qafin_fp_")
    dst = os.path.join(tmp, "fp.db")
    for suf in ("", "-wal", "-shm"):
        if os.path.exists(SRC_DB + suf):
            shutil.copyfile(SRC_DB + suf, dst + suf)
    db = FP.FingerprintDB(dst)
    cfg = json.load(open(CFG, encoding="utf-8"))
    base2sid = {}
    for pp, vv in cfg.get("music_meta", {}).items():
        if isinstance(vv, dict) and vv.get("song_id") is not None:
            base2sid[os.path.basename(pp)] = vv["song_id"]

    def match_hist(sig, sr):
        hs = FP.fingerprint_audio(sig, sr)
        if not hs:
            return {}
        qoff_map, hashes = {}, []
        for h, qoff in hs:
            qoff_map.setdefault(h, []).append(qoff)
            hashes.append(h)
        votes = {}
        for i in range(0, len(hashes), 500):
            chunk = hashes[i:i + 500]
            ph = ",".join("?" * len(chunk))
            for (h, song_id, dboff) in db.conn.execute(
                    "SELECT hash, song_id, offset FROM fingerprints "
                    "WHERE hash IN (%s)" % ph, chunk):
                for qoff in qoff_map.get(h, ()):
                    key = (song_id, dboff - qoff)
                    votes[key] = votes.get(key, 0) + 1
        return votes

    files = sorted(glob.glob(os.path.join(TOFU, "*.flac")))
    seqdump = []
    print("采集：素材 %d 首；前 %ds，hop=%.1fs，窗=%.1fs" % (len(files), maxsec, hop, win),
          flush=True)
    for song in files:
        exp = base2sid.get(os.path.basename(song), -1)
        if exp < 0:
            continue
        data, sr = sf.read(song, dtype="float32", always_2d=True,
                           frames=int(maxsec * 44100))
        mono = np.mean(data, axis=1).astype(np.float32)
        if sr != 44100:
            x = np.linspace(0, len(mono) - 1, int(len(mono) * 44100 / sr))
            mono = np.interp(x, np.arange(len(mono)), mono).astype(np.float32)
            sr = 44100
        wl = int(win * sr)
        t = 0
        wins = []
        while t + wl <= len(mono):
            hist = match_hist(mono[t:t + wl], sr)
            gmax = max(hist.values()) if hist else 0
            cand = [[s, d, v] for (s, d), v in hist.items()
                    if v >= 2 or s == exp or v == gmax]
            wins.append({"t": round(t / sr, 3), "exp": int(exp), "cand": cand})
            t += int(hop * sr)
        seqdump.append({"song": os.path.basename(song), "exp": int(exp), "wins": wins})
        print("  %-46s 窗=%3d" % (os.path.basename(song)[:46], len(wins)), flush=True)
    db.close()
    shutil.rmtree(tmp, ignore_errors=True)
    json.dump(seqdump, open(SEQ_HALF, "w", encoding="utf-8"), ensure_ascii=False)
    print("已写 %s" % SEQ_HALF)
    return seqdump


# ------------------------------------------------------------------ 稀窗场景
def make_sparse(base_wins, gap_windows):
    """从 base_wins 里每隔 gap_windows 取一个窗（其它丢弃）→ 模拟识别窗变稀。"""
    out = []
    for i, w in enumerate(base_wins):
        if i % gap_windows == 0:
            out.append(w)
    return out


def sec_sparse(data, dt_cap):
    print("\n" + "=" * 92)
    print("C) 稀窗场景（把 1.0s 数据按每 N 个窗取 1 个 → 模拟识别变稀；看是否还能锁上）")
    print("   dt_cap=%.1f；N=2→2.0s/窗，N=3→3.0s/窗，N=5→5.0s/窗，N=8→8.0s/窗" % dt_cap)
    print("=" * 92)
    print("  %-10s %10s %12s %14s %10s" % ("窗间隔", "总误锁s", "最长误锁s", "锁定覆盖", "锁定中位s"))
    for N in (1, 2, 3, 5, 8):
        tot = mx = 0.0
        got = 0
        nall = 0
        locks = []
        for d in data:
            w = make_sparse(d["wins"], N)
            if len(w) < 3:
                continue
            errs, lt = replay(w, d["exp"], d["bl"], d["anchor"], 0.0, dt_cap)
            tot += bad_dur(errs)
            mx = max(mx, bad_dur(errs))
            locks.append(lt)
            nall += 1
        gg = [x for x in locks if x is not None]
        got = len(gg)
        med = statistics.median(gg) if gg else float("nan")
        print("  %-10s %10.0f %12.1f %14s %10.1f" % (
            "%.1fs" % (N * 1.0), tot, mx, "%d/%d" % (got, nall), med))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode == "collect0.5":
        collect_half(hop=0.5, win=4.5, maxsec=150)
        return

    starts = [0, 3, 6, 9, 12, 15, 18, 21, 24, 27, 30]

    print("=" * 92)
    print("A) 1.0s 窗口（=工程师 harness 口径）220 例：dt∈{3,4,5,6}")
    print("=" * 92)
    d1 = load(SEQ_1S)
    print("载入 %d 首（BPM %.1f~%.1f）" % (len(d1), min(x["bpm"] for x in d1),
                                          max(x["bpm"] for x in d1)))
    print("  %-8s %8s %10s %5s %12s %10s %8s" % ("dt", "总误锁s", "最长误锁s", ">5s",
                                                 "锁定中位s", "锁定最大s", "覆盖"))
    for cap in (3.0, 4.0, 5.0, 6.0):
        scan(d1, starts, cap)
    print("  —— t=0 逐首（重点 CWB / Lost）——")
    for cap in (4.0, 6.0):
        cwb = t0_bad(d1, "Believe", cap)
        lost = t0_bad(d1, "Lost", cap)
        print("    dt=%.1f  CWB t=0 误锁=%.1fs   Lost t=0 误锁=%.1fs"
              % (cap, cwb[1] if cwb else -1, lost[1] if lost else -1))

    if os.path.exists(SEQ_HALF):
        print("\n" + "=" * 92)
        print("B) 0.5s 窗口（=生产音频识别周期）同批 220 例：dt∈{3,4,5,6}  ← 独立验证")
        print("=" * 92)
        d5 = load(SEQ_HALF)
        print("载入 %d 首；每首窗数=%s" % (len(d5), [len(x["wins"]) for x in d5[:3]]))
        print("  %-8s %8s %10s %5s %12s %10s %8s" % ("dt", "总误锁s", "最长误锁s", ">5s",
                                                     "锁定中位s", "锁定最大s", "覆盖"))
        for cap in (3.0, 4.0, 5.0, 6.0):
            scan(d5, starts, cap)
        for cap in (4.0, 6.0):
            cwb = t0_bad(d5, "Believe", cap)
            lost = t0_bad(d5, "Lost", cap)
            print("    dt=%.1f  CWB t=0 误锁=%.1fs   Lost t=0 误锁=%.1fs"
                  % (cap, cwb[1] if cwb else -1, lost[1] if lost else -1))
    else:
        print("\n（未找到 %s，跳过 B；先跑 collect0.5）" % SEQ_HALF)

    sec_sparse(d1, 4.0)


if __name__ == "__main__":
    main()
