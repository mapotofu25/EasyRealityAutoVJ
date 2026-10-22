"""alpha 判定修复的性能影响核查（临时工具）

修好 `av.open` 的 GBK 问题、并把判定改成「解几帧确认真有透明」之后，素材会分流到
不同的播放器上。这份核查量一下净影响：

  A) 纠正「误判成 alpha」的素材（25 个）→ 从 PyAV+ARGB32 换回 cv2+BGR24，应当更省
  B) 真带 alpha 的素材（9 个新增）→ 从 cv2 换成 PyAV，会贵一点（但透明度是真的）

两边都跟同一素材的另一种路径对照，交替多轮取中位数。

用法：venv\\Scripts\\python.exe tools\\_alpha_perf_check.py
"""

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np  # noqa: E402

import media_manager as mm  # noqa: E402

LIB = r"Y:\EasyRealityAutoVJ\素材库"
OUT = []


def w(s=""):
    print(s, flush=True)
    OUT.append(str(s))


def cpu_of(path, kind, secs=3.0):
    """跑一个播放器 secs 秒，返回平均核当量。"""
    p = mm.VideoPlayer(path) if kind == "cv2" else mm.AvAlphaPlayer(path, __import__("av"))
    p.set_active(True)
    t0, c0 = time.perf_counter(), time.process_time()
    n, last = 0, None
    while time.perf_counter() - t0 < secs:
        img = p.current()
        if img is not None and img is not last:
            last, n = img, n + 1
        time.sleep(0.004)
    wall = time.perf_counter() - t0
    cpu = (time.process_time() - c0) / wall
    p.close()
    time.sleep(0.3)
    return cpu, n


def compare(path, label, secs=3.0, rounds=3):
    a, b = [], []
    for r in range(rounds):
        order = ("cv2", "pyav") if r % 2 == 0 else ("pyav", "cv2")
        for k in order:
            cpu, n = cpu_of(path, k, secs)
            (a if k == "cv2" else b).append(cpu)
    mc, mp = float(np.median(a)), float(np.median(b))
    w("  %-34s cv2 %.3f 核   PyAV %.3f 核  →  %s %.1f%%"
      % (label, mc, mp, "cv2 省" if mc < mp else "PyAV 省", abs(1 - mc / mp) * 100))
    return mc, mp


def codec_of(path):
    try:
        import av
        c = av.open(path, metadata_errors="replace")
        try:
            return c.streams.video[0].codec_context.name
        finally:
            c.close()
    except Exception:
        return ""


def pick(pool, n=3):
    """挑 n 个 DXV 素材（对比才有代表性）。"""
    out = []
    for it in pool:
        if codec_of(it.path) != "dxv":
            continue
        out.append(it)
        if len(out) >= n:
            break
    return out


def main():
    items = [mm.MediaItem(os.path.join(LIB, f)) for f in sorted(os.listdir(LIB))
             if f.lower().endswith(".mov")]
    video = [it for it in items if it.kind == "video"]
    w("=" * 92)
    w("alpha 判定修复的性能影响核查   %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    w("=" * 92)

    plain_pool = [it for it in video
                  if not os.path.exists(it._thumb_path(".png"))
                  and os.path.exists(it._thumb_path(".jpg"))]
    alpha_pool = [it for it in video if os.path.exists(it._thumb_path(".png"))]
    plain, alpha = pick(plain_pool), pick(alpha_pool)
    w("样本：判不带 alpha 的 %d 个；判带 alpha 的 %d 个" % (len(plain_pool), len(alpha_pool)))

    w("")
    w("[A] 纠正过来的素材（改动前走 PyAV，现在走 cv2）—— 应更省")
    for it in plain:
        compare(it.path, it.name[:34])

    w("")
    w("[B] 真带 alpha 的素材（改动前走 cv2，现在走 PyAV）—— 会贵一些，但透明是真的")
    for it in alpha:
        compare(it.path, it.name[:34])

    with open(os.path.join(ROOT, "_alpha_perf_check.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))
    print("\n→ 已写入 _alpha_perf_check.txt")


if __name__ == "__main__":
    main()
