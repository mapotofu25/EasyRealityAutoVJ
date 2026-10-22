# -*- coding: utf-8 -*-
"""待办清单 高-1 / 高-2：识别链路的重复计算与多余拷贝。

① fp._find_peaks 每个候选峰 `.copy()` 一个 41×41 邻域，只为把中心置 -inf。
   改成"切片视图 + 计数"，语义完全等价，不再拷贝（候选峰动辄上千个）。
② fp._hashes 每对峰都 f-string 拼串 + `.encode()` + sha1，且 `f1 // FREQ_QUANT`
   在内层循环里重复算。
   ⚠ **刻意不改哈希算法本身**：换了哈希就等于换指纹格式，`fingerprints.db` 得整库重扫
   （用户库里 1421 首）。这里只做"输出逐字节相同"的加速：量化值提出内层循环、
   用 bytes 格式化省掉 encode、常量提局部变量。
③ audio_engine._analyze_chunk 每块（≈46 次/秒）都重算 rfftfreq / 频带掩码 /
   geomspace 边界 / `sr // BLOCK`。采样率固定 ⇒ 只算一次（按 sr 缓存）。
"""
import io
import os
import sys

ROOT = r"P:\AutoVJ"
PAT = []


def rep(rel, old, new, cnt=1):
    PAT.append((rel, old, new, cnt))


# ============================== fp.py ①峰值
rep("src/fp.py",
    """    peaks = []
    for r, c in zip(*cand):
        f, t = int(r) + N, int(c) + N
        val = spec[f, t]
        nb = spec[f - N:f + N + 1, t - N:t + N + 1].copy()
        nb[N, N] = -np.inf
        if np.any(nb >= val):          # 平顶并列 → 非严格最大，跳过
            continue
        peaks.append((f, t))
""",
    """    peaks = []
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
""")

# ============================== fp.py ②哈希
rep("src/fp.py",
    """def _hashes(peaks):
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
""",
    """def _hashes(peaks):
    \"\"\"峰对 → [(hash, offset)]。

    ⚠⚠ **不要改这里的哈希算法**：换了就等于换指纹格式，用户库里 1421 首得整库重扫
    （`FP_SCHEMA_VERSION` 会检测到并清空 fingerprints 表）。所以下面只做
    「输出逐字节相同」的加速：
      · `f1 // FREQ_QUANT` 提到内层循环外（原来每对峰都重算一次）；
      · `b"%d|%d|%d" % (...)` 直接产 bytes，省掉 str→encode 的一次分配；
      · 常量与 append 提成局部变量（内层循环每窗口要跑 ~1700 次）。
    \"\"\"
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
""")

# ============================== audio_engine ③每块重算
rep("src/audio_engine.py",
    """def _band_energy(spec, freqs, lo, hi):
    m = (freqs >= lo) & (freqs < hi)
    if not m.any():
        return 0.0
    return float(np.mean(spec[m]))
""",
    """def _band_energy(spec, freqs, lo, hi):
    \"\"\"频带平均能量（保留原接口，供外部/测试用）。

    ⚠ 热路径**不要**用它 —— 每次都要建布尔掩码再索引。`_Analyzer` 内部改用
    `_band_mean(spec, (i0, i1))`（切片求平均，索引由 `_fft_plan()` 预算好）。
    \"\"\"
    m = (freqs >= lo) & (freqs < hi)
    if not m.any():
        return 0.0
    return float(np.mean(spec[m]))


def _band_mean(spec, rg):
    \"\"\"按预算好的频带索引区间求平均（空区间返回 0.0，与 _band_energy 一致）。

    RFFT 的频率轴是单调递增的 ⇒ `freqs >= lo & freqs < hi` 一定是**连续区间**，
    可以直接用切片，不必每次建掩码。
    \"\"\"
    i0, i1 = rg
    if i1 <= i0:
        return 0.0
    return float(np.mean(spec[i0:i1]))
""")

