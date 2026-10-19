# -*- coding: utf-8 -*-
"""测量两个 ONNX 模型的推理耗时与线程占用（DirectML 改造前的基线）。

    python tools/onnx_cost.py [重复次数]

只测推理，不碰主程序。用真实缩略图当输入（和导入时同一条路）。
"""
import os
import sys
import time
import glob

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import numpy as np  # noqa: E402

out = []


def w(s=""):
    print(s)
    out.append(s)


def timeit(label, fn, n=10, warm=3):
    for _ in range(warm):
        fn()
    t0 = time.perf_counter()
    for _ in range(n):
        fn()
    el = (time.perf_counter() - t0) / n
    w("   %-38s %8.2f ms/次" % (label, el * 1000))
    return el


def find_thumb():
    """找一个真实素材缩略图当 CLIP 输入"""
    cands = []
    for d in (os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "thumbs"),
              r"Y:\EasyRealityAutoVJ"):
        if os.path.isdir(d):
            cands += glob.glob(os.path.join(d, "*.jpg")) + glob.glob(os.path.join(d, "*.png"))
    cands = [c for c in cands if os.path.getsize(c) > 2000]
    return cands[:12]


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    import onnxruntime as ort
    w("=" * 78)
    w("ONNX 推理基线   onnxruntime %s   providers=%s" % (ort.__version__, ort.get_available_providers()))
    w("=" * 78)
    w("")

    # ---------------- ① Discogs-EffNet（曲风识别） ----------------
    w("[1] Discogs-EffNet 曲风识别（输入固定 [1,128,96]）")
    from audio_genre import get_genre_classifier, SR, FRAMES, MEL
    t = time.perf_counter()
    gc = get_genre_classifier()
    w("   模型加载 %.2f s" % (time.perf_counter() - t))
    x = np.random.rand(FRAMES, MEL).astype(np.float32)
    sig = np.random.randn(48000 * 2).astype(np.float32) * 0.1
    timeit("整段 classify（含特征提取）", lambda: gc.classify(sig, 48000), n, 2)
    timeit("纯 ONNX run", lambda: gc.sess.run(None, {gc.inp_name: x[None]}), n, 3)
    w("   模型文件 %s" % _size(os.path.join(_assets(), "discogs", "discogs-effnet-bsdynamic-1.onnx")))
    w("")

    # ---------------- ② Chinese-CLIP（素材打标签） ----------------
    w("[2] Chinese-CLIP 视觉打标（输入 [1,3,224,224]）")
    from tagger import get_tagger
    t = time.perf_counter()
    tg = get_tagger()
    w("   模型加载 %.2f s" % (time.perf_counter() - t))
    thumbs = find_thumb()
    w("   找到 %d 张真实缩略图" % len(thumbs))
    if thumbs:
        import cv2
        img = cv2.imread(thumbs[0])
        if img is not None:
            w("   样例 %s  %s" % (os.path.basename(thumbs[0]), img.shape))
        else:
            img = (np.random.rand(224, 224, 3) * 255).astype(np.uint8)
            w("   缩略图读不出，用随机图代替")
    else:
        img = (np.random.rand(224, 224, 3) * 255).astype(np.uint8)
        w("   没有缩略图，用随机图代替")

    # 预处理（cv2 部分）
    def prep():
        import cv2
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        rgb = cv2.resize(rgb, (224, 224)).astype(np.float32) / 255.0
        return ((rgb - np.array([0.48145466, 0.4578275, 0.40821073], np.float32))
                / np.array([0.26862954, 0.26130258, 0.27577711], np.float32))
    timeit("预处理（cvtColor+resize+归一）", prep, n, 2)

    px = np.transpose(prep(), (2, 0, 1))[None].astype(np.float32)
    timeit("纯 ONNX run", lambda: tg.session.run(None, {"pixel_values": px}), n, 3)
    timeit("完整 tag_frame", lambda: tg.tag_frame(img, 8), n, 2)
    e = timeit("_embed + 88 标签相似度", lambda: tg._embed(img) @ tg.tag_emb.T, n, 2)
    w("   模型文件 %s" % _size(os.path.join(_assets(), "chinese_clip", "cn_clip_vision.onnx")))
    w("")

    w("=" * 78)
    w("换算（导入 164 个素材，每个视频抽 5 帧）")
    w("=" * 78)
    w("· 每素材 CLIP 推理 = 5 帧；164 个素材 = 820 次 embed")
    w("· 若单次 embed 约 %.0f ms → 总计约 %.0f 秒（单核串行）" % (e * 1000, e * 820))
    w("· 挪到 GPU 后这部分 CPU 占用可按比例释放")
    open(r"P:\AutoVJ\_onnx_cost.txt", "w", encoding="utf-8").write("\n".join(out))


def _assets():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets", "models")


def _size(p):
    try:
        return "%.1f MB" % (os.path.getsize(p) / 1e6)
    except OSError:
        return "?"


if __name__ == "__main__":
    main()
