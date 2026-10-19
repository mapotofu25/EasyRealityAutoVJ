# -*- coding: utf-8 -*-
"""CLIP 走 CPU 还是 DirectML —— 隔离对比探针。

用法（必须分两个进程跑，onnxruntime 同名不能同进程混用）：
    python tools/dml_probe.py cpu
    python tools/dml_probe.py dml

各自把结果写进 _dml_cpu.json / _dml_gpu.json，
再跑： python tools/dml_probe.py cmp   做一致性 + 速度对比。
"""
import os
import sys
import json
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DML_ENV = os.path.join(BASE, "_dml_env")
MODEL = os.path.join(BASE, r"assets\models\chinese_clip\cn_clip_vision.onnx")
FIXED = os.path.join(BASE, "_clip_fixed.onnx")
OUT_CPU = os.path.join(BASE, "_dml_cpu.json")
OUT_GPU = os.path.join(BASE, "_dml_gpu.json")

# 固定输入（psnr/一致性对比必须同一份数据）
SEED = 20260924


def sample_inputs():
    import numpy as np
    rng = np.random.RandomState(SEED)
    # 用真实缩略图更贴实际；找不到就随机
    import glob
    import cv2
    thumbs = []
    for d in (os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "thumbs"),):
        if os.path.isdir(d):
            thumbs = [p for p in glob.glob(os.path.join(d, "*.jpg"))
                      if os.path.getsize(p) > 3000][:4]
    imgs = []
    for p in thumbs:
        im = cv2.imread(p)
        if im is None:
            continue
        rgb = cv2.cvtColor(im, cv2.COLOR_BGR2RGB)
        rgb = cv2.resize(rgb, (224, 224)).astype("float32") / 255.0
        rgb = ((rgb - [0.48145466, 0.4578275, 0.40821073])
               / [0.26862954, 0.26130258, 0.27577711])
        imgs.append(rgb.transpose(2, 0, 1)[None].astype("float32"))
    while len(imgs) < 4:
        imgs.append(rng.rand(1, 3, 224, 224).astype("float32"))
    return imgs


def run(tag):
    if tag == "dml":
        sys.path.insert(0, DML_ENV)
    import numpy as np
    import onnxruntime as ort

    src = FIXED if os.path.exists(FIXED) else MODEL
    avail = ort.get_available_providers()
    print("onnxruntime %s  (%s)" % (ort.__version__, os.path.dirname(ort.__file__)))
    print("available providers:", avail)
    want = "DmlExecutionProvider" if tag == "dml" else "CPUExecutionProvider"
    if want not in avail:
        print("!! 该 provider 不可用:", want)
        return 1

    opts = ort.SessionOptions()
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    opts.log_severity_level = 3
    t0 = time.perf_counter()
    sess = ort.InferenceSession(src, sess_options=opts, providers=[want, "CPUExecutionProvider"])
    load_s = time.perf_counter() - t0
    prov = sess.get_providers()
    print("模型:", os.path.basename(src))
    print("session providers:", prov)
    print("加载(含首次 kernel 编译) %.2f s" % load_s)

    imgs = sample_inputs()
    inp = sess.get_inputs()[0].name
    # 预热
    for _ in range(3):
        sess.run(None, {inp: imgs[0]})
    N = 15
    t0 = time.perf_counter()
    for i in range(N):
        out = sess.run(None, {inp: imgs[i % len(imgs)]})
    ms = (time.perf_counter() - t0) / N * 1000
    print("单次推理 %.2f ms  (n=%d)" % (ms, N))

    embs = np.concatenate([sess.run(None, {inp: x})[0] for x in imgs], axis=0)
    res = {"tag": tag, "src": os.path.basename(src), "ort": ort.__version__,
           "providers": list(prov), "load_s": load_s, "ms": ms,
           "emb": embs.tolist()}
    with open(OUT_GPU if tag == "dml" else OUT_CPU, "w") as f:
        json.dump(res, f)
    print("已写:", OUT_GPU if tag == "dml" else OUT_CPU)
    return 0


def cmp_():
    import numpy as np
    if not (os.path.exists(OUT_CPU) and os.path.exists(OUT_GPU)):
        print("缺少结果文件，先分别跑 cpu 和 dml")
        return 1
    a = json.load(open(OUT_CPU))
    b = json.load(open(OUT_GPU))
    ea = np.array(a["emb"], dtype=np.float64)
    eb = np.array(b["emb"], dtype=np.float64)
    # 每个向量的余弦相似度
    def norm(x):
        return x / np.linalg.norm(x, axis=1, keepdims=True)
    cos = (norm(ea) * norm(eb)).sum(axis=1)
    print("=" * 74)
    print("一致性（同一份输入，CPU vs DirectML）")
    print("=" * 74)
    print("  样本数            : %d" % len(cos))
    print("  余弦相似度 min/mean: %.6f / %.6f" % (cos.min(), cos.mean()))
    print("  最大绝对差         : %.6f" % np.abs(ea - eb).max())
    print("  相对误差 max       : %.6f" % (np.abs(ea - eb).max() / max(1e-9, np.abs(ea).max())))
    print()
    print("速度")
    print("  CPU      : %.2f ms/次  (onnxruntime %s)" % (a["ms"], a["ort"]))
    print("  DirectML : %.2f ms/次  (onnxruntime %s)" % (b["ms"], b["ort"]))
    sp = a["ms"] / max(1e-9, b["ms"])
    print("  加速比   : ×%.2f" % sp)
    print()
    ok = cos.min() > 0.999
    print("判定：数值一致性 %s（余弦 > %.6f → %s）"
          % ("通过" if ok else "不通过", 0.999, "可直接替换" if ok else "需谨慎"))
    out = []
    out.append("CPU      %.2f ms" % a["ms"])
    out.append("DirectML %.2f ms" % b["ms"])
    out.append("加速 ×%.2f" % sp)
    out.append("余弦 min %.6f" % cos.min())
    open(os.path.join(BASE, "_dml_result.txt"), "w", encoding="utf-8").write("\n".join(out))
    return 0


if __name__ == "__main__":
    tag = sys.argv[1] if len(sys.argv) > 1 else "cpu"
    sys.exit(cmp_() if tag == "cmp" else run(tag))
