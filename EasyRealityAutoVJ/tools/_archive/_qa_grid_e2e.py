# -*- coding: utf-8 -*-
"""QA: 真数据端到端 —— 真 FLAC(只读) → 真指纹库(临时目录) → match → 换算 offset → 喂网格时钟。

绝不触碰 %LOCALAPPDATA%\\AutoVJ；指纹库建在 tempfile 临时目录。
"""
import hashlib
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np            # noqa: E402
import soundfile as sf        # noqa: E402
import beatgrid as bg         # noqa: E402
from fp import FingerprintDB  # noqa: E402
from beatgrid import GridClock  # noqa: E402

DIR = "W:/临时结界相关/2of/tofu"
SONGS = ["Rolling Contact - Cosmic String.flac",
         "Rolling Contact - Demon's Mischief.flac",
         "Rolling Contact - Raver Stone.flac"]

SR = 44100


def to44(mono, sr0):
    return bg.resample(mono, sr0, SR) if sr0 != SR else mono.astype(np.float32)


def main():
    tmp = tempfile.mkdtemp(prefix="qa_grid_e2e_")
    dbp = os.path.join(tmp, "fp.db")
    print("临时指纹库：", dbp)
    vdj = bg.load_vdj()
    db = FingerprintDB(dbp)
    sids = {}
    for f in SONGS:
        p = os.path.join(DIR, f)
        data, sr = sf.read(p, dtype="float32", always_2d=True)
        sig44 = to44(np.mean(data, axis=1), sr)
        sid = int(hashlib.md5(p.encode("utf-8")).hexdigest()[:8], 16)
        sids[f] = (p, sig44, sid)
        db.add_song(sid, sig44, SR)
        print("入库 %-40s sid=%d  dur=%.1fs" % (f[:40], sid, len(sig44) / SR))
    db.commit_batch()

    ok_all = True
    print("\n" + "=" * 78)
    for f in SONGS:
        p, sig44, sid = sids[f]
        grid = bg.compute_grid(p, vdj)
        bl, anchor = float(grid["beat_len"]), float(grid["anchor"])
        for P in (40.0, 95.0, 150.0):
            i0 = int(P * SR)
            q = sig44[i0:i0 + int(4.5 * SR)]
            if len(q) < SR:
                continue
            msid, votes, off = db.match(q, SR)
            # 引擎换算（r=1）
            r = 1.0
            recognized_offset = off + 4.5 * r
            # 网格时钟
            clk = GridClock()
            clk.update(msid, bl, anchor, recognized_offset, r, 0.0)
            pos = clk.pos(0.0)
            exp_pos = (recognized_offset - anchor) / bl
            b2p = clk.beats_to_phrase(0.0)
            # 独立公式核对：下一个八拍头
            nxt_sec = bg.grid_anchor_after(grid, recognized_offset)
            b2p_sec = b2p * bl / r
            sid_ok = (msid == sid)
            off_ok = abs(off - P) < 0.15
            pos_ok = abs(pos - exp_pos) < 1e-6
            phrase_ok = abs(b2p_sec - (nxt_sec - recognized_offset)) < 0.05
            allok = sid_ok and off_ok and pos_ok and phrase_ok
            ok_all &= allok
            print("%-28s P=%5.1fs  命中sid=%s votes=%4d  match_off=%.3f(真值%.1f)"
                  % (f.replace("Rolling Contact - ", "")[:28], P,
                     "OK" if sid_ok else "错", votes, off, P))
            print("      识别offset(+4.5r)=%.3f | grid坐标 pos=%.4f (期望 (off-anchor)/bl=%.4f) %s"
                  % (recognized_offset, pos, exp_pos, "OK" if pos_ok else "!!"))
            print("      下一个八拍头: beats_to_phrase=%.4f 拍 = %.3f 秒; 独立公式 grid_anchor_after=%.3f 秒 (差 %.3f) %s"
                  % (b2p, b2p_sec, nxt_sec, abs(b2p_sec - (nxt_sec - recognized_offset)),
                     "OK" if phrase_ok else "!!"))
    db.close()
    print("\n总体：" + ("真数据链路通过（识别→offset→网格拍位/八拍头全部自洽）"
                       if ok_all else "存在不一致"))
    try:
        os.remove(dbp)
        os.rmdir(tmp)
    except Exception:
        pass
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
