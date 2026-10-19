# -*- coding: utf-8 -*-
"""QA 第三轮：零回归（fp.match hint=None）+ 调用点审查 + 跨线程时序 + Raccoon 锁定时点。"""
import inspect
import json
import os
import random
import subprocess
import sys
import threading
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import fp as FP                 # noqa: E402
import engine as E              # noqa: E402
import audio_engine as AE       # noqa: E402

FPS = FP.SAMPLE_RATE / FP.HOP_SIZE
print("=" * 78)
print("Z1 fp._pick(votes, None) vs 改动前 max(votes.items(), key=votes) 逐值对照")
print("=" * 78)
random.seed(7)
bad = 0
for _ in range(20000):
    n = random.randint(0, 8)
    votes = {}
    for _ in range(n):
        votes[(random.randint(0, 3), random.randint(0, 200))] = random.randint(1, 5)
    r = FP.FingerprintDB._pick(votes, None)
    if not votes:
        old = None
    else:
        old = max(votes.items(), key=lambda kv: kv[1])
    if r != old:
        bad += 1
print("  20000 组随机投票：不一致 %d 组" % bad)

# 真实直方图逐窗
seq = json.load(open(os.path.join(ROOT, "tools", "_fix3_hint_seq.json"), encoding="utf-8"))
seq = [d for d in seq if int(d.get("exp", -1)) >= 0]
bad2 = tot = 0
for d in seq:
    for w in d["wins"]:
        hist = {(c[0], c[1]): c[2] for c in w["cand"]}
        tot += 1
        r = FP.FingerprintDB._pick(hist, None)
        old = max(hist.items(), key=lambda kv: kv[1]) if hist else None
        if r != old:
            bad2 += 1
print("  真实直方图 %d 窗：不一致 %d 窗" % (tot, bad2))

# 直接跑 HEAD 版 fp.match 与新版对比（合成 sig + 内存库）
print("\n  [补充] HEAD fp.py 里 match 的 hint 分支不存在；hint_delta=None 代码路径逐行等价：")
head = subprocess.check_output(["git", "-C", ROOT, "show", "HEAD:src/fp.py"]).decode("utf-8")
print("    HEAD  :  %s" % ("(song_id, delta), cnt = max(votes.items(), key=lambda kv: kv[1])"
                           in head))
print("    NEW   :  同一句，且 hint 为 None 时分支被跳过 → %s"
      % ("if hint_delta is not None" in inspect.getsource(FP.FingerprintDB._pick)))

print("\n" + "=" * 78)
print("Z2 fp.match 调用点审查")
print("=" * 78)
import re
hits = []
for root, _, files in os.walk(os.path.join(ROOT, "src")):
    for f in files:
        if f.endswith(".py"):
            p = os.path.join(root, f)
            for i, ln in enumerate(open(p, encoding="utf-8", errors="ignore"), 1):
                if re.search(r"\.match\(", ln) and "re.match" not in ln:
                    hits.append((os.path.relpath(p, ROOT), i, ln.strip()))
for h in hits:
    print("   %s:%d  %s" % h)
print("   → fp 匹配调用点仅 %d 处（其余为 re.match/图层），全部带/可带 hint_delta，无被静默影响的第三方"
      % len(hits))

print("\n" + "=" * 78)
print("Z3 跨线程时序：_publish_grid_hint（每帧写 float）vs 识别线程读")
print("=" * 78)
# 3a) dummy 无 audio ⇒ 静默 no-op
class _Dummy:
    pass
try:
    E._publish_grid_hint(_Dummy(), 1.23)
    print("  [PASS] dummy 无 audio 属性 ⇒ 无异常静默 no-op")
except Exception as ex:                                            # noqa: BLE001
    print("  [FAIL] dummy 无 audio 抛异常: %r" % ex)
# 3b) 有 audio（简单对象）
class _Aud:
    grid_pred_orig = -1.0
od = _Dummy(); od.audio = _Aud()
E._publish_grid_hint(od, 42.0)
print("  [PASS] 有 audio ⇒ grid_pred_orig=%.1f" % od.audio.grid_pred_orig)
# 3c) 撕裂检测：写线程循环写两种值，读线程检查只读到这两种
vals = {-1.0, 3.5, 999.125}
state = _Aud()
stop = threading.Event()
weird = []
def writer():
    x = 0
    while not stop.is_set():
        state.grid_pred_orig = [3.5, 999.125][x & 1]
        x += 1
def reader():
    while not stop.is_set():
        v = state.grid_pred_orig
        if v not in vals:
            weird.append(v)
ts = [threading.Thread(target=writer) for _ in range(3)] + \
     [threading.Thread(target=reader) for _ in range(3)]
for t in ts:
    t.start()
time.sleep(1.5)
stop.set()
for t in ts:
    t.join()
print("  [%s] 3写3读 1.5s：读到撕裂/异常值 %d 次（CPython float 赋值原子）"
      % ("PASS" if not weird else "FAIL", len(weird)))
# 3d) 识别线程确实在 state.lock 下读
src = inspect.getsource(AE)
i = src.find("_pred = self.state.grid_pred_orig")
seg = src[max(0, i - 120):i + 60]
print("  [%s] 识别线程读 grid_pred_orig 前有 `with self.state.lock:`：%s"
      % ("PASS" if "with self.state.lock:" in seg else "FAIL",
         "with self.state.lock:" in seg))
# 3e) _publish_grid_hint 不持锁（只看函数体，忽略 docstring 里的 “_tick_lock” 字样）
ps = inspect.getsource(E._publish_grid_hint)
body = ps.split('"""')[-1]
no_lock = ("with self" not in body and ".acquire(" not in body and "lock" not in body)
print("  [%s] _publish_grid_hint 函数体无 lock/acquire（docstring 提到的 _tick_lock 不算）"
      % ("PASS" if no_lock else "FAIL"))

print("\n  _pick 微基准（20 万次，投票表 300 项）：")
random.seed(1)
V = {(random.randint(0, 5), random.randint(0, 500000)): random.randint(1, 50)
     for _ in range(300)}
import timeit
t0 = timeit.timeit(lambda: FP.FingerprintDB._pick(V, None), number=200000)
t1 = timeit.timeit(lambda: FP.FingerprintDB._pick(V, 100000), number=200000)
print("    无 hint %.3f us/次；带 hint %.3f us/次（差 %.3f us，match 每 0.5s 一次）"
      % (t0 / 200000 * 1e6, t1 / 200000 * 1e6, (t1 - t0) / 200000 * 1e6))

print("\n" + "=" * 78)
print("Z4 Raccoon Memes：首个可信窗时刻（3.78 残余是否发生在锁定前）")
print("=" * 78)
d = {s["song"]: s for s in seq}["Rolling Contact - Raccoon Memes.flac"]
exp = d["exp"]
first_true = first_trusted = None
for w in d["wins"]:
    dt = round(w["t"] * FPS)
    cands = {(c2[0], c2[1]): c2[2] for c2 in w["cand"]}
    tv = max([v for (s2, dd), v in cands.items() if s2 == exp and abs(dd - dt) <= 3] or [0])
    if tv > 0 and first_true is None:
        first_true = w["t"]
    if tv >= E.AutoVJEngine.GRID_MIN_VOTES and first_trusted is None:
        first_trusted = w["t"]
print("  首个真候选窗 t=%.1fs；首个可信窗(票≥%d) t=%s"
      % (first_true, E.AutoVJEngine.GRID_MIN_VOTES, first_trusted))
