# -*- coding: utf-8 -*-
"""★ 后台身份扫描：给一首曲目算「文件 md5」+「音频指纹签名」+「BPM」。

**为什么必须后台**：一首 165 s 的 FLAC，解码 + 全曲 STFT 指纹要**秒级**；放 GUI 线程会卡界面。
**为什么要有回执**：用户被"点了没反应"坑过 ⇒ 阶段文案 + 结果/失败一律**发信号让 UI 显示**，
**禁止静默失败**。

信号：
  `staged(str)`                   —— 当前阶段的人类可读文案（"计算音频指纹与 BPM…"）
  `progress(int)`                 —— 0..100（粗粒度，只发几次，不刷屏）
  `done(int, str, str, float, str, dict)` —— `(曲目 index, md5, 指纹签名, BPM, 错误文案, BPM 来源信息)`；
                                     错误为空 = 成功；BPM 为 0.0 = **没识别出来**（不是错误）；
                                     来源信息 = `{"source": "vdj"|"audio"|"", "peaks": [...], "score": f}`

丢弃策略：窗口侧用"**代号（generation）+ 曲目 index**"判定过期结果（换歌/换码表后旧回执直接丢）。

★【修复】指纹签名按**源文件原生采样率**计算（`AudioTrack(samplerate=None)`），与演出端
  （AutoVJ `fp.py` 用 `soundfile` 按原生率解码）**同口径**；此前强制重采样到 44.1k，
  48k 素材算出的 `fp_id` 在演出端必然对不上（且完全静默）。
  ⚠ 本版本之前导出的码表里 `fp_id` 是旧口径算的 ⇒ 48k 源的歌**升级后请重新扫描身份并重新导出**。

★【新增】BPM 自动识别（`model/bpm_core.py`，逐字移植演出端 `beatgrid.py`）：
  **复用同一次整曲解码**（`_decode_mono()` 只解一遍，指纹与 BPM 共用同一份 mono），
  绝不为了 BPM 再解一次盘。BPM 识别失败不影响 md5 / 指纹写入。
  **优先级与演出端一致**：先查 VirtualDJ 库（有记录就直接用、不跑扫描），
  查不到才做音频扫描；结果里带 `source`（"vdj" / "audio"）供 UI 说明"这个 BPM 从哪来的"。

★【新增 C3】整曲哈希序列**顺手缓存**（`HASH_CACHE`）：
  身份扫描已经把整曲解码 + 全曲 STFT 算过了，`fingerprint_audio()` 的结果（几万条
  `(hash, offset)`）就是导出 `fp/<fp_id>.bin` 需要的**全部内容**。这里把它留在内存里
  ⇒ 导出时直接 `fp_pack.pack_hashes(...)`，**不要**为了写包再把整曲解一遍
  （2.67 s/首 × N 首会明显拖慢导出）。
  · 内存占用：49462 条 ≈ 3 MB/首（结构 `(20 字符 str, int)`）⇒ 上限 48 MB（约 16 首）；
  · **只缓存"当前这张表里用到的曲目"**：换表 / 新建 / 导入 / 关窗时 `clear()`；
  · 没扫过的曲目在导出时由窗口侧**后台线程补算 + 进度条**（见 `window._compute_fp_packs`），
    绝不卡 GUI。
"""
from __future__ import annotations

import logging
import os
import threading
from collections import OrderedDict

from PySide6.QtCore import QThread, Signal

from ercue.model import bpm_core, fp_core

log = logging.getLogger("ercue.ui.idscan")

# ==========================================================================
# ★ 整曲哈希序列缓存（C3：导出时不用再解一遍整曲）
# ==========================================================================
# 单条 `(hash_hex, offset_frame)` 的实际开销：str(20 字符) ≈ 69 B + int ≈ 28 B +
# tuple ≈ 56 B ≈ 150 B 实测偏大，这里保守按 60 B 估（只是**淘汰**用的粗估，不是精确值）。
_BYTES_PER_HASH = 60
CACHE_MAX_BYTES = 48 * 1024 * 1024     # 48 MB ≈ 16 首（每首 ~3 MB）
CACHE_MAX_ENTRIES = 16


