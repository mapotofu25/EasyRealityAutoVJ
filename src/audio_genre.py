# -*- coding: utf-8 -*-
"""Discogs-EffNet 本地曲风识别（离线）。

输入：任意采样率的单声道 float32 采样序列，内部重采样到 16kHz，
      计算 Slaney log-mel 谱（96 频带 × 128 帧，log1p），送 ONNX，
      得到 400 种 Discogs 风格的 sigmoid 概率 → 输出英文曲风原词标签。
"""
import os
import sys
import json
import threading

import numpy as np

SR = 16000
FFT, HOP, MEL, FRAMES = 512, 256, 96, 128


def _model_dir():
    base = getattr(sys, "_MEIPASS", None)
    if base is None:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets", "models", "discogs")


def style_name(class_name):
    """'Electronic---Hardcore' → 'Hardcore'（英文原词，取 --- 后的 style 部分）"""
    return class_name.partition("---")[2]


class GenreClassifier:
    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        import onnxruntime as ort
        d = _model_dir()
        # ⚠ 限制线程数 + **关掉池线程自旋**：onnxruntime 默认 intra-op = CPU 核数，
        # 并且池线程在两次推理之间**自旋等待**（`allow_spinning` 默认开）——
        # 对 EffNet 这种 2ms 级的小推理，并行毫无收益，而自旋是**持续**吃 CPU 的。
        # 演出时这个 session 常驻、每 3 秒唤醒一次 → 自旋反复触发。
        # ⭐ 实测（tools/cpu_profile.py 同条件 A/B）：这一项就省下约 **1.6 个核**，
        #    是本轮"解放 CPU"里单笔最大的收益（比 OpenBLAS 那 0.46 核大得多）。
        # 想改回去：AUTO_VJ_ORT_SPIN=1（恢复自旋）、AUTO_VJ_ORT_THREADS=N（改线程数）。
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = int(os.environ.get("AUTO_VJ_ORT_THREADS", "2"))
        opts.inter_op_num_threads = 1
        if os.environ.get("AUTO_VJ_ORT_SPIN", "0") != "1":
            try:
                opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
                opts.add_session_config_entry("session.inter_op.allow_spinning", "0")
            except Exception:
                pass
        self.sess = ort.InferenceSession(
            os.path.join(d, "discogs-effnet-bsdynamic-1.onnx"),
            sess_options=opts, providers=["CPUExecutionProvider"])
        self.inp_name = self.sess.get_inputs()[0].name
        self.classes = json.load(
            open(os.path.join(d, "discogs-effnet-bsdynamic-1.json"), encoding="utf-8"))["classes"]
        self.fbank = self._mel_filterbank()

    @staticmethod
    def _hz2mel(f):
        f = np.asarray(f, float)
        return np.where(f < 1000, f * (3 / 200), 15 + np.log(f / 1000 + 1e-9) / np.log(6.4 / 27))

    @staticmethod
    def _mel2hz(m):
        m = np.asarray(m, float)
        return np.where(m < 15, m / (3 / 200), 1000 * np.exp((m - 15) * np.log(6.4 / 27)))

    def _mel_filterbank(self):
        pts = self._mel2hz(np.linspace(self._hz2mel(0), self._hz2mel(SR / 2), MEL + 2))
        fs = (SR / 2) / (FFT / 2)
        fbank = np.zeros((MEL, FFT // 2 + 1), dtype=np.float32)
        for b in range(MEL):
            l, c, r = pts[b], pts[b + 1], pts[b + 2]
            s = int(np.ceil(l / fs))
            e = int(np.floor(r / fs))
            for k in range(s, min(e + 1, FFT // 2 + 1)):
                f = k * fs
                w = (f - l) / (c - l + 1e-9) if f < c else (r - f) / (r - c + 1e-9)
                if w > 0:
                    fbank[b, k] = w
            a = fbank[b].sum()
            if a > 0:
                fbank[b] *= 2 / a
        return fbank

    def classify(self, samples, sr, top=5):
        """samples: float32 单声道；返回 {'styles':[(英文名, 概率)], 'tags':[英文原词...]}"""
        samples = np.asarray(samples, dtype=np.float32).ravel()
        if sr != SR:
            x = np.linspace(0, len(samples) - 1, max(1, int(len(samples) * SR / sr)))
            samples = np.interp(x, np.arange(len(samples)), samples).astype(np.float32)
        # 取最近 need 采样点，保证正好 FRAMES 帧
        need = (FRAMES - 1) * HOP + FFT
        if len(samples) > need:
            samples = samples[-need:]
        nf = 1 + (len(samples) - FFT) // HOP
        if nf < FRAMES:
            samples = np.pad(samples, (0, need - len(samples)))
            nf = 1 + (len(samples) - FFT) // HOP
        frames = np.lib.stride_tricks.sliding_window_view(samples, FFT)[::HOP][:FRAMES]
        spec = np.abs(np.fft.rfft(frames * np.hanning(FFT), FFT))
        mel = self.fbank @ spec.T            # [96, FRAMES]
        win = mel[:, :FRAMES].T              # [FRAMES, 96]
        x = np.log1p(win).astype(np.float32)[None]
        out = self.sess.run(None, {self.inp_name: x})[0][0]
        order = np.argsort(-out)[:top]
        styles = [(self.classes[i], float(out[i])) for i in order]
        # 英文原词标签（去重，按置信度排序）
        seen, tags = set(), []
        for i in order:
            t = style_name(self.classes[i])
            if t and t not in seen:
                seen.add(t)
                tags.append(t)
        return {"styles": styles, "tags": tags}


class GenreTracker:
    """多窗口时序投票：累积最近多次分类，只有"同一风格连续胜出"才输出，抑制单次误判。"""

    def __init__(self, window=12, agree=4, min_score=0.2):
        from collections import deque
        self.hist = deque(maxlen=window)
        self.agree = agree          # 最近 agree 次里要有 agree 次进 top3 才算稳
        self.min_score = min_score  # 单风格累计分数下限（约 = 单次阈值 × agree）

    def push(self, styles):
        self.hist.append(list(styles))

    def stable(self):
        """返回稳定风格名，或 None（证据不足）"""
        if len(self.hist) < self.agree:
            return None
        agg = {}
        for st in self.hist:
            for name, sc in st[:5]:
                agg[name] = agg.get(name, 0.0) + float(sc)
        if not agg:
            return None
        best = max(agg, key=agg.get)
        score = agg[best]
        recent = list(self.hist)[-self.agree:]
        appear = sum(1 for st in recent if best in [n for n, _ in st[:3]])
        if score >= self.min_score * self.agree and appear >= self.agree:
            return best
        return None


def get_genre_classifier():
    if GenreClassifier._instance is None:
        with GenreClassifier._lock:
            if GenreClassifier._instance is None:
                GenreClassifier._instance = GenreClassifier()
    return GenreClassifier._instance
