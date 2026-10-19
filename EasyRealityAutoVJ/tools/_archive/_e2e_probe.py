# -*- coding: utf-8 -*-
"""探针 v6：端到端模拟扫描 N 首（临时空库、联网开），逐首拆步计时。

不改用户数据：指纹写 tools/_probe_e2e.db；联网缓存写 tools/_probe_genre_cache.json。
"""
import os
import sys
import time
import hashlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "_probe_e2e.db")
CACHE = os.path.join(HERE, "_probe_genre_cache.json")

FILES = [
    r"W:/临时结界相关/2of/tofu/Rolling Contact - Black Warrior (2025 Remaster).flac",
    r"W:/临时结界相关/2of/tofu/Rolling Contact - Cosmic String.flac",
    r"W:/临时结界相关/2of/tofu/Rolling Contact - Crescent Horizon (Rebuild).flac",
    r"W:/临时结界相关/2of/tofu/Rolling Contact - Demon's Mischief.flac",
    r"C:/Users/Administrator/Downloads/90s trance.mp3",
]


def ms(t0):
    return (time.perf_counter() - t0) * 1000


def main():
    import numpy as np
    import soundfile as sf
    import genre_lookup
    genre_lookup._CACHE_FILE = CACHE
    genre_lookup._cache = None
    if os.path.exists(CACHE):
        os.remove(CACHE)
    for f in (DB, DB + "-wal", DB + "-shm"):
        try:
            os.remove(f)
        except OSError:
            pass

    from fp import FingerprintDB
    from music_meta import resolve_music_genre
    from genre_keywords import simplify_genres
    from ui_main import MusicScanThread as MST
    from audio_genre import get_genre_classifier
    gc = get_genre_classifier()

    print(f"{'#':>2} {'file':<34}{'db(MB)':>7}{'decode':>8}{'fp写库':>8}{'查询':>8}{'本地AI':>8}{'单首':>8}{'累计':>8}")
    print("-" * 96)
    db = FingerprintDB(DB)
    cum = 0.0
    for i, p in enumerate([f for f in FILES if os.path.exists(f)], 1):
        t_song = time.perf_counter()
        t0 = time.perf_counter()
        data, sr = sf.read(p, dtype="float32", always_2d=True)
        sig = np.mean(data, axis=1)
        d_dec = ms(t0)
        t0 = time.perf_counter()
        sid = int(hashlib.md5(p.encode()).hexdigest()[:8], 16)
        db.add_song(sid, sig, sr)
        d_fp = ms(t0)
        t0 = time.perf_counter()
        r = resolve_music_genre(p)
        d_net = ms(t0)
        t0 = time.perf_counter()
        if r.get("level") != "track":
            simplify_genres(MST._local_ai_genre(gc, sig, sr))
        d_ai = ms(t0)
        d_tot = ms(t_song)
        cum += d_tot
        sz = os.path.getsize(DB) / 1048576
        print(f"{i:>2} {os.path.basename(p)[:32]:<34}{sz:>7.0f}{d_dec:>8.0f}{d_fp:>8.0f}"
              f"{d_net:>8.0f}{d_ai:>8.0f}{d_tot:>8.0f}{cum:>8.0f}")
    db.close()
    print("-" * 96)
    print(f"本机实测单首均值 ≈ {cum/len([f for f in FILES if os.path.exists(f)]):.0f} ms（网络良好、空库起步）")
    for f in (DB, DB + "-wal", DB + "-shm", CACHE):
        try:
            os.remove(f)
        except OSError:
            pass


if __name__ == "__main__":
    main()