rep("src/audio_engine.py",
    """        # ---- FFT ----
        buf = np.zeros(2048, dtype=np.float32)
        n = min(len(sig), 2048)
        buf[:n] = sig[:n]
        spec = np.abs(np.fft.rfft(buf * self.win)) / 1024.0
        freqs = np.fft.rfftfreq(2048, 1.0 / self.sr)
        bass = _band_energy(spec, freqs, 20, 160)
        mid = _band_energy(spec, freqs, 160, 2000)
        high = _band_energy(spec, freqs, 2000, 12000)
""",
    """        # ---- FFT ----
        # ★ 频带边界 / 24 段频谱的边界 / BPM 判定周期**只算一次**（见 _fft_plan）：
        #   原来每块（≈46 次/秒）都要 rfftfreq + geomspace + 三组布尔掩码。
        _freqs, rg_bass, rg_mid, rg_high, edges, bpm_period = self._fft_plan()
        buf = np.zeros(2048, dtype=np.float32)
        n = min(len(sig), 2048)
        buf[:n] = sig[:n]
        spec = np.abs(np.fft.rfft(buf * self.win)) / 1024.0
        bass = _band_mean(spec, rg_bass)
        mid = _band_mean(spec, rg_mid)
        high = _band_mean(spec, rg_high)
""")

rep("src/audio_engine.py",
    """        # ---- 24 段对数频谱（可视化特效数据源：径向频谱/音频反应变形）----
        edges = np.geomspace(2, len(spec) - 1, 25).astype(int)
        edges = np.unique(edges)
        band_vals""",
    """        # ---- 24 段对数频谱（可视化特效数据源：径向频谱/音频反应变形）----
        band_vals""")

rep("src/audio_engine.py",
    """        if self.flux_i % int(self.sr // BLOCK) == 0:
            fps = self.sr / BLOCK""",
    """        if self.flux_i % bpm_period == 0:
            fps = self.sr / BLOCK""")

# ---- _Analyzer 里加 _fft_plan（懒算 + 按 sr 缓存）
rep("src/audio_engine.py",
    """    def feed(self, data, st):""",
    """    def _fft_plan(self):
        \"\"\"FFT 相关的预算表：(freqs, 低频区间, 中频区间, 高频区间, 24 段边界, BPM 周期)。

        采样率在一次采集里是固定的 ⇒ 只算一次，按 sr 缓存（换设备/换采样率自动重算）。
        频带区间用 searchsorted 求**连续下标**：RFFT 频率轴单调递增，所以
        「freqs >= lo 且 < hi」等价于一个切片，比每次建布尔掩码快得多。
        \"\"\"
        got = getattr(self, "_fft_plan_cache", None)
        if got is not None and got[0] == self.sr:
            return got[1]
        freqs = np.fft.rfftfreq(2048, 1.0 / self.sr)

        def _rg(lo, hi):
            return (int(np.searchsorted(freqs, lo, "left")),
                    int(np.searchsorted(freqs, hi, "left")))

        edges = np.unique(np.geomspace(2, len(freqs) - 1, 25).astype(int))
        plan = (freqs, _rg(20, 160), _rg(160, 2000), _rg(2000, 12000),
                edges, max(1, int(self.sr // BLOCK)))
        self._fft_plan_cache = (self.sr, plan)
        return plan

    def feed(self, data, st):""")

def main():
    for i, (rel, old, new, cnt) in enumerate(PAT, 1):
        p = os.path.join(ROOT, rel)
        s = io.open(p, encoding="utf-8").read()
        n = s.count(old)
        if n != cnt:
            print("✗ 第 %d 处（%s）命中 %d 次（应为 %d）" % (i, rel, n, cnt))
            print("   锚点首行：", old.splitlines()[0][:90])
            return 1
    # 全部锚点 OK 再写
    for rel, old, new, cnt in PAT:
        if old == new:
            continue                       # 占位项，跳过
        p = os.path.join(ROOT, rel)
        s = io.open(p, encoding="utf-8").read()
        io.open(p, "w", encoding="utf-8", newline="\n").write(s.replace(old, new))
        print("✓", rel, old.splitlines()[0][:60])
    import py_compile
    for f in ("src/fp.py", "src/audio_engine.py"):
        py_compile.compile(os.path.join(ROOT, f), doraise=True)
    print("语法 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
