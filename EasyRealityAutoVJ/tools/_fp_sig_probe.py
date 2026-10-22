# -*- coding: utf-8 -*-
"""指纹签名**跨端一致性**探针。

背景（用户 2026-10-17「注意md5的对比」）：
  码表里 `identity.fp_id` 由 **EasyRealityCue** 算（`fp_core.fingerprint_signature`），
  演出端要拿它跟本机曲库同一首歌算出的 fp_id 比对。若两端喂给指纹的 **PCM 不同**，
  哈希就不同 ⇒ 签名不同 ⇒ 这一级认歌**永远不命中**（而且静默）。

已知差异点：
  ① Cue 端 `AudioTrack(path, samplerate=44100)` 用 **PyAV + swresample**
     把任意采样率的源**统一重采样到 44.1k 立体声**，再 `mean` 成 mono；
  ② 演出端 `sf.read(p, dtype="float32")` 用 **soundfile**，**保持原生采样率**，
     再 `mean`；重采样只在 `fp.fingerprint_audio()` 里用 `np.interp` 做。

本脚本对同一批真实文件算三份签名：
  A = 演出端口径（soundfile 原生率）
  B = Cue 端**当前**口径（PyAV 44.1k 立体声 → mono）
  C = Cue 端**修正**口径（PyAV 原生率立体声 → mono）
输出 A/B/C 与两两是否相等。只读文件，不写任何配置。
"""
from __future__ import annotations

import hashlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

import numpy as np

import fp as fpvj


def sig_a(path):
    """演出端口径：soundfile 原生率 + mean(axis=1)。"""
    import soundfile as sf
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    mono = np.mean(data, axis=1)
    return "sf/%.0fHz" % sr, fpvj.signature_from_hashes(
        fpvj.fingerprint_audio(np.ascontiguousarray(mono, dtype=np.float32), int(sr)))


def _av_mono(path, rate=None):
    """PyAV 解码 → (mono float32, sr)。rate=None ⇒ 用源的原始采样率（不重采样）。"""
    import av
    with av.open(path, metadata_errors="replace") as c:
        st = [s for s in c.streams if s.type == "audio"][0]
        native = int(st.rate)
        want = int(rate or native)
        res = av.AudioResampler(format="fltp", layout="stereo", rate=want)
        chunks = []
        for frame in c.decode(st):
            for fr in res.resample(frame):
                chunks.append(np.asarray(fr.to_ndarray(), dtype=np.float32))
        try:
            for fr in res.resample(None):
                chunks.append(np.asarray(fr.to_ndarray(), dtype=np.float32))
        except Exception:
            pass
        buf = np.concatenate(chunks, axis=1) if chunks else None
    if buf is None or buf.shape[1] == 0:
        return None, want
    mono = buf.mean(axis=0) if buf.shape[0] > 1 else buf[0]
    return np.ascontiguousarray(mono, dtype=np.float32), want


def sig_b(path):
    """Cue 端当前口径：固定 44.1k。"""
    mono, sr = _av_mono(path, 44100)
    if mono is None:
        return "av/44100", ""
    return "av/44100=native?" , fpvj.signature_from_hashes(
        fpvj.fingerprint_audio(mono, 44100))


def sig_c(path):
    """Cue 端修正口径：原生率。"""
    mono, sr = _av_mono(path, None)
    if mono is None:
        return "av/native", ""
    return "av/%d" % sr, fpvj.signature_from_hashes(
        fpvj.fingerprint_audio(mono, sr))


def main(argv):
    paths = argv[1:]
    if not paths:
        cfg = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "config.json")
        import json
        try:
            lib = json.load(open(cfg, encoding="utf-8")).get("music_library") or []
        except Exception:
            lib = []
        paths = lib[:3]
    if not paths:
        print("没有可测文件（传路径进来）")
        return 2
    print("%-52s %-18s %-18s %-18s %s" % ("file", "A=sf/native", "B=av/44.1k", "C=av/native", "A==B A==C"))
    for p in paths:
        if not os.path.isfile(p):
            print("缺失:", p)
            continue
        la, sa = sig_a(p)
        lb, sb = sig_b(p)
        lc, sc = sig_c(p)
        print("%-52s %-18s %-18s %-18s %s %s" % (
            os.path.basename(p)[:50], sa or "-", sb or "-", sc or "-",
            "Y" if (sa and sa == sb) else "n",
            "Y" if (sa and sa == sc) else "n"))
        print("    A %s   B %s   C %s" % (la, lb, lc))
        # md5（文件内容）与 song_id（路径 md5）是两码事，顺带打出来避免混用
        h = hashlib.md5()
        with open(p, "rb") as f:
            for blk in iter(lambda: f.read(1 << 20), b""):
                h.update(blk)
        print("    file_md5=%s  song_id(path md5)=%d" % (
            h.hexdigest(), int(hashlib.md5(p.encode("utf-8")).hexdigest()[:8], 16)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
