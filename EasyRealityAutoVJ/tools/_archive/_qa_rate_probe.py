# -*- coding: utf-8 -*-
"""QA 辅助：量真实出帧率，判定"软解 ~15fps"是本来就慢还是改动引入的退化。

分别量：视频帧率 / GPU 路径 fps / 纯软解(AvAlphaPlayer) fps / GpuDxvPlayer 强制软解 fps。
用法：venv\\Scripts\\python.exe tools\\_qa_rate_probe.py
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))


def count_frames(player, secs):
    """统计 secs 秒内 current() 变化的次数（=真实出帧）。"""
    player.set_active(True)
    time.sleep(0.6)                        # 预热：开容器/解码起头
    last, n = player.current(), 0
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < secs:
        img = player.current()
        if img is not None and img is not last:
            last = img
            n += 1
        time.sleep(0.002)
    dt = time.perf_counter() - t0
    player.close()
    time.sleep(0.3)
    return n, n / dt


def main():
    import av
    import media_manager as mm
    LIB = r"Y:\EasyRealityAutoVJ\素材库"

    # 选 DXV 1080p **DXT5** 素材（hang_e2e / startup_fail 用的就是这类）
    import dxvnative
    target = os.environ.get("QA_FILE")
    path = None
    for f in sorted(os.listdir(LIB)):
        if not f.lower().endswith((".mov", ".mp4", ".mkv")):
            continue
        p = os.path.join(LIB, f)
        try:
            c = av.open(p, metadata_errors="replace")
            st = c.streams.video[0]
            if st.codec_context.name != "dxv" or st.width * st.height < 1.9e6:
                c.close()
                continue
            pk = next(c.demux(st))
            hd = dxvnative.parse_header(bytes(pk))
            fmt = None if hd is None else int(hd[0])
            fps, w, h = float(st.average_rate or 0), st.width, st.height
            c.close()
            if target and os.path.basename(p) != target:
                continue
            if target or fmt == 5:
                path = p
                break
        except Exception:
            continue
    if not path:
        print("★ 没找到素材")
        return
    print("素材 %s  标称 %g fps  %dx%d  fmt=%s" % (os.path.basename(path), fps, w, h, fmt))
    print("=" * 70)

    # GPU
    mm.set_gpu_decode(True)
    mm._GPU_DISABLED = ""
    mm._GPU_STATUS = None
    n, r = count_frames(mm.GpuDxvPlayer(path, av, out_alpha=True), 3.0)
    print("GPU 路径        : %5d 帧 / 3s = %6.1f fps" % (n, r))

    # GpuDxvPlayer 强制软解
    mm._GPU_DISABLED = "probe: forced soft"
    n, r = count_frames(mm.GpuDxvPlayer(path, av, out_alpha=True), 3.0)
    print("GpuDxv 强制软解 : %5d 帧 / 3s = %6.1f fps" % (n, r))
    mm._GPU_DISABLED = ""

    # AvAlphaPlayer（原软解路径）
    n, r = count_frames(mm.AvAlphaPlayer(path, av), 3.0)
    print("AvAlphaPlayer   : %5d 帧 / 3s = %6.1f fps" % (n, r))

    # VideoPlayer（OpenCV，不带 alpha 路径）
    try:
        n, r = count_frames(mm.VideoPlayer(path), 3.0)
        print("VideoPlayer cv2 : %5d 帧 / 3s = %6.1f fps" % (n, r))
    except Exception as e:
        print("VideoPlayer cv2 : 失败 %s" % e)


if __name__ == "__main__":
    main()
