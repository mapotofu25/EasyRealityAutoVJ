# -*- coding: utf-8 -*-
"""曲风在线查询：Discogs（主力，style 细分）→ iTunes（兜底）→ 空（交给本地 AI）。

只用于「演出前分析曲库」阶段；结果本地缓存，现场零联网、零延迟。
两个 API 均无需 key，国内可直连（Discogs 走 Cloudflare、偶有波动，失败自动降级 iTunes）。
"""
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from config import app_base_dir

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

_CACHE_FILE = os.path.join(app_base_dir(), "genre_cache.json")

# 日文曲风 → 英文原词（仅 iTunes country=JP 可能返回日文时用；
# 英文曲风原词直接保留，不再翻译成中文）
GENRE_EN = {
    "エレクトロニック": "Electronic", "ダンス": "Dance", "ハウス": "House",
    "テクノ": "Techno", "トランス": "Trance", "ロック": "Rock",
    "メタル": "Metal", "ヒップホップ": "Hip Hop", "ヒップホップ／ラップ": "Hip Hop",
    "ポップ": "Pop", "ジャズ": "Jazz", "クラシック": "Classical",
    "アニメ": "Anime", "サウンドトラック": "Soundtrack", "R&B／ソウル": "R&B",
}

# 内存 + 磁盘缓存（key = artist|title）
_lock = threading.Lock()
_cache = None


def _load_cache():
    global _cache
    if _cache is not None:
        return _cache
    d = {}
    try:
        if os.path.exists(_CACHE_FILE):
            d = json.load(open(_CACHE_FILE, encoding="utf-8"))
    except Exception:
        d = {}
    _cache = d
    return _cache


def _save_cache():
    try:
        json.dump(_cache, open(_CACHE_FILE, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    except Exception:
        pass


# ★ 2026-09-26：联网查询超时从 12s 收紧到 3s（现场实测的根因修复）。
#   反馈「800 首导入，半小时只扫完 200 首（≈9s/首，最坏几十秒）」——
#   根因就是这些请求**挂在 12s 超时上**：单首歌最多 8 次请求（Discogs track→artist→组合 + iTunes 兜底，
#   × 2 个候选词），最坏 8×12s = 96s（探针实测过 96005ms）。
#   ⚠ 注意：**断网反而快**（1ms 直接失败降级）；真正致命的是"半通不通/被限流"，
#   请求会一直挂到超时 —— 那正是国内访问 Discogs(Cloudflare) 的典型症状。
#   收紧后单次最坏 3s，再配合 music_meta 的「单首联网总预算」，整首最坏从 96s 降到个位数秒。
NET_TIMEOUT = 3.0

# ★★ 单首歌的联网查询**总预算**（秒）。由 `music_meta.resolve_music_genre` 在每首歌开始时
#    调用 `begin_online_budget()` 设置；`_fetch_json` 是**唯一的网络出口**，在这里统一执行：
#      · 预算已耗尽 ⇒ 直接抛异常（各 lookup 的 except 会兜住 → 自然降级到本地 AI）
#      · 单次 timeout 还会**按剩余预算收缩** ⇒ 整首不会超出预算太多
#    效果：单首最坏耗时从 96s（8 次请求 × 12s）压到 **≈ 预算值**。
#    ⚠ 在线查询只是"锦上添花"（查不到有本地 AI 兜底），**绝不能让一首歌卡几十秒**。
ONLINE_BUDGET_SECS = 6.0
_deadline = 0.0


def begin_online_budget(secs=ONLINE_BUDGET_SECS):
    """开始一首歌的联网查询并设置总预算（秒）。secs 为 0/None 表示不限制。"""
    global _deadline
    _deadline = (time.monotonic() + float(secs)) if secs else 0.0


def _budget_left():
    if not _deadline:
        return 1e9
    return _deadline - time.monotonic()


def online_expired():
    """本次单首查询的联网预算是否已耗尽"""
    return _deadline > 0 and time.monotonic() > _deadline


# ★★ 限流防护（2026-09-26）。Discogs 官方速率限制：**未认证只有 25 请求/分钟**，
#    而扫描时**每首歌最多发 8 次请求**（track→artist→组合 + iTunes × 2 个候选词）⇒ **必然撞限流**。
#    一旦被拒（429 限流 / 403 拒绝 / 503 不可用），继续发只是白白浪费时间、
#    还可能被临时封 IP ⇒ **本次运行直接停用联网**，后续歌曲全部走本地 AI 兜底。
_blocked = False
_online_enabled = True      # 扫描的**并行阶段**会临时关掉它（避免多线程同时联网撞限流）


def online_blocked():
    """本次运行是否已因被限流/拒绝而停用在线曲风查询"""
    return _blocked


def set_online_enabled(on):
    """在线查询总开关。扫描时：**并行阶段设为 False**（只做本地），**串行阶段再打开**。"""
    global _online_enabled
    _online_enabled = bool(on)


def online_enabled():
    return _online_enabled


def _log_blocked(code):
    """被限流时记一行到 scan_time.log（便于用户理解"为什么后面没曲风了"）"""
    try:
        p = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "scan_time.log")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write("[%s] !! 在线曲风查询被拒（HTTP %s；Discogs 未认证限流约 25 次/分钟）"
                     "—— 本次运行剩余歌曲改用本地 AI 识别\n"
                     % (time.strftime("%Y-%m-%d %H:%M:%S"), code))
    except Exception:                                                    # noqa: BLE001
        pass


def _fetch_json(url, headers=None, timeout=NET_TIMEOUT):
    global _blocked
    if not _online_enabled:
        raise TimeoutError("online lookups disabled (parallel phase)")
    if _blocked:
        raise TimeoutError("online lookups disabled (rate limited / rejected earlier)")
    left = _budget_left()
    if left <= 0:
        raise TimeoutError("online budget exhausted")
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, context=_CTX,
                                   timeout=max(0.5, min(timeout, left))) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code in (429, 403, 503) and not _blocked:
            _blocked = True
            _log_blocked(e.code)
        raise


