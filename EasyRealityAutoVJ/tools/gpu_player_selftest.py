"""GPU 解码播放器自测（集成后）

验证 6 件事：
  1. **解码线程（非主线程）里能否创建 GL 上下文并解码** —— 集成后的关键假设
  2. GpuDxvPlayer 对各类素材的路径判定（GPU / 降级）是否符合预期
  3. GPU 路径能连续出帧、尺寸/格式正确、内容非全黑
  4. QImage 格式与「现状是否输出 alpha 通道」一致（ARGB32 / RGB32）
  5. 分流：只有**普查出是 DXV** 且开关打开才接管，其余维持原路径
  6. GPU vs 软解的 CPU/帧（交替多轮取中位数 —— process_time 只有 15.6ms 粒度）

用法：venv\\Scripts\\python.exe tools\\gpu_player_selftest.py
"""

import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np  # noqa: E402

import media_manager as mm  # noqa: E402

LIB = r"Y:\EasyRealityAutoVJ\素材库"
OUT = []
FAIL = []


def w(s=""):
    print(s, flush=True)
    OUT.append(s)


def chk(cond, msg):
    w("    [%s] %s" % ("OK  " if cond else "FAIL", msg))
    if not cond:
        FAIL.append(msg)


def qnp(img):
    ww, hh = img.width(), img.height()
    bpl = img.bytesPerLine()
    arr = np.frombuffer(img.bits(), dtype=np.uint8, count=bpl * hh).reshape(hh, bpl)
    return arr[:, :ww * 4].reshape(hh, ww, 4).copy()


def find_samples():
    import av
    import dxvnative
    out = {"alpha_hd": None, "dxt5_hd": None, "dxt1_hd": None,
           "unsupported": None, "nondxv": None}
    for f in sorted(os.listdir(LIB)):
        if not f.lower().endswith((".mov", ".mp4", ".mkv", ".webm", ".avi")):
            continue
        p = os.path.join(LIB, f)
        try:
            c = av.open(p, metadata_errors="replace")
            try:
                st = c.streams.video[0]
                codec = st.codec_context.name
                ww, hh = int(st.width), int(st.height)
                info = None
                if codec == "dxv":
                    try:
                        pk = next(c.demux(st))
                        hd = dxvnative.parse_header(bytes(pk))
                        info = None if hd is None else hd[0]
                    except Exception:
                        info = "?"
            finally:
                c.close()
        except Exception:
            continue
        px = ww * hh
        hd_ok = 1.9e6 < px < 2.3e6
        # 真带 alpha 的样本：缩略图是 PNG（alpha 校正后 PNG ⇔ 真带 alpha，零成本判断）
        if (hd_ok and out["alpha_hd"] is None and codec == "dxv" and info == 5
                and os.path.exists(mm.MediaItem(p)._thumb_path(".png"))):
            out["alpha_hd"] = (p, ww, hh)
        if codec != "dxv":
            if out["nondxv"] is None:
                out["nondxv"] = (p, ww, hh)
        elif info == 5 and hd_ok and out["dxt5_hd"] is None:
            out["dxt5_hd"] = (p, ww, hh)
        elif info == 1 and hd_ok and out["dxt1_hd"] is None:
            out["dxt1_hd"] = (p, ww, hh)
        elif info is None and out["unsupported"] is None:
            out["unsupported"] = (p, ww, hh)
        if all(v is not None for v in out.values()):
            break
    return out


# ---------------------------------------------------------------- 1. 解码线程 GL
def t_thread_gl():
    w("=" * 92)
    w("1) 从非主线程启动 GL 工作线程并建唯一上下文（集成后的关键假设）")
    w("=" * 92)
    w("    主线程名 = %s" % threading.current_thread().name)
    res = {}

    def job():
        res["thread"] = threading.current_thread().name
        try:
            import glctx
            res["preinit"] = glctx.preinit()
            wk = glctx.GLWorker.get()          # 现在由 GLWorker 持有唯一上下文
            res["ok"], res["err"] = wk.ensure(), wk.last_error
        except Exception as e:                                        # noqa: BLE001
            res["ok"], res["err"] = False, str(e)[:120]

    th = threading.Thread(target=job, name="selftest-decode")
    th.start()
    th.join()
    w("    启动线程名 = %s   preinit=%s" % (res.get("thread"), res.get("preinit")))
    chk(res.get("ok"), "工作线程启动并建唯一 GL 上下文成功（失败原因：%s）" % res.get("err"))


