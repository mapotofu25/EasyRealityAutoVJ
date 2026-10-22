# -*- coding: utf-8 -*-
"""第三轮 ★C 实测：给 `fp.match` 加「连续性提示(hint_delta)」后，端到端重放是否消除
强重复段落导致的**大幅瞬时偏移**（原 148~738 拍）。

做法（真实数据、真实代码路径）：
  1. 复制生产指纹库到临时目录（绝不改用户数据），素材只读。
  2. 对 20 首可核对曲目，逐窗跑**指纹查询**，但保留**完整投票直方图** (sid,delta)->votes
     （而不是只留全局众数），落盘 `_fix3_hint_seq.json`。
  3. 端到端重放：模拟真实链路 —— 识别线程每窗用「引擎上一帧发布的预测位置」当 hint 调
     `FingerprintDB._pick(hist, hint)` 得到 (sid,votes,off)，喂现役引擎 `_beat_pos_grid`；
     引擎每帧把预测位置写回 `audio.grid_pred_orig`（与生产 `engine._set_grid_hint` 一致）。
  4. 对照：同一份 hist、hint 恒为 None（= 旧行为）跑一遍，验证重放器能复现旧结果。

用法： P:\\AutoVJ\\venv\\Scripts\\python.exe tools/_fix3_d_hint.py
"""
import glob
import json
import os
import shutil
import statistics
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import numpy as np                 # noqa: E402
import soundfile as sf             # noqa: E402
import fp as FP                    # noqa: E402
import engine as E                 # noqa: E402

LA = os.environ.get("LOCALAPPDATA", "")
SRC_DB = os.path.join(LA, "AutoVJ", "fingerprints.db")
CFG = os.path.join(LA, "AutoVJ", "config.json")
TOFU = r"W:\临时结界相关\2of\tofu"
MAXSEC = int(os.environ.get("FIX3_MAXSEC", "150"))
HOP = 1.0
WIN = 4.5
SEQ = os.path.join(ROOT, "tools", "_fix3_hint_seq.json")
REUSE = os.environ.get("FIX3_REUSE", "0") == "1"
DT = 1.0 / 60.0


class FT:
    t = 0.0

    @staticmethod
    def perf_counter():
        return FT.t


E.time = FT


# ---------------------------------------------------------------- 采集直方图
def collect():
    tmp = tempfile.mkdtemp(prefix="fix3h_fp_")
    dst = os.path.join(tmp, "fp.db")
    for suf in ("", "-wal", "-shm"):
        if os.path.exists(SRC_DB + suf):
            shutil.copyfile(SRC_DB + suf, dst + suf)
    db = FP.FingerprintDB(dst)
    cfg = json.load(open(CFG, encoding="utf-8"))
    base2sid, base2grid = {}, {}
    for pp, vv in cfg.get("music_meta", {}).items():
        if isinstance(vv, dict) and vv.get("song_id") is not None:
            base2sid[os.path.basename(pp)] = vv["song_id"]
        if isinstance(vv, dict) and isinstance(vv.get("grid"), dict):
            base2grid[os.path.basename(pp)] = vv["grid"]

    files = sorted(glob.glob(os.path.join(TOFU, "*.flac")))
    print("素材 %d 首；前 %ds，hop=%.0fs，窗=%.1fs" % (len(files), MAXSEC, HOP, WIN))
    seqdump = []
    for song in files:
        exp = base2sid.get(os.path.basename(song), -1)
        g = base2grid.get(os.path.basename(song)) or {}
        bl = float(g.get("beat_len") or 0.5)
        anchor = float(g.get("anchor") or 0.0)
        data, sr = sf.read(song, dtype="float32", always_2d=True,
                           frames=int(MAXSEC * 44100))
        mono = np.mean(data, axis=1).astype(np.float32)
        if sr != 44100:
            x = np.linspace(0, len(mono) - 1, int(len(mono) * 44100 / sr))
            mono = np.interp(x, np.arange(len(mono)), mono).astype(np.float32)
            sr = 44100
        wl = int(WIN * sr)
        t = 0
        wins = []
        while t + wl <= len(mono):
            hist = _match_hist(db, mono[t:t + wl], sr)
            # 剪枝（压缩体积）：保留 exp 歌的**全部**候选（票可能=1，但正是 hint 要救的对象）、
            # 其它歌 votes>=2 的候选，并强制保留全局众数（可能 votes==1）。
            gmax = max(hist.values()) if hist else 0
            cand = [[s, d, v] for (s, d), v in hist.items()
                    if v >= 2 or s == exp or v == gmax]
            wins.append({"t": round(t / sr, 3), "exp": int(exp), "bl": bl,
                         "anchor": anchor, "cand": cand})
            t += int(HOP * sr)
        seqdump.append({"song": os.path.basename(song), "exp": int(exp), "wins": wins})
        print("  %-44s 窗=%3d" % (os.path.basename(song)[:44], len(wins)), flush=True)
    db.close()
    shutil.rmtree(tmp, ignore_errors=True)
    json.dump(seqdump, open(SEQ, "w", encoding="utf-8"), ensure_ascii=False)
    print("已写 %s" % SEQ)
    return seqdump


