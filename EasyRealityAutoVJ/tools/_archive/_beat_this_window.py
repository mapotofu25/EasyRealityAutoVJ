# -*- coding: utf-8 -*-
"""窗口长度与「downbeat 相位正确性」的关系（只读）。

问题：要准实时就得缩短窗口以降低延迟/算力 —— 但窗口太短，模型还能不能判对
「哪一拍是小节头(downbeat)」？

做法：对每个窗口长度 W 秒，把 180s 曲目按非重叠 W 秒切块、各自独立推理，
得到每块的 downbeat 时间；把每个 downbeat 对齐到官方全曲的最近 downbeat，
换算成「拍」的偏差 delta：
    |delta| < 0.75 拍  -> 相位正确（落在正确的小节头）
    |delta| ~ 1    拍  -> 错到相邻拍（半小节错位）
    |delta| ~ 2    拍  -> 错一小节（即两个小节里选错了小节头）
参考相位 = 官方 split_piece 全曲结果（已验证 9/9 与 VDJ 小节线一致）。
输出：打印 + 写 P:\AutoVJ\_beat_this_window.txt
"""
from __future__ import annotations
import argparse, os, sys, time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
import _beat_this_probe as P          # noqa: E402

OUT = os.path.join(ROOT, "_beat_this_window.txt")
WINS = [1.0, 2.0, 3.0, 5.0, 8.0, 10.0, 15.0, 20.0, 30.0]


def classify_window(pred, ref, beat_len):
    """返回 (多数类, 类计数dict, 平均|delta|拍). 类: ok / offbeat / offbar"""
    if len(pred) == 0 or len(ref) == 0:
        return ("empty", {}, float("nan"))
    ref = np.asarray(ref)
    cnt = {"ok": 0, "offbeat": 0, "offbar": 0}
    mags = []
    for t in pred:
        j = int(np.argmin(np.abs(ref - t)))
        d = abs((t - ref[j]) / beat_len)
        mags.append(d)
        if d < 0.75:
            cnt["ok"] += 1
        elif d < 1.5:
            cnt["offbeat"] += 1
        else:
            cnt["offbar"] += 1
    top = max(cnt, key=cnt.get)
    return (top, cnt, float(np.mean(mags)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=os.path.join(HERE, "_beat_this_models", "final0_cpp.onnx"))
    ap.add_argument("--maxsec", type=float, default=180.0)
    args = ap.parse_args()

    files = sorted(f for f in os.listdir(P.DIR) if f.lower().endswith(".flac"))
    prefer = [
        "Rolling Contact - Outburst Oxygen (Sharp Remaster).flac",
        "Rolling Contact - The Lost (135 Extended).flac",
        "Rolling Contact - Can We Believe That.flac",
    ]
    chosen = ([f for f in prefer if f in files] + [f for f in files if f not in prefer])[:3]

    import beatgrid as bg
    vdj = bg.load_vdj()
    model = P.BeatThis(args.model)
    print("模型: %s | 窗口扫描 %s 秒 | 参考=官方全曲 downbeat\n" % (os.path.basename(args.model), WINS))

    agg = {w: {"ok": 0, "n": 0, "t": [], "mag": []} for w in WINS}

    for fn in chosen:
        path = os.path.join(P.DIR, fn)
        e = bg.vdj_lookup(vdj, path)
        beat_len = e["beat_len"] if e else 0.5
        mono = P.load_mono(path, maxsec=args.maxsec)
        mel = P.logmel(mono)
        b, d = model.frames(mel)
        _, ref = P.postprocess(b, d)
        ref = np.asarray(ref)
        print("=" * 88)
        print("# %s  官方 %d 个小节头  拍长 %.4fs"
              % (fn.replace("Rolling Contact - ", "")[:44], len(ref), beat_len))
        print("=" * 88)
        print("  窗口(s)  块数  相位正确块%  半小节错块%  错小节块%  |delta|均(拍)  每块耗时(ms)  最小延迟")
        for W in WINS:
            n = max(1, int(round(W * P.FPS)))
            T = mel.shape[0]
            nok = noffb = noffbar = nblk = 0
            ts = []
            mags = []
            for st in range(0, T, n):
                seg = mel[st:st + n]
                if seg.shape[0] < n:
                    seg = np.vstack([seg, np.zeros((n - seg.shape[0], P.N_MELS), np.float32)])
                t0 = time.time()
                bb, dd = model._run_chunk(seg)
                ts.append(time.time() - t0)
                _, dt = P.postprocess(bb, dd)
                c, cnt, mg = classify_window(np.asarray(dt), ref, beat_len)
                nblk += 1
                if c == "ok":
                    nok += 1
                elif c == "offbeat":
                    noffb += 1
                elif c == "offbar":
                    noffbar += 1
                if not np.isnan(mg):
                    mags.append(mg)
            agg[W]["ok"] += nok
            agg[W]["n"] += nblk
            agg[W]["t"].append(np.mean(ts) * 1000)
            agg[W]["mag"].append(np.mean(mags) if mags else float("nan"))
            print("  %5.1f   %4d      %6.1f%%       %6.1f%%      %6.1f%%      %7.3f       %7.1f     ~%.1fs"
                  % (W, nblk, 100 * nok / nblk, 100 * noffb / nblk, 100 * noffbar / nblk,
                     np.mean(mags) if mags else float("nan"), np.mean(ts) * 1000, W + P.BORDER / P.FPS))
        print()

    print("=" * 88)
    print("汇总（3 首 × 各块合并）：窗口长度 vs 相位正确率")
    print("=" * 88)
    print("  窗口(s)  相位正确块占比  每块多线程耗时(ms)  最小可用延迟")
    for W in WINS:
        print("  %5.1f      %6.1f%%           %7.1f          ~%.1fs"
              % (W, 100.0 * agg[W]["ok"] / max(1, agg[W]["n"]),
                 np.mean(agg[W]["t"]), W + P.BORDER / P.FPS))
    return 0


if __name__ == "__main__":
    fh = open(OUT, "w", encoding="utf-8")
    orig = sys.stdout

    class Tee:
        def write(self, s):
            orig.write(s); fh.write(s)
        def flush(self):
            orig.flush(); fh.flush()

    sys.stdout = Tee()
    try:
        rc = main()
    finally:
        sys.stdout = orig
        fh.close()
        print("全文已写入 %s" % OUT)
    sys.exit(rc)
