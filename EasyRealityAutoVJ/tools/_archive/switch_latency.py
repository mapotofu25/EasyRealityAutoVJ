# -*- coding: utf-8 -*-
r"""切素材首帧延迟实测（switch_latency）

测三件事（都用项目真实的 media_manager 播放器，离屏，不需要 GUI）：
  1) 构造 → 容器打开完成 的耗时（应在解码线程里，不阻塞渲染线程）
  2) 构造 → 首帧可用 的耗时  ← 这就是「切素材卡几帧」的根因时长
     · 现状路径：create → set_active(True) → 等 current() 有效
     · 预热路径：create → warm_up() → 等 current() 有效（提前做，所以切换时是 0）
  3) close() 是否阻塞调用线程（渲染线程里驱逐缓存会走这里）
用法：venv\Scripts\python.exe tools\switch_latency.py [素材目录] [样本数]
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from PySide6.QtGui import QImage  # noqa: E402
import media_manager as mm  # noqa: E402

LIB = sys.argv[1] if len(sys.argv) > 1 else r"Y:\EasyRealityAutoVJ\素材库"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 6
MAXW, MAXH = 1920, 1080          # 画布 1280x720 ×1.5 的近似（用户当前输出 720p）
out = []


def w(s=""):
    out.append(str(s))
    print(s, flush=True)


def pick_samples():
    fs = []
    for f in sorted(os.listdir(LIB)):
        p = os.path.join(LIB, f)
        if os.path.isfile(p) and f.lower().endswith((".mov", ".mp4", ".mkv", ".avi")):
            fs.append((os.path.getsize(p), p))
    fs.sort(reverse=True)
    return [p for _, p in fs[:N]]


def wait_frame(p, timeout=3.0):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        img = p.current()
        if img is not None and not img.isNull():
            return time.perf_counter() - t0
        time.sleep(0.002)
    return float("nan")


def main():
    has_warm = all(hasattr(mm.VideoPlayer, m) for m in ("warm_up",))
    w("样本：%d 个最大素材" % N)
    w("VideoPlayer 有 warm_up()：%s" % has_warm)
    w("")
    hdr = "%9s %8s %10s %10s %10s %10s %8s" % (
        "体积MB", "类型", "构造ms", "开容器ms", "首帧ms", "预热后首帧ms", "close_ms")
    w(hdr)
    w("-" * len(hdr))
    rows = []
    for p in pick_samples():
        item = mm.MediaItem(p)
        t0 = time.perf_counter()
        pl = mm.make_player(item)
        t_ctor = (time.perf_counter() - t0) * 1000
        pl.set_max_size(MAXW, MAXH)
        kind = type(pl).__name__
        # 容器打开完成（VideoPlayer: cap / AvAlphaPlayer: c）
        t1 = time.perf_counter()
        while time.perf_counter() - t1 < 3.0:
            ready = (getattr(pl, "cap", None) is not None
                     or getattr(pl, "c", None) is not None)
            if ready:
                break
            time.sleep(0.002)
        t_open = (time.perf_counter() - t1) * 1000
        # 现状路径：立即置 active，等首帧
        pl.set_active(True)
        t_first = wait_frame(pl) * 1000
        # close 阻塞时间
        t2 = time.perf_counter()
        pl.close()
        t_close = (time.perf_counter() - t2) * 1000

        # 预热路径（同一素材再来一次，先 warm_up 再等；不打 active）
        pl2 = mm.make_player(item)
        pl2.set_max_size(MAXW, MAXH)
        t3 = time.perf_counter()
        if hasattr(pl2, "warm_up"):
            pl2.warm_up()
        t_warm = wait_frame(pl2, 3.0) * 1000
        t_warm_cost = (time.perf_counter() - t3) * 1000
        pl2.close()
        w("%9.1f %8s %10.1f %10.1f %10.1f %10.1f %8.1f" % (
            os.path.getsize(p) / 1e6, kind[:8], t_ctor, t_open, t_first, t_warm, t_close))
        rows.append((os.path.getsize(p) / 1e6, t_open, t_first, t_warm, t_close))
        w("           └ %s" % os.path.basename(p)[:60])
        del pl, pl2, item
    if rows:
        w("")
        w("中位数：开容器 %.1f ms / 现状首帧 %.1f ms / 预热后首帧 %.1f ms / close %.1f ms"
          % (sorted(r[1] for r in rows)[len(rows) // 2],
             sorted(r[2] for r in rows)[len(rows) // 2],
             sorted(r[3] for r in rows)[len(rows) // 2],
             sorted(r[4] for r in rows)[len(rows) // 2]))
        w("→ 60fps 下：现状首帧延迟 ≈ %.0f 帧；预热后 ≈ %.0f 帧"
          % (sorted(r[2] for r in rows)[len(rows) // 2] / 16.7,
             sorted(r[3] for r in rows)[len(rows) // 2] / 16.7))
    open(r"P:\AutoVJ\_switch_latency.txt", "w", encoding="utf-8").write("\n".join(out))


if __name__ == "__main__":
    main()