def _norm_genres(words):
    """英文曲风原词保留（Discogs/iTunes 已是标准大小写），日文映射成英文"""
    out = []
    for w in words:
        w = str(w).strip()
        if not w:
            continue
        z = GENRE_EN.get(w.lower(), w)
        if z and z not in out:
            out.append(z)
    return out


def lookup_discogs(artist, title=None):
    """返回 {'styles':[..], 'genres':[英文原词], 'source':'discogs'} 或 None"""
    q = f"{artist} {title}".strip() if title else artist
    u = ("https://api.discogs.com/database/search?" +
         urllib.parse.urlencode({"q": q, "type": "release", "per_page": "3"}))
    try:
        d = _fetch_json(u, headers={
            "User-Agent": "AutoVJGenreLookup/0.1 +https://github.com/local"})
    except Exception:
        return None
    for r in d.get("results", []):
        g = r.get("genre") or []
        st = r.get("style") or []
        if g or st:
            return {"genres": _norm_genres(list(g) + list(st)), "source": "discogs",
                    "res_title": str(r.get("title") or "")}
    return None


def lookup_discogs_multi(q, per_page=5):
    """返回多条 Discogs 结果（供上层防串校验 + 标签投票）"""
    u = ("https://api.discogs.com/database/search?" +
         urllib.parse.urlencode({"q": q, "type": "release", "per_page": str(per_page)}))
    try:
        d = _fetch_json(u, headers={
            "User-Agent": "AutoVJGenreLookup/0.1 +https://github.com/local"})
    except Exception:
        return []
    out = []
    for r in d.get("results", []):
        g = r.get("genre") or []
        st = r.get("style") or []
        if g or st:
            out.append({"res_title": str(r.get("title") or ""),
                        "genres": _norm_genres(list(g) + list(st))})
    return out


def lookup_discogs_track(artist, title, per_page=5):
    """track 级精确查询：artist+track 定位单曲所在 release（单曲差异化曲风）"""
    u = ("https://api.discogs.com/database/search?" +
         urllib.parse.urlencode({"type": "release", "artist": artist,
                                 "track": title, "per_page": str(per_page)}))
    try:
        d = _fetch_json(u, headers={
            "User-Agent": "AutoVJGenreLookup/0.1 +https://github.com/local"})
    except Exception:
        return []
    out = []
    for r in d.get("results", []):
        g = r.get("genre") or []
        st = r.get("style") or []
        if g or st:
            out.append({"res_title": str(r.get("title") or ""),
                        "genres": _norm_genres(list(g) + list(st))})
    return out


def lookup_itunes(artist, title=None, country="US"):
    """返回 {'genres':[英文原词], 'styles':[], 'source':'itunes'} 或 None"""
    q = f"{artist} {title}".strip() if title else artist
    u = ("https://itunes.apple.com/search?" +
         urllib.parse.urlencode({"term": q, "country": country,
                                 "media": "music", "entity": "song", "limit": "3"}))
    try:
        d = _fetch_json(u)
    except Exception:
        return None
    for r in d.get("results", []):
        g = r.get("primaryGenreName") or r.get("genreName")
        if g:
            return {"genres": _norm_genres([g]), "source": "itunes"}
    return None


def lookup_genre(artist, title=None, duration=None, force=False):
    """Discogs 优先 → iTunes 兜底 → None（交给本地 AI）。
    force=True 忽略缓存重新查询。结果含 None 也缓存（避免重复查失败项）。"""
    artist = (artist or "").strip()
    title = (title or "").strip()
    if not artist and not title:
        return None
    key = f"{artist}|{title}"
    with _lock:
        _load_cache()
        if not force and key in _cache:
            hit = _cache[key]
            return None if not hit else dict(hit)
        r = lookup_discogs(artist, title) or lookup_itunes(artist, title)
        _cache[key] = r or None
        _save_cache()
        return dict(r) if r else None
