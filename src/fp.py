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

# 「连续性提示」窗口（单位 = hop 帧，与 match 内部的 delta 同一坐标轴）。
# 背景：强重复段落里，同一段音频在歌里出现多次，查询窗与"另一处出现"哈希对得上，
#   票数甚至高于正确位置 ⇒ 纯靠票数取全局众数会挑到错位置（实测偏差 148~738 拍，
#   ≈1490~7450 帧）。引擎若把「当前预测位置」作为 hint 传进来，就能在这堆候选里
#   优先选**靠近预测位置**的那一个，从根上避开重复段落歧义。
# 取值依据（tools/_fix3_d_hint.py 实测，20 首真实逐窗序列、逐窗投票直方图）：
#   · 正确候选距"真值"的偏差：max 10.3 帧（p99 0.6）—— 指纹量化误差；
#   · 生产侧 hint 误差：时钟跟踪 ≲1 拍(≈10.7 帧) + 一窗时滞(≈0.5s≈10.8 帧) ⇒ 合计 ≲21 帧；
#   · 可触发硬重锚的错误候选(|err|≥3.5 拍)距"真值"的下界：min 37.7 帧（p1 50.7）。
#   取 **32 帧**≈1.5s：≥3× 正确候选偏差、覆盖 hint 误差(21 帧)并留余量，且低于错误候选下界
#   (37.7 帧)。端到端实测 W∈[16,32] 全局 max|err| ≤ 3.78 拍；W≥40 升到 7.28；W≥96 反升到
#   14.55（开始放进错误候选）。窗口内无候选 ⇒ 退回全局众数 ⇒ 真实远距离 seek 仍生效。
#
# ⚠ 32 帧是**帧**坐标，换算成拍数随 BPM 变化（1 拍 = beat_len·SR/HOP 帧）：
#   128BPM ⇒ 32 帧 ≈ 2.97 拍；200BPM ⇒ 32 帧 ≈ 4.44 拍（已越过 3.5 拍错误候选下界！）；
#   90BPM ⇒ 32 帧 ≈ 1.87 拍。⇒ 固定帧窗在两端 BPM 都不合理。**受支持的定义改为按拍数**：
#   `HINT_W` 仅作为「调用方未显式给窗口」时的缺省（保持旧调用方与既有行为逐值一致）。
#   引擎按当前歌 grid 的 beat_len 用 `hint_window_frames()` 换算后传 `hint_w`。
HINT_W = 32

# 「连续性提示」窗口半宽的**受支持定义**（单位 = 拍）：跨 BPM 一致。
# 依据（第三轮 fix：正确候选偏差 ≲0.5 拍；生产侧 hint 误差 ≲1 拍跟踪 + 0.5s 时滞；
#   可触发硬重锚的错误候选下界 ≈3.5 拍）：
#   · 下界：须覆盖 hint 误差。时滞 0.5s 在帧坐标里是常数(≈10.8 帧)，折成拍数 = 0.5/bl，
#     高 BPM（bl 小）时更大；1 拍跟踪 + 0.5/bl 拍 ≈ 2.0 拍(150BPM)~2.7 拍(200BPM)。
#   · 上界：必须 < 3.5 拍（错误候选下界），否则会放进"另一处出现"的高票错候选。
#   实测扫描（tools/_gridfix4_accept.py，真实逐窗直方图 × 跨 BPM）取 **2.5 拍**：
#     既覆盖 hint 误差（含 200BPM 的 2.67 拍仍在校验范围），又低于 3.5 拍错误下界。
HINT_W_BEATS = 2.5


def hint_window_frames(beat_len, w_beats=None):
    """把「hint 窗口半宽（拍）」换算成 delta 坐标的 hop 帧数。

    delta 的单位 = 44100 下的 hop 帧。1 拍 = beat_len 秒 = beat_len·SR/HOP 帧。
    `w_beats` 缺省用 `HINT_W_BEATS`。`beat_len` 非法时回退缺省帧窗 `HINT_W`。
    """
    try:
        bl = float(beat_len)
    except (TypeError, ValueError):
        bl = 0.0
    wb = HINT_W_BEATS if w_beats is None else float(w_beats)
    if bl <= 0.0 or wb <= 0.0:
        return float(HINT_W)
    return wb * bl * SAMPLE_RATE / HOP_SIZE


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
    ap = peaks.append
    for r, c in zip(*cand):
        f, t = int(r) + N, int(c) + N
        val = spec[f, t]
        # ⚠ 原来这里 `.copy()` 一个 41×41 邻域、把中心改成 -inf 再 any(>= val)；
        #   候选峰动辄上千个 ⇒ 每次识别白拷贝几 MB。改成"切片视图 + 计数"：
        #   邻域里 >= val 的个数减去中心自己（它恒等于 val）> 0 ⇒ 存在并列。
        #   语义与原写法**完全等价**，但零拷贝。
        nb = spec[f - N:f + N + 1, t - N:t + N + 1]
        if int(np.count_nonzero(nb >= val)) > 1:
            continue
        ap((f, t))
    peaks.sort(key=lambda p: (p[1], p[0]))
    return peaks


