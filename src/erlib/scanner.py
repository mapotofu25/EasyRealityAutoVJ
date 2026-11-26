# -*- coding: utf-8 -*-
r"""erlib **扫描核心** —— 复用 ERAVJ 主程序的模块，产出**只有演出要用的四样**。

| 产出 | 复用谁 | 耗时 |
|---|---|---|
| 音频指纹 | `fp.fingerprint_audio` | ~2.4 s/首（大头） |
| 节拍网格 | `beatgrid.grid_from_signal` | 复用同一次解码，很便宜 |
| 波形包络 | `waveform.waveform_from_signal` | 复用同一次解码，极便宜 |
| 封面 | `erlib.cover.cover_bytes`（**自带的轻量版**，只用 mutagen + PIL） | ~5~15 ms |

另读 ID3 的**艺人/曲名**（`erlib.tags.read_tags`，只给界面看的，不影响识别）。

## ⚠ 曲风已经整块砍掉（2026-10-30「大砍刀」）
`music_meta` / `audio_genre` / `genre_lookup` / `genre_visual_default` 四个模块
**已被删除**。本文件原来在 `scan_one` 里 `from music_meta import resolve_music_genre`
⇒ 点「开始扫描」直接 ImportError（2026-10-31 用户「erlib 只管扫描导出啊」才发现）。
现在扫描链路**完全不碰曲风**：需要艺人/曲名就自己读标签，不再依赖那两个模块。
导出包里的 `genres/styles/source/level/ai_zh` 列仍保留（老库/老包兼容），新扫的一律空。

## 为什么每首只解码一次
主程序 `MusicScanThread._one` 就是这么做的：指纹要整曲解码（2.4s 是大头），
网格复用同一份 `sig`，不再读第二遍盘。erlib 沿用同一条路径，
**保证两边的指纹/网格算法逐字节一致**（否则导出的包在演出机上认不出来）。

## 并发
主程序实测：1/4/8 线程 = 2.94/2.53/2.45 秒·首，**加速只有 1.16~1.20×**
（瓶颈是磁盘 IO + 内存带宽）。erlib 默认 `min(4, 核数//4)` —— 够用且不抢机器资源。
"""

import io
import os
import sqlite3
import threading
import time

import numpy as np

# ★ 2026-11-06：自然排序键统一由 `format.win_sort_key` 提供（三处必须同一口径）
from erlib.format import win_sort_key  # noqa: E402

AUDIO_EXT = (".flac", ".wav", ".mp3", ".m4a", ".aac", ".aiff", ".aif",
             ".ogg", ".opus", ".wv", ".ape")

# 与主程序一致的解码上限：网格只取前 180s 分析（`beatgrid.grid_from_signal` 默认）
_GRID_MAXSEC = 180.0


