# -*- coding: utf-8 -*-
r"""erlib **扫描核心** —— 复用 ERAVJ 主程序的四个模块，产出四样东西。

| 产出 | 复用谁 | 耗时 |
|---|---|---|
| 音频指纹 | `fp.fingerprint_audio` | ~2.4 s/首（大头） |
| 节拍网格 | `beatgrid.grid_from_signal` | 复用同一次解码，很便宜 |
| 曲风 | `music_meta.resolve_music_genre` + `audio_genre` 本地 AI | 关键词/ID3 极快；AI 三段推理 ~0.3s |
| 封面 | `erlib.cover.cover_bytes`（**自带的轻量版**，只用 mutagen + PIL） | ~5~15 ms |

## 为什么每首只解码一次
主程序 `MusicScanThread._one` 就是这么做的：指纹要整曲解码（2.4s 是大头），
网格复用同一份 `sig`，不再读第二遍盘。erlib 沿用同一条路径，
**保证两边的指纹/网格算法逐字节一致**（否则导出的包在演出机上认不出来）。

## 并发
主程序实测：1/4/8 线程 = 2.94/2.53/2.45 秒·首，**加速只有 1.16~1.20×**
（瓶颈是磁盘 IO + 内存带宽）。erlib 默认 `min(4, 核数//4)` —— 够用且不抢机器资源。
"""

import os
import time

import numpy as np

AUDIO_EXT = (".flac", ".wav", ".mp3", ".m4a", ".aac", ".aiff", ".aif",
             ".ogg", ".opus", ".wv", ".ape")

# 与主程序一致的解码上限：网格只取前 180s 分析（`beatgrid.grid_from_signal` 默认）
_GRID_MAXSEC = 180.0


def find_audio(root, follow=True):
    """递归收集音频文件（排序保证每次扫描顺序一致，便于续扫对账）。"""
    out = []
    try:
        if os.path.isfile(root):
            low = root.lower()
            if low.endswith(AUDIO_EXT):
                return [root]
            return []
        for dirpath, dirnames, filenames in os.walk(root, followlinks=follow):
            dirnames.sort()
            for fn in sorted(filenames):
                if fn.lower().endswith(AUDIO_EXT) and not fn.startswith("."):
                    out.append(os.path.join(dirpath, fn))
    except Exception:                                            # noqa: BLE001
        pass
    return out


def decode(path):
    """解码成「单声道 float32 + 采样率 + 时长」。失败抛异常（由调用方吞）。"""
    import soundfile as sf
    info = sf.info(path)
    dur = float(info.duration)
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    sig = np.mean(data, axis=1).astype(np.float32, copy=False)
    return sig, int(sr), dur


def local_ai_genre(gc, sig, sr, fracs=(0.25, 0.5, 0.75), seg_sec=6.0):
    """整曲本地 AI：取 25%/50%/75% 三段各 6s 分类，投票取前 4。

    ⚠ 必须与主程序 `ui_main.MusicScanThread._local_ai_genre` **逐行等价** ——
      否则 erlib 导出的曲风会和演出机自己扫出来的不一样，用户会以为是 bug。
      改动这里时，那边要同步改。
    """
    if gc is None:
        return []
    from collections import Counter
    seg = int(sr * seg_sec)
    if len(sig) < seg:
        pts = [sig]
    else:
        pts = [sig[int(sr * f):int(sr * f) + seg] for f in fracs]
    votes = []
    for s in pts:
        try:
            votes.extend(gc.classify(s, sr)["tags"])
        except Exception:                                        # noqa: BLE001
            pass
    return [t for t, _ in Counter(votes).most_common(4)]


def simplify_genres(tags):
    """英文曲风 → 中文（主程序用 `simplify_genres`，这里做同样的兜底调用）。"""
    if not tags:
        return []
    try:
        from genre_visual_default import simplify_genres as _sg
        return _sg(tags) or []
    except Exception:                                            # noqa: BLE001
        return list(tags)