def _match_hist(db, sig, sr):
    """与 FingerprintDB.match 完全相同的投票统计，但**返回整张直方图** (sid,delta)->votes。"""
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
                f"SELECT hash, song_id, offset FROM fingerprints "
                f"WHERE hash IN ({ph})", chunk):
            for qoff in qoff_map.get(h, ()):
                key = (song_id, dboff - qoff)
                votes[key] = votes.get(key, 0) + 1
    return votes


# ---------------------------------------------------------------- 重放
def make_self():
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
    s._grid_prov = None
    s.audio = types.SimpleNamespace(grid_pred_orig=-1.0)
    for nm in ("_beat_pos_grid",):
        setattr(s, nm, types.MethodType(getattr(E.AutoVJEngine, nm), s))
    s.GRID_MIN_VOTES = E.AutoVJEngine.GRID_MIN_VOTES
    s.GRID_STRONG_VOTES = E.AutoVJEngine.GRID_STRONG_VOTES
    s.GRID_CONSIST_LO = E.AutoVJEngine.GRID_CONSIST_LO
    s.GRID_CONSIST_HI = E.AutoVJEngine.GRID_CONSIST_HI
    return s


def snap(sid, off, votes, ref_t):
    return {"bpm": 128.0, "beat_active": True, "beat_count": 0, "beat_phase": 0.0,
            "downbeat_off": 0.0, "energy": 0.5, "energy_trend": 0.0,
            "recognized_song_id": sid, "recognized_offset": off, "recognized_rate": 1.0,
            "recognized_t": ref_t, "recognized_votes": votes}


