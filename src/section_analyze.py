# -*- coding: utf-8 -*-
"""离线曲目段落分析：整曲能量曲线 → 完整段落时间轴（intro/buildup/drop/break/outro）。

供「放歌时按预解析段落精确匹配」使用：曲库扫描时对每首歌算一次存进 music_meta，
现场指纹识别返回 offset（当前播放秒数）后直接查表，比实时启发式更准、零延迟。
"""
import numpy as np


def _energy_curve(mono, sr, win=0.25):
    step = max(1, int(sr * win))
    n = (len(mono) - step) // step + 1
    if n < 8:
        return np.array([]), 0.0
    frames = np.lib.stride_tricks.sliding_window_view(mono, step)[::step][:n]
    rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
    return rms, step / sr


def analyze_sections(path):
    """返回 [{"t": 秒, "label": "intro"/"buildup"/"drop"/"break"/"outro"}, ...]（升序）。

    用带滞回的状态机沿能量曲线走完整时间轴（一首歌有多个 drop/build 循环），
    再按位置把 quiet 段细分为 intro（首段）/ outro（末段）/ break（中间间奏）。
    匹配规则：当前秒 t 取最后一个 t<=当前秒 的 label。
    """
    import soundfile as sf
    try:
        data, sr = sf.read(path, dtype="float32", always_2d=True)
    except Exception:
        return []
    if len(data) == 0:
        return []
    mono = np.mean(data, axis=1)
    rms, dt = _energy_curve(mono, sr)
    if len(rms) < 16:
        return []

    peak = float(np.percentile(rms, 95)) + 1e-9
    r = np.clip(rms / peak, 0.0, 1.0)
    k = 8                          # ~2s 平滑（压掉鼓点级抖动）
    r = np.convolve(r, np.ones(k) / k, mode="same")
    t = np.arange(len(r)) * dt
    n = len(r)

    # 滞回阈值：进入 drop 要 >0.70，退出 drop 要 <0.55（避免临界抖动）；<0.45 归 quiet
    DROP_ENTER, DROP_EXIT, LOW = 0.70, 0.55, 0.45

    def _init(v):
        if v >= DROP_ENTER:
            return "drop"
        if v < LOW:
            return "quiet"
        return "build"

    state = _init(r[0])
    raw = [{"t": round(float(t[0]), 1), "label": state}]
    for i in range(1, n):
        ns = state
        if state == "drop":
            if r[i] < DROP_EXIT:
                ns = "quiet" if r[i] < LOW else "build"
        elif state == "build":
            if r[i] >= DROP_ENTER:
                ns = "drop"
            elif r[i] < LOW:
                ns = "quiet"
        else:  # quiet
            if r[i] >= DROP_ENTER:
                ns = "drop"
            elif r[i] >= LOW:
                ns = "build"
        if ns != state:
            state = ns
            raw.append({"t": round(float(t[i]), 1), "label": state})

    # 碎片合并：<4s 的短段跳过（看"到下一段"的间隔）。
    # intro 里的零散波动靠后面的「首段归并」统一收进 intro，不会产生碎片。
    MIN_SEG = 4.0
    merged = []
    for i, s in enumerate(raw):
        seg_len = (raw[i + 1]["t"] - s["t"]) if i + 1 < len(raw) else (float(t[-1]) + dt - s["t"])
        if seg_len < MIN_SEG:
            continue
        merged.append(s)

    # 首尾归并：第一个 drop 之前的所有段都是 intro（含其中的零散波动）；
    # 之后的 quiet→break（中间间奏）/ outro（末段收尾）；build→buildup。
    first_drop = next((k for k, s in enumerate(merged) if s["label"] == "drop"), -1)
    n_m = len(merged)
    out = []
    for k, s in enumerate(merged):
        lab = s["label"]
        if first_drop >= 0 and k < first_drop:
            lab = "intro"
        elif lab == "quiet":
            lab = "outro" if k == n_m - 1 else "break"
        elif lab == "build":
            lab = "buildup"
        out.append({"t": s["t"], "label": lab})

    # 相邻同 label 合并（连续的 drop 显示上等价，段落序列更干净）
    final = []
    for s in out:
        if final and final[-1]["label"] == s["label"]:
            continue
        final.append(s)
    return final
