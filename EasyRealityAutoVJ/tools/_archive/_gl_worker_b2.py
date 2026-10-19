# -*- coding: utf-8 -*-
"""B2 端到端：同一个 GpuDxvPlayer 因超时降级到软解后，worker 恢复 → **自动切回 GPU**。

验证 team-lead 要求的 B2（长演出里不换素材的图层也能把 GPU 拿回来）：
  1) 初始走 GPU 且正常出帧；
  2) 注入“慢帧”（> timeout）→ 该播放器降级到软解，画面**不断**；
  3) 恢复解码速度后 **同一个播放器**（**不新建**）自动切回 GPU：`.gpu` 变回 True
     且 worker 重新出现成功解码（`n_ok` 增长）——即真的在用 GPU 出帧，不只是标志位；
  4) 全程无卡顿（帧间最大间隔受控）。

用法：venv\\Scripts\\python.exe tools\\_gl_worker_b2.py
"""
import json
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
LIB = r"Y:\EasyRealityAutoVJ\素材库"

OUT = []
FAIL = []


def w(s=""):
    print(s, flush=True)
    OUT.append(str(s))


def chk(cond, msg):
    w("    [%s] %s" % ("OK  " if cond else "FAIL", msg))
    if not cond:
        FAIL.append(msg)
    return cond


def _dxv_files():
    import av
    import dxvnative
    out = []
    for f in sorted(os.listdir(LIB)):
        if not f.lower().endswith((".mov", ".mp4", ".mkv")):
            continue
        p = os.path.join(LIB, f)
        try:
            c = av.open(p, metadata_errors="replace")
            try:
                st = c.streams.video[0]
                if st.codec_context.name != "dxv":
                    continue
                ww, hh = int(st.width), int(st.height)
                pk = next(c.demux(st))
                hd = dxvnative.parse_header(bytes(pk))
                fmt = None if hd is None else int(hd[0])
            finally:
                c.close()
        except Exception:
            continue
        if fmt in (1, 5) and ww * hh >= 1.9e6:
            out.append((p, fmt, ww, hh))
    return out