# ---------------------------------------------------------------- 2/3/4/6. 播放
def run_player(path, label, secs=2.5, expect_gpu=None, out_alpha=True, force_soft=False):
    import av
    mm.set_gpu_decode(True)
    if force_soft:
        mm._GPU_DISABLED = "selftest: forced software"
    try:
        p = mm.GpuDxvPlayer(path, av, out_alpha=out_alpha)
        p.set_active(True)
        t0, c0 = time.perf_counter(), time.process_time()
        last, frames, nonblack, alpha_ok = None, 0, 0, 0
        sizes = {}
        while time.perf_counter() - t0 < secs:
            img = p.current()
            if img is not None and img is not last:
                last = img
                frames += 1
                sizes[(img.width(), img.height())] = sizes.get((img.width(), img.height()), 0) + 1
                if img.hasAlphaChannel() == out_alpha:
                    alpha_ok += 1
                if qnp(img)[:, :, :3].std() > 3.0:
                    nonblack += 1
            time.sleep(0.004)
        wall, cpu = time.perf_counter() - t0, time.process_time() - c0
        p.close()
        time.sleep(0.25)
    finally:
        if force_soft:
            mm._GPU_DISABLED = ""
    w("  %-24s gpu=%-5s 帧 %4d/%d  尺寸 %s  CPU %6.3f s  墙钟 %.2f s"
      % (label, p.gpu, frames, nonblack, list(sizes.keys())[:2], cpu, wall))
    w("        reason: %s" % p.reason)
    if expect_gpu is not None:
        chk(p.gpu == expect_gpu, "%s：gpu 期望 %s，实际 %s（%s）"
            % (label, expect_gpu, p.gpu, p.reason))
    chk(frames > int(secs * 8), "%s：有连续出帧（%d 帧）" % (label, frames))
    if frames:
        chk(nonblack >= frames * 0.8, "%s：画面非全黑（%d/%d）" % (label, nonblack, frames))
        chk(alpha_ok == frames, "%s：QImage 格式与现状一致（hasAlphaChannel=%s）"
            % (label, out_alpha))
    return p.gpu


def compare_cpu(path, label, secs=3.0, rounds=3, soft="alpha"):
    """同一素材交替多轮取中位数。soft='alpha' → 对照 AvAlphaPlayer（PyAV）；
    soft='cv2' → 对照 VideoPlayer（OpenCV，不带 alpha 素材的现状路径）；
    soft='gpupath' → 对照 GpuDxvPlayer 自己的软解路径。"""
    import av
    cpu = {"gpu": [], "soft": []}
    for r in range(rounds):
        for tag in (("gpu", "soft") if r % 2 == 0 else ("soft", "gpu")):
            if tag == "gpu":
                p = mm.GpuDxvPlayer(path, av, out_alpha=True)
            elif soft == "alpha":
                p = mm.AvAlphaPlayer(path, av)
            elif soft == "cv2":
                p = mm.VideoPlayer(path)
            else:
                mm._GPU_DISABLED = "selftest: forced software"
                p = mm.GpuDxvPlayer(path, av, out_alpha=True)
                mm._GPU_DISABLED = ""
            p.set_active(True)
            # ⚠ 先等它开容器 / 建 GL 上下文 / 编译着色器 / 出首帧，只测**稳态** CPU。
            # 不预热的话首轮会把冷启动成本算进去 —— 同一条 DXT1 素材因此量出过
            # “多花 46%”和“省 15%”两个互相矛盾的结论。
            time.sleep(1.5)
            t0, c0 = time.perf_counter(), time.process_time()
            while time.perf_counter() - t0 < secs:
                time.sleep(0.004)
            cpu[tag].append((time.process_time() - c0) / secs)
            p.close()
            time.sleep(0.3)
    mg, ms = float(np.median(cpu["gpu"])), float(np.median(cpu["soft"]))
    w("  第 %d 轮交替：GPU %s vs 软解 %s  （核当量）"
      % (rounds, ["%.3f" % x for x in cpu["gpu"]], ["%.3f" % x for x in cpu["soft"]]))
    if ms <= 0.001:
        w("  ★ %s：软解对照为 0（该素材现状路径根本打不开，见报告），本次不作结论" % label)
        return mg, ms
    w("  ★ %s：GPU %.3f vs 软解 %.3f → %s %.1f%%"
      % (label, mg, ms, "省" if mg < ms else "多花", abs(1 - mg / ms) * 100))
    return mg, ms


