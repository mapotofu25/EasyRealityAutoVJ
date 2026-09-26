# -*- coding: utf-8 -*-
"""音乐文件曲风解析：ID3/Vorbis 标签 → 文件夹名/artist 在线查询 → 本地 AI。

实测结论（Rolling Contact 同人曲库）：
- ID3 的 genre 字段基本无用（全是 "Game"/"Anime" 这种宽泛词）。
- ID3 的 artist="天音" 直接查 Discogs 会串到同名爵士艺人（Free Jazz）→ 不可信。
- 文件夹名（社团名 "Rolling Contact"）才是最可靠的查询词。
故查询词优先级：文件夹名 > ID3 artist > 空（交本地 AI）。
"""
import os
import re

from genre_lookup import (GENRE_EN, lookup_discogs_multi,
                          lookup_discogs_track, lookup_genre, lookup_itunes)
from genre_keywords import match_genre_keywords

# 无价值的宽泛 genre 词（同人音乐常见），命中则忽略、走在线查询
USELESS_GENRE = {
    "game", "anime", "other", "unknown", "soundtrack", "jpop", "j-pop",
    "video game", "ost", "instrumental",
}


def read_music_tags(path):
    """读 flac/mp3/m4a 的 artist/title/album/genre，返回 dict 或 None"""
    p = path.lower()
    try:
        if p.endswith(".flac"):
            from mutagen.flac import FLAC
            a = FLAC(path)
            d = dict(a.tags or {})
            return {
                "artist": (d.get("artist") or [""])[0].strip(),
                "title": (d.get("title") or [""])[0].strip(),
                "album": (d.get("album") or [""])[0].strip(),
                "genre": (d.get("genre") or [""])[0].strip(),
            }
        if p.endswith(".mp3"):
            from mutagen.id3 import ID3
            a = ID3(path)
            return {
                "artist": str(a.get("TPE1", "")).strip(),
                "title": str(a.get("TIT2", "")).strip(),
                "album": str(a.get("TALB", "")).strip(),
                "genre": str(a.get("TCON", "")).strip(),
            }
        if p.endswith((".m4a", ".mp4", ".aac", ".aiff", ".aif")):
            from mutagen.mp4 import MP4
            a = MP4(path)
            return {
                "artist": (a.get("\xa9ART") or [""])[0].strip(),
                "title": (a.get("\xa9nam") or [""])[0].strip(),
                "album": (a.get("\xa9alb") or [""])[0].strip(),
                "genre": (a.get("\xa9gen") or [""])[0].strip(),
            }
    except Exception:
        pass
    return None


def _plausible_name(name):
    """目录名有效性：过滤碎片/纯数字等垃圾名（如 "2of"、"tofu"）"""
    n = (name or "").strip()
    if len(n) < 3 or n.isdigit():
        return False
    if re.fullmatch(r"[0-9\s.\-()_]+", n):
        return False
    return True


def _plausible_release(res_title, q, title):
    """防串校验：返回的专辑标题需包含候选词或歌名关键词（否则视为串扰丢弃）"""
    rt = (res_title or "").lower()
    if q and q.lower() in rt:
        return True
    if title:
        for w in re.split(r"[\s\[\]()\-]+", title):
            if len(w) >= 4 and w.lower() in rt:
                return True
    return False


_qcache = {}          # 进程内查询缓存（同社团多首复用，避免重复联网）
import threading as _th
_qlock = _th.Lock()


def _vote(cands):
    """多条通过校验的结果做标签众数投票（孤例串扰被稀释）"""
    from collections import Counter
    cnt = Counter()
    for c in cands:
        for z in c["genres"]:
            cnt[z] += 1
    top = [z for z, _ in cnt.most_common(4)]
    return {"genres": top, "source": "discogs", "res_title": cands[0]["res_title"]}


_multi_cache = {}    # q -> Discogs release 列表（不含校验；社团级查询与歌名无关，按词缓存）


def clear_online_cache():
    """清空在线查询缓存。

    ★ 2026-09-26（修一个真 bug）：曲库扫描分两阶段时，**必须在阶段 2 前调用** ——
    阶段 1（并行）联网是关着的，那些"查不到"的结果会被写成 `None` 存进缓存；
    阶段 2 打开联网后，每首都**命中这个失败缓存直接返回、压根不发请求**
    （实测 200 首只花 0.2 秒），等于联网完全没生效。
    """
    with _qlock:
        _qcache.clear()
        _multi_cache.clear()


def _artist_candidates(q):
    """社团/艺人级 Discogs 查询（按 q 缓存）：同专辑多首歌只发一次网络请求"""
    with _qlock:
        if q in _multi_cache:
            return _multi_cache[q]
    cands = lookup_discogs_multi(q)
    with _qlock:
        _multi_cache[q] = cands
    return cands