def main():
    sys.path.insert(0, SRC)
    import glctx
    import media_manager as mm
    import av
    w("=" * 94)
    w("B2：同一播放器 降级 → worker 恢复 → **自动切回 GPU**    %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    w("=" * 94)
    if not glctx.preinit():
        chk(False, "glfw 不可用")
        return
    files = _dxv_files()
    if not files:
        chk(False, "素材库无 DXV")
        return
    path, fmt, ww, hh = files[0]
    w("素材：%s  DXT%d %dx%d" % (os.path.basename(path), 5 if fmt == 5 else 1, ww, hh))

    mm.set_gpu_decode(True)
    mm._GPU_DISABLED = ""
    mm._GPU_STATUS = None
    wk = glctx.GLWorker.get()
    if not chk(wk.ensure(), "worker 就绪"):
        return
    wk.RECOVER_AFTER = 1.5            # 冷却 1.5s（便于验证，默认 60s）
    wk.decode_timeout = 0.4           # 单次等待上限 0.4s
    w("RECOVER_AFTER=%.1fs  decode_timeout=%.1fs  _GPU_REPROBE_SECS=%.1fs"
      % (wk.RECOVER_AFTER, wk.decode_timeout, mm.GpuDxvPlayer._GPU_REPROBE_SECS))

    P = mm.GpuDxvPlayer(path, av, out_alpha=True)
    P.set_active(True)

    # ---- 后台监测：只比对对象身份，记录出帧数与帧间最大间隔 ----
    stop = threading.Event()
    stat = {"n": 0, "max_gap": 0.0}

    def mon():
        prev = None
        lastt = time.perf_counter()
        while not stop.is_set():
            img = P.current()
            if img is not None and img is not prev:
                now = time.perf_counter()
                if prev is not None:
                    stat["max_gap"] = max(stat["max_gap"], now - lastt)
                prev = img
                lastt = now
                stat["n"] += 1
            time.sleep(0.002)

    th = threading.Thread(target=mon, name="b2-mon", daemon=True)
    th.start()

    # ---------- 阶段1：初始 GPU ----------
    time.sleep(2.0)
    c1 = stat["n"]
    f1 = c1 / 2.0
    nok1 = wk.n_ok
    w("\n① 初始 2.0s：%d 帧（%.1ffps）gpu=%s n_ok=%d reason=%s" % (c1, f1, P.gpu, nok1, P.reason))
    chk(P.gpu is True, "① 初始走 GPU")
    chk(c1 > 20, "① 初始正常出帧（%d 帧，%.1ffps）" % (c1, f1))
    chk(nok1 > 20, "① GPU 解码确有成功（n_ok=%d）" % nok1)

    # ---------- 阶段2：注入慢帧 → 降级，画面不断 ----------
    orig = wk._dec.decode

    def slow(*a, **k):
        time.sleep(0.9)
        return orig(*a, **k)

    wk._dec.decode = slow
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 4.0:
        if P.gpu is False:
            break
        time.sleep(0.02)
    dt_deg = time.perf_counter() - t0
    w("\n② 注入慢帧后 %.2fs 内降级：gpu=%s reason=%s" % (dt_deg, P.gpu, P.reason))
    chk(P.gpu is False, "② 该播放器降级到软解")
    chk(dt_deg < 2.0, "② 降级切换很快（%.2fs）" % dt_deg)
    # 冷却期（1.5s）内观察软解出帧，此时不会切回
    c2 = stat["n"]
    time.sleep(0.9)
    c3 = stat["n"]
    soft_frames = c3 - c2
    w("   降级后 0.9s（冷却期内）：%d 帧（%.1ffps）gpu=%s" % (soft_frames, soft_frames / 0.9, P.gpu))
    chk(P.gpu is False, "② 冷却期内仍为软解（gpu=False）")
    chk(soft_frames > 10, "② 降级后画面**不断**（%d 帧，%.1ffps）" % (soft_frames, soft_frames / 0.9))

    # ---------- 阶段3：恢复解码速度 → 同一播放器自动切回 GPU ----------
    wk._dec.decode = orig
    nok_rec0 = wk.n_ok
    t0 = time.perf_counter()
    recovered = False
    while time.perf_counter() - t0 < 10.0:
        if P.gpu is True and (wk.n_ok - nok_rec0) >= 15:
            recovered = True
            break
        time.sleep(0.02)
    dt_rec = time.perf_counter() - t0
    w("\n③ 恢复后 %.2fs：gpu=%s n_ok+%d reason=%s" % (dt_rec, P.gpu, wk.n_ok - nok_rec0, P.reason))
    chk(recovered, "★ ③ **同一个播放器**（未新建）自动切回 GPU，并重新成功解码")
    chk(P.gpu is True, "★ ③ .gpu 变回 True")
    chk(wk.n_ok - nok_rec0 >= 15, "★ ③ worker 重新出现成功解码（n_ok +%d → 真的在用 GPU）" % (wk.n_ok - nok_rec0))
    c4 = stat["n"]
    time.sleep(1.5)
    c5 = stat["n"]
    f_rec = (c5 - c4) / 1.5
    w("   切回后 1.5s：%d 帧（%.1ffps）" % (c5 - c4, f_rec))
    chk((c5 - c4) > 20, "★ ③ 切回后正常出帧（%d 帧，%.1ffps）" % (c5 - c4, f_rec))

    # ---------- 阶段4：worker 状态 + 全程无卡顿 ----------
    stop.set()
    th.join(timeout=1.0)
    w("\n④ worker stats=%s  _degraded=%s  available=%s"
      % (wk.stats(), wk._degraded, wk.available()))
    w("   全程帧间最大间隔 = %.2fs（含降级/切回瞬间）" % stat["max_gap"])
    chk(wk.available() and not wk._degraded, "④ worker 已恢复、未处于降级")
    chk(stat["max_gap"] < 1.5, "④ 全程**无卡顿**（帧间最大间隔 %.2fs < 1.5s）" % stat["max_gap"])
    P.close()

    w("")
    w("=" * 94)
    w("结果：%s" % ("全部通过" if not FAIL else "存在失败 %d 项" % len(FAIL)))
    for f in FAIL:
        w("    · %s" % f)
    w("=" * 94)
    print("##B2_JSON##" + json.dumps(
        {"passed": not FAIL, "n_fail": len(FAIL), "fails": FAIL[:20],
         "f_gpu": round(f1, 1), "f_rec": round(f_rec, 1), "soft_frames": soft_frames,
         "max_gap": round(stat["max_gap"], 3), "dt_deg": round(dt_deg, 2),
         "dt_rec": round(dt_rec, 2)}, ensure_ascii=False), flush=True)
    with open(os.path.join(ROOT, "_gl_worker_b2.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))


if __name__ == "__main__":
    main()
