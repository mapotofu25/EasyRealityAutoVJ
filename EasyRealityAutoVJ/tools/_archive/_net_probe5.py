# -*- coding: utf-8 -*-
"""探针 v5：精确统计「单首 resolve_music_genre」发出的 HTTP 请求次数（在 urlopen 层计数）。"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_probe_genre_cache.json")

FILES = [
    r"W:/临时结界相关/2of/tofu/Rolling Contact - Black Warrior (2025 Remaster).flac",
    r"W:/临时结界相关/2of/tofu/Rolling Contact - Cosmic String.flac",
    r"C:/Users/Administrator/Downloads/90s trance.mp3",
]


def main():
    import urllib.request
    import genre_lookup
    genre_lookup._CACHE_FILE = CACHE
    genre_lookup._cache = None
    if os.path.exists(CACHE):
        os.remove(CACHE)

    import music_meta
    from music_meta import resolve_music_genre

    log = []
    orig = urllib.request.urlopen

    def counting_urlopen(req, *a, **k):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        t0 = time.perf_counter()
        try:
            r = orig(req, *a, **k)
            log.append((url, (time.perf_counter() - t0) * 1000, None))
            return r
        except Exception as e:
            log.append((url, (time.perf_counter() - t0) * 1000, type(e).__name__))
            raise

    urllib.request.urlopen = counting_urlopen

    print("冷缓存逐首（每首先清 music_meta 进程缓存，模拟新专辑首见）：")
    for path in FILES:
        if not os.path.exists(path):
            continue
        log.clear()
        music_meta._qcache.clear()
        music_meta._multi_cache.clear()
        t0 = time.perf_counter()
        r = resolve_music_genre(path)
        dt = (time.perf_counter() - t0) * 1000
        print(f"\n{os.path.basename(path)}")
        print(f"  HTTP 请求数 = {len(log)}   合计耗时 = {dt:.0f} ms   level={r.get('level')} src={r.get('source')}")
        for u, msec, err in log:
            print(f"    {msec:7.0f}ms  {u[:70]}  {err or ''}")

    # 同一专辑第二首（artist 查询已进 _multi_cache）——测缓存能省多少
    print("\n\n同一社团第 2 首（_multi_cache 已热）：")
    log.clear()
    music_meta._qcache.clear()          # 保留 _multi_cache
    t0 = time.perf_counter()
    r = resolve_music_genre(FILES[1])
    print(f"  HTTP 请求数 = {len(log)}   合计耗时 = {(time.perf_counter()-t0)*1000:.0f} ms  level={r.get('level')}")

    urllib.request.urlopen = orig
    if os.path.exists(CACHE):
        os.remove(CACHE)


if __name__ == "__main__":
    main()
