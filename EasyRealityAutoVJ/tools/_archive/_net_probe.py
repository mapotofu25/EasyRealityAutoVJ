# -*- coding: utf-8 -*-
"""细粒度探针：拆解 (a) 指纹内部 (b) 联网请求次数与单次延迟。

只读，不写用户数据（联网只发 GET 到 Discogs/iTunes 公开接口，不缓存到用户目录）。
"""
import os
import sys
import time
import json

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

FILES = [
    r"W:/临时结界相关/2of/tofu/Rolling Contact - Black Warrior (2025 Remaster).flac",
    r"C:/Users/Administrator/Downloads/90s trance.mp3",
]


def main():
    import numpy as np
    import soundfile as sf

    import genre_lookup
    # 把缓存文件指到临时路径，避免写用户 genre_cache.json
    genre_lookup._CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "_probe_genre_cache.json")
    genre_lookup._cache = None

    # ---- 记录每一次 _fetch_json 调用 ----
    calls = []
    orig_fetch = genre_lookup._fetch_json

    def counting_fetch(url, headers=None, timeout=12):
        t0 = time.perf_counter()
        err = None
        try:
            r = orig_fetch(url, headers, timeout)
        except Exception as e:
            err = type(e).__name__
            r = None
            raise
        finally:
            calls.append((time.perf_counter() - t0, url.split("?")[0], timeout, err))
        return r

    genre_lookup._fetch_json = counting_fetch

    import music_meta
    # _artist_candidates 用 lookup_discogs_multi -> 走 genre_lookup.lookup_discogs_multi
    # 但 music_meta 里 import 的是函数引用，需替换模块属性
    music_meta.lookup_discogs_multi = lambda q, per_page=5: _wrap(genre_lookup.lookup_discogs_multi, q, per_page)
    music_meta.lookup_discogs_track = lambda a, t, per_page=5: _wrap(genre_lookup.lookup_discogs_track, a, t, per_page)
    music_meta.lookup_itunes = lambda a, t=None, country="US": _wrap(genre_lookup.lookup_itunes, a, t, country)
    music_meta.lookup_genre = genre_lookup.lookup_genre

    def _wrap(fn, *a, **k):
        return fn(*a, **k)

    # ---- 指纹内部拆解 ----
    import fp
    print("=" * 90)
    print("指纹内部拆解（整曲）")
    print("-" * 90)
    for path in FILES:
        if not os.path.exists(path):
            continue
        data, sr = sf.read(path, dtype="float32", always_2d=True)
        sig = np.mean(data, axis=1)
        t0 = time.perf_counter(); spec = fp._spectrogram(sig); t_spec = (time.perf_counter()-t0)*1000
        t0 = time.perf_counter(); peaks = fp._find_peaks(spec); t_peak = (time.perf_counter()-t0)*1000
        t0 = time.perf_counter(); hs = fp._hashes(peaks); t_hash = (time.perf_counter()-t0)*1000
        print(f"{os.path.basename(path)[:46]:<48} dur={len(sig)/sr:5.0f}s "
              f"spec={t_spec:7.0f}ms peaks={t_peak:7.0f}ms({len(peaks)}) "
              f"hashes={t_hash:6.0f}ms({len(hs)}) 合计={t_spec+t_peak+t_hash:.0f}ms")

    # ---- 联网请求计数 ----
    print("=" * 90)
    print("联网请求拆解（冷缓存，逐首）")
    print("-" * 90)
    from music_meta import resolve_music_genre
    for path in FILES:
        if not os.path.exists(path):
            continue
        calls.clear()
        # 清空 music_meta 的进程缓存，模拟扫描首见
        music_meta._qcache.clear()
        music_meta._multi_cache.clear()
        t0 = time.perf_counter()
        r = resolve_music_genre(path)
        dt = (time.perf_counter() - t0) * 1000
        print(f"\n{os.path.basename(path)}: 总 {dt:.0f}ms, {len(calls)} 次请求, "
              f"level={r.get('level')} source={r.get('source')}")
        for ms, host, to, err in calls:
            print(f"    {ms:7.0f}ms  timeout={to}  {host}  {'ERR=' + err if err else ''}")
        print(f"    → 请求总耗时 {sum(c[0] for c in calls)*1000:.0f}ms")


if __name__ == "__main__":
    main()
