# -*- coding: utf-8 -*-
"""**整曲真数据重放**：把"引擎实际听到的音频"按 0.5s 步长过真指纹库，喂真位置层，
并用**已知真值**（我们自己造的播放时间轴）算绝对误差。

为什么这能把问题测死：
  · 音频切片是 `[t-4.5, t]`，内容恰好落在曲内第 `t` 秒 ⇒ **真值 = t**，不需要任何推断；
  · 识别 = 项目自己的 `fp.FingerprintDB.match()` + 真指纹库；
  · 位置 = 项目自己的 `CuePositionTracker` + `RateEstimator`（被测代码本体）。
唯一"离线化"的是挂钟（固定步长代替真实调度），识别结果本身是真的。

判据（1.0× 播放）：
  · 绝对误差 |pos − t|：P50/P95/max
  · 倒退跳变：Δpos < −0.15s 的次数与幅度（观众看到"倒一下"）
  · 丢位：status=lost 的采样数

用法：
  python tools/_replay_pos.py full    [wav] [rate]
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np                                              # noqa: E402
import soundfile as sf                                          # noqa: E402

import cue_position as CP                                       # noqa: E402
import fp as F                                                  # noqa: E402
from audio_engine import RateEstimator                           # noqa: E402

DB = os.environ.get("FP_DB") or os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ",
                                              "fingerprints.db")
TEMPO = os.path.join(ROOT, "tools", "_tempo")
XING = r"Y:/EasyRealityAutoVJ/码表素材/西行谣 feat. 凛花 - Sociable State, 凛花, Northad, DJ Tranceair.flac"
WIN = 4.5
STEP = 0.5
BL = 60.0 / 142.0


def main():
    src = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] not in ("", "-") else XING
    rate = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
    data, sr = sf.read(src, dtype="float32", always_2d=True)
    mon = data.mean(axis=1)
    dur = len(mon) / sr / rate
    print("重放 %s  曲长 %.1fs  倍率 %.2f  重放时长 %.1fs"
          % (os.path.basename(src), len(mon) / sr, rate, dur))

    db = F.FingerprintDB(DB)
    trk = CP.CuePositionTracker()
    est = RateEstimator()
    rows = []
    t = WIN
    while t + STEP <= dur:
        # 真值 = t；切片 [t*rate-WIN*rate, t*rate) 换算成源文件采样点
        a = int(max(0.0, (t - WIN) * rate) * sr)
        b = int(min(len(mon), t * rate * sr))
        seg = mon[a:b]
        sid, votes, off = db.match(seg, sr)
        if sid >= 0 and votes > 0:
            trk.rate = est.update(sid, off, t) or 1.0
        st, pb, _song = trk.feed(sid, off, votes, t, beat_len=BL)
        pos = None if pb is None else pb * BL
        rows.append((t, sid, votes, off, st, pos, trk.rate))
        t += STEP
    db.close()

    good = [r for r in rows if r[5] is not None]
    errs = sorted(abs(r[5] - r[0]) for r in good)
    back = []
    for a, b in zip(good, good[1:]):
        d = b[5] - a[5]
        if d < -0.15:
            back.append((b[0], d))
    lost = sum(1 for r in rows if r[4] == "lost")
    print("\n采样 %d（有效 %d），丢位 %d（%.1f%%）"
          % (len(rows), len(good), lost, 100.0 * lost / max(1, len(rows))))
    if errs:
        n = len(errs)
        print("绝对误差 |pos−真值|：P50 %.2fs  P95 %.2fs  max %.2fs"
              % (errs[n // 2], errs[min(n - 1, int(n * 0.95))], errs[-1]))
    print("倒退跳变 %d 次；最大倒退 %.2fs" % (len(back), min([d for _t, d in back], default=0.0)))
    for t, d in back[:12]:
        print("   t=%6.1fs  倒退 %.2fs" % (t, d))
    print("末段倍率 r=%.3f" % rows[-1][6])
    return 0


if __name__ == "__main__":
    sys.exit(main())