def replay(song, use_hint=True):
    """端到端重放一首歌。返回 (max|err|, errs, hint_errs, ngrid)。

    与生产链路对应：每窗先用「引擎上帧发布的 hint」跑 `_pick` 得到匹配结果，再逐帧喂引擎。
    use_hint=False 时 hint 恒 None（= 旧行为），用于复现基线。
    """
    wins = song["wins"]
    exp = song["exp"]
    if not wins:
        return 0.0, [], [], 0
    anchor = wins[0]["anchor"]
    bl = wins[0]["bl"]
    s = make_self()
    errs, hint_errs = [], []
    ngrid = 0
    win_by_start = {w["t"]: w for w in wins}
    t_end = wins[-1]["t"]
    cur = None            # 当前识别结果 {sid, off, votes}
    cur_ref = 1000.0
    t = 0.0
    while t <= t_end + DT:
        w = win_by_start.get(round(t, 3))
        if w is not None:
            # ---- 识别线程：读取引擎发布的预测位置当 hint，跑一次"匹配" ----
            hint_f = None
            if use_hint and s.audio.grid_pred_orig is not None and s.audio.grid_pred_orig >= 0.0:
                hint_f = s.audio.grid_pred_orig * FP.SAMPLE_RATE / FP.HOP_SIZE
            hist = {(c[0], c[1]): c[2] for c in w["cand"]}
            picked = FP.FingerprintDB._pick(hist, hint_f)
            if picked is None:
                cur = None
            else:
                (sid, delta), votes = picked
                cur = {"sid": int(sid), "off": delta * FP.HOP_SIZE / FP.SAMPLE_RATE,
                       "votes": int(votes)}
            # 诊断：hint 与"正确候选(t 处的 delta)"的偏差（锁定时才有意义）
            if hint_f is not None:
                true_delta = w["t"] * FP.SAMPLE_RATE / FP.HOP_SIZE
                hint_errs.append(abs(hint_f - true_delta))
            cur_ref = 1000.0 + t
        FT.t = 1000.0 + t
        grid_on = (cur is not None and cur["sid"] == exp and cur["votes"] > 0)
        if grid_on:
            # ★ 与生产一致：引擎消费的是**窗口末端**的原曲秒数（audio_engine:
            #   recognized_offset = off + 4.5·r），不是窗口起点。replay 必须同轴，
            #   否则引擎发布的 hint（= 末端 − 4.5·r）会系统性偏掉 4.5s。
            off_end = float(cur["off"]) + 4.5
            gs = ({"beat_len": bl, "anchor": anchor}, bl, anchor, off_end, 1.0, exp)
            pos = s._beat_pos_grid(snap(exp, off_end, int(cur["votes"]), cur_ref), gs)
            errs.append((t, pos - (t + 4.5 - anchor) / bl))
            ngrid += 1
        else:
            # 模拟生产：`_grid_state` 返回 None ⇒ `_beat_pos` 走原逻辑并清除 hint
            s.audio.grid_pred_orig = -1.0
        t += DT
    mx = max((abs(e) for _, e in errs), default=0.0)
    return mx, errs, hint_errs, ngrid


def report(seq, use_hint, title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)
    per = []
    allbad = []
    hmax = 0.0
    for song in seq:
        try:
            mx, errs, herr, ng = replay(song, use_hint=use_hint)
        except Exception as e:                                          # noqa: BLE001
            print("  %-40s 重放异常：%s" % (song["song"][:40], e))
            continue
        if not errs:
            continue
        bad = sum(1 for _, e in errs if abs(e) > 1.0) / max(1, len(errs))
        per.append((song["song"], mx, bad, ng))
        allbad.append(1.0 - bad)
        if herr:
            hmax = max(hmax, max(herr))
    per.sort(key=lambda r: -r[1])
    for nm, mx, bad, ng in per:
        print("  %-42s max|err|=%8.2f 拍；|err|<1 拍占比 %5.1f%%（网格帧 %d）"
              % (nm[:42], mx, 100 * (1 - bad), ng))
    print("  总体：%d 首；|err|<1 的时间占比 中位 %.1f%% / 均值 %.1f%%"
          % (len(per), 100 * statistics.median(allbad),
             100 * (sum(allbad) / max(1, len(allbad)))))
    if use_hint:
        print("  hint 与正确候选的偏差 max=%.1f 帧（HINT_W=%d）" % (hmax, FP.HINT_W))
    return per


def main():
    if REUSE and os.path.exists(SEQ):
        seq = json.load(open(SEQ, encoding="utf-8"))
        print("复用 %s（%d 首）" % (SEQ, len(seq)))
    else:
        seq = collect()
    seq = [d for d in seq if int(d.get("exp", -1)) >= 0]
    print("可核对曲目 %d 首" % len(seq))
    report(seq, False, "对照（hint=None，旧行为，应复现 148~738 拍）")
    report(seq, True, "★C（hint_delta=引擎预测位置）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