def t_shunt():
    w("")
    w("=" * 92)
    w("5) 分流：只有「普查出是 DXV」+ 开关打开才接管")
    w("=" * 92)

    class FakeItem:
        def __init__(self, is_dxv, has_alpha=True):
            self.path = r"X:\nonexist.mov"
            self._has_alpha = has_alpha
            self._is_dxv = is_dxv
            self.kind = "video"

    mm.set_gpu_decode(True)
    cases = [("DXV + 带 alpha", FakeItem(True, True), "GpuDxvPlayer"),
             ("DXV + 不带 alpha", FakeItem(True, False), "GpuDxvPlayer"),
             ("非 DXV（mjpeg）", FakeItem(False, False), "VideoPlayer"),
             ("还没普查（None）", FakeItem(None, True), "AvAlphaPlayer")]
    for name, item, want in cases:
        pl = mm.create_video_player(item)
        got = type(pl).__name__
        chk(got == want, "%-18s → %s（期望 %s）" % (name, got, want))
        pl.close()
    mm.set_gpu_decode(False)
    pl = mm.create_video_player(FakeItem(True, True))
    chk(type(pl).__name__ == "AvAlphaPlayer", "开关关闭 → AvAlphaPlayer（不改原有行为）")
    pl.close()
    mm.set_gpu_decode(True)
    time.sleep(0.3)


def t_real_items(samples):
    """用真实 MediaItem 走一遍完整分流（含后台普查）。"""
    w("")
    w("=" * 92)
    w("5b) 真实素材分流（后台普查 → 再取播放器）")
    w("=" * 92)
    mm.set_gpu_decode(True)
    for key in ("dxt5_hd", "dxt1_hd", "unsupported", "nondxv"):
        s = samples.get(key)
        if not s:
            continue
        it = mm.MediaItem(s[0])
        if getattr(it, "_is_dxv", None) is None:
            c = None
            import av
            try:
                c = av.open(it.path, metadata_errors="replace")
                it._codec = c.streams.video[0].codec_context.name
            finally:
                if c is not None:
                    c.close()
            it._is_dxv = (it._codec == "dxv")
        pl = mm.create_video_player(it)
        w("    %-14s codec=%-6s is_dxv=%-5s has_alpha=%-5s → %s"
          % (key, it._codec, it._is_dxv, it._has_alpha, type(pl).__name__))
        pl.close()
        time.sleep(0.2)


def main():
    mm.gpu_status()
    samples = find_samples()
    w("=" * 92)
    w("GPU 解码播放器自测   %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    w("=" * 92)
    ok, msg = mm.gpu_status()
    w("gpu_status = %s / %s" % (ok, msg))
    w("环境：AUTO_VJ_GPU=%r  AUTO_VJ_GPU_DXT1_MIN_PX=%r"
      % (os.environ.get("AUTO_VJ_GPU"), os.environ.get("AUTO_VJ_GPU_DXT1_MIN_PX")))
    w("")
    for k, v in samples.items():
        w("  样本 %-12s %s" % (k, ("%s  %dx%d" % (os.path.basename(v[0]), v[1], v[2]))
                              if v else "（没找到）"))
    w("")

    t_thread_gl()

    w("")
    w("=" * 92)
    w("2/3/4) 路径判定、出帧与 QImage 格式")
    w("=" * 92)
    # ⚠ 2026-09-26：`dxt1_hd` 的期望从 False 改成 True —— 门槛从 3.0 Mpx 降到 1.0 Mpx 后，
    # 1080p 的 DXT1 **本来就该走 GPU**（重测：GPU 0.185~0.229 核 vs 软解 0.567~0.755 核，省 63~70%）。
    for key, exp in (("alpha_hd", True), ("dxt5_hd", True), ("dxt1_hd", True),
                     ("unsupported", False)):
        s = samples.get(key)
        if not s:
            continue
        ha = os.path.exists(mm.MediaItem(s[0])._thumb_path(".png"))   # 真实判定（看缩略图）
        run_player(s[0], "%s(alpha=%s)" % (key, ha), expect_gpu=exp, out_alpha=ha)

    w("")
    w("=" * 92)
    w("6) CPU 对比（同素材交替 3 轮取中位数）")
    w("=" * 92)
    if samples.get("dxt5_hd"):
        compare_cpu(samples["dxt5_hd"][0], "DXT5-1080p（对照现状 cv2）", soft="cv2")
    if samples.get("alpha_hd"):
        compare_cpu(samples["alpha_hd"][0], "DXT5-1080p 真带 alpha（对照 PyAV）", soft="alpha")
    if samples.get("dxt1_hd"):
        compare_cpu(samples["dxt1_hd"][0], "DXT1-1080p（强制 GPU vs 软解）", soft="gpupath")

    t_shunt()
    t_real_items(samples)

    with open(os.path.join(ROOT, "_gpu_player_selftest.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))
    w("")
    w("=" * 92)
    w("结果：%s" % ("全部通过" if not FAIL else "有 %d 项失败" % len(FAIL)))
    for f in FAIL:
        w("  ★ %s" % f)
    w("→ 已写入 _gpu_player_selftest.txt")


if __name__ == "__main__":
    main()