class Scanner:
    """一次扫描会话。

    · `root`：曲库根目录（用于算 `rel_path`，让包内路径可读）
    · `on_progress(done, total, path)`：进度回调（在 worker 线程里调，UI 请自行切回主线程）
    · `use_online`：是否允许联网查曲风（erlib 里做成"事后补跑"，扫描阶段默认关）
    """

    def __init__(self, root="", on_progress=None, use_online=False,
                 app_ver=""):
        self.root = root
        self.on_progress = on_progress
        self.use_online = bool(use_online)
        self.app_ver = app_ver
        self._gc = None
        self._vdj = None
        self._stop = False
        self.stats = {"ok": 0, "fail": 0, "skip": 0, "t_decode": 0.0,
                      "t_fp": 0.0, "t_meta": 0.0, "t_grid": 0.0, "t_cover": 0.0}

    # ---------------- 资源准备（进程级只做一次） ----------------
    def _ensure(self):
        if self._gc is None:
            try:
                from audio_genre import get_genre_classifier
                self._gc = get_genre_classifier()
            except Exception:                                    # noqa: BLE001
                self._gc = False                                 # False = 试过但没有
        if self._vdj is None:
            try:
                import beatgrid as bg
                self._vdj = bg.load_vdj() or False
            except Exception:                                    # noqa: BLE001
                self._vdj = False
        # 联网开关：扫描阶段默认关（在线查询很慢且会被 Discogs 限流）
        try:
            import genre_lookup as gl
            gl.set_online_enabled(self.use_online)
        except Exception:                                        # noqa: BLE001
            pass

    def stop(self):
        self._stop = True

    # ---------------- 单首 ----------------
    def scan_one(self, path):
        """返回 `(sid, meta_dict, fps, cover_bytes)`；失败抛异常（调用方吞）。"""
        self._ensure()
        from erlib.format import song_id_of
        t0 = time.perf_counter()
        sig, sr, dur = decode(path)
        t1 = time.perf_counter()

        import fp as _fp
        fps = _fp.fingerprint_audio(sig, sr)
        t2 = time.perf_counter()

        from music_meta import resolve_music_genre
        r = resolve_music_genre(path) or {}
        if self._gc and r.get("level") != "track":
            ai_zh = simplify_genres(local_ai_genre(self._gc, sig, sr))
            r["ai_zh"] = ai_zh
            on = list(r.get("genres") or [])
            if ai_zh:
                seen, merged = set(), []
                order = (on[:3] + ai_zh[:2]) if r.get("level") == "track" \
                    else (ai_zh[:3] + on[:2])
                for z in order:
                    if z and z not in seen:
                        seen.add(z)
                        merged.append(z)
                r["genres"] = merged
                if not on and ai_zh:
                    r["source"] = "localai"
        t3 = time.perf_counter()

        try:
            import beatgrid as _bg
            g = _bg.grid_from_signal(sig, sr, self._vdj or None,
                                     path=path, dur=dur)
            if g:
                r["grid"] = g
        except Exception:                                        # noqa: BLE001
            pass
        t4 = time.perf_counter()

        cover_bytes = None
        try:
            # ★ 用 erlib 自带的轻量封面提取（mutagen + PIL）而**不是**主程序的
            #   `music_cover` —— 后者会拖进 cv2(97MB) + av/FFmpeg(63MB)，
            #   而这里只需要"抠出内嵌图 + 缩成 200px"。见 erlib/cover.py 的说明。
            from erlib.cover import cover_bytes as _cb
            cover_bytes = _cb(path)
        except Exception:                                        # noqa: BLE001
            cover_bytes = None
        t5 = time.perf_counter()

        for k, dt in (("t_decode", t1 - t0), ("t_fp", t2 - t1),
                      ("t_meta", t3 - t2), ("t_grid", t4 - t3),
                      ("t_cover", t5 - t4)):
            self.stats[k] += dt

        try:
            rel = os.path.relpath(path, self.root) if self.root else path
        except Exception:                                        # noqa: BLE001
            rel = path
        try:
            fsize = os.path.getsize(path)
        except Exception:                                        # noqa: BLE001
            fsize = 0

        meta = {
            "rel_path": rel,
            "file_name": os.path.basename(path),
            "file_size": fsize,
            "duration": dur,
            "artist": r.get("artist", ""),
            "title": r.get("title", ""),
            "genre_raw": r.get("genre", ""),
            "genres": r.get("genres") or [],
            "styles": r.get("styles") or [],
            "source": r.get("source") or "",
            "level": r.get("level") or "",
            "ai_zh": r.get("ai_zh") or [],
            "grid": r.get("grid") or {},
        }
        return song_id_of(path), meta, fps, cover_bytes

    # ---------------- 整目录（串行版；并行见 scan_tree_parallel） ----------------
    def scan_tree(self, root, writer, progress_every=1):
        """串行扫描并写入 writer。返回统计 dict。"""
        files = find_audio(root)
        done_ids = writer.existing_ids() if writer is not None else set()
        from erlib.format import song_id_of
        todo = []
        for p in files:
            if song_id_of(p) in done_ids:
                self.stats["skip"] += 1
            else:
                todo.append(p)
        total = len(todo)
        t0 = time.perf_counter()
        for i, p in enumerate(todo, 1):
            if self._stop:
                break
            try:
                sid, meta, fps, cov = self.scan_one(p)
                writer.add_song(sid, meta, fps, cov, commit=False)
                if i % 6 == 0:
                    writer.commit()
                self.stats["ok"] += 1
            except Exception as e:                               # noqa: BLE001
                self.stats["fail"] += 1
                # ⚠⚠ **失败必须留下原因**（2026-10-08 现场教训）：
                #   原来只 `fail += 1`，界面只能显示"失败 N 首"，
                #   用户不知道发生了什么、我也无从下手 —— 白跑一小时才发现全废。
                #   现在前 20 条带文件名与异常原文，界面直接列给用户看。
                msg = "%s: %s" % (type(e).__name__, e)
                errs = self.stats.setdefault("errors", [])
                if len(errs) < 20:
                    errs.append((os.path.basename(p), msg))
                if len(errs) == 1:
                    print("[erlib] 首条扫描失败：%s\n         → %s" % (p, msg))
            if self.on_progress and (i % progress_every == 0 or i == total):
                try:
                    self.on_progress(i, total, p)
                except Exception:                                # noqa: BLE001
                    pass
        if writer is not None:
            writer.commit()
        self.stats["elapsed"] = time.perf_counter() - t0
        self.stats["total"] = total
        return self.stats
