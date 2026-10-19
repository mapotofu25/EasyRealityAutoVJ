# -*- coding: utf-8 -*-
"""实测 2：视频解码与帧取用（PyAV vs OpenCV），含 alpha 探测与耗时。"""
import os, sys, time, glob

MP4 = None
for pat in ("P:/avi~/*.mp4",):
    c = sorted(glob.glob(pat))
    if c:
        MP4 = c[0]
        break
MOV_DIR = "Y:/EasyRealityAutoVJ/素材库"
MOVS = sorted(glob.glob(MOV_DIR + "/*.mov"))[:40]

print("mp4 sample :", MP4)
print("mov samples:", len(MOVS))
print("=" * 78)

# ---------- PyAV ----------
try:
    import av
    import numpy as np
    t0 = time.perf_counter()
    import av  # already
    t_imp = time.perf_counter() - t0
    print("[PyAV] version", av.__version__, "import %.0fms" % (t_imp * 1000))

    def av_probe(path):
        t = time.perf_counter()
        c = av.open(path, metadata_errors="replace")
        try:
            st = c.streams.video[0]
            info = {
                "codec": st.codec_context.name,
                "pix_fmt": st.pix_fmt,
                "w": st.codec_context.width, "h": st.codec_context.height,
                "fps": float(st.average_rate or 0),
                "frames": int(st.frames or 0),
                "dur": float(st.duration * st.time_base) if st.duration else 0.0,
            }
            # 取第一帧
            fr = next(c.decode(video=0))
            arr = fr.to_ndarray(format="rgba")
            info["first_frame_shape"] = arr.shape
            info["has_alpha_ch"] = arr.shape[2] == 4
            info["alpha_min"] = int(arr[..., 3].min()) if arr.shape[2] == 4 else None
        finally:
            c.close()
        info["probe_ms"] = round((time.perf_counter() - t) * 1000, 1)
        return info

    if MP4:
        print("  mp4 :", av_probe(MP4))
    # 扫 mov 统计 alpha + 计时
    n_alpha = 0
    t_all = time.perf_counter()
    samples = []
    for p in MOVS:
        try:
            i = av_probe(p)
        except Exception as e:
            samples.append((os.path.basename(p), "ERR:" + type(e).__name__)); continue
        is_a = i["pix_fmt"] and any(m in i["pix_fmt"].lower() for m in ("rgba", "bgra", "yuva", "argb", "abgr", "pal8"))
        real = i["alpha_min"] is not None and i["alpha_min"] < 255
        if real:
            n_alpha += 1
        samples.append((os.path.basename(p)[:28], i["codec"], i["pix_fmt"], f"{i['w']}x{i['h']}", round(i["fps"], 2), i["probe_ms"]))
    dt = time.perf_counter() - t_all
    print(f"  mov 扫描 {len(MOVS)} 个：真带 alpha {n_alpha} 个；总耗时 {dt:.1f}s（均值 {dt/max(1,len(MOVS))*1000:.0f}ms/个）")
    for s in samples[:12]:
        print("     ", s)
except Exception as e:
    print("[PyAV] FAILED:", type(e).__name__, e)

print("=" * 78)

# ---------- OpenCV ----------
try:
    import cv2
    import numpy as np
    print("[cv2] version", cv2.__version__, "| build FFmpeg? ->", "FFMPEG" in cv2.getBuildInformation())
    def cv_probe(path):
        t = time.perf_counter()
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            return {"open": False}
        info = {
            "open": True,
            "fps": round(cap.get(cv2.CAP_PROP_FPS), 3),
            "frames": int(cap.get(cv2.CAP_PROP_FRAME_COUNT)),
            "w": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "h": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "fourcc": int(cap.get(cv2.CAP_PROP_FOURCC)),
        }
        ok, f = cap.read()
        info["read_ok"] = bool(ok)
        info["frame_shape"] = tuple(f.shape) if ok else None
        # 取中间帧
        n = info["frames"]
        if n > 2:
            cap.set(cv2.CAP_PROP_POS_FRAMES, n // 2)
            ok2, f2 = cap.read()
            info["mid_ok"] = bool(ok2)
        cap.release()
        info["probe_ms"] = round((time.perf_counter() - t) * 1000, 1)
        return info
    if MP4:
        print("  mp4 :", cv_probe(MP4))
    # mov 扫描：是否含 4 通道
    ok4 = 0
    t_all = time.perf_counter()
    for p in MOVS:
        try:
            i = cv_probe(p)
            if i.get("open") and i.get("frame_shape") and i["frame_shape"][-1] == 4:
                ok4 += 1
        except Exception:
            pass
    print(f"  mov 扫描 {len(MOVS)} 个：cv2 读出 4 通道(Qt 可得 alpha) 的 {ok4} 个；"
          f"总耗时 {time.perf_counter()-t_all:.1f}s")
    print("  注意：opencv-python 默认把视频压成 BGR 3 通道，4 通道只在极少数编码下出现")
except Exception as e:
    print("[cv2] FAILED:", type(e).__name__, e)
