# -*- coding: utf-8 -*-
"""音频指纹（Dejavu 算法，参数与 VJVision fp/fingerprint.h 一致）。

STFT(4096/Hann/hop2048) → log-power 谱 → 2D 峰值(分块最大值, 12dB) →
SHA1(量化 freq1|freq2|dt) 前 20 hex → SQLite 倒排索引 → 对齐投票匹配。

自建库自匹配（建库与查询用同一套峰值检测），无需与 VJVision 二进制指纹互通。
"""
import hashlib
import sqlite3

import numpy as np

SAMPLE_RATE = 44100
FFT_WINDOW = 4096
HOP_SIZE = 2048
FAN_VALUE = 8            # 每峰配后 fan-1=7 个峰
AMP_MIN = 12.0           # dB
HASH_LEN = 20            # SHA1 前 20 hex = 80 bit
MAX_HASH_TIME_DELTA = 200
DT_QUANT = 4             # 时间差量化（抗 keylock ±8% 变速）
FREQ_QUANT = 4           # 频率量化
MIN_CONFIDENCE = 0.05
PEAK_NEIGHBORHOOD = 20   # 41×41 邻域严格局部极大值（VJVision peaks.cpp 同款）
FP_SCHEMA_VERSION = 2    # 峰值检测算法版本（v1=规则分块；v2=邻域极大值，抗变速）


