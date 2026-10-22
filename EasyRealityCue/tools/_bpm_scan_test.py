# -*- coding: utf-8 -*-
"""★ BPM 自动识别验证（V1 真值对照 / V2 先验反向 / V3 VDJ 优先 / V4 惰性缓存 / V5 端到端）。

跑法：
    P:\\ERAVJ\\EasyRealityCue\\venv\\Scripts\\python.exe -u tools\\_bpm_scan_test.py
    P:\\ERAVJ\\EasyRealityCue\\venv\\Scripts\\python.exe -u tools\\_bpm_scan_test.py --shots

V1  演出端真实 `%LOCALAPPDATA%\\AutoVJ\\config.json` 的 `music_meta[path]["grid"]["bpm"]` 当真值，
    抽 12 首（含 ≥1 首 48k），每首跑两遍：
      · **纯音频扫描**（`vdj=None`）⇒ 报命中率；
      · **VDJ 优先**（真 VDJ 索引）⇒ 报命中率（应与演出端 `src="vdj"` 同源，接近满分）。
    另报「识别值与演出端函数是否逐位一致」（纯音频那一列），保证移植保真。
V2  反向复现 `pick_bpm` 的先验：合成 120 BPM click 正常 120；运行期把 `BPM_PRIOR` 改 (40,80)
    ⇒ 必须出现半速（≈真值/2）。源码不留临时改动。
V3  VDJ 优先的**三个方向**：
      · `vdj=None`         ⇒ `source=="audio"`；
      · 真 VDJ 且有记录     ⇒ `source=="vdj"` 且 `bpm` **等于 VDJ 值**（取一首两者差异 >1 BPM 的歌）；
      · 不存在的 xml / 坏 xml ⇒ 退回 `source=="audio"`、不抛。
V4  惰性 + 缓存：`get_vdj_index()` 第二次调用**不重新解析**（loads 计数 + 耗时数字）。
V5  端到端：`IdentityScanWorker.run()` 同步跑 ⇒ done 里带 BPM 与来源；window 落库；
    bpm=0 写入 / 手填不覆盖 / 手动重算覆盖。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import numpy as np                                                # noqa: E402

from ercue.model import bpm_core                                  # noqa: E402

RESULTS = []
SHOT_DIR = os.path.join(_HERE, "_cueshots_ercue")

HIT_DELTA = 0.5
MAX_SEC_PER_TRACK = 2.5

# ★ 截图必须用真 Windows 平台渲染（offscreen 没有 CJK 字体 ⇒ 全是豆腐块）
_SHOTS_ONLY = "--shots-only" in sys.argv
if not _SHOTS_ONLY:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["ERCUE_NO_AUTOSAVE"] = "1"


def _chk(tag, cond, detail=""):
    ok = bool(cond)
    RESULTS.append((tag, ok))
    print("   [%s] %s %s" % ("PASS" if ok else "FAIL", tag, detail))
    return ok


# ==========================================================================
# 素材
# ==========================================================================
def make_click_wav(path, bpm=128.0, sec=45.0, sr=44100, seed=20261018):
    """合成"像歌一样"的 click track → wav（16 位 PCM）。"""
    import wave
    rng = np.random.default_rng(seed)
    n = int(sr * sec)
    y = np.zeros(n, dtype=np.float64)
    period = 60.0 / float(bpm)
    ln = int(0.03 * sr)
    env = np.exp(-np.linspace(0.0, 8.0, ln))
    t_tone = np.arange(int(0.12 * sr)) / float(sr)
    k = 0
    while True:
        pos = int(round(k * period * sr))
        if pos >= n:
            break
        end = min(n, pos + ln)
        y[pos:end] += env[:end - pos] * (0.8 if k % 4 == 0 else 0.5)
        f0 = float(rng.uniform(200.0, 2400.0))
        seg = min(n - pos, t_tone.size)
        if seg > 0:
            y[pos:pos + seg] += 0.18 * np.sin(2 * np.pi * f0 * t_tone[:seg]) \
                * np.exp(-np.linspace(0.0, 5.0, t_tone.size))[:seg]
        k += 1
    y += rng.standard_normal(n) * 0.001
    pcm = np.round(np.clip(y, -1.0, 1.0) * 32767.0).astype(np.int16)
    with wave.open(path, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes(pcm.tobytes())
    return path


def _av_rate(path):
    """PyAV 只读流头拿采样率（不解码，很快）。"""
    try:
        import av
        with av.open(path, metadata_errors="replace") as c:
            st = next((s for s in c.streams if s.type == "audio"), None)
            return int(getattr(st, "rate", 0) or 0) if st is not None else 0
    except Exception:                                            # noqa: BLE001
        return 0


def pick_real_tracks(want=12):
    """从演出端 config 的 music_meta 选 `want` 首（含 ≥1 首 48k）。

    返回 `[(path, bpm_truth, sr, dur)]`。
    """
    cfg = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "config.json")
    if not os.path.isfile(cfg):
        print("  ! 找不到演出端 config：%s" % cfg)
        return []
    try:
        with open(cfg, encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:                                       # noqa: BLE001
        print("  ! 读不了演出端 config（%s: %s）" % (type(e).__name__, e))
        return []
    mm = d.get("music_meta") or {}
    rows = []
    for p, meta in mm.items():
        if not isinstance(meta, dict):
            continue
        g = meta.get("grid") or {}
        bpm = float(g.get("bpm") or 0.0)
        if bpm <= 0 or not os.path.isfile(p):
            continue
        rows.append((p, bpm, _av_rate(p), float(g.get("dur") or 0.0)))
    rows.sort(key=lambda r: r[0].lower())
    if not rows:
        return []
    hi = [r for r in rows if r[2] >= 48000]
    lo = [r for r in rows if r[2] < 48000]
    out = hi[:2] + lo[:max(0, want - 2)]
    if len(out) < want:
        out += [r for r in hi[2:] + lo[max(0, want - 2):] if r not in out][:want - len(out)]
    return out[:want]


def _mono_of(path):
    """解码 → `(mono float32 原生率, sr)`；失败 `(None, 0)`。"""
    from ercue.audio.player import AudioTrack
    tr = AudioTrack(path, samplerate=None)
    if not tr.ok:
        return None, 0
    buf = tr.samples()
    mono = (buf.mean(axis=0) if buf.shape[0] > 1 else buf[0]).astype(np.float32)
    return mono, int(tr.samplerate or 44100)


def _autovj_reference(mono, sr, maxsec=180.0):
    """演出端自己的函数在同一段 PCM 上算 BPM（移植保真度的黄金标准）。"""
    sys.path.insert(0, "P:/ERAVJ/AutoVJ/src")
    import beatgrid as bg                                      # noqa: E402
    x = np.ascontiguousarray(mono, dtype=np.float32)
    n = int(maxsec * sr)
    if 0 < n < x.size:
        x = x[:n]
    x, s2 = bg.resample(x, sr, bg.SR_TARGET)
    f = bg.features(x, s2)
    env = bg.onset_env(f, "mix")
    peaks = bg.scan_bpm(f, env)
    bpm, score = bg.pick_bpm(peaks)
    return float(bpm), float(score), peaks[:4]


# ==========================================================================
# V1：真值对照（纯音频 vs VDJ 优先）+ 与演出端函数逐位一致
# ==========================================================================
def v1_truth_compare():
    print("\n=== V1 真值对照（12 首；纯音频 vs VDJ 优先）===")
    rows = pick_real_tracks(12)
    if not rows:
        _chk("V1 素材可用", False, "演出端 config 或曲库不可用")
        return
    idx = bpm_core.get_vdj_index()
    st = bpm_core.vdj_stats()
    print("  VDJ 索引：%s（条目 %d / 已分析 %d；加载 %d 次 %.3fs）"
          % (st["path"], st["entries"], st["analyzed"], st["loads"], st["last_cost"]))
    hit_a = hit_v = exact = 0
    slow_a = 0.0
    worst = (0.0, "")
    print("  %-44s %8s %8s %6s %8s %6s %6s %7s"
          % ("曲目", "真值", "音频", "Δ", "VDJ优先", "Δ", "来源", "耗时s"))
    for p, truth, sr_hint, dur in rows:
        mono, sr = _mono_of(p)
        if mono is None:
            print("  %-44s  解码失败" % os.path.basename(p)[:42])
            continue
        t0 = time.perf_counter()
        ba, _sa, pa, srca = bpm_core.detect_bpm(mono, sr, maxsec=180.0, vdj=None)
        cost = time.perf_counter() - t0
        slow_a = max(slow_a, cost)
        bv, _sv, _pv, srcv = bpm_core.detect_bpm(mono, sr, maxsec=180.0,
                                                 vdj=idx, path=p, dur=dur)
        try:
            ref, _rs, _rp = _autovj_reference(mono, sr, maxsec=180.0)
            exact += 1 if abs(ba - ref) < 1e-9 else 0
        except Exception:                                        # noqa: BLE001
            ref = float("nan")
        da, dv = abs(ba - truth), abs(bv - truth)
        hit_a += 1 if da <= HIT_DELTA else 0
        hit_v += 1 if dv <= HIT_DELTA else 0
        if da > worst[0]:
            worst = (da, os.path.basename(p))
        print("  %-44s %8.2f %8.2f %6.2f %8.2f %6.2f %6s %7.2f"
              % (os.path.basename(p)[:42], truth, ba, da, bv, dv, srcv or "-", cost))
    n = len(rows)
    print("  ⇒ 纯音频扫描命中 %d/%d；VDJ 优先命中 %d/%d；最大误差 %.2f（%s）；"
          "纯音频单首最慢 %.2fs" % (hit_a, n, hit_v, n, worst[0], worst[1], slow_a))
    _chk("V1 纯音频识别与演出端函数逐位一致", exact == n, "%d/%d" % (exact, n))
    _chk("V1 VDJ 优先命中率 ≥11/12", hit_v >= 11, "%d/%d" % (hit_v, n))
    _chk("V1 VDJ 优先优于纯音频", hit_v >= hit_a, "VDJ %d vs 音频 %d" % (hit_v, hit_a))
    _chk("V1 纯音频单首 ≤%.1fs" % MAX_SEC_PER_TRACK, slow_a <= MAX_SEC_PER_TRACK,
         "最慢 %.2fs" % slow_a)
    _chk("V1 覆盖到 48k 素材", any(r[2] >= 48000 for r in rows),
         "48k 首数=%d" % sum(1 for r in rows if r[2] >= 48000))


# ==========================================================================
# V2：先验反向复现
# ==========================================================================
def v2_prior_reverse(tmpdir):
    print("\n=== V2 反向复现：改掉 BPM_PRIOR 是否会让结果变半速 ===")
    wav = make_click_wav(os.path.join(tmpdir, "click120.wav"), bpm=120.0, sec=45.0)
    mono, sr = _mono_of(wav)
    b0, s0, pk0, src0 = bpm_core.detect_bpm(mono, sr, maxsec=60.0)
    _chk("V2 合成 120 BPM 被正确识别", abs(b0 - 120.0) <= 0.5 and src0 == "audio",
         "实得 %.2f（source=%s score=%.2f）" % (b0, src0, s0))
    cases = [("合成 120", mono, sr, 120.0)]
    real = [r for r in pick_real_tracks(12) if r[1] >= 120.0][:1]
    if real:
        p, truth, _sr_hint, _dur = real[0]
        m2, s2 = _mono_of(p)
        if m2 is not None:
            cases.append(("真实 %s" % os.path.basename(p)[:26], m2, s2, truth))
    old_prior = bpm_core.BPM_PRIOR
    half = 0
    try:
        bpm_core.BPM_PRIOR = (40.0, 80.0)                       # 运行期垫片（源码不留改动）
        for name, m, s_, truth in cases:
            bb, _ss, _pp, _sr = bpm_core.detect_bpm(m, s_, maxsec=180.0)
            ok_half = abs(bb - truth / 2.0) <= 0.5
            half += 1 if ok_half else 0
            print("    %-32s 真值 %6.2f → 先验[40,80] 得 %6.2f（半速 %.2f，%s）"
                  % (name, truth, bb, truth / 2.0, "半速 ✓" if ok_half else "非半速"))
    finally:
        bpm_core.BPM_PRIOR = old_prior
    _chk("V2 先验改动后至少 1 例掉到半速", half >= 1, "半速例数=%d/%d" % (half, len(cases)))
    b_again, _s, _p, _src = bpm_core.detect_bpm(mono, sr, maxsec=60.0)
    _chk("V2 先验还原后结果不变", abs(b_again - b0) <= 1e-9, "%.2f == %.2f" % (b_again, b0))


# ==========================================================================
# V3：VDJ 优先的三个方向
# ==========================================================================
def v3_vdj_priority(tmpdir):
    print("\n=== V3 VDJ 优先（命中 / 未命中 / 库损坏 三条路）===")
    idx = bpm_core.get_vdj_index()
    _chk("V3 本机找到 VirtualDJ 库", bool(idx), "%s（%d 条 / 已分析 %d）"
         % (bpm_core.vdj_db_path(), idx.n, idx.analyzed))

    # 找一首「音频扫描 vs VDJ 值」差异明显的歌，才能证明用的是 VDJ 值
    chosen = None
    for p, truth, _sr, dur in pick_real_tracks(12):
        mono, sr = _mono_of(p)
        if mono is None:
            continue
        ba, _s, _pk, _src = bpm_core.detect_bpm(mono, sr, maxsec=180.0, vdj=None)
        bv, _sv, _pv, srcv = bpm_core.detect_bpm(mono, sr, maxsec=180.0,
                                                vdj=idx, path=p, dur=dur)
        if srcv == "vdj" and abs(ba - bv) > 1.0:
            chosen = (p, ba, bv, dur)
            break
    if chosen is None:
        _chk("V3 找到差异明显的样本", False, "12 首里没有 audio≠vdj 的样本")
        return
    p, ba, bv, dur = chosen
    print("  样本：%s" % os.path.basename(p))
    print("    音频扫描 %.2f ／ VDJ（优先） %.2f" % (ba, bv))
    mono, sr = _mono_of(p)
    # ① 明确不用 VDJ（同一份 mono）⇒ 必须走音频扫描
    b_a, _s1, k1, src_a = bpm_core.detect_bpm(mono, sr, vdj=None, path=p, dur=dur)
    _chk("V3 vdj=None ⇒ source=='audio'", src_a == "audio" and abs(b_a - ba) < 1e-9,
         "source=%r bpm=%.2f peaks=%d" % (src_a, b_a, len(k1 or [])))
    # ② 真 VDJ ⇒ 用 VDJ 的值，且不跑扫描
    b_v, _s2, pk2, src_v = bpm_core.detect_bpm(mono, sr, vdj=idx, path=p, dur=dur)
    e = bpm_core.vdj_lookup(idx, p, dur)
    vdj_val = float(e["bpm"]) if e else -1.0
    _chk("V3 真 VDJ ⇒ source=='vdj' 且 bpm 等于 VDJ 值",
         src_v == "vdj" and abs(b_v - vdj_val) < 1e-9 and abs(b_v - ba) > 1.0,
         "source=%r bpm=%.4f（VDJ 值 %.4f；音频值 %.2f）" % (src_v, b_v, vdj_val, ba))
    _chk("V3 VDJ 命中时 peaks 为空（不跑扫描）", pk2 == [], "peaks=%r" % (pk2,))

    # 失败降级：不存在的 xml / 坏 xml
    bad1 = bpm_core.load_vdj(os.path.join(tmpdir, "__no_such_vdj__.xml"))
    _chk("V3 不存在的 xml ⇒ 空索引、不抛", isinstance(bad1, bpm_core.VdjIndex) and not bad1,
         "n=%d" % bad1.n)
    broken = os.path.join(tmpdir, "broken.xml")
    with open(broken, "wb") as f:
        f.write(b"<DJ_PLAYLISTS><Song FilePath='truncated")     # 故意截断
    bad2 = bpm_core.load_vdj(broken)
    _chk("V3 坏 xml ⇒ 空索引、不抛", isinstance(bad2, bpm_core.VdjIndex) and not bad2,
         "n=%d" % bad2.n)
    b3, _s3, _k3, src3 = bpm_core.detect_bpm(mono, sr, vdj=bad2, path=p, dur=dur)
    _chk("V3 库不可用 ⇒ 自动退回音频扫描", src3 == "audio" and abs(b3 - ba) < 1e-9,
         "source=%r bpm=%.2f" % (src3, b3))


# ==========================================================================
# V4：惰性 + 缓存
# ==========================================================================
def v4_lazy_cache():
    print("\n=== V4 惰性加载 + 缓存（不重复解析 2.6MB xml）===")
    bpm_core.reset_vdj_cache()
    st0 = bpm_core.vdj_stats()
    _chk("V4 import 后未解析（reset 后 loads=0、无索引）",
         st0["loads"] == 0 and st0["entries"] == 0, "%r" % (st0,))
    t0 = time.perf_counter()
    i1 = bpm_core.get_vdj_index()
    c1 = time.perf_counter() - t0
    t1 = time.perf_counter()
    i2 = bpm_core.get_vdj_index()
    c2 = time.perf_counter() - t1
    st = bpm_core.vdj_stats()
    print("  第一次 %.3fs（条目 %d）／第二次 %.6fs（同一对象=%r）；loads=%d"
          % (c1, i1.n, c2, i2 is i1, st["loads"]))
    _chk("V4 第二次调用不重复解析", st["loads"] == 1 and i2 is i1 and c2 < max(0.01, c1 * 0.2),
         "loads=%d 第二次 %.6fs（第一次 %.3fs）" % (st["loads"], c2, c1))


# ==========================================================================
# V5：端到端
# ==========================================================================
def v5_end_to_end(tmpdir):
    print("\n=== V5 端到端：worker.run() → done(含来源) → window 落库 ===")
    from PySide6.QtWidgets import QApplication
    from ercue.ui.identity_scan import IdentityScanWorker

    app = QApplication.instance() or QApplication([])          # noqa: F841
    wav = make_click_wav(os.path.join(tmpdir, "song128.wav"), bpm=128.0, sec=40.0)

    got = {}

    def _on_done(i, md5, sig, bpm, err, info):
        got.update(idx=i, md5=md5, sig=sig, bpm=float(bpm), err=err, info=dict(info or {}))

    w = IdentityScanWorker(wav, 0)
    w.done.connect(_on_done)
    w.run()
    _chk("V5 worker 回执带 BPM>0 且带来源", got.get("bpm", 0) > 0 and "source" in got.get("info", {}),
         "bpm=%.2f source=%r md5=%s sig=%s err=%r"
         % (got.get("bpm", 0.0), got.get("info", {}).get("source"),
            (got.get("md5") or "")[:8], got.get("sig") or "（无）", got.get("err")))
    _chk("V5 worker 同时算出了 md5 与指纹", bool(got.get("md5")) and bool(got.get("sig")), "")

    from ercue.ui.window import MainWindow
    win = MainWindow()
    win._interactive = False
    win.thumbs_enabled = False
    try:
        win.new_sheet("bpm-e2e")
        idx = win.add_track("BPM 端到端", "测试", 40.0, 0.0)
        win.session.track_audio[idx] = wav
        win._ti = max(0, int(idx))
        win._id_scan_key = "%d|%s" % (win._track_index(), os.path.abspath(wav))
        win._on_id_done(idx, got["md5"], got["sig"], got["bpm"], "", got["info"])
        written = float(win._track().identity.bpm or 0.0)
        _chk("V5 bpm=0 的曲目被写入识别值", abs(written - got["bpm"]) < 1e-6,
             "identity.bpm=%.2f" % written)

        win.new_sheet("bpm-keep")
        idx2 = win.add_track("手填曲目", "测试", 40.0, 128.0)
        win.session.track_audio[idx2] = wav
        win._ti = max(0, int(idx2))
        win._id_scan_key = "%d|%s" % (win._track_index(), os.path.abspath(wav))
        win._on_id_done(idx2, "", "", 133.5, "", {"source": "audio", "peaks": [], "score": 3.0})
        kept = float(win._track().identity.bpm or 0.0)
        _chk("V5 手填 128 不被自动扫描覆盖", abs(kept - 128.0) < 1e-9, "identity.bpm=%.2f" % kept)

        win._bpm_force = True
        win._on_id_done(idx2, "", "", 133.5, "", {"source": "audio", "peaks": [], "score": 3.0})
        forced = float(win._track().identity.bpm or 0.0)
        _chk("V5 手动重算覆盖已有值", abs(forced - 133.5) < 1e-9, "identity.bpm=%.2f" % forced)

        # 三种来源的状态行文案
        cases = [("vdj", 150.0, {"source": "vdj"}),
                 ("audio", 145.0, {"source": "audio", "score": 3.4,
                                   "peaks": [(145.0, 3.4), (160.0, 3.0), (80.0, 1.9)]}),
                 ("", 0.0, {"source": ""})]
        texts = []
        for tag, bpmv, info in cases:
            win._track().identity.bpm = 0.0
            win._on_id_done(idx2, "", "", bpmv, "", info)
            texts.append(win._id_status)
        print("  状态行文案：\n    ① %s\n    ② %s\n    ③ %s" % tuple(texts))
        _chk("V5 状态行写明来源（VirtualDJ）", "来自 VirtualDJ" in texts[0], texts[0])
        _chk("V5 状态行写明来源（音频分析，建议核对）", "音频分析，建议核对" in texts[1], texts[1])
        _chk("V5 失败文案中性（不吓人、说明可留空）", "可以留空" in texts[2], texts[2])
        tip = win.track_panel.sp_bpm.toolTip()
        win._on_id_done(idx2, "", "", 145.0, "", cases[1][2])
        tip = win.track_panel.sp_bpm.toolTip()
        _chk("V5 tooltip 列出备选候选", "备选：" in tip and "160.00" in tip,
             tip.splitlines()[-1] if tip else "(空)")
    finally:
        try:
            win.shutdown()
        except Exception:                                        # noqa: BLE001
            pass
    return wav


# ==========================================================================
# 截图（真平台）
# ==========================================================================
def shots():
    """三种 source 的状态行 + BPM 控件截图。返回是否全部通过。"""
    print("\n=== 截图（真窗口 / 真平台渲染）===")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from ercue.ui.window import MainWindow
    os.makedirs(SHOT_DIR, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    print("  平台：%s" % app.platformName())
    win = MainWindow()
    win._interactive = False
    win.thumbs_enabled = False
    ok = True
    try:
        win.new_sheet("BPM 来源截图")
        idx = win.add_track("Cosmic String", "Rolling Contact", 372.4, 0.0)
        win._ti = max(0, int(idx))
        # 与 `_on_id_done` 的过期判定用**同一套** key 算法（`os.path.abspath` 对标），
        # 否则回执会被当成过期直接丢掉，状态行永远是空的。
        win._id_scan_key = "%d|%s" % (win._track_index(),
                                      os.path.abspath(win._audio_path() or ""))
        win._refresh_all()
        win.resize(1500, 950)
        win.show()
        app.processEvents()
        cases = [
            ("6_vdj", 150.0, {"source": "vdj"}, "来自 VirtualDJ"),
            ("7_audio", 145.0, {"source": "audio", "score": 3.4,
                                "peaks": [(145.0, 3.4), (160.0, 3.0), (80.0, 1.9)]},
             "音频分析，建议核对"),
            ("8_fail", 0.0, {"source": ""}, "可以留空"),
        ]
        # ① BPM 未设状态（specialValueText）+ ② 识别中 + ③ 识别后 的值 + ⑤ 说明文字
        sp = win.track_panel.sp_bpm
        win._id_status = ""
        win.track_panel.set_bpm_hint("")
        win._track().identity.bpm = 0.0
        win._refresh_track_panel()
        app.processEvents()
        p1 = os.path.join(SHOT_DIR, "bpm_1_unset.png")
        win.track_panel.grab().save(p1)
        txt = sp.text()
        need = sp.lineEdit().fontMetrics().horizontalAdvance(txt)
        have = sp.lineEdit().width()
        ok &= _chk("截图 1_unset：未设状态文案完整显示", txt == "未设 BPM（可选）" and need <= have,
                   "文本=%r 需要 %dpx / 可用 %dpx" % (txt, need, have))
        print("  已存：%s（%r）" % (p1, txt))
        win.track_panel.set_bpm_detecting(True)
        app.processEvents()
        p2 = os.path.join(SHOT_DIR, "bpm_2_detecting.png")
        win.track_panel.grab().save(p2)
        print("  已存：%s（按钮=%r）" % (p2, win.track_panel.btn_bpm.text()))
        win.track_panel.set_bpm_detecting(False)
        p5 = os.path.join(SHOT_DIR, "bpm_5_how_label.png")
        win.track_panel.lbl_how.grab().save(p5)
        print("  已存：%s（说明文字）" % p5)
        for tag, bpmv, info, want in cases:
            win._track().identity.bpm = 0.0
            win._on_id_done(idx, "", "", bpmv, "", info)
            app.processEvents()
            path = os.path.join(SHOT_DIR, "bpm_%s.png" % tag)
            win.track_panel.grab().save(path)
            print("  已存：%s（状态行：%s）" % (path, win._id_status))
            ok &= _chk("截图 %s：状态行含「%s」" % (tag, want), want in win._id_status,
                       win._id_status)
        # 说明文字仍是富文本、无字面星号
        lbl = win.track_panel.lbl_how
        ok &= _chk("说明文字无字面星号且是富文本",
                   ("*" not in lbl.text()) and lbl.textFormat() == Qt.TextFormat.RichText, "")
    finally:
        try:
            win.shutdown()
        except Exception:                                        # noqa: BLE001
            pass
    return ok


def _shots_via_subprocess():
    import subprocess
    env = dict(os.environ)
    env.pop("QT_QPA_PLATFORM", None)
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [sys.executable, "-u", os.path.abspath(__file__), "--shots-only"]
    print("  （截图走子进程：真 Windows 平台）")
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env, timeout=600)
    print(((r.stdout or "") + (r.stderr or "")).rstrip())
    return r.returncode == 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--shots-only" in argv:
        return 0 if shots() else 1
    print("=" * 78)
    print("BPM 自动识别验证（bpm_core = 移植自 AutoVJ/src/beatgrid.py；VDJ 优先）")
    print("=" * 78)
    t_all = time.perf_counter()
    tmpdir = tempfile.mkdtemp(prefix="ercue_bpm_")
    try:
        v1_truth_compare()
        v2_prior_reverse(tmpdir)
        v3_vdj_priority(tmpdir)
        v4_lazy_cache()
        v5_end_to_end(tmpdir)
        if "--shots" in argv and not _shots_via_subprocess():
            RESULTS.append(("截图（真平台）", False))
    except Exception as e:                                       # noqa: BLE001
        print("  ✗ 异常：%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()))
        RESULTS.append(("脚本未异常完成", False))
    print("\n" + "-" * 78)
    passed = sum(1 for _t, ok in RESULTS if ok)
    print("总耗时 %.1fs" % (time.perf_counter() - t_all))
    print("结果：%d 通过 / %d 失败" % (passed, len(RESULTS) - passed))
    for tag, o in RESULTS:
        if not o:
            print("   ✗ %s" % tag)
    print("汇总：%s" % ("全部通过 ✓" if passed == len(RESULTS) else "有失败 ✗"))
    print("BPM_SCAN_TEST: %s" % ("OK" if passed == len(RESULTS) else "FAIL"))
    print("=" * 78)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