def _write_retry(fn, tries=6, delay=0.8):
    """写库遇到 `database is locked` 时退避重试（另一个实例/连接在写）。"""
    for k in range(tries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or k == tries - 1:
                raise
            time.sleep(delay * (k + 1))


def find_audio(root, follow=True):
    """递归收集音频文件（排序保证每次扫描顺序一致，便于续扫对账）。

    ★ 2026-11-06：排序键换成 `win_sort_key`（贴近资源管理器），
      **这是"导入顺序 = 文件夹里看到的顺序"的一半**；另一半在 `scan_paths`
      （倒序扫描，因为列表按 `import_seq DESC` 显示）。
    """
    out = []
    try:
        if os.path.isfile(root):
            low = root.lower()
            if low.endswith(AUDIO_EXT):
                return [root]
            return []
        for dirpath, dirnames, filenames in os.walk(root, followlinks=follow):
            dirnames.sort(key=win_sort_key)
            for fn in sorted(filenames, key=win_sort_key):
                if fn.lower().endswith(AUDIO_EXT) and not fn.startswith("."):
                    out.append(os.path.join(dirpath, fn))
    except Exception:                                            # noqa: BLE001
        pass
    return out


def decode(path):
    """解码成「单声道 float32 + 采样率 + 时长」。失败抛异常（由调用方吞）。"""
    # ★ 2026-11-22：soundfile 对个别 FLAC 会报 "decoder lost sync"；
    #   统一走 audio_decode（PyAV 兜底），波形/网格才不会整首消失。
    from audio_decode import decode_mono
    sig, sr, dur = decode_mono(path, maxsec=None)
    return sig, int(sr), float(dur)


def read_tags(path):
    """读 ID3/Vorbis 的**艺人 / 曲名**（只给界面显示，读不到就空字符串）。

    ★ 2026-10-31：原来这两列来自 `music_meta.resolve_music_genre`，那个模块已随
      「大砍刀」删除 ⇒ 现在只做一件事：从标签里取两个字段。
      ⚠ 复用 `erlib.cover._embedded_bytes` 同款思路：**任何异常都吞掉**，
        标签缺失/编码怪/文件损坏都不该让一首歌扫不出来。
    """
    artist = title = ""
    low = str(path).lower()
    try:
        if low.endswith(".flac"):
            from mutagen.flac import FLAC
            a = FLAC(path)
            artist = (a.get("artist") or [""])[0]
            title = (a.get("title") or [""])[0]
        elif low.endswith(".mp3"):
            from mutagen.id3 import ID3
            a = ID3(path)
            artist = str(a.get("TPE1") or "")
            title = str(a.get("TIT2") or "")
        elif low.endswith((".m4a", ".mp4", ".aac", ".aiff", ".aif")):
            from mutagen.mp4 import MP4
            t = MP4(path).tags or {}
            artist = (t.get("\xa9ART") or [""])[0]
            title = (t.get("\xa9nam") or [""])[0]
        elif low.endswith((".ogg", ".opus")):
            import mutagen
            m = mutagen.File(path)
            t = getattr(m, "tags", None) or {}
            artist = (t.get("artist") or [""])[0]
            title = (t.get("title") or [""])[0]
        else:
            import mutagen
            m = mutagen.File(path)
            t = getattr(m, "tags", None) or {}
            for k in ("artist", "ARTIST", "\xa9ART", "TPE1"):
                v = t.get(k)
                if v:
                    artist = str(v[0] if isinstance(v, (list, tuple)) else v)
                    break
            for k in ("title", "TITLE", "\xa9nam", "TIT2"):
                v = t.get(k)
                if v:
                    title = str(v[0] if isinstance(v, (list, tuple)) else v)
                    break
    except Exception:                                            # noqa: BLE001
        pass
    return str(artist or "").strip(), str(title or "").strip()


class Scanner:
    """一次扫描会话。

    · `root`：曲库根目录（用于算 `rel_path`，让包内路径可读）
    · `on_progress(done, total, path)`：进度回调（在 worker 线程里调，UI 请自行切回主线程）

    ⚠ `use_online` 参数已废弃（曲风整块砍掉，留着只为兼容老调用方，传什么都无效）。
    """

    def __init__(self, root="", on_progress=None, use_online=False,
                 app_ver=""):
        self.root = root
        self.on_progress = on_progress
        self.use_online = False        # 废弃参数，恒 False（见 docstring）
        self.app_ver = app_ver
        self._stop = False
        # ★ 2026-11-14：并行扫描时 worker 会累加 t_* 计时，加锁避免丢更新
        self._stats_lock = threading.Lock()
        self.stats = {"ok": 0, "fail": 0, "skip": 0, "t_decode": 0.0,
                      "t_fp": 0.0, "t_meta": 0.0, "t_grid": 0.0,
                      "t_wave": 0.0, "t_cover": 0.0}

    # ---------------- 资源准备（进程级只做一次） ----------------
    def _ensure(self):
        """目前没有需要"进程级只做一次"的重资源。

        ★ 2026-10-31：**去掉了 VDJ 索引加载** —— 主程序扫描已改成
          `use_vdj=False`（完全本地），erlib 若还读 VDJ 只会两边结果不同。
        """

    def stop(self):
        self._stop = True

    # ---------------- 单首 ----------------
    def scan_one(self, path):
        """返回 `(sid, meta_dict, fps, cover_bytes, waveform)`；失败抛异常（调用方吞）。"""
        self._ensure()
        from erlib.format import song_id_of
        t0 = time.perf_counter()
        sig, sr, dur = decode(path)
        t1 = time.perf_counter()

        import fp as _fp
        fps = _fp.fingerprint_audio(sig, sr)
        t2 = time.perf_counter()

        artist, title = read_tags(path)
        t3 = time.perf_counter()

        grid = {}
        secs = secs_raw = None
        try:
            import beatgrid as _bg
            # ★★ 2026-10-31（用户「erlib 的扫描要和 eravj 一样」）：
            #   `grid_from_signal` 的 `use_vdj` 默认是 **True**，而主程序
            #   `MusicScanThread._one` 显式传 `use_vdj=False`（2026-10-30 用户要求
            #   「去掉 vdj 源，完全依赖本地扫描确定 bpm」）。
            #   不传 ⇒ erlib 拿 VDJ 的 BPM 当先验 ⇒ 同一个文件两边结果不同：
            #     实测 `System Overloaded.flac`：erlib bpm=175.0001 / src=vdj /
            #     anchor=0.0278 / conf=1.0，主程序 bpm=175.0 / src=bt /
            #     anchor=0.0313 / conf=0.95 —— 锚点差 3.5ms，长曲累积成半拍。
            #   ⇒ 必须**逐参数照抄主程序**，并把 `out_feats` 拿回来给能量分段复用。
            _feats = {}
            g = _bg.grid_from_signal(sig, sr, None, path=path, dur=dur,
                                     use_vdj=False, out_feats=_feats)
            if g:
                # ★ 2026-11-18（用户：八拍线整首偏 4 拍）：
                #   用「不吸附网格」的分段边界反推八拍相位 —— 真实段落边界落在
                #   ±4 拍外时，说明整首网格晚了半句，这里把 anchor 挪回对齐位置。
                #   只在证据一致（边界相位离散度 ≤1 拍、偏移 ≥1.5 拍、边界数 ≥6）时生效。
                try:
                    import sections as _sec0
                    _fr = _feats.get("f")
                    _dr = float(_feats.get("dur") or 0.0)
                    _bl = float((g or {}).get("beat_len") or 0.0)
                    _an = float((g or {}).get("anchor") or 0.0)
                    if _fr is not None and _dr > 0 and _bl > 0:
                        _raw = _sec0.auto_segments(_fr, dur=_dr, grid=None) or []
                        _ts = [float(x["t0"]) for x in _raw[1:]]
                        if len(_ts) >= 6:
                            _mods = np.array([((t - _an) / _bl) % 8.0 for t in _ts])
                            _cand = np.arange(0.0, 8.0, 0.25)

                            def _cd(x, c):
                                return (x - c + 4.0) % 8.0 - 4.0

                            _best = min(_cand, key=lambda c: float(np.mean(np.abs(_cd(_mods, c)))))
                            _dev = float(np.mean(np.abs(_cd(_mods, _best))))
                            if abs(_best) >= 1.5 and _dev <= 1.0:
                                _sh = -int(round(_best))
                                _new = (_an + _sh * _bl) % (8.0 * _bl)
                                g["anchor"] = round(float(_new), 4)
                                g["phase_fix"] = int(_sh)
                                _b = float(g.get("base_anchor") or 0.0)
                                g["phrase"] = int(round((_new - _b) / _bl)) % 8
                                for _sg in (g.get("segments") or []):
                                    if isinstance(_sg, dict) and "anchor" in _sg:
                                        _sg["anchor"] = round(float(_sg["anchor"]) + _sh * _bl, 4)
                except Exception:                        # noqa: BLE001
                    pass
                grid = g
            # ★ 能量分段（drop 段落）也照抄主程序：同一份 STFT 复用，不重新解码。
            try:
                import sections as _sec
                _f2 = _feats.get("f")
                _d2 = float(_feats.get("dur") or 0.0)
                if _f2 is not None and _d2 > 0 and grid:
                    # ★ 2026-11-16（用户：「用音色分段 能量还是我们之前的算法」）：
                    #   `ERLIB_SEG_MODE=timbre` 时边界来自音色变化（`timbre_seg`），
                    #   能量/档位仍由 `sections.segments_from_edges()` 用老口径算；
                    #   默认（energy）完全走原来的能量分段，互不影响。
                    # 优先级：环境变量 > %LOCALAPPDATA%\AutoVJ\erlib_settings.json 的 seg_mode > 默认 timbre
                    _seg_mode = (os.environ.get("ERLIB_SEG_MODE") or "").strip().lower()
                    if not _seg_mode:
                        try:
                            import json as _json
                            _sp = os.path.join(os.environ.get("LOCALAPPDATA") or "",
                                               "AutoVJ", "erlib_settings.json")
                            with io.open(_sp, "r", encoding="utf-8") as _sf:
                                _seg_mode = str((_json.load(_sf) or {}).get("seg_mode")
                                                or "").strip().lower()
                        except Exception:                    # noqa: BLE001
                            _seg_mode = ""
                    if not _seg_mode:
                        _seg_mode = "structure"
                    _sg = None
                    _sg_from_timbre = False
                    if _seg_mode == "structure":
                        # ★ 2026-11-16（The Gravelord）：8 拍块级 DP 结构分段。
                        #   第一段 drop 之前沿用旧能量分段边界，drop 之后按
                        #   8/16/32/48/56/64 拍句长重切，不再跑 16 倍数硬修。
                        try:
                            import structure_seg as _sseg
                            _gm = _sec.grid_from_meta(
                                {"grid": grid, "grid_manual": {}}) or None
                            _base = _sec.auto_segments(_f2, dur=_d2, grid=_gm)
                            _af0 = _sec.take_folded_anchor()
                            if _af0 is not None and isinstance(grid, dict):
                                grid["anchor"] = _af0
                                try:
                                    grid["phrase"] = int((int(grid.get("phrase") or 0) - 4) % 8)
                                except (TypeError, ValueError):
                                    grid["phrase"] = 0
                                _gm = _sec.grid_from_meta(
                                    {"grid": grid, "grid_manual": {}}) or _gm
                            _se = _sseg.structure_edges_hybrid(_f2, _d2, _gm, _base)
                            if _se and len(_se) >= 3:
                                _sg2 = _sec.segments_from_edges(
                                    _f2, _se, dur=_d2, grid=_gm, post=False)
                                # 防碎：结构分段明显比旧能量分段更碎时回退旧结果
                                _lim = max(len(_base or []) + 3,
                                           int(len(_base or []) * 1.4))
                                if _sg2 and len(_sg2) <= _lim:
                                    _sg = _sg2
                                    _sg_from_timbre = True
                        except Exception:                    # noqa: BLE001
                            _sg = None
                    elif _seg_mode == "timbre":
                        try:
                            import timbre_seg as _tseg
                            _te = _tseg.edges(sig, sr, grid, dur=dur)
                            if _te and len(_te) >= 3:
                                _sg = _sec.segments_from_edges(
                                    _f2, _te, dur=_d2,
                                    grid=_sec.grid_from_meta(
                                        {"grid": grid, "grid_manual": {}}) or None)
                                _sg_from_timbre = True
                        except Exception:                    # noqa: BLE001
                            _sg = None
                    if _sg is None:
                        _sg = _sec.auto_segments(
                            _f2, dur=_d2,
                            grid=_sec.grid_from_meta(
                                {"grid": grid, "grid_manual": {}}) or None)
                    # ★ 折过锚点 ⇒ 写回网格，否则 8 拍线还是旧相位（用户 2026-11-07）
                    _af = _sec.take_folded_anchor()
                    if _af is not None and isinstance(grid, dict):
                        grid["anchor"] = _af
                        # ★ 2026-11-12：折锚点 = 把八拍头提前 4 拍（等价于翻转
                        #   半句相位）。只改 anchor 不改 phrase 会让 (anchor, phrase)
                        #   这一对自相矛盾 —— 主程序 `effective_grid`/详情页按
                        #   `anchor + phrase*beat_len` 反推基准拍时会整体偏 4 拍。
                        #   phrase 的定义是「八拍头相对基准拍的拍偏移」，因此同步 -4。
                        try:
                            _ph = int(grid.get("phrase") or 0)
                            grid["phrase"] = int((_ph - 4) % 8)
                        except (TypeError, ValueError):
                            grid["phrase"] = 0
                    # `raw` 同样过乐句判据（只是不吸附位置），与主程序一致；
                    # 音色模式没有「未吸附」的独立结果，raw 直接复用分段结果。
                    if _sg_from_timbre:
                        _sgr = _sg
                    else:
                        _sgr = _sec.auto_segments(
                            _f2, dur=_d2, grid=None,
                            phrase_grid=_sec.grid_from_meta(
                                {"grid": grid, "grid_manual": {}}) or None)
                    if _sg:
                        secs, secs_raw = _sg, _sgr
                del _f2
            except Exception:                                    # noqa: BLE001
                pass
        except Exception:                                        # noqa: BLE001
            pass
        t4 = time.perf_counter()

        # ★ 波形峰值包络：复用同一次已解码的 `sig`（每首只解码一次），现算现存。
        #   失败不阻断扫描（详情页没波形就显示「无波形」）。
        wf = None
        try:
            import waveform as _wf
            wf = _wf.waveform_from_signal(sig, sr)
        except Exception:                                        # noqa: BLE001
            wf = None
        t5 = time.perf_counter()

        cover_bytes = None
        try:
            # ★ 用 erlib 自带的轻量封面提取（mutagen + PIL）而**不是**主程序的
            #   `music_cover` —— 后者会拖进 cv2(97MB) + av/FFmpeg(63MB)，
            #   而这里只需要"抠出内嵌图 + 缩成 200px"。见 erlib/cover.py 的说明。
            from erlib.cover import cover_bytes as _cb
            cover_bytes = _cb(path)
        except Exception:                                        # noqa: BLE001
            cover_bytes = None
        t6 = time.perf_counter()

        with self._stats_lock:
            for k, dt in (("t_decode", t1 - t0), ("t_fp", t2 - t1),
                          ("t_meta", t3 - t2), ("t_grid", t4 - t3),
                          ("t_wave", t5 - t4), ("t_cover", t6 - t5)):
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
            # ★ 2026-10-31（用户「详情页还得能播放，只是导出的文件不要带位置」）：
            #   本机绝对路径单独存一列，**只给工作库试听用**；导出时整列丢弃
            #   （见 `transfer.export_subset`）⇒ 包里不带位置。
            "src_path": os.path.abspath(path),
            "file_size": fsize,
            "duration": dur,
            "artist": artist,
            "title": title,
            # 曲风四列保留为空（表结构没变，老库/老包还能读；新扫的一律空）
            "grid": grid,
            # ★ 2026-10-31：能量段落（与主程序扫描产出完全一致）
            "sections": secs or [],
            "sections_raw": secs_raw or [],
        }
        return song_id_of(path), meta, fps, cover_bytes, wf

    # ---------------- 整目录 / 显式文件列表（串行） ----------------
    def scan_tree(self, root, writer, progress_every=1):
        """扫整个目录（递归）→ 写 writer。返回统计 dict。"""
        return self.scan_paths(find_audio(root), writer,
                               progress_every=progress_every)

    def scan_paths(self, paths, writer, progress_every=1, force=False):
        """扫**明确给出的文件列表**（「导入文件」/ 拖拽/「重新分析」用）。

        :param force: ★ 2026-11-05：「重新分析勾选」用。
              False（默认）＝已扫过的跳过（中断续扫的关键，别改）；
              True ＝**不查 `existing_ids`**，逐个重算并覆盖写。
              安全前提：`LibWriter.add_song` 是幂等的（先删指纹/封面/波形再
              `INSERT OR REPLACE`），所以重扫不会留下重复行或孤儿记录。
              调用方要自己保证 `paths` 就是用户勾选的那几首 —— 这里不做筛选。

        ★ 2026-10-31（用户「扫描改成导入文件夹和导入文件 / 支持拖入」）：
          `scan_tree` 只能整目录，而拖进来的可能是一堆散落在不同目录的文件 ⇒
          把循环抽到这里，两种入口共用同一套扫描/落库逻辑（保证结果一致）。
        """
        done_ids = set() if force else (writer.existing_ids() if writer is not None else set())
        from erlib.format import song_id_of
        todo = []
        for p in paths:
            if not p or not os.path.isfile(p):
                continue
            if song_id_of(p) in done_ids:
                self.stats["skip"] += 1
            else:
                todo.append(p)
        total = len(todo)
        t0 = time.perf_counter()
        # ★★★ 2026-11-06（用户：「新导入的堆在最上面；导入文件夹按 Windows 里的显示顺序」）：
        #   列表是 `ORDER BY import_seq DESC`（新在上），而 `import_seq` 按**扫描顺序递增**
        #   ⇒ 这里必须**倒着扫**，最终列表才等于资源管理器里的 A/B/C 顺序（否则是 C/B/A）。
        #   扫描顺序不影响分析结果（每首独立、写入按 song_id 幂等），只影响发号先后。
        todo.reverse()

        # ★★★ 2026-11-14（用户「扫描速度太慢了」）：并行扫描。
        #   单首 6~10 s 里 grid（BT 推理）占 75~85%，纯 CPU、每首独立；
        #   `phrase_bt` 已改成每线程一条 ORT 会话，所以这里可以真并行。
        #   worker 数：ERLIB_SCAN_WORKERS 优先；否则 min(4, 核数//8)（本机 32 核 ⇒ 4）。
        #   1 = 完全退回原来的串行路径（排查用）。
        workers = 1
        try:
            workers = int(os.environ.get("ERLIB_SCAN_WORKERS") or 0)
        except (TypeError, ValueError):
            workers = 0
        if workers <= 0:
            workers = min(4, max(1, (os.cpu_count() or 4) // 8))
        workers = max(1, min(workers, max(1, total)))
        if workers > 1 and total > 1:
            return self._scan_parallel(todo, total, writer, workers)

        for i, p in enumerate(todo, 1):
            if self._stop:
                break
            # ★ 2026-11-13（用户「重新分析卡 0%」）：单首要 8~25 s，而进度只在**扫完**
            #   才 +1，前 20 秒界面一直是 0% —— 看着就像卡死。这里先报一次
            #   “正在扫哪首”（done 仍是 i-1，不虚报进度），界面立刻有反应。
            if self.on_progress:
                try:
                    self.on_progress(i - 1, total, p)
                except Exception:                                # noqa: BLE001
                    pass
            try:
                sid, meta, fps, cov, wf = self.scan_one(p)
                _write_retry(lambda: writer.add_song(
                    sid, meta, fps, cov, commit=False, waveform=wf))
                if i % 6 == 0:
                    _write_retry(writer.commit)
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

    # ---------------- 并行扫描（2026-11-14 提速） ----------------
    def _scan_parallel(self, todo, total, writer, workers):
        """并行跑 `todo`：worker 只算，写库/进度/统计都在本线程。

        完成顺序不可控，但 `import_seq` 决定列表顺序 ⇒ 用「重排缓冲」：
        结果按原始下标存着，从 0 开始连续就绪才落库，保证顺序与串行一致。
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        t0 = time.perf_counter()
        self.stats["workers"] = workers
        if self.on_progress:
            try:
                self.on_progress(0, total, todo[0])
            except Exception:                                    # noqa: BLE001
                pass

        def _fail(p, e):
            self.stats["fail"] += 1
            msg = "%s: %s" % (type(e).__name__, e)
            errs = self.stats.setdefault("errors", [])
            if len(errs) < 20:
                errs.append((os.path.basename(p), msg))
            if len(errs) == 1:
                print("[erlib] 首条扫描失败：%s\n         → %s" % (p, msg))

        buf = {}
        nxt = 0
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="erlib-scan") as ex:
            futs = {ex.submit(self.scan_one, p): (i, p)
                    for i, p in enumerate(todo)}
            for fut in as_completed(futs):
                i, p = futs[fut]
                if self._stop:
                    for f in futs:
                        f.cancel()
                    break
                try:
                    buf[i] = (p, fut.result())
                except Exception as e:                            # noqa: BLE001
                    buf[i] = (p, e)
                while nxt in buf:
                    pp, res = buf.pop(nxt)
                    nxt += 1
                    if isinstance(res, Exception):
                        _fail(pp, res)
                    else:
                        sid, meta, fps, cov, wf = res
                        try:
                            _write_retry(lambda: writer.add_song(
                                sid, meta, fps, cov, commit=False, waveform=wf))
                        except Exception as e:                    # noqa: BLE001
                            _fail(pp, e)
                        else:
                            self.stats["ok"] += 1
                            if self.stats["ok"] % 6 == 0:
                                _write_retry(writer.commit)
                    if self.on_progress:
                        try:
                            self.on_progress(nxt, total, pp)
                        except Exception:                        # noqa: BLE001
                            pass
        if writer is not None:
            writer.commit()
        self.stats["elapsed"] = time.perf_counter() - t0
        self.stats["total"] = total
        return self.stats
