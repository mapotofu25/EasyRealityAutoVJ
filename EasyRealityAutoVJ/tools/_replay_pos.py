# -*- coding: utf-8 -*-
"""**真数据重放**：把"引擎实际听到的音频"按 0.5s 步长过一遍真指纹库，
再把识别结果逐条喂给 `CuePositionTracker`，看位置层会不会跳。

为什么这不是"脚本模拟"：
  · 音频 = `tools/_tempo/*.wav`（真 SOLA 变速产物，和真机放的是同一份）
  · 识别 = 项目自己的 `fp.FingerprintDB.match()` + 真指纹库
  · 位置 = 项目自己的 `CuePositionTracker`（被测代码本体）
唯一被"离线化"的只有挂钟时间（用固定步长代替真实调度），而识别结果本身是真的。

用法：
  python tools/_replay_pos.py                 # 中途变速 1.10→1.20
  python tools/_replay_pos.py 1.20            # 单段恒定变速
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np                                              # noqa: E402
import soundfile as sf                                          # noqa: E402

import cue_position as CP                                       # noqa: E402
from audio_engine import RateEstimator                           # noqa: E402
import fp as F                                                  # noqa: E402

DB = os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ", "fingerprints.db")
TEMPO = os.path.join(ROOT, "tools", "_tempo")
WIN = 4.5            # 引擎的识别窗口（FP_WIN_SEC）
STEP = 0.5           # 引擎的识别节奏
BL = 60.0 / 140.0    # 揽月 140 BPM


def load_program(mode):
    """拼出"真机实际听到的那段音频"。"""
    if mode == "mid":
        a, sr = sf.read(os.path.join(TEMPO, "lanyue_1.10.wav"), dtype="float32",
                        always_2d=True, frames=int(30.0 * 44100))
        b, _ = sf.read(os.path.join(TEMPO, "lanyue_1.20.wav"), dtype="float32",
                       always_2d=True, start=int(27.5 * 44100), frames=int(50.0 * 44100))
        return np.concatenate([a, b], axis=0), sr
    rate = float(mode)
    return sf.read(os.path.join(TEMPO, "lanyue_%.2f.wav" % rate), dtype="float32",
                   always_2d=True, frames=int(90.0 * 44100))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "mid"
    data, sr = load_program(mode)
    mon = data.mean(axis=1)
    print("重放 mode=%s  时长 %.1fs  sr=%d" % (mode, len(data) / sr, sr))
    db = F.FingerprintDB(DB)
    trk = CP.CuePositionTracker()
    est = RateEstimator()      # ★ 必须和引擎一样：先把实测倍率写进 trk.rate，
                               #   否则 `_pred` 按 1.0 外推 ⇒ 误判"大跳变"（重放会失真）
    rows = []
    t = WIN
    while t + STEP <= len(mon) / sr:
        seg = mon[int((t - WIN) * sr):int(t * sr)]
        sid, votes, off = db.match(seg, sr)
        if sid >= 0 and votes > 0:
            trk.rate = est.update(sid, off, t) or 1.0
        st, pb, _song = trk.feed(sid, off, votes, t, beat_len=BL)
        rows.append((t, sid, votes, off, st, pb))
        t += STEP
    db.close()

    prev = None
    jumps = 0
    for (t, sid, votes, off, st, pb) in rows:
        d = None
        if prev is not None and pb is not None and prev is not None:
            d = (pb - prev) / STEP
        flag = ""
        if d is not None and (d > 6.0 or d < -1.0):
            flag = "  <<< 跳 %.1f 拍/秒" % d
            jumps += 1
        if flag or votes < 25 or st != "ok":
            print("  t=%5.1fs votes=%-3d off=%7.2f %-5s pb=%s%s"
                  % (t, votes, off, st, ("%.1f" % pb) if pb is not None else "None", flag))
        prev = pb if pb is not None else prev

    print("\n跳变次数 = %d（%d 个采样，%.0f 秒）" % (jumps, len(rows), len(rows) * STEP))
    return 0


if __name__ == "__main__":
    sys.exit(main())
