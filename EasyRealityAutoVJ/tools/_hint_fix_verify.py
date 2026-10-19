# -*- coding: utf-8 -*-
"""对照：同一段现场录音，三种 hint 策略下「电荷条要几拍才能确认」。

A 无 hint                 —— 上一版（101718）：重复段落里 offset 会跳，charging 判据不成立
B 拍钟 hint（未锁定时）     —— 更早的版本：窗口落点无意义，挑中低票候选
C 连续性 hint（本版）       —— 上次偏移 + dt
"""
import os, sys, shutil, tempfile, time
sys.path.insert(0, "src")
import numpy as np, soundcard as sc
import fp as fpvj
from charge_engine import ChargeBarEngine, FpResult

SR, WIN, SEC = 48000, 4.5, 30.0
spk = sc.default_speaker()
mic = sc.get_microphone(id=str(spk.name), include_loopback=True)
print("录 %.0fs 现场声音…" % SEC)
with mic.recorder(samplerate=SR, channels=2) as rec:
    data = rec.record(numframes=int(SR * SEC))
mono = np.ascontiguousarray(data.mean(axis=1), dtype=np.float32)
print("RMS=%.4f\n" % float(np.sqrt(np.mean(mono ** 2))))

db_src = os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ", "fingerprints.db")
tmp = os.path.join(tempfile.mkdtemp(), "fp.db"); shutil.copyfile(db_src, tmp)
db = fpvj.FingerprintDB(tmp)

FP_SR, FP_HOP = fpvj.SAMPLE_RATE, fpvj.HOP_SIZE
step, w = int(SR * 0.5), int(SR * WIN)


def run(mode):
    eng = ChargeBarEngine()
    last = None          # (sid, votes, off)
    t = time.time()
    for k, end in enumerate(range(w, len(mono) + 1, step)):
        seg = mono[end - w:end]
        hint, hw = None, None
        if mode == "B" and last is not None:
            # 模拟"拍钟预测"：未锁定时它没有意义 ⇒ 用随机偏一个量来代表"落点随机"
            hint = (last[2] + 40.0) * FP_SR / FP_HOP
            hw = 0.3 * FP_SR / FP_HOP
        elif mode == "C" and last is not None and last[1] >= 4:
            hint = (last[2] + 0.5) * FP_SR / FP_HOP
            hw = 0.3 * FP_SR / FP_HOP
        sid, votes, off = db.match(seg, SR, hint_delta=hint, hint_w=hw)
        last = (sid, votes, off)
        eng.tick(FpResult(matched=(votes > 0), song_id=sid, aligned_votes=votes,
                          offset_sec=off), t + k * 0.5)
        if eng.current_song_id >= 0:
            return k + 1, eng.current_song_id, votes
    return None, -1, last[1] if last else 0


names = {"A": "无 hint（101718）", "B": "拍钟 hint（未锁定·落点随机）", "C": "连续性 hint（本版）"}
for m in ("A", "B", "C"):
    n, sid, v = run(m)
    print("  %-34s %s" % (names[m],
          ("%d 拍后确认  sid=%s（末拍 %d 票）" % (n, sid, v)) if n else "**30s 内未确认**（末拍 %d 票）" % v))
db.close()
