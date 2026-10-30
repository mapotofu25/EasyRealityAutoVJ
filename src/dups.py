"""曲库查重：同一首歌被导入多份时，**认歌的票会被劈成两半**。

症状（2026-10-29 用户真机遇到）：曲库里同时有
`Rolling Contact - System Overloaded.mp3` 与 `(02) [Amane Oikawa] System Overloaded.flac`
（同一段 206 秒录音），两首各有约 2.5 万行指纹 ⇒ 每次投票 `ptot=244` 而第二名
`p2=223` ⇒「总票要领先 2 倍」这类判据永远不成立，引擎还会在两个 sid 之间来回跳。

判据（用户原话：「名字或者指纹相似时给个弹窗」）：

  ① **名字**：标题 token 相似度 ≥ `TITLE_SIM`
  ② **时长**：必须接近（`DUR_TOL` 秒）。同名的原版/加长版/remix 时长差得远，
     不能算重复 —— 光看名字会把它们误判
  ③ **指纹**：哈希集合的重合度（containment）。这是"同一段音频"的硬证据，
     能抓住改名/换容器/换编码的情况

最终 `is_dup` 的两条通道（都要时长接近）：
  · 名字像（≥0.75）⇒ 判重（不需要算指纹，快）
  · 名字不太像但时长几乎一样（≤0.5 秒）⇒ 再算指纹，重合 ≥0.45 才判重

⚠ 只有"名字像"不够：实测库里存在大量同名不同曲（不同作者的 cover/remix）。
  所以名字通道也必须过时长这一关。
"""

import os
import re
import sqlite3

# 名字相似度：token Jaccard。0.75 相当于「两个词里差一个」以内
TITLE_SIM = 0.75
# 时长容差（秒）。flac/mp3 的容器开销会差几十毫秒，2 秒足够
DUR_TOL = 2.0
# 名字不像但时长几乎一样时，才值得去算指纹（这几秒内）
DUR_TIGHT = 0.5
# 指纹重合度门槛（containment = |A∩B| / min(|A|,|B|)）
FP_OVERLAP = 0.45
# 单首歌最多取多少行指纹参与比对（够判定了；25k 全取也无所谓，这里保守一点）
FP_CAP = 20000
# 一次查重最多算多少对指纹（防大库卡住；超出就只报"名字+时长"命中的）
FP_MAX_PAIRS = 60

_SPLIT = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)
# 常见"同一首歌"的无关后缀：只在**去掉后仍有剩余**时才去（避免把标题删空）
_NOISE = {"feat", "ft", "featuring", "official", "mv", "lyric", "lyrics",
          "audio", "hd", "hq", "1080p", "720p", "4k", "remaster", "remastered"}
# 「版本标记」：出现即说明是**另一个版本**，哪怕是同一首歌
VARIANT_MARKERS = {"vip", "remix", "extended", "instrumental", "bootleg",
                   "edit", "mix", "dub", "acoustic", "live", "cover",
                   "nightcore", "sped", "slowed", "rework", "flip"}


def norm_title(s):
    """归一化标题：小写 → 去噪音词 → 非字母数字/汉字处切词。

    ⚠ 故意**不**去掉 `remix` / `extended` / `vip` / `instrumental` ——
      那些是**不同的曲目**（时长也不同），去掉会让它们互相判重。
    """
    if not s:
        return []
    s = str(s).lower()
    toks = [t for t in _SPLIT.split(s) if t]
    keep = [t for t in toks if t not in _NOISE]
    return keep or toks


def title_sim(a, b):
    """两段标题的相似度（0~1）。空标题一律 0。

    用 **containment**（|A∩B| / min(|A|,|B|)）而不是 Jaccard：
      · `Song (feat. X)` 与 `Song` ⇒ Jaccard 只有 0.5（被那个 feat 词拖低），
        但 containment = 1.0 —— 这才是"同一段录音，只是标题多写了个合作歌手"。
      · Jaccard 下这两种情况**分数一样**，区分不开：
          `Song (feat. X)` vs `Song`      —— 同一录音，该判重
          `Hardstyle VIP`  vs `Hardstyle` —— 不同版本，不该判重
    所以额外用 `VARIANT_MARKERS`：**对称差里出现"版本标记"就压到 0.5**。
    """
    ta, tb = norm_title(a), norm_title(b)
    if not ta or not tb:
        return 0.0
    sa, sb = set(ta), set(tb)
    inter = len(sa & sb)
    if not inter:
        return 0.0
    contain = inter / float(min(len(sa), len(sb)))
    extra = (sa - sb) | (sb - sa)
    if extra & VARIANT_MARKERS:
        # 多出来的是 remix / vip / extended 这类 —— 它们是**不同的曲目**
        # （时长通常也不同，但标题相同的情况下这里必须挡住）
        return min(contain, 0.5)
    return contain