class HashCache:
    """`(音频路径, fp_id) → 哈希序列` 的**有界 LRU** 缓存（线程安全）。

    ★ 为什么 key 里要带 `fp_id`：指纹是按 `fp_id` 写进包的。若音频文件被替换过
      （同路径不同内容），旧哈希序列与新 `fp_id` 不匹配 ⇒ 必须算不出来就**不取**，
      否则会把 A 的指纹写到 B 的包里（演出端就此永远认错歌）。
    """

    def __init__(self, max_bytes: int = CACHE_MAX_BYTES, max_entries: int = CACHE_MAX_ENTRIES):
        self._d = OrderedDict()
        self._lock = threading.Lock()
        self._bytes = 0
        self._max_bytes = int(max_bytes)
        self._max_entries = int(max_entries)

    @staticmethod
    def _key(path, fp_id) -> tuple:
        p = str(path or "")
        p = os.path.normcase(os.path.abspath(p)) if p else ""
        return (p, str(fp_id or ""))

    def put(self, path, fp_id, hashes) -> bool:
        """存入一首歌的哈希序列。`False` = 没存（参数不全 / 空序列 / 单首超上限）。"""
        key = self._key(path, fp_id)
        if not key[0] or not key[1] or not hashes:
            return False
        size = len(hashes) * _BYTES_PER_HASH
        if size > self._max_bytes:
            return False                          # 单首就超上限 ⇒ 不缓存（导出时现算）
        with self._lock:
            old = self._d.pop(key, None)
            if old is not None:
                self._bytes -= len(old) * _BYTES_PER_HASH
            self._d[key] = list(hashes)
            self._bytes += size
            while self._d and (self._bytes > self._max_bytes
                               or len(self._d) > self._max_entries):
                _k, v = self._d.popitem(last=False)     # FIFO/LRU：先淘汰最久未用的
                self._bytes -= len(v) * _BYTES_PER_HASH
        return True

    def get(self, path, fp_id):
        """取哈希序列；没有 ⇒ `None`（调用方据此决定"补算"）。命中会刷新 LRU 顺序。"""
        key = self._key(path, fp_id)
        with self._lock:
            v = self._d.get(key)
            if v is None:
                return None
            self._d.move_to_end(key)
            return v

    def clear(self) -> None:
        with self._lock:
            self._d.clear()
            self._bytes = 0

    def stats(self) -> dict:
        with self._lock:
            return {"entries": len(self._d), "bytes": self._bytes,
                    "max_bytes": self._max_bytes, "max_entries": self._max_entries}


# 进程内单例（窗口侧 `window.py` 直接用它取/清）
HASH_CACHE = HashCache()


def cached_hashes(path, fp_id):
    """取缓存（`None` = 没有）。**导出路径**直接调它，命中就不要重算。"""
    return HASH_CACHE.get(path, fp_id)


def put_cached_hashes(path, fp_id, hashes) -> bool:
    """写缓存（见 `HashCache.put`）。"""
    return HASH_CACHE.put(path, fp_id, hashes)


def clear_hash_cache() -> None:
    """换表 / 新建 / 导入 / 关窗时清空（**只缓存当前这张表的曲目**）。"""
    HASH_CACHE.clear()


# ==========================================================================
# 整曲解码 / 哈希（模块级：身份扫描与"导出补算"共用同一套实现）
# ==========================================================================
def decode_mono(path) -> tuple:
    """整曲解码为 **mono float32 @ 源文件原生采样率**。返回 `(mono, sr)`；失败 `(None, 0)`。

    ★★ 为什么必须**原生采样率**（跨端认歌的判据 `identity.fp_id`）：
      演出端（AutoVJ `fp.py`）用 `soundfile.read()` 按**文件原生率**解码，重采样只发生在
      `fp_core.fingerprint_audio()` 里的 `np.interp`；本端旧写法却让 PyAV/swresample
      **强制重采样到 44.1k** ⇒ 48k 素材两端喂给指纹算法的 PCM 不同 ⇒ 签名不同 ⇒
      码表里的 `fp_id` 在演出端**静默对不上**。
      ⇒ 这里统一走 `samplerate=None`（保持原生率），由 `fingerprint_audio()` 做与演出端
         **同款**的 `np.interp` 重采样。**绝不对 44.1k 特判绕过**（统一路径才不可能分叉）。
      顺带的好处：BPM 识别（`bpm_core` 用 `sr` 算 `dt` 与频率轴，与采样率无关）也直接吃
      原生率，**不需要为了 BPM 再重采样一次**。
    """
    import numpy as np
    from ercue.audio.player import AudioTrack
    if not path or not os.path.isfile(path):
        return None, 0
    tr = AudioTrack(path, samplerate=None)          # 保持源文件原生采样率
    if not tr.ok:
        return None, 0
    sr_native = int(tr.samplerate or fp_core.SAMPLE_RATE)
    buf = tr.samples()
    if buf is None or buf.shape[1] == 0:
        return None, 0
    mono = buf.mean(axis=0) if buf.shape[0] > 1 else buf[0]
    return np.ascontiguousarray(mono, dtype=np.float32), sr_native


