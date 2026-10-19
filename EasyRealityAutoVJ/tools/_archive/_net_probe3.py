# -*- coding: utf-8 -*-
"""细粒度探针 v3：直接分别计时每个网络函数 + resolve 内部各步。只读。"""
import os
import sys
import time
import json

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_probe_genre_cache.json")


def ms(t0):
    return (time.perf_counter() - t0) * 1000


def main():
    import genre_lookup
    genre_lookup._CACHE_FILE = CACHE
    genre_lookup._cache = None
    if os.path.exists(CACHE):
        os.remove(CACHE)

    print("=== 1) 直接逐个测网络函数（每次独立，真实 HTTP） ===")
    tests = [
        ("lookup_discogs(artist,title)", lambda: genre_lookup.lookup_discogs("Rolling Contact", "Black Warrior")),
        ("lookup_discogs_multi(q)", lambda: genre_lookup.lookup_discogs_multi("Rolling Contact")),
        ("lookup_discogs_track(a,t)", lambda: genre_lookup.lookup_discogs_track("Rolling Contact", "Black Warrior")),
        ("lookup_itunes(a,t)", lambda: genre_lookup.lookup_itunes("Rolling Contact", "Black Warrior")),
    ]
    for name, fn in tests:
        try:
            t0 = time.perf_counter(); r = fn(); dt = ms(t0)
            n = len(r) if isinstance(r, list) else (1 if r else 0)
            print(f"  {name:<34} {dt:8.0f} ms  -> {n} 结果")
        except Exception as e:
            t0 = time.perf_counter()
            print(f"  {name:<34} 异常 {type(e).__name__}: {e}  ({ms(t0):.0f}ms)")

    print("\n=== 2) resolve_music_genre 内部逐步计时（冷缓存） ===")
    import music_meta
    from music_meta import read_music_tags, resolve_music_genre
    path = r"W:/临时结界相关/2of/tofu/Rolling Contact - Black Warrior (2025 Remaster).flac"
    t0 = time.perf_counter(); tags = read_music_tags(path); print(f"  read_music_tags            {ms(t0):8.0f} ms")
    from genre_keywords import match_genre_keywords
    t0 = time.perf_counter(); kw = match_genre_keywords(**tags); print(f"  match_genre_keywords       {ms(t0):8.0f} ms -> {kw}")
    music_meta._qcache.clear(); music_meta._multi_cache.clear()
    t0 = time.perf_counter(); r = resolve_music_genre(path); dt = ms(t0)
    print(f"  resolve_music_genre(总)    {dt:8.0f} ms -> level={r.get('level')} genres={r.get('genres')}")

    # 第二次（缓存已热）
    music_meta._qcache.clear(); music_meta._multi_cache.clear()
    t0 = time.perf_counter(); r = resolve_music_genre(path); dt2 = ms(t0)
    print(f"  resolve_music_genre(第2次)  {dt2:8.0f} ms  (qcache已清, multi_cache已清)")

    print("\n=== 3) 无网络时（模拟断网/限流）单首 resolve 最坏耗时 ===")
    import urllib.request
    orig = urllib.request.urlopen

    def dead(*a, **k):
        time.sleep(0.0)
        raise urllib.error.URLError("simulated network down")

    urllib.request.urlopen = dead
    music_meta._qcache.clear(); music_meta._multi_cache.clear()
    t0 = time.perf_counter()
    r = resolve_music_genre(path)
    print(f"  断网 resolve_music_genre   {ms(t0):8.0f} ms -> source={r.get('source')} level={r.get('level')}")
    urllib.request.urlopen = orig

    print("\n=== 4) 超时场景：请求挂起到 timeout（Discogs 卡顿） ===")

    def hang(*a, **k):
        time.sleep(float(k.get("timeout", 12)) if k.get("timeout") else 12.0)
        raise TimeoutError("simulated hang")

    urllib.request.urlopen = hang
    music_meta._qcache.clear(); music_meta._multi_cache.clear()
    t0 = time.perf_counter()
    r = resolve_music_genre(path)
    print(f"  单请求挂满12s→单首resolve {ms(t0):8.0f} ms (≈12s即单步超时上限)")
    urllib.request.urlopen = orig

    if os.path.exists(CACHE):
        os.remove(CACHE)


if __name__ == "__main__":
    main()