def dur_of(meta):
    """从 `music_meta` 记录里取时长（秒）。取不到返回 0.0。

    来源优先级：`grid.dur`（离线分析时写死的）→ `sections` 末段 → 0。
    """
    if not isinstance(meta, dict):
        return 0.0
    g = meta.get("grid") or {}
    try:
        d = float(g.get("dur") or 0.0)
        if d > 0:
            return d
    except (TypeError, ValueError):
        pass
    secs = meta.get("sections") or []
    if isinstance(secs, list) and secs:
        try:
            return float(secs[-1].get("t1") or 0.0)
        except (TypeError, ValueError, AttributeError):
            return 0.0
    return 0.0


def fp_hashes(db_path, song_id, cap=FP_CAP):
    """取一首歌的指纹哈希集合（最多 `cap` 行）。拿不到返回空集。

    ⚠ 只读打开（`mode=ro`）——查重不该给指纹库加写锁，演出中也可能跑。
    """
    if not db_path or not os.path.exists(db_path):
        return set()
    try:
        con = sqlite3.connect("file:%s?mode=ro" % str(db_path).replace("\\", "/"),
                              uri=True, timeout=2.0)
    except sqlite3.Error:
        return set()
    try:
        rows = con.execute("SELECT hash FROM fingerprints WHERE song_id=? LIMIT ?",
                           (int(song_id), int(cap))).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    return {r[0] for r in rows if r and r[0]}


def fp_overlap(set_a, set_b):
    """containment：`|A∩B| / min(|A|,|B|)`。

    用 containment 而不是 Jaccard：两份同一录音但指纹行数不同（重编码会少一些）时，
    Jaccard 会被"多出来的那些行"压低，containment 才反映"短的那份几乎都在长的那份里"。
    """
    if not set_a or not set_b:
        return 0.0
    inter = len(set_a & set_b)
    return inter / float(min(len(set_a), len(set_b)))


def scan_pairs(items, db_path=None, find_fp_overlap=None,
               title_sim_thr=TITLE_SIM, dur_tol=DUR_TOL,
               fp_thr=FP_OVERLAP, max_fp_pairs=FP_MAX_PAIRS):
    """在一批曲目里找重复对。

    :param items: `[(path, meta_dict), ...]`（meta 就是 `cfg["music_meta"][path]`）
    :param db_path: 指纹库路径；None 或传了 `find_fp_overlap` 时不读库
    :param find_fp_overlap: 可选 `fn(sid_a, sid_b) -> float`，测试可注入
    :return: `[{"a": path_a, "b": path_b, "why": "title"/"fp",
                "title_sim": float, "dur_a": float, "dur_b": float,
                "overlap": float or None, "tier": 0/1}, ...]`

    `tier`：0 = 名字像 + 时长接近（最强，弹窗默认建议保留一份）；
             1 = 只有指纹重合（名字不像，可能是改名/换编码的同一段音频）。
    """
    items = [(p, m) for p, m in (items or []) if p and isinstance(m, dict)]
    n = len(items)
    out = []
    fp_used = 0
    for i in range(n):
        pa, ma = items[i]
        ta = ma.get("title") or ""
        da = dur_of(ma)
        for j in range(i + 1, n):
            pb, mb = items[j]
            db = dur_of(mb)
            if da <= 0 or db <= 0:
                continue
            dd = abs(da - db)
            if dd > dur_tol:
                continue
            s = title_sim(ta, mb.get("title") or "")
            why, tier, ov = None, 0, None
            if s >= title_sim_thr:
                why, tier = "title", 0
            elif dd <= DUR_TIGHT and fp_used < max_fp_pairs:
                sa, sb = ma.get("song_id"), mb.get("song_id")
                if sa is not None and sb is not None:
                    if find_fp_overlap is not None:
                        ov = float(find_fp_overlap(int(sa), int(sb)) or 0.0)
                    elif db_path:
                        ov = fp_overlap(fp_hashes(db_path, int(sa)),
                                        fp_hashes(db_path, int(sb)))
                        fp_used += 1
                    else:
                        ov = None
                    if ov is not None and ov >= fp_thr:
                        why, tier = "fp", 1
            if why:
                out.append({"a": pa, "b": pb, "why": why, "tier": tier,
                            "title_sim": round(s, 3), "dur_a": round(da, 2),
                            "dur_b": round(db, 2),
                            "overlap": (round(ov, 3) if ov is not None else None)})
    # 名字命中的排前面（更可信），同 tier 里按重合度/相似度降序
    out.sort(key=lambda d: (d["tier"], -(d["overlap"] or d["title_sim"])))
    return out


def describe(path, meta):
    """给弹窗用的一行摘要（不做 IO，只读 meta）。"""
    meta = meta or {}
    try:
        size = os.path.getsize(path) / 1048576.0
    except OSError:
        size = 0.0
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    g = meta.get("grid") or {}
    return {
        "path": path,
        "name": os.path.basename(path),
        "title": meta.get("title") or "",
        "artist": meta.get("artist") or "",
        "dur": dur_of(meta),
        "bpm": float(g.get("bpm") or 0.0),
        "fmt": ext,
        "size_mb": size,
        "has_cue": bool(meta.get("cue") or meta.get("cue_path")),
    }
