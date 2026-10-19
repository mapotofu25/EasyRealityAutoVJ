# -*- coding: utf-8 -*-
"""QA 第二轮 ★F：grid 到底有没有落盘？—— 用真实歌曲走一遍 Config 持久化往返。"""
import sys, os, json, tempfile, glob
sys.path.insert(0, r"P:\AutoVJ\src")
import numpy as np
import soundfile as sf
import config as CFG
import beatgrid as BG

TOFU = r"W:\临时结界相关\2of\tofu"
files = sorted(glob.glob(os.path.join(TOFU, "*.flac")))
print("素材:", len(files), "首")

# ---- 1) 用真实信号算一个 grid（只取前 45s，快）----
song = files[0]
mono, sr = sf.read(song, dtype="float32", always_2d=True, frames=int(45 * 44100))
sig = np.mean(mono, axis=1).astype(np.float32)
print("计算网格:", os.path.basename(song))
g = BG.grid_from_signal(sig, sr, None, path=song, maxsec=45.0)
print("grid =", g)

# ---- 2) 可 JSON 序列化？----
try:
    s = json.dumps(g, ensure_ascii=False)
    print("\n[1] json.dumps(grid) OK, 长度", len(s))
except Exception as e:
    print("\n[1] json.dumps(grid) 失败:", type(e).__name__, e)

# ---- 3) Config 往返（临时 CONFIG_FILE，绝不碰真配置）----
tmpdir = tempfile.mkdtemp(prefix="qa2_")
CFG.CONFIG_FILE = os.path.join(tmpdir, "config.json")
cfg = CFG.Config()
p = song
cfg["music_meta"][p] = {"genres": ["hardstyle"], "song_id": 123456, "grid": g}
before = "grid" in cfg["music_meta"][p]
try:
    cfg.save()
    saved = True
    err = ""
except Exception as e:
    saved = False; err = "%s: %s" % (type(e).__name__, e)
print("[2] cfg.save() 成功=%s %s" % (saved, err))
raw = open(CFG.CONFIG_FILE, encoding="utf-8").read()
print("[3] 落盘文件里含 '\"grid\"' =", '"grid"' in raw, " 文件大小", len(raw))
cfg2 = CFG.Config()   # 重新加载
rec = cfg2["music_meta"].get(p, {})
print("[4] 重新加载后 music_meta 里 grid 存在 =", isinstance(rec, dict) and "grid" in rec)
if isinstance(rec, dict) and "grid" in rec:
    print("    读回的 grid:", rec["grid"])
print()
print("before 写入:", before)

# ---- 4) 模拟 phase2 覆盖：resolve_music_genre 返回的 r2 ----
# 真实 r2 **不含 grid / song_id**（见 tools/_fix3_f.py 对 music_meta 返回键的核对）。
r2 = {"genres": ["house"], "level": "artist", "source": "discogs"}
# 旧实现（= r2）：整条替换 ⇒ 抹掉 grid（这里仅作对照，复刻旧 bug）
print("[5a] 旧实现(=r2) 后 grid 还在吗 =", "grid" in r2,
      " keys=", list(r2.keys()))
# 新实现（ui_main 阶段2）：合并 r0.update(r2) ⇒ grid 必须保住（阻塞项 F-b）
r0 = cfg["music_meta"].get(p) or {}
r0.update(r2)
cfg["music_meta"][p] = r0
print("[5b] 新实现(merge) 后 grid 还在吗 =",
      isinstance(cfg["music_meta"][p].get("grid"), dict),
      " song_id=", cfg["music_meta"][p].get("song_id"),
      " level=", cfg["music_meta"][p].get("level"))

import shutil
shutil.rmtree(tmpdir, ignore_errors=True)
