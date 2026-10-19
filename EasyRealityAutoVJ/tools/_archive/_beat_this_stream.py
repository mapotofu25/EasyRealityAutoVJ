# -*- coding: utf-8 -*-
"""准实时分块可行性验证：把 Beat This 当作「流式」跑，看块边界会不会破坏 downbeat 连续性。

方法（不重新读盘两次，先把整首 180s 的 log-mel 算好，再模拟不同的分块策略）：
  官方基线 official : 官方 split_piece（1500/6/keep_first）一次性跑全曲
  方案 A 非重叠     : 连续 1500 帧（30s）块，各块独立推理后直接拼接
  方案 B 重叠滑窗   : 窗口 1500 帧、步长 stride 帧（如 750=15s），
                      每次只采纳 [已产出的下一帧, 窗口尾部-border) 的预测，单调拼接

指标：与官方的 downbeat 一致性、块边界处 downbeat 间距是否仍≈4 拍、
      每窗口推理耗时（多线程 / 单线程）、折算占单核百分比、端到端滞后。

用法：
  python tools/_beat_this_stream.py --n 2 --model <onnx>
输出：打印 + 写 P:\AutoVJ\_beat_this_stream.txt
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import _beat_this_probe as P            # noqa: E402
import beatgrid as bg                   # noqa: E402

OUT = os.path.join(ROOT, "_beat_this_stream.txt")


class Tee:
    def __init__(self, *fhs):
        self.fhs = fhs

    def write(self, s):
        for f in self.fhs:
            try:
                f.write(s)
            except Exception:
                pass

    def flush(self):
        for f in self.fhs:
            try:
                f.flush()
            except Exception:
                pass


def _pad_block(seg, chunk):
    if seg.shape[0] >= chunk:
        return seg[:chunk]
    return np.vstack([seg, np.zeros((chunk - seg.shape[0], P.N_MELS), np.float32)])


def scheme_nonoverlap(model, mel, chunk=P.CHUNK):
    """连续非重叠块，各块独立推理，整块输出直接拼接。"""
    T = mel.shape[0]
    ob = np.full(T, -1000.0)
    od = np.full(T, -1000.0)
    ts = []
    for st in range(0, T, chunk):
        seg = _pad_block(mel[st:st + chunk], chunk)
        t0 = time.time()
        b, d = model._run_chunk(seg)
        ts.append(time.time() - t0)
        L = min(chunk, T - st)
        ob[st:st + L] = b[:L]
        od[st:st + L] = d[:L]
    return ob, od, ts


def scheme_sliding(model, mel, win=P.CHUNK, stride=750):
    """重叠滑窗，只采纳单调新增且去掉尾部 border 的预测。"""
    T = mel.shape[0]
    ob = np.full(T, -1000.0)
    od = np.full(T, -1000.0)
    ts = []
    emitted = 0
    w = 0
    while w < T:
        seg = _pad_block(mel[w:w + win], win)
        t0 = time.time()
        b, d = model._run_chunk(seg)
        ts.append(time.time() - t0)
        lo = max(w + P.BORDER, emitted)
        hi = min(w + win - P.BORDER, T)
        if hi > lo:
            ob[lo:hi] = b[lo - w:hi - w]
            od[lo:hi] = d[lo - w:hi - w]
            emitted = hi
        w += stride
    return ob, od, ts


def db_times(b, d):
    _, dt = P.postprocess(b, d)
    return dt


def match_frac(a, ref, tol=0.030):
    """a 中能在 ref 里找到 ±tol 匹配的比例。"""
    if len(a) == 0:
        return 0.0
    ref = np.asarray(ref)
    hit = 0
    for t in a:
        if len(ref) and np.min(np.abs(ref - t)) <= tol:
            hit += 1
    return hit / len(a)


def boundary_gaps(dt, T, chunk, fps):
    """块边界（chunk 帧的整数倍）±1s 内的 downbeat 间距（拍）。"""
    gaps = []
    bar = None
    for k in range(1, int(np.ceil(T / chunk))):
        tb = k * chunk / fps
        near = dt[np.abs(dt - tb) < 1.0]
        if len(near) >= 2:
            gaps.append(np.diff(near) * 1000)     # ms
    return gaps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2)
    ap.add_argument("--maxsec", type=float, default=180.0)
    ap.add_argument("--model", default=os.path.join(HERE, "_beat_this_models", "final0_cpp.onnx"))
    args = ap.parse_args()

    files = sorted(f for f in os.listdir(P.DIR) if f.lower().endswith(".flac"))
    prefer = [
        "Rolling Contact - Outburst Oxygen (Sharp Remaster).flac",
        "Rolling Contact - The Lost (135 Extended).flac",
        "Rolling Contact - Can We Believe That.flac",
    ]
    chosen = ([f for f in prefer if f in files] + [f for f in files if f not in prefer])[:args.n]

    model_m = P.BeatThis(args.model)                     # 多线程（cpu_count）
    import onnxruntime as ort
    so = ort.SessionOptions(); so.intra_op_num_threads = 1
    sess1 = ort.InferenceSession(args.model, sess_options=so, providers=["CPUExecutionProvider"])
    in1 = sess1.get_inputs()[0].name; out1 = [o.name for o in sess1.get_outputs()]

    def run1(seg):
        b, d = sess1.run(out1, {in1: seg[None, :, :].astype(np.float32)})
        return b[0].astype(np.float64), d[0].astype(np.float64)

    nthreads = max(1, os.cpu_count() or 1)
    print("模型: %s | CPU 逻辑核 %d | 窗口 %d 帧 (%.0fs) | border %d 帧"
          % (os.path.basename(args.model), nthreads, P.CHUNK, P.CHUNK / P.FPS, P.BORDER))
    print()

    for fn in chosen:
        path = os.path.join(P.DIR, fn)
        e = bg.vdj_lookup(bg.load_vdj(), path)
        bl, a = (e["beat_len"], e["anchor"]) if e else (None, None)
        mono = P.load_mono(path, maxsec=args.maxsec)
        mel = P.logmel(mono)
        T = mel.shape[0]
        print("=" * 96)
        print("# %s  (%.0fs, %d 帧)" % (fn.replace("Rolling Contact - ", "")[:44], len(mono) / P.SR, T))
        print("=" * 96)

        # 官方基线
        t0 = time.time()
        bo, do = model_m.frames(mel)
        t_official = time.time() - t0
        dto = db_times(bo, do)
        print("  官方基线: downbeats=%d 全曲多线程耗时 %.2fs（含 6 块）" % (len(dto), t_official))

        # 方案 A 非重叠
        bA, dA, tsA = scheme_nonoverlap(model_m, mel)
        dtA = db_times(bA, dA)
        print("  方案A 非重叠: downbeats=%d  与官方匹配 %.0f%%  每块多线程 %.2fs  边界间隙(ms) %s"
              % (len(dtA), match_frac(dtA, dto) * 100,
                 float(np.mean(tsA)), np.round(np.concatenate(boundary_gaps(dtA, T, P.CHUNK, P.FPS)) if boundary_gaps(dtA, T, P.CHUNK, P.FPS) else [], 0)))

        # 方案 B 重叠滑窗 stride=750（15s）
        bB, dB, tsB = scheme_sliding(model_m, mel, P.CHUNK, 750)
        dtB = db_times(bB, dB)
        print("  方案B 滑窗15s: downbeats=%d  与官方匹配 %.0f%%  每窗口多线程 %.2fs  (窗口数 %d)"
              % (len(dtB), match_frac(dtB, dto) * 100, float(np.mean(tsB)), len(tsB)))

        # 单线程计时（取若干窗口）
        one, two = mel[:P.CHUNK], mel[2 * P.CHUNK:3 * P.CHUNK]
        t1 = []
        for seg in (one, two):
            s = _pad_block(seg, P.CHUNK)
            tt = time.time(); run1(s); t1.append(time.time() - tt)
        t_mult = float(np.mean([tsB[0]]))
        print("  单窗口推理耗时: 多线程 %.2fs | 单线程 %.2fs" % (t_mult, float(np.mean(t1))))
        print("  → 折算占单核: 若每 %ss 推进一次(滑动 stride)，单核占用 ≈ %.1f%%；多线程占用 ≈ %.1f%%（%d 核）"
              % (" 2.5", 100 * float(np.mean(t1)) / 2.5, 100 * t_mult / 2.5, nthreads))
        # 不同 stride 的占核
        for s_frames, s_name in [(50, "1s"), (100, "2s"), (150, "3s"), (750, "15s")]:
            step_s = s_frames / P.FPS
            # 每窗口覆盖 stride 帧 = step_s 秒
            print("      stride=%-4s : 单核占用 %.1f%%  多线程占用 %.1f%%  理论滞后≈ %.1f~%.1fs"
                  % (s_name, 100 * float(np.mean(t1)) / step_s,
                     100 * t_mult / step_s, step_s, step_s + P.BORDER / P.FPS))
        print()
    return 0


if __name__ == "__main__":
    fh = open(OUT, "w", encoding="utf-8")
    orig = sys.stdout
    sys.stdout = Tee(orig, fh)
    try:
        rc = main()
    finally:
        sys.stdout = orig
        fh.close()
        print("全文已写入 %s" % OUT)
    sys.exit(rc)
