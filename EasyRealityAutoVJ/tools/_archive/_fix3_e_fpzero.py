# -*- coding: utf-8 -*-
"""第三轮 ★D：`fp.match` 零回归 + 性能对照。

1. 零回归：从 `git show HEAD:src/fp.py` 取**改动前**实现，与当前实现搭对照；
   同一批真实查询窗，逐值比较 `match(sig,sr)`（hint_delta=None）的 (song_id, votes, off)。
2. 性能：同一批窗分别计时 —— 旧实现 / 新实现(hint=None) / 新实现(带 hint)，
   给出「每次调用耗时」对比，验证新增的窗口筛选没有可感开销。
3. hint 效果抽检：对若干"坏窗"（全局众数位置错），展示 hint 把它拉回正确位置。

用法： P:\\AutoVJ\\venv\\Scripts\\python.exe tools/_fix3_e_fpzero.py
"""
import glob
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import numpy as np          # noqa: E402
import soundfile as sf      # noqa: E402
import fp as FP             # noqa: E402

LA = os.environ.get("LOCALAPPDATA", "")
SRC_DB = os.path.join(LA, "AutoVJ", "fingerprints.db")
CFG = os.path.join(LA, "AutoVJ", "config.json")
TOFU = r"W:\临时结界相关\2of\tofu"


def load_old():
    src = subprocess.check_output(
        ["git", "-C", ROOT, "show", "HEAD:src/fp.py"]).decode("utf-8")
    d = tempfile.mkdtemp(prefix="fpo_")
    p = os.path.join(d, "fp_old.py")
    open(p, "w", encoding="utf-8").write(src)
    spec = importlib.util.spec_from_file_location("fp_old", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main():
    old = load_old()
    print("旧实现 FP_SCHEMA_VERSION=%s / 新实现=%s（必须相同，否则会清库）"
          % (getattr(old, "FP_SCHEMA_VERSION", "?"), FP.FP_SCHEMA_VERSION))
    assert getattr(old, "FP_SCHEMA_VERSION", None) == FP.FP_SCHEMA_VERSION

    tmp = tempfile.mkdtemp(prefix="fpz_")
    dst = os.path.join(tmp, "fp.db")
    for suf in ("", "-wal", "-shm"):
        if os.path.exists(SRC_DB + suf):
            shutil.copyfile(SRC_DB + suf, dst + suf)
    db_new = FP.FingerprintDB(dst)
    db_old = old.FingerprintDB(dst)

    cfg = json.load(open(CFG, encoding="utf-8"))
    base2sid = {os.path.basename(p): v["song_id"]
                for p, v in cfg.get("music_meta", {}).items()
                if isinstance(v, dict) and v.get("song_id") is not None}
    files = sorted(glob.glob(os.path.join(TOFU, "*.flac")))[:6]
    wins = []          # (sig, sr, true_t, exp_sid, bl)
    for song in files:
        exp = base2sid.get(os.path.basename(song), -1)
        g = (cfg["music_meta"].get(next((p for p in cfg["music_meta"]
             if os.path.basename(p) == os.path.basename(song)), ""), {}) or {})
        bl = float((g.get("grid") or {}).get("beat_len") or 0.5)
        data, sr = sf.read(song, dtype="float32", always_2d=True, frames=int(60 * 44100))
        mono = np.mean(data, axis=1).astype(np.float32)
        if sr != 44100:
            x = np.linspace(0, len(mono) - 1, int(len(mono) * 44100 / sr))
            mono = np.interp(x, np.arange(len(mono)), mono).astype(np.float32)
            sr = 44100
        wl = int(4.5 * sr)
        for ts in range(0, 55, 3):
            t0 = ts * sr
            if t0 + wl > len(mono):
                break
            wins.append({"sig": mono[t0:t0 + wl], "sr": sr, "t": ts,
                         "exp": exp, "bl": bl, "song": os.path.basename(song)})
    print("对照窗口 %d 个（%d 首）" % (len(wins), len(files)))

    # ---- 1) 零回归逐值比较 ----
    diffs = 0
    for w in wins:
        s0, v0, o0 = db_old.match(w["sig"], w["sr"])
        s1, v1, o1 = db_new.match(w["sig"], w["sr"])          # hint_delta=None
        if (s0, v0) != (s1, v1) or abs(o0 - o1) > 1e-12:
            diffs += 1
            if diffs <= 5:
                print("  差异：t=%ds old=(%d,%d,%.9f) new=(%d,%d,%.9f)"
                      % (w["t"], s0, v0, o0, s1, v1, o1))
    print("[零回归] hint_delta=None：%d/%d 窗逐值一致  => %s"
          % (len(wins) - diffs, len(wins), "PASS" if diffs == 0 else "FAIL"))

    # ---- 2) 性能 ----
    def bench(fn, n=2):
        t0 = time.perf_counter()
        for _ in range(n):
            for w in wins:
                fn(w)
        return (time.perf_counter() - t0) / (n * len(wins)) * 1000.0

    def f_old(w):
        db_old.match(w["sig"], w["sr"])

    def f_new_none(w):
        db_new.match(w["sig"], w["sr"])

    def f_new_hint(w):
        db_new.match(w["sig"], w["sr"], hint_delta=w["t"] * FP.SAMPLE_RATE / FP.HOP_SIZE)

    ms_old = bench(f_old)
    ms_new0 = bench(f_new_none)
    ms_newh = bench(f_new_hint)
    print("[性能] 每次 match 平均 %.2f ms：旧=%.2f  新(hint=None)=%.2f(Δ%+.2f%%)  "
          "新(带hint)=%.2f(Δ%+.2f%%)"
          % (ms_old, ms_old, ms_new0, 100 * (ms_new0 - ms_old) / ms_old,
             ms_newh, 100 * (ms_newh - ms_new0) / ms_new0))

    # ---- 3) hint 抽检：坏窗被拉回 ----
    fixed = 0
    checked = 0
    for w in wins:
        s0, v0, o0 = db_new.match(w["sig"], w["sr"])
        if s0 != w["exp"] or v0 <= 0:
            continue
        if abs(o0 - w["t"]) / w["bl"] <= 1.0:
            continue                                          # 本来就是好窗
        checked += 1
        _, vh, oh = db_new.match(w["sig"], w["sr"],
                                 hint_delta=w["t"] * FP.SAMPLE_RATE / FP.HOP_SIZE)
        if abs(oh - w["t"]) / w["bl"] < 1.0:
            fixed += 1
        if checked <= 8:
            print("  坏窗 t=%ds：全局 off=%.2fs(%+.1f拍) → 带hint off=%.2fs(%+.2f拍)"
                  % (w["t"], o0, (o0 - w["t"]) / w["bl"], oh, (oh - w["t"]) / w["bl"]))
    print("[hint 抽检] 坏窗 %d 个；带 hint 后回到正确位置 %d 个（%.1f%%）"
          % (checked, fixed, 100 * fixed / max(1, checked)))

    db_new.close()
    db_old.close()
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if diffs == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