def _hashes(peaks):
    """峰对 → [(hash, offset)]。

    ⚠⚠ **不要改这里的哈希算法**：换了就等于换指纹格式，用户库里 1421 首得整库重扫
    （`FP_SCHEMA_VERSION` 会检测到并清空 fingerprints 表）。所以下面只做
    「输出逐字节相同」的加速：
      · `f1 // FREQ_QUANT` 提到内层循环外（原来每对峰都重算一次）；
      · `b"%d|%d|%d" % (...)` 直接产 bytes，省掉 str→encode 的一次分配；
      · 常量与 append 提成局部变量（内层循环每窗口要跑 ~1700 次）。
    """
    out = []
    ap = out.append
    n = len(peaks)
    fq, dq, hl = FREQ_QUANT, DT_QUANT, HASH_LEN
    fan, dmax = FAN_VALUE, MAX_HASH_TIME_DELTA
    for i in range(n):
        f1, t1 = peaks[i]
        q1 = f1 // fq
        for j in range(1, fan):
            if i + j >= n:
                break
            f2, t2 = peaks[i + j]
            dt = t2 - t1
            if dt < 0 or dt > dmax:
                continue
            h = hashlib.sha1(b"%d|%d|%d" % (q1, f2 // fq, dt // dq)).hexdigest()[:hl]
            ap((h, t1))
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

    @staticmethod
    def _pick(votes, hint_delta=None, hint_w=None):
        """从 (song_id, delta)->votes 的投票表里挑出结果，返回 ((sid, delta), votes) 或 None。

        · hint_delta 为 None：与旧实现**逐值一致** —— `max(items, key=votes)`（同分取先出现的）。
        · hint_delta 给定：先在 |delta - hint_delta| <= hint_w 的候选里取最高票；该窗口内
          **没有候选时**才退回全局众数（保证真实远距离 seek 仍有结果）。
          ⚠ 这里**只按"窗口内是否命中有候选"选择，不判断 hint 本身是否正确**：
          一旦窗口内存在候选（哪怕是"另一处出现"的同歌候选），就取它 —— 即 hint 能给错时
          会把它带到错位置。这是**有意的**：没有可靠的"hint 对不对"判据（QA 实测"投票比"
          区分不了"hint 正确但弱"与"hint 错误"，两者全局票都更高）。因此**调用方必须**
          只在坐标系可信时给 hint（见 engine._beat_pos_grid：未锁定不发布；重锚后有界暂停）。
          hint_w 缺省用 `HINT_W`（帧）—— 引擎按该歌 beat_len 换算后传入，跨 BPM 一致。
        delta 单位 = hop 帧；窗内同分同样取先出现者（dict 插入序确定）。
        """
        if not votes:
            return None
        if hint_delta is not None:
            w = HINT_W if hint_w is None else float(hint_w)
            if w < 0.0:
                w = 0.0
            lo = hint_delta - w
            hi = hint_delta + w
            best = None
            for key, cnt in votes.items():
                if lo <= key[1] <= hi and (best is None or cnt > best[1]):
                    best = (key, cnt)
            if best is not None:
                return best
        return max(votes.items(), key=lambda kv: kv[1])

    def match(self, sig, sr, hint_delta=None, hint_w=None):
        """返回 (song_id, aligned_votes, offset_sec) 或 (-1, 0, 0.0)

        hint_delta: 可选。预测的「对齐帧偏移」（单位 = HOP 帧，与内部 delta 同轴）。
          None（默认）⇒ 与改动前**逐值一致**（全局众数）；给定值 ⇒ 优先选预测位置
          附近（±hint_w 帧）的最高票候选，避开强重复段落的歧义错匹配；**窗内无候选**
          时才退回全局众数。
        hint_w: 可选。hint 窗口半宽（帧）。None ⇒ 用缺省 `HINT_W`（保持旧调用方逐值一致）。
          引擎按该歌 grid 的 beat_len 用 `hint_window_frames()` 换算后传入，使窗口
          **按拍数**定义、跨 BPM 一致（固定帧窗在 90~200BPM 间对应的拍数差 2 倍以上）。
          调用点见 engine._beat_pos_grid → audio_engine._recognize_loop。"""
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
        picked = self._pick(votes, hint_delta, hint_w)
        if picked is None:
            return -1, 0, 0.0
        (song_id, delta), cnt = picked
        return song_id, cnt, delta * HOP_SIZE / SAMPLE_RATE

    def close(self):
        self.conn.close()