def compute_hashes(path) -> tuple:
    """整曲 → `(hashes, sig)`：**一次**解码 + **一次** STFT。失败 `([], "")`（绝不抛）。

    导出补算（`window._compute_fp_packs`，后台线程）与身份扫描走的是**同一条路径**，
    所以"扫描出来的指纹"与"导出写的指纹"必然逐条一致。
    """
    try:
        mono, sr_native = decode_mono(path)
        if mono is None:
            return [], ""
        return fp_core.hashes_and_signature(mono, sr_native)
    except Exception:                                     # noqa: BLE001
        log.exception("计算音频指纹失败: %s", path)
        return [], ""



class IdentityScanWorker(QThread):
    """算一首曲目的身份（md5 + 指纹签名 + BPM）。**只读文件、绝不动模型**（结果由窗口线程落库）。"""

    staged = Signal(str)
    progress = Signal(int)
    # ★ 末位 `dict` = BPM 的**来源信息** `{"source": "vdj"|"audio"|"", "peaks": [...], "score": f}`
    #   （UI 要据此告诉用户"这个 BPM 是 VirtualDJ 抄的还是软件听出来的、要不要核对"）
    done = Signal(int, str, str, float, str, dict)   # index, md5, fp_sig, bpm, error, bpm_info

    def __init__(self, path: str, index: int, parent=None, dur: float = 0.0):
        super().__init__(parent)
        self.path = str(path or "")
        self.index = int(index)
        self.dur = float(dur or 0.0)      # 曲目时长（VDJ 第三级匹配用；0=不知道）

    # ------------------------------------------------------------------
    def run(self):                                          # noqa: D102  (QThread 入口)
        md5, sig, err, bpm = "", "", "", 0.0
        bpm_info = {"source": "", "peaks": [], "score": 0.0}
        hashes = []
        try:
            # ① 文件 md5（分块读，几十 MB 也就几十 ms；仍放这里以免大文件偶发卡顿）
            self.staged.emit("计算文件 md5…")
            self.progress.emit(10)
            if not self.path:
                err = "这首曲目还没导入音频文件"
            else:
                md5 = fp_core.file_md5(self.path)
                if not md5:
                    err = "读文件失败：算不出 md5（文件不存在 / 无权限 / 环境禁用了 md5）"
            self.progress.emit(35)

            # ② 整曲解码**一次** → 指纹（hashes + 签名）+ BPM（共用同一份 mono，绝不重复解码）
            if not err and not self.isInterruptionRequested():
                self.staged.emit("解码音频并计算指纹与 BPM…")
                self.progress.emit(45)
                mono, sr_native = None, 0
                try:
                    mono, sr_native = self._decode_mono()
                except Exception as e:                      # noqa: BLE001
                    log.exception("解码音频失败: %s", self.path)
                    err = "解码音频失败：%s: %s" % (type(e).__name__, e)
                if mono is not None and not err:
                    try:
                        # ★ C3：**一次** STFT 同时拿到 hashes 与签名（不要算两遍）
                        hashes, sig = fp_core.hashes_and_signature(mono, sr_native)
                        if not sig:
                            err = "指纹算不出来：文件里没有可解码的音频 / 音频太短"
                    except Exception as e:                  # noqa: BLE001
                        log.exception("计算指纹失败: %s", self.path)
                        err = "计算指纹失败：%s: %s" % (type(e).__name__, e)
                    # ③ BPM（失败**不算错误**：md5/指纹照常可用；窗口侧会提示"可以留空"）
                    #    ★ 先查 VirtualDJ 库（惰性加载 + 进程内缓存；**在 worker 线程里**解析，
                    #      绝不放 GUI 线程）；库里没有这首歌才做音频扫描。
                    self.progress.emit(75)
                    if not self.isInterruptionRequested():
                        self.staged.emit("识别 BPM…")
                        try:
                            vdj = bpm_core.get_vdj_index()
                        except Exception:                   # noqa: BLE001
                            log.exception("加载 VirtualDJ 索引失败（退回音频扫描）")
                            vdj = None
                        bpm, bpm_info = self._bpm_from(mono, sr_native, vdj=vdj,
                                                       path=self.path, dur=self.dur)
                elif not err:
                    err = "解码音频失败：文件里没有可解码的音频"
            self.progress.emit(100)
        except Exception as e:                              # noqa: BLE001
            log.exception("身份扫描失败: %s", self.path)
            err = err or ("扫描失败：%s: %s" % (type(e).__name__, e))
        # ★ C3：把哈希序列留在内存里，导出时直接 pack（**不再解一遍整曲**）。
        #   键 = (路径, fp_id)：fp_id 变了（音频被换过）⇒ 取不到旧序列，绝不会写错指纹。
        if hashes and sig:
            if put_cached_hashes(self.path, sig, hashes):
                log.info("已缓存指纹哈希：%s（%d 条，%.1f MB，缓存共 %d 首）",
                         os.path.basename(self.path), len(hashes),
                         len(hashes) * _BYTES_PER_HASH / 1048576.0,
                         HASH_CACHE.stats()["entries"])
            else:
                log.warning("指纹哈希未能缓存（%d 条）：%s", len(hashes), self.path)
        self.staged.emit("完成" if not err else "失败")
        self.done.emit(self.index, md5, sig, float(bpm), err, dict(bpm_info or {}))

    # ------------------------------------------------------------------
    def _decode_mono(self):
        """整曲解码为 **mono float32 @ 源文件原生采样率**。返回 `(mono, sr)`；失败 `(None, 0)`。

        实现委托给**模块级** `decode_mono(path)` —— 因为"导出补算指纹"的路径
        （`window._compute_fp_packs`，普通线程、不是 QThread）也要走**完全相同**的解码链，
        否则两条路径可能算出口径不同的指纹。

        ★★ 为什么必须**原生采样率**（跨端认歌的判据 `identity.fp_id`）：
          演出端（AutoVJ `fp.py`）用 `soundfile.read()` 按**文件原生率**解码，重采样只发生在
          `fp_core.fingerprint_audio()` 里的 `np.interp`；本端旧写法却让 PyAV/swresample
          **强制重采样到 44.1k** ⇒ 48k 素材两端喂给指纹算法的 PCM 不同 ⇒ 签名不同 ⇒
          码表里的 `fp_id` 在演出端**静默对不上**。
          ⇒ 这里统一走 `samplerate=None`（保持原生率），由 `fingerprint_audio()` 做与演出端
             **同款**的 `np.interp` 重采样。**绝不对 44.1k 特判绕过**（统一路径才不可能分叉）。
          顺带的好处：BPM 识别（`bpm_core` 用 `sr` 算 `dt` 与频率轴，与采样率无关）也直接吃
          原生率，**不需要为了 BPM 再重采样一次**。
        """
        return decode_mono(self.path)

    @staticmethod
    def _fp_signature_from(mono, sr_native) -> str:
        """已解码的 mono → 指纹签名（`fp2-<16hex>`）；无峰值返回 ""。"""
        return fp_core.fingerprint_signature(mono, int(sr_native))

    @staticmethod
    def _bpm_from(mono, sr_native, vdj=None, path="", dur=None):
        """已解码的 mono → `(bpm, info)`；识别不出返回 `(0.0, {...source:""})`，**绝不抛**。

        优先级与演出端一致：**先查 VirtualDJ 库**（有记录就直接用、不跑扫描），
        查不到才做音频扫描。`info` 里带 `source` / `peaks` / `score` 供 UI 说明来源。
        """
        info = {"source": "", "peaks": [], "score": 0.0}
        try:
            bpm, score, peaks, source = bpm_core.detect_bpm(
                mono, int(sr_native), vdj=vdj, path=path, dur=dur)
        except Exception:                                   # noqa: BLE001
            log.exception("BPM 识别失败")
            return 0.0, info
        info = {"source": str(source or ""),
                "peaks": [(float(b), float(s)) for b, s in (peaks or [])],
                "score": float(score or 0.0)}
        return (float(bpm) if bpm and bpm > 0 else 0.0), info

    def _fp_signature(self) -> str:
        """（保留的便捷入口）解码整曲 → 指纹签名。`tools/_fp_sig_parity.py` 直接调它。"""
        mono, sr_native = self._decode_mono()
        if mono is None:
            return ""
        return self._fp_signature_from(mono, sr_native)