def _lookup_validated(q, title):
    """单个候选词查询：Discogs 多条(校验+投票) → Discogs 组合查 → iTunes 组合查"""
    key = f"{q}|{title or ''}"
    with _qlock:
        if key in _qcache:
            hit = _qcache[key]
            return dict(hit) if hit else None
    r = None
    # 0) track 级精确查询（单曲所在 release 的 style，单曲差异化）
    if title:
        cands = [c for c in lookup_discogs_track(q, title)
                 if _plausible_release(c["res_title"], q, title)]
        if cands:
            r = _vote(cands)
            r["level"] = "track"
    # 1) 社团/艺人级（多条投票）——按 q 缓存（与歌名无关），同专辑后续歌直接复用
    if r is None:
        cands = [c for c in _artist_candidates(q)
                 if _plausible_release(c["res_title"], q, title)]
        if cands:
            r = _vote(cands)
            r["level"] = "artist"
    if r is None and title:
        cands = [c for c in _artist_candidates(f"{q} {title}")
                 if _plausible_release(c["res_title"], q, title)]
        if cands:
            r = _vote(cands)
    if r is None and title:
        r = lookup_itunes(q, title)
        if r:
            r["level"] = "itunes"
    with _qlock:
        _qcache[key] = r or None
    return dict(r) if r else None


def _candidate_queries(path, tags):
    """查询词候选：ID3 artist(社团名,实测最可靠) > 有效目录名（过滤碎片名）。上限 2 个控制耗时"""
    cands = []
    artist = (tags or {}).get("artist") or ""
    if artist:
        cands.append(artist)
    parent = os.path.basename(os.path.dirname(path))
    grand = os.path.basename(os.path.dirname(os.path.dirname(path)))
    for d in (grand, parent):
        if d and d not in cands and _plausible_name(d):
            cands.append(d)
        if len(cands) >= 2:
            break
    return cands[:2]


def _en_genre_list(genre):
    """ID3 genre → 英文原词列表。逗号/分号多值拆分，剥 ID3v1 数字代号 (23) 前缀，
    过滤无价值词，逐词 title case 规范化（日文→英文）；映射不到的丢弃。全失败返回 []。"""
    out = []
    for g in re.split(r"[,;，；]+", genre):
        g = re.sub(r"^\(\d+\)\s*", "", g.strip())
        if not g or g.lower() in USELESS_GENRE:
            continue
        low = g.lower()
        z = GENRE_EN.get(low, low.title())
        if z and z not in out:
            out.append(z)
    return out


def _merge_genres(sub, major):
    """细分（关键词）优先 + 大类（在线/ID3）补充，去重"""
    seen, out = set(), []
    for g in list(sub) + list(major):
        if g and g not in seen:
            seen.add(g)
            out.append(g)
    return out


def resolve_music_genre(path):
    """对音乐文件解析曲风，返回 {'artist','title','genre','genres':[英文原词],'source','query'}。
    source: keyword / id3 / discogs / itunes / manual / None（None=交给本地 AI）。
    优先级：关键词细分 → ID3 genre(有效) → 在线查询(artist>目录名) → 本地 AI。

    ★ 2026-09-26：进来先给"这一首歌"的联网查询设**总预算**（`genre_lookup.ONLINE_BUDGET_SECS`）。
    在线查询只是锦上添花（查不到会走本地 AI 兜底），绝不能让一首歌卡几十秒 ——
    现场实测过单首最坏 96 秒（8 次请求 × 12s 超时）。"""
    try:
        import genre_lookup
        genre_lookup.begin_online_budget()
    except Exception:
        pass
    tags = read_music_tags(path) or {}
    artist = tags.get("artist") or ""
    title = tags.get("title") or ""
    album = tags.get("album") or ""
    genre = tags.get("genre") or ""

    # 0) 关键词细分匹配（文本铁证：曲名/艺人/专辑/标签里出现细分词）
    kw = match_genre_keywords(artist, title, album, genre)

    # 1) ID3 genre 有效才用：逗号多值拆分 → 英文原词
    if genre and genre.lower() not in USELESS_GENRE:
        genres = _en_genre_list(genre)
        if genres:
            return {"artist": artist, "title": title, "genre": genre,
                    "genres": _merge_genres(kw, genres), "source": "id3", "query": genre}
        # 全部映射失败 → 视为无价值标签，继续走在线查询

    # 2) artist > 有效目录名 依次在线查询（带防串校验）
    for q in _candidate_queries(path, tags):
        r = _lookup_validated(q, title)
        if r:
            r["artist"] = artist
            r["title"] = title
            r["query"] = q
            r["genres"] = _merge_genres(kw, r.get("genres") or [])
            return r

    # 3) 都没查到 → 交给本地 AI（关键词命中的先带上）
    return {"artist": artist, "title": title, "genre": "",
            "genres": kw, "source": "keyword" if kw else None, "query": ""}
