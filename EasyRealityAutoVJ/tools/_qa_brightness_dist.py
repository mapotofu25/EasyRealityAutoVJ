# -*- coding: utf-8 -*-
"""任务A：真实素材落档分布独立复现。用法: python tools/_qa_brightness_dist.py [N]
N=0 表示全量；默认 25。"""
import sys, os, time
sys.path.insert(0, "src")
import numpy as np
import config
from tagger import Tagger
import tags_def as td

N = int(sys.argv[1]) if len(sys.argv) > 1 else 25
c = config.Config()
lib = [p for p in c["library"] if os.path.exists(p)]
if N and N < len(lib):
    # 均匀抽样，覆盖各种素材而非只取前 N
    idx = np.linspace(0, len(lib) - 1, N).astype(int)
    lib = [lib[i] for i in idx]
print("将分析素材数 =", len(lib), "(全库可用", len([p for p in c['library'] if os.path.exists(p)]), ")")

def kind_of(p):
    return "image" if os.path.splitext(p)[1].lower() in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp") else "video"

def bucket(med):
    if med is None:
        return None
    if med < td.BRIGHTNESS_DARK_THRESHOLD:
        return td.BRIGHT_DARK
    if med >= td.BRIGHTNESS_BRIGHT_THRESHOLD:
        return td.BRIGHT_LIGHT
    return td.BRIGHT_MID

meds = []
none_cnt = 0
cnt = {td.BRIGHT_DARK: 0, td.BRIGHT_MID: 0, td.BRIGHT_LIGHT: 0}
t0 = time.time()
for i, p in enumerate(lib):
    k = kind_of(p)
    try:
        motion, flicker, diff, med = Tagger.analyze_dynamic(p, k)
    except Exception as e:
        print("  [异常]", os.path.basename(p), repr(e))
        med = None
    if med is None:
        none_cnt += 1
        print("  [med=None]", os.path.basename(p), "kind=", k)
        continue
    meds.append(med)
    b = bucket(med)
    cnt[b] += 1
    if i % 10 == 0 or i == len(lib) - 1:
        print("  ...%d/%d  %.1fs" % (i + 1, len(lib), time.time() - t0))

el = time.time() - t0
print("=" * 60)
print("耗时 %.1f 秒 / %d 素材" % (el, len(lib)))
print("med 为 None 的数量 =", none_cnt)
if meds:
    a = np.asarray(meds, dtype=np.float64)
    print("有效 med 数 =", len(a))
    print("min=%.1f P10=%.1f P25=%.1f 中位=%.1f P75=%.1f P90=%.1f max=%.1f" % (
        a.min(), np.percentile(a, 10), np.percentile(a, 25), np.median(a),
        np.percentile(a, 75), np.percentile(a, 90), a.max()))
    tot = len(meds)
    print("落档: 暗 %d(%.1f%%) / 中 %d(%.1f%%) / 亮 %d(%.1f%%)" % (
        cnt[td.BRIGHT_DARK], 100.0 * cnt[td.BRIGHT_DARK] / tot,
        cnt[td.BRIGHT_MID], 100.0 * cnt[td.BRIGHT_MID] / tot,
        cnt[td.BRIGHT_LIGHT], 100.0 * cnt[td.BRIGHT_LIGHT] / tot))
print("设计方基准: 暗26.5%% / 中45.7%% / 亮27.8%% (162素材)")
