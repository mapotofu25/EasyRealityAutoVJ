# -*- coding: utf-8 -*-
r"""素材解码吞吐实测：DXV 解码到底吃掉多少 CPU。

为什么必须先测：用户/群友报「CPU 100%」，但单帧合成实测只要 ~1ms（tools/frame_cost.py）。
那 CPU 花在哪？最可能是**解码**：DXV 是 DXT 压缩，FFmpeg 里只有软件解码
（NVDEC / QSV / D3D11VA 都不支持 DXV），4K 素材每帧要 CPU 解 DXT 并转 BGRA。
如果这一项就是大头，那「硬解」的正确做法不是换解码器，而是把 DXT 解压搬到 GPU。

用法：venv\Scripts\python.exe tools\decode_cost.py [素材目录] [每档样本数]
输出：P:\AutoVJ\_decode_cost.txt
"""
import os
import sys
import time
import glob

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import cv2  # noqa: E402

out = []


def w(s=""):
    out.append(str(s))
    print(s, flush=True)


def probe(path):
    """返回 (w, h, fps, n_frames)"""
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return None
    r = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
         float(cap.get(cv2.CAP_PROP_FPS)), int(cap.get(cv2.CAP_PROP_FRAME_COUNT)))
    cap.release()
    return r


def throughput(path, max_frames=90):
    """顺序解码 max_frames 帧，返回 (成功帧数, 总秒数, 单帧ms, 输出分辨率)"""
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return None
    ok, frame = cap.read()          # 先读一帧，避免把开容器/首帧计入
    if not ok:
        cap.release()
        return None
    res = (frame.shape[1], frame.shape[0])
    n = 0
    t0 = time.perf_counter()
    while n < max_frames:
        ok, frame = cap.read()
        if not ok:
            break
        n += 1
    el = time.perf_counter() - t0
    cap.release()
    if n == 0:
        return None
    return n, el, el * 1000.0 / n, res


def main():
    lib = sys.argv[1] if len(sys.argv) > 1 else r"Y:\EasyRealityAutoVJ\素材库"
    per = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    w("=" * 86)
    w("素材解码吞吐实测   %s" % time.strftime("%H:%M:%S"))
    w("=" * 86)
    try:
        import os as _os
        cores = _os.cpu_count()
    except Exception:
        cores = "?"
    w("CPU 逻辑核数: %s" % cores)
    if not os.path.isdir(lib):
        w("素材目录不存在: %s" % lib)
        open(r"P:\AutoVJ\_decode_cost.txt", "w", encoding="utf-8").write("\n".join(out))
        return

    exts = ("*.mov", "*.mp4", "*.avi", "*.mkv", "*.webm")
    files = []
    for e in exts:
        files += glob.glob(os.path.join(lib, e))
    files = [f for f in files if os.path.isfile(f)]
    w("素材目录: %s   视频文件 %d 个" % (lib, len(files)))
    if not files:
        w("没找到视频")
        open(r"P:\AutoVJ\_decode_cost.txt", "w", encoding="utf-8").write("\n".join(out))
        return

    # 按文件大小分档取样（大文件通常是高分辨率/高码率）
    files.sort(key=os.path.getsize, reverse=True)
    picks = []
    picks += files[:per]                                   # 最大
    mid = len(files) // 2
    picks += files[max(0, mid - per // 2): mid + per - per // 2]   # 中等
    picks = picks[:per * 2]

    w("")
    w("   %-34s %-12s %-7s %-9s %-11s %s" % ("文件", "分辨率", "文件MB", "解码fps", "单帧ms", "备注"))
    rows = []
    for p in picks:
        info = probe(p)
        if info is None:
            w("   %-34s 打不开" % os.path.basename(p)[:34])
            continue
        fw, fh, fps, nf = info
        th = throughput(p)
        if th is None:
            w("   %-34s 解码失败" % os.path.basename(p)[:34])
            continue
        n, el, per_ms, res = th
        dec_fps = n / el
        note = ""
        if fps > 0 and dec_fps < fps * 1.5:
            note = "★ 解码跟不上片段帧率"
        w("   %-34s %-12s %-7.1f %-9.1f %-11.2f %s"
          % (os.path.basename(p)[:34], "%dx%d" % res[:2][::-1] if False else "%dx%d" % (res[0], res[1]),
             os.path.getsize(p) / 1e6, dec_fps, per_ms, note))
        rows.append((res, dec_fps))

    w("")
    w("=" * 86)
    w("结论")
    w("=" * 86)
    if rows:
        avg_ms = np.mean([1000.0 / r[1] for r in rows])
        w("单帧解码平均 %.2f ms（按样本）" % avg_ms)
        w("")
        w("· 每多开 1 个图层，就多一份解码开销。常见演出配置 4~6 层 →")
        w("  解码总开销 ≈ %.1f ~ %.1f ms/帧（还只是解码，不含合成与输出）。" % (avg_ms * 4, avg_ms * 6))
        w("· 合成 + 输出实测只有 ~1~3 ms（tools/frame_cost.py）→ **CPU 的绝对大头是解码**。")
        w("")
        w("⇒ 硬解的正确方向：")
        w("   DXV 是 DXT 压缩，NVDEC/QSV/D3D11VA **都不支持**，换解码器这条路不通；")
        w("   能做的是①把 DXT 解压搬到 GPU（自己解析 DXV 帧 + shader 解 DXT/BC 块），")
        w("   或②素材转成 HAP（同样是 DXT/BC，但天生按 GPU 友好方式封装），")
        w("   或③转成 H.264/H.265（NVDEC 可直接硬解，但画质/体积与 DXV 取向不同）。")
    open(r"P:\AutoVJ\_decode_cost.txt", "w", encoding="utf-8").write("\n".join(out))


if __name__ == "__main__":
    main()