def _spectrogram(sig):
    """mono float32(44.1k) → log-power 谱 (dB)，shape [freq_bins, frames]"""
    if len(sig) < FFT_WINDOW:
        return np.zeros((FFT_WINDOW // 2 + 1, 0), dtype=np.float32)
    win = np.hanning(FFT_WINDOW).astype(np.float32)
    nframes = 1 + (len(sig) - FFT_WINDOW) // HOP_SIZE
    idx = np.arange(nframes)[:, None] * HOP_SIZE + np.arange(FFT_WINDOW)[None, :]
    frames = sig[idx] * win                      # [nframes, FFT]
    spec = np.abs(np.fft.rfft(frames, FFT_WINDOW, axis=1)).T   # [bins, frames]
    return 10.0 * np.log10(spec ** 2 + 1e-10)


def _find_peaks(spec):
    """2D 峰值：41×41 邻域严格局部极大值（VJVision peaks.cpp 同款，抗变速）。

    先做可分离最大滤波（先时间轴、后频率轴的滑动最大值）得到每个点的邻域最大值，
    候选 = 值等于邻域最大且 > 阈值；再排除「平顶并列」（邻域有同值）保证严格最大。
    """
    from numpy.lib.stride_tricks import sliding_window_view
    F, T = spec.shape
    N = PEAK_NEIGHBORHOOD
    W = 2 * N + 1
    if F <= 2 * N or T <= 2 * N:
        return []

    # 1) 时间轴滑动最大（每频率行）
    pad_t = np.full((F, N), -np.inf, dtype=np.float32)
    a = np.concatenate([pad_t, spec, pad_t], axis=1)          # (F, T+2N)
    tMax = sliding_window_view(a, W, axis=1).max(axis=2)      # (F, T)

    # 2) 频率轴滑动最大（每时间列）
    pad_f = np.full((N, T), -np.inf, dtype=np.float32)
    b = np.concatenate([pad_f, tMax, pad_f], axis=0)          # (F+2N, T)
    sqMax = sliding_window_view(b, W, axis=0).max(axis=2)     # (F, T)

    # 3) 候选：内部点，值 == 邻域最大 且 > 阈值
    inner = spec[N:F - N, N:T - N]
    cand = np.where((inner == sqMax[N:F - N, N:T - N]) & (inner > AMP_MIN))
    peaks = []
    for r, c in zip(*cand):
        f, t = int(r) + N, int(c) + N
        val = spec[f, t]
        nb = spec[f - N:f + N + 1, t - N:t + N + 1].copy()
        nb[N, N] = -np.inf
        if np.any(nb >= val):          # 平顶并列 → 非严格最大，跳过
            continue
        peaks.append((f, t))
    peaks.sort(key=lambda p: (p[1], p[0]))
    return peaks


def _hashes(peaks):
    out = []
    n = len(peaks)
    for i in range(n):
        f1, t1 = peaks[i]
        for j in range(1, FAN_VALUE):
            if i + j >= n:
                break
            f2, t2 = peaks[i + j]
            dt = t2 - t1
            if dt < 0 or dt > MAX_HASH_TIME_DELTA:
                continue
            key = f"{f1 // FREQ_QUANT}|{f2 // FREQ_QUANT}|{dt // DT_QUANT}"
            h = hashlib.sha1(key.encode()).hexdigest()[:HASH_LEN]
            out.append((h, t1))
    return out


def fingerprint_audio(sig, sr):
    """sig: mono float32 → [(hash, offset_frame), ...]（offset 单位 = hop 帧）"""
    if sr != SAMPLE_RATE:
        x = np.linspace(0, len(sig) - 1, max(1, int(len(sig) * SAMPLE_RATE / sr)))
        sig = np.interp(x, np.arange(len(sig)), sig).astype(np.float32)
    spec = _spectrogram(sig)
    return _hashes(_find_peaks(spec))


class FingerprintDB:
    """指纹库：add_song 建库，match 现场匹配（返回对齐票数，供音乐电池引擎消费）"""

    def __init__(self, path):
        # check_same_thread=False：指纹库在主线程创建、识别线程读取（扫描建库在后台线程写）
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA journal_mode=WAL")   # WAL：扫描写/识别读可并发
        self._pending = 0          # 批量模式下"已写未 commit"的歌曲数（见 commit_batch）
        self._scan_orig_sync = None
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS fingerprints ("
            "hash TEXT NOT NULL, song_id INTEGER NOT NULL, offset INTEGER NOT NULL)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_hash ON fingerprints(hash)")
        # ★ 2026-09-26：`add_song` 里的 `DELETE FROM fingerprints WHERE song_id=?`
        #   原先**没有 song_id 索引** ⇒ 每次都是全表扫描，库越大越慢
        #   （实测：2M 行 0.15s → 24M 行 1.1s，而扫描时**每一首歌**都要付这个成本）。
        #   加上索引后按 song_id 直接定位。`IF NOT EXISTS` 幂等：老库只在第一次补建
        #   （大库补建可能花几秒~几十秒，但发生在**扫描线程**里，不影响界面与演出）。
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_song ON fingerprints(song_id)")
        # 指纹算法版本：参数/峰值检测变了 → 旧指纹不兼容，自动清库强制重扫
        stored = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if stored != FP_SCHEMA_VERSION:
            self.conn.execute("DELETE FROM fingerprints")
            self.conn.execute(f"PRAGMA user_version={FP_SCHEMA_VERSION}")
        self.conn.commit()

    def add_song(self, song_id, sig, sr, commit=True):
        # 重扫时先删该曲旧指纹，防止库膨胀、匹配变慢（DELETE+INSERT 同一隐式事务）
        # ★ `idx_song` 索引让这条 DELETE 从"全表扫描"变成按索引定位（2026-09-26 实测 0.000s）。
        self.conn.execute("DELETE FROM fingerprints WHERE song_id=?", (song_id,))
        rows = [(h, song_id, off) for h, off in fingerprint_audio(sig, sr)]
        if rows:
            self.conn.executemany(
                "INSERT INTO fingerprints VALUES (?,?,?)", rows)
        if commit:
            self.conn.commit()
            self._pending = 0
        else:
            self._pending += 1          # 攒着，由调用方 commit_batch() 统一落盘
        return len(rows)

    def pending(self):
        """批量模式下"已写未 commit"的歌曲数"""
        return self._pending

    def commit_batch(self):
        """把批量模式下挂起的事务落盘（配合 `add_song(commit=False)` 用）。

        ★ 为什么值得：`INSERT` 3~4.5 万行本身贵，但**每首一次 commit 还要多付一次 fsync**；
        在 HDD 上单次 fsync 要等盘片（10~50ms）。攒 5~10 首再落盘能把这部分摊薄。
        """
        if self._pending:
            self.conn.commit()
            self._pending = 0

    def begin_scan(self):
        """扫描开始的批量写入模式（**用完必须调 end_scan() 恢复**）。

        `synchronous=OFF`：扫描期间不做"每次提交都落盘"的强同步 —— 扫描是**可重建**的操作
        （出问题重扫即可），换来明显更快的写入。同时把 cache 调大、临时表放内存。
        """
        try:
            self.conn.commit()                       # 先把之前的事务收干净
            self._scan_orig_sync = self.conn.execute("PRAGMA synchronous").fetchone()[0]
            self.conn.execute("PRAGMA synchronous=OFF")
            self.conn.execute("PRAGMA cache_size=-40000")     # ~40MB page cache
            self.conn.execute("PRAGMA temp_store=MEMORY")
        except Exception:                                                # noqa: BLE001
            pass

    def end_scan(self):
        """结束批量写入模式：落盘 + 把同步级别恢复回原值。"""
        try:
            self.conn.commit()
            self._pending = 0
            if self._scan_orig_sync is not None:
                self.conn.execute("PRAGMA synchronous=%d" % int(self._scan_orig_sync))
                self._scan_orig_sync = None
        except Exception:                                                # noqa: BLE001
            pass

    def match(self, sig, sr):
        """返回 (song_id, aligned_votes, offset_sec) 或 (-1, 0, 0.0)"""
        hs = fingerprint_audio(sig, sr)
        if not hs:
            return -1, 0, 0.0
        qoff_map = {}
        hashes = []
        for h, qoff in hs:
            qoff_map.setdefault(h, []).append(qoff)
            hashes.append(h)
        votes = {}
        # 批量 IN 查询（分块避开 SQLite 999 变量上限）
        for i in range(0, len(hashes), 500):
            chunk = hashes[i:i + 500]
            ph = ",".join("?" * len(chunk))
            for (h, song_id, dboff) in self.conn.execute(
                    f"SELECT hash, song_id, offset FROM fingerprints "
                    f"WHERE hash IN ({ph})", chunk):
                for qoff in qoff_map.get(h, ()):
                    key = (song_id, dboff - qoff)
                    votes[key] = votes.get(key, 0) + 1
        if not votes:
            return -1, 0, 0.0
        (song_id, delta), cnt = max(votes.items(), key=lambda kv: kv[1])
        return song_id, cnt, delta * HOP_SIZE / SAMPLE_RATE

    def close(self):
        self.conn.close()
