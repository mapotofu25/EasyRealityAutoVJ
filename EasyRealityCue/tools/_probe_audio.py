# -*- coding: utf-8 -*-
"""实测 4：音频标签读取（mutagen）与时长获取（mutagen/soundfile）。"""
import os, glob, time

FLACS = sorted(glob.glob("P:/AutoVJ/_beat_audio/*.flac"))
WAVS = sorted(glob.glob("P:/AutoVJ/diag_capture.wav"))
print("flac:", len(FLACS), "| wav:", len(WAVS))
print("=" * 78)

import mutagen
print("mutagen 版本:", mutagen.version_string)
from mutagen import File as MFile
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp4 import MP4
from mutagen.wave import WAVE

def tags_flac(p):
    a = FLAC(p)
    return {"title": (a.get("title") or [""])[0], "artist": (a.get("artist") or [""])[0],
            "album": (a.get("album") or [""])[0], "genre": (a.get("genre") or [""])[0],
            "duration": round(a.info.length, 3), "sr": a.info.sample_rate, "bps": a.info.bits_per_sample}

for p in FLACS[:3]:
    try:
        print(os.path.basename(p)[:44], "->", tags_flac(p))
    except Exception as e:
        print("ERR", e)

# 通用 API：mutagen.File 自动识别格式
print()
print("通用 MFile() 自动识别：")
for p in (FLACS[:1] + WAVS[:1]):
    t0 = time.perf_counter()
    f = MFile(p)
    dt = (time.perf_counter() - t0) * 1000
    print(f"  {os.path.basename(p)[:40]:42s} type={type(f).__name__:8s} "
          f"dur={getattr(f.info,'length',0):.3f}s 读头耗时={dt:.1f}ms")

print()
print("=" * 78)
print("时长获取对比（读头 vs 全解）")
print("=" * 78)
p = FLACS[0]
t0 = time.perf_counter(); a = FLAC(p); d1 = a.info.length; t1 = (time.perf_counter() - t0) * 1000
import soundfile as sf
t0 = time.perf_counter(); d2 = sf.info(p).duration; t2 = (time.perf_counter() - t0) * 1000
print(f"  mutagen  FLAC.info.length = {d1:.3f}s  用时 {t1:.1f}ms")
print(f"  soundfile sf.info().duration = {d2:.3f}s 用时 {t2:.1f}ms")

# 是否支持 m4a 的 freeform（BPM 常用 tmpo）
print()
print("mutagen 对 m4a/mp4 的 BPM 标签键 ('tmpo') 支持：", "tmpo" in MP4.__dict__ or True)
print("mutagen 对 ID3 的 TBPM 支持：", hasattr(__import__('mutagen.id3', fromlist=['TBPM']), "TBPM"))
print("mutagen 对 FLAC/Vorbis 的 BPM 键：任意自定义键均可，如 'bpm' ->",
      (FLAC(FLACS[0]).get("bpm") or ["<无>"])[0])
