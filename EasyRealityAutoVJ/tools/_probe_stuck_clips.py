# -*- coding: utf-8 -*-
"""探针：日志里"解不出首帧"的那几个素材到底能不能解。

对每个目标文件依次测：
  · 存在性 / 大小 / 修改时间
  · OpenCV 能否打开 + 读首帧（VideoPlayer 走这条）
  · PyAV 能否打开 + 解首帧（AvAlphaPlayer / 软解生成器走这条）
  · 容器/编码格式、分辨率、时长、帧数
只读，不修改任何文件。
"""
import os
import sys
import time

LIB = r"Y:\EasyRealityAutoVJ\素材库"
TARGETS = ["dusk oct ring.mov", "4 (2).mov", "30.mov"]

# 兜底：日志里出现过的其它名字也一起看（有就报，没有就跳过）
EXTRA = ["4.mov", "17.mov", "30.mov", "001.mov", "8.mov"]


def find(name):
    for root, _dirs, files in os.walk(LIB):
        for f in files:
            if f.lower() == name.lower():
                return os.path.join(root, f)
    return None


def probe(path):
    print("-" * 78)
    print("文件:", path)
    try:
        st = os.stat(path)
        print("  大小 %.2f MB   修改 %s" % (st.st_size / 1048576.0,
                                           time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))))
    except Exception as e:                                            # noqa: BLE001
        print("  !! stat 失败:", e)

    # --- OpenCV ---
    try:
        import cv2
        t0 = time.perf_counter()
        cap = cv2.VideoCapture(path)
        ok_open = cap.isOpened()
        t_open = (time.perf_counter() - t0) * 1000
        got = False
        t_read = 0.0
        if ok_open:
            t1 = time.perf_counter()
            ok, frame = cap.read()
            t_read = (time.perf_counter() - t1) * 1000
            got = bool(ok) and frame is not None
            fps = cap.get(cv2.CAP_PROP_FPS)
            n = cap.get(cv2.CAP_PROP_FRAME_COUNT)
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            print("  [OpenCV] 打开=%s(%.0fms) 首帧=%s(%.0fms)  %dx%d  fps=%.1f  帧数=%.0f"
                  % (ok_open, t_open, got, t_read, w, h, fps, n))
        else:
            print("  [OpenCV] **打不开** (%.0fms)" % t_open)
        cap.release()
    except Exception as e:                                            # noqa: BLE001
        print("  [OpenCV] 异常:", e)

    # --- PyAV ---
    try:
        import av
        t0 = time.perf_counter()
        c = av.open(path, metadata_errors="replace")
        t_open = (time.perf_counter() - t0) * 1000
        st0 = c.streams.video[0]
        print("  [PyAV] 打开 OK(%.0fms)  编码=%s  像素格式=%s  %dx%d  fps=%s  时长=%.2fs"
              % (t_open, st0.codec_context.name, st0.codec_context.pix_fmt,
                 st0.codec_context.width, st0.codec_context.height,
                 st0.average_rate, float(c.duration or 0) / 1000000.0))
        t1 = time.perf_counter()
        n = 0
        first = None
        err = ""
        try:
            for frame in c.decode(video=0):
                n += 1
                if first is None:
                    first = frame
                if n >= 3:
                    break
        except Exception as e:                                        # noqa: BLE001
            err = "%s: %s" % (type(e).__name__, str(e)[:80])
        t_dec = (time.perf_counter() - t1) * 1000
        print("  [PyAV] 解出 %d 帧(%.0fms)  首帧=%s%s"
              % (n, t_dec, first is not None, ("  异常=" + err) if err else ""))
        if first is not None:
            print("        首帧格式=%s %dx%d" % (first.format.name, first.width, first.height))
        c.close()
    except Exception as e:                                            # noqa: BLE001
        print("  [PyAV] 异常: %s: %s" % (type(e).__name__, str(e)[:120]))


def main():
    print("素材库:", LIB, "存在:", os.path.isdir(LIB))
    names = []
    for n in TARGETS + EXTRA:
        if n not in names:
            names.append(n)
    for n in names:
        p = find(n)
        if p:
            probe(p)
    print("=" * 78)
    print("完成")


if __name__ == "__main__":
    sys.exit(main())
