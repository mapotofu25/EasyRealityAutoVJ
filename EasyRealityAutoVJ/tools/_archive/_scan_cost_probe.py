# -*- coding: utf-8 -*-
"""只读性能探针：定位「扫描一首歌」各步耗时（不改用户数据）。

用法：
    P:/AutoVJ/venv/Scripts/python.exe P:/AutoVJ/tools/_scan_cost_probe.py [音频文件...]

- 指纹写入走 tools/_probe_fp.db（临时），绝不碰 %LOCALAPPDATA%\\AutoVJ\\fingerprints.db
- 联网查询写 tools/_probe_genre_cache.json（临时），绝不碰用户 genre_cache.json
"""
import os
import sys
import time
import json
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

HERE = os.path.dirname(os.path.abspath(__file__))
PROBE_DB = os.path.join(HERE, "_probe_fp.db")
PROBE_CACHE = os.path.join(HERE, "_probe_genre_cache.json")

DEFAULT_FILES = [
    r"W:/临时结界相关/2of/tofu/Rolling Contact - Black Warrior (2025 Remaster).flac",
    r"W:/临时结界相关/2of/tofu/Rolling Contact - Cosmic String.flac",
    r"C:/Users/Administrator/Downloads/90s trance.mp3",
]


def t(fn, *a, **k):
    t0 = time.perf_counter()
    r = fn(*a, **k)
    return (time.perf_counter() - t0) * 1000.0, r


def main():
    files = sys.argv[1:] or DEFAULT_FILES
    files = [f for f in files if os.path.exists(f)]
    if not files:
        print("no input files")
        return

    import numpy as np
    import soundfile as sf

    # 屏蔽用户缓存文件：把 genre_lookup 的缓存文件指到临时路径
    import genre_lookup
    genre_lookup._CACHE_FILE = PROBE_CACHE
    genre_lookup._cache = None

    # 临时指纹库
    for f in (PROBE_DB, PROBE_DB + "-wal", PROBE_DB + "-shm"):
        try:
            os.remove(f)
        except OSError:
            pass

    from fp import FingerprintDB

    # 本地 AI（懒加载，预热一次不计入）
    gc = None
    try:
        from audio_genre import get_genre_classifier
        t0 = time.perf_counter()
        gc = get_genre_classifier()
        print(f"[warmup] ONNX session load: {(time.perf_counter()-t0)*1000:.0f} ms")
    except Exception as e:
        print("[warmup] local AI unavailable:", e)

    from ui_main import MusicScanThread as MST

    print("=" * 100)
    print(f"{'file':<52}{'decode':>9}{'fp':>9}{'meta':>7}{'net':>9}{'ai':>9}{'total':>9}")
    print("-" * 100)
    agg = {}
    for path in files:
        size_mb = os.path.getsize(path) / 1048576.0
        row = {}
        # 1) 整曲解码
        ms_dec, (data, sr) = t(sf.read, path, dtype="float32", always_2d=True)
        sig = np.mean(data, axis=1)
        dur = len(sig) / sr
        # 2) 指纹（STFT+峰值+哈希+写库）
        import hashlib
        song_id = int(hashlib.md5(path.encode()).hexdigest()[:8], 16)
        db = FingerprintDB(PROBE_DB)
        ms_fp, nrows = t(db.add_song, song_id, sig, sr)
        db.close()
        # 3) 元数据解析（联网）——冷缓存
        from music_meta import resolve_music_genre, read_music_tags
        ms_meta, _ = t(read_music_tags, path)
        ms_net, r = t(resolve_music_genre, path)
        # 4) 本地 AI 曲风（3 段 × 6s，与扫描同逻辑），level==track 时真实扫描会跳过
        ms_ai = 0.0
        if gc is not None:
            ms_ai, _ = t(MST._local_ai_genre, gc, sig, sr)
            ms_ai_skip = (r.get("level") == "track")
        else:
            ms_ai_skip = False
        total = ms_dec + ms_fp + ms_net + (0 if r.get("level") == "track" else ms_ai)
        tag = "track→跳过AI" if r.get("level") == "track" else "跑AI"
        print(f"{os.path.basename(path)[:50]:<52}{ms_dec:>9.0f}{ms_fp:>9.0f}"
              f"{ms_meta:>7.0f}{ms_net:>9.0f}{ms_ai:>9.0f}{total:>9.0f}  [{tag}] "
              f"{dur:.0f}s {size_mb:.1f}MB level={r.get('level')} src={r.get('source')}")
        for k, v in (("decode", ms_dec), ("fp", ms_fp), ("meta", ms_meta),
                     ("net", ms_net), ("ai", ms_ai), ("total", total)):
            agg.setdefault(k, []).append(v)
    print("-" * 100)
    if agg:
        print("均值(ms): " + "  ".join(f"{k}={sum(v)/len(v):.0f}" for k, v in agg.items()))
    # 清理临时指纹库
    for f in (PROBE_DB, PROBE_DB + "-wal", PROBE_DB + "-shm"):
        try:
            os.remove(f)
        except OSError:
            pass


if __name__ == "__main__":
    main()
