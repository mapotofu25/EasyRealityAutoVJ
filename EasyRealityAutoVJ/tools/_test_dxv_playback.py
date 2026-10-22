# -*- coding: utf-8 -*-
"""验收测试：素材库里**所有 DXV** 都必须能出帧（用户"切几次突然一个素材就卡住"的真凶）。

背景（2026-10-02）：DXV 的 **YUV 变体**（`yuv420p`/`yuva420p`，`parse_header → None`）被 GPU
格式闸门拒绝后走软解，而 PyAV 18.1.0 **无法把这类帧转成 numpy**（`to_ndarray`/`reformat`
全部 OSError -129）⇒ `GpuDxvPlayer._soft_frames` 永远出不了帧（且因 `got` 计数过早，
退避/收工判据失效 ⇒ 无限忙等，画面冻住）。修复：`_open` 检测到 `pix_fmt` 以 `yuv` 开头
时，软解改用 OpenCV（`_cv_frames`）。

本测试**只读**扫描素材库，对每个 DXV 用**真实播放器**跑 `SECS` 秒，断言 `_frames > 0`。

用法：`QT_QPA_PLATFORM=offscreen venv/Scripts/python.exe -u tools/_test_dxv_playback.py [SECS]`
退出码 0 = 所有 DXV 都出帧；1 = 有 DXV 出不了帧（打印失败清单）。
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

import av                                                          # noqa: E402
import media_manager as mm                                          # noqa: E402

LIB = r"Y:\EasyRealityAutoVJ\素材库"
SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
# 必须包含的三个（修复前必失败）
MUST = {"dusk oct ring.mov", "4 (2).mov", "30.mov"}


def list_movs(root):
    out = []
    for d, _sub, files in os.walk(root):
        for f in files:
            if f.lower().endswith(".mov"):
                out.append(os.path.join(d, f))
    return sorted(out)


def classify(path):
    """返回 (codec, cls, note)。cls ∈ {DXT1, DXT5, None}。"""
    try:
        c = av.open(path, metadata_errors="replace")
    except Exception as e:                                          # noqa: BLE001
        return ("", "?", "打开失败: %s" % str(e)[:50])
    try:
        st = c.streams.video[0]
        codec = st.codec_context.name
        pix = str(st.codec_context.pix_fmt or "")
        if codec != "dxv":
            return (codec, "-", "非 DXV (%s)" % codec)
        pkt = None
        for p in c.demux(st):
            if p.size:
                pkt = bytes(p)
                break
        hd = None
        try:
            import dxvnative
            hd = dxvnative.parse_header(pkt) if pkt else None
        except Exception:                                          # noqa: BLE001
            hd = None
        if hd is None:
            return (codec, "None", "YUV/不支持(yuv或LZF) pix=%s" % pix)
        fmt = int(hd[0])
        return (codec, ("DXT1" if fmt == 1 else "DXT5" if fmt == 5 else "BC%d" % fmt), "pix=%s" % pix)
    finally:
        try:
            c.close()
        except Exception:
            pass


def backend(p):
    if getattr(p, "gpu", False):
        return "GPU"
    return "软解-OpenCV" if getattr(p, "_soft_cv", False) else "软解-PyAV"


def run_player(path, secs):
    """真实播放器跑 secs 秒；返回 (frames, backend, reason, err)。"""
    try:
        p = mm.GpuDxvPlayer(path, av)
    except Exception as e:                                          # noqa: BLE001
        return (0, "-", "", "构造失败: %s" % str(e)[:60])
    try:
        p.set_max_size(2880, 1620)
        p.set_active(True)
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < secs:
            time.sleep(0.1)
        return (int(getattr(p, "_frames", 0)), backend(p),
                str(getattr(p, "reason", "")), "")
    finally:
        try:
            p.close()
        except Exception:
            pass
        time.sleep(0.08)                    # 让解码线程收尾退出


def main():
    if not os.path.isdir(LIB):
        print("!! 素材库不存在: %s" % LIB)
        return 2
    movs = list_movs(LIB)
    print("扫描 %d 个 .mov（%s，每个跑 %.1fs）…" % (len(movs), LIB, SECS))
    rows = []
    fails = []
    n_dxv = 0
    for i, path in enumerate(movs):
        codec, cls, note = classify(path)
        if codec != "dxv":
            continue
        n_dxv += 1
        frames, be, reason, err = run_player(path, SECS)
        name = os.path.basename(path)
        ok = frames > 0
        rows.append((name, cls, be, frames, reason, note, err, ok))
        if not ok:
            fails.append(name)
        print("  [%3d] %-26s %-5s %-11s frames=%-4d %s%s"
              % (n_dxv, name[:26], cls, be, frames, note,
                 ("  ERR=%s" % err) if err else ""))
    print()
    print("=" * 96)
    print("DXV 分类表（%d 个）：文件 | 分类 | 后端 | %.0fs出帧 | 说明 | 结果" % (n_dxv, SECS))
    print("-" * 96)
    for name, cls, be, frames, reason, note, err, ok in rows:
        print("  %-26s | %-5s | %-11s | %-5d | %-22s | %s"
              % (name[:26], cls, be, frames, note[:22], "OK" if ok else "!! 无帧"))
    print("=" * 96)
    # 三个必过项
    must_missing = [m for m in MUST if m not in {r[0] for r in rows}]
    must_bad = [m for m in MUST if m in fails]
    if must_missing:
        print("!! 必测项在素材库中找不到：%s" % must_missing)
    print("失败清单（%d）：%s" % (len(fails), fails))
    bad = bool(fails) or bool(must_bad) or bool(must_missing)
    print("结果：%s" % ("全部 DXV 均出帧 ✓" if not bad else "存在出不了帧的 DXV ✗"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
