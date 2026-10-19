# -*- coding: utf-8 -*-
"""QA 第三轮：#3 真实重复段落素材实测 —— 「从重复段落的第二处开始播放」。

真实 FLAC（只读）+ 真实指纹库（复制到临时目录）。对 Cosmic String 等歌扫描全曲，
找出「同一段音频在歌里出现两次（X1<X2）」的窗口，然后：
  · hint=None            → 看全局众数把它放在 X1 还是 X2；
  · hint=X1（较早一处）  → 看是否被 hint 强行拉向 X1（真实播放却在 X2）—— 这就是 hint 主动制造错误；
  · hint=X2（正确一处）  → 看是否稳定跟随。
"""
import glob
import os
import shutil
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import numpy as np                    # noqa: E402
import soundfile as sf                # noqa: E402
import fp as FP                       # noqa: E402

FPS = FP.SAMPLE_RATE / FP.HOP_SIZE
HOP = FP.HOP_SIZE / FP.SAMPLE_RATE
TOFU = r"W:\临时结界相关\2of\tofu"
LA = os.environ.get("LOCALAPPDATA", "")


def open_db():
    tmp = tempfile.mkdtemp(prefix="qa3_real_")
    dst = os.path.join(tmp, "fp.db")
    for suf in ("", "-wal", "-shm"):
        src = os.path.join(LA, "AutoVJ", "fingerprints.db") + suf
        if os.path.exists(src):
            shutil.copyfile(src, dst + suf)
    return FP.FingerprintDB(dst), tmp


def hist_of(db, sig, sr):
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
                f"SELECT hash, song_id, offset FROM fingerprints WHERE hash IN ({ph})",
                chunk):
            for qoff in qoff_map.get(h, ()):
                key = (song_id, dboff - qoff)
                votes[key] = votes.get(key, 0) + 1
    return votes


def scan(song_name, sid, mono, db):
    print("\n" + "=" * 78)
    print("扫描 %s（song_id=%s，%.0fs）—— 找重复段落" % (song_name, sid, len(mono) / 44100))
    print("=" * 78)
    wl = int(4.5 * 44100)
    rows = []
    pos = 0
    while pos + wl <= len(mono):
        hist = hist_of(db, mono[pos:pos + wl], 44100)
        same = {k: v for k, v in hist.items() if k[0] == int(sid)}
        if len(same) >= 2:
            dt = pos / FP.HOP_SIZE        # 真候选 delta（帧）= 窗起点样本 / HOP
            near = min(same.items(), key=lambda kv: abs(kv[0][1] - dt))
            far = max(same.items(), key=lambda kv: kv[1])
            if abs(far[0][1] - dt) > FP.HINT_W and far[1] >= 40:
                rows.append((pos / 44100.0, far[0][1] / FPS, far[1], near[1],
                             abs(far[0][1] - dt)))
        pos += 44100
    rows.sort(key=lambda r: -r[2])
    print("强重复窗（同歌错候选票≥40 且距真值>HINT_W）：%d 个" % len(rows))
    for t, X, fv, nv, dist in rows[:8]:
        print("  起点 t=%6.1fs：另一处 X=%7.2fs（Δ=%+7.2fs=%.0f 帧）错票=%4d 真票=%4d"
              % (t, X, X - t, dist, fv, nv))
    return rows


def hint_test(db, mono, t_true, x_other, label):
    """查询窗位于 t_true（真实播放处），另一处 x_other。测三种 hint。"""
    wl = int(4.5 * 44100)
    sig = np.asarray(mono[int(t_true * 44100):int(t_true * 44100) + wl], dtype=np.float32)
    for hn in (None, x_other, t_true):
        hint_f = None if hn is None else hn * FPS
        r = db.match(sig, 44100, hint_delta=hint_f)
        off = r[2]
        tag = "无 hint" if hn is None else ("hint=%.1fs(%s)" % (hn, label))
        verdict = ""
        if abs(off - t_true) <= 0.1:
            verdict = "→ 正确(跟随真实处)"
        elif abs(off - x_other) <= 0.1:
            verdict = "→ 被拉到另一处 ✗"
        else:
            verdict = "→ 其它"
        print("      %-26s sid=%s votes=%4d off=%8.2fs  真值=%.1fs  %s"
              % (tag, r[0], r[1], off, t_true, verdict))
    return


def main():
    db, tmp = open_db()
    try:
        cfg_songs = {}
        # Cosmic String：历史数据里错候选恒 +115.22s
        for name in ("Rolling Contact - Cosmic String.flac",
                     "Rolling Contact - Can We Believe That.flac"):
            p = os.path.join(TOFU, name)
            if not os.path.exists(p):
                continue
            data, sr = sf.read(p, dtype="float32", always_2d=True,
                               frames=int(280 * 44100))
            mono = np.mean(data, axis=1).astype(np.float32)
            # 找 sid
            hist0 = hist_of(db, mono[100*44100:100*44100 + int(4.5*44100)], 44100)
            sid = max(hist0.items(), key=lambda kv: kv[1])[0][0] if hist0 else -1
            rows = scan(name, sid, mono, db)
            if rows:
                t_true, x_other = rows[0][0], rows[0][1]
                print("\n  ▶ 关键实验：假设从「较晚一处 X=%.2fs」开始播放" % x_other)
                # 真实播放处在 x_other：查询窗取 x_other 起点
                print("     [场景 A] 真实播放于 X2=%.2fs，另一处在 X1=%.2fs" % (x_other, t_true))
                hint_test(db, mono, x_other, t_true, "较早X1")
                print("     [场景 B] 真实播放于 X1=%.2fs，另一处在 X2=%.2fs" % (t_true, x_other))
                hint_test(db, mono, t_true, x_other, "较晚X2")
    finally:
        db.close()
        shutil.rmtree(tmp, ignore_errors=True)


main()
