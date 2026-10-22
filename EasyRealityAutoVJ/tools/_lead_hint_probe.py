# -*- coding: utf-8 -*-
"""复核 V1 那一处 offset 偏差：到底是 bug，还是「重复段落 + 无 hint」的已知降级路径？

fp.py 的设计（见其注释）：match() 可带 `hint_delta`（引擎网格时钟预测的窗口起点，单位 hop 帧）
⇒ 优先选**预测位置附近**的候选；窗内无候选才退回全局众数。
生产路径 `audio_engine._recognize_loop` **是带 hint 的**（`state.grid_pred_orig`）。

本脚本对同一段音频跑三种调用：
  A 无 hint（我刚才的测法）        → 应复现错位置 28.05s
  B hint = 正确位置 140.6s        → 必须选对（证明 hint 真的解决歧义）
  C hint = 错误位置 28.05s        → 应选 28.05（证明它确实跟着预测走，不是碰巧）
"""
import os, sys, glob, shutil, tempfile, hashlib
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import numpy as np, soundfile as sf
import fp as fpvj

TMP = tempfile.mkdtemp(prefix="hintprobe_")
db_tmp = os.path.join(TMP, "fp.db")
shutil.copyfile(os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ", "fingerprints.db"), db_tmp)
db = fpvj.FingerprintDB(db_tmp)

P = "W:/CloudMusic/Sociable State/東方太嗨 -Touhou Toohigh Trance-/Sociable State - 1!2!3!☆J.A.C.R.P.D.mp3"
data, sr = sf.read(P, dtype="float32", always_2d=True)
mono = np.ascontiguousarray(np.mean(data, axis=1), dtype=np.float32)
AT = 140.6
seg = mono[int(AT*sr): int(AT*sr)+int(4.5*sr)]
HOP = fpvj.HOP_SIZE
fw = int(4.5 * fpvj.SAMPLE_RATE / HOP)      # 窗口半宽（帧），宽松一点

def run(tag, hint_sec=None):
    hint = None if hint_sec is None else hint_sec * fpvj.SAMPLE_RATE / HOP
    sid, votes, off = db.match(seg, sr, hint_delta=hint, hint_w=fw if hint is not None else None)
    print("%-26s sid=%s votes=%-4s off=%8.3f  (真值 %.1f, Δ=%+.3f)" % (
        tag, sid, votes, off, AT, off - AT))
    return off

print("真值 %.1fs / 期望 sid=2651287981" % AT)
a = run("A 无 hint")
b = run("B hint=140.6s(正确)", AT)
c = run("C hint=28.05s(错误)", 28.05)
print()
print("结论：")
print("  A 复现错位置 28.05s        :", "是" if abs(a-28.05) < 2 else "否（%.2f）" % a)
print("  B 带正确 hint ⇒ 选对      :", "是" if abs(b-AT) <= 0.5 else "否（%.2f）" % b)
print("  C 带错误 hint ⇒ 跟着走    :", "是" if abs(c-28.05) < 2 else "否（%.2f）" % c)
print("HINT_PROBE 完成")
db.close()
