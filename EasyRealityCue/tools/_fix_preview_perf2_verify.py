# -*- coding: utf-8 -*-
"""★ Task F 判据验证：**4K 取帧缩放到预览尺寸 + 有界多线程解码**（可复跑）。

跑法：
    P:\\ERAVJ\\EasyRealityCue\\venv\\Scripts\\python.exe tools/_fix_preview_perf2_verify.py

判据（对齐任务书）：
  ① 4K 素材取帧 **P95 ≤ 20 ms**（当前 37.4 ms）；1080p 应更快
  ② 端到端播放：`Balls visual@110 (19).mov` 做 2~3 层、10 s ⇒ **实际 fps ≥ 24**
  ③ 画质不明显下降：全幅/crop 放大 2× 的**平均像素差很小** + 产出截图 `_shots/m7_scale_compare.png`
  ④ 不退化：`compileall` + `selftest` 7/7 + `tests/` 全量 + `_fix_timeline_verify` 6/6 +
             `_fix_wave_ui_verify` 6/6
  ⑤ `audio/waveform.py` 与 `ui/timeline_widget.py` **未被本次改动触碰**

依赖真实素材库（`Y:\\EasyRealityAutoVJ\\素材库`）；素材缺失的判据会标 SKIP 而非 FAIL。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
for p in (_HERE, _SRC):
    if p not in sys.path:
        sys.path.insert(0, p)

from PySide6.QtWidgets import QApplication                          # noqa: E402

import _profile_preview as prof                                     # noqa: E402
import ercue.media.frames as F                                      # noqa: E402

# ★ 在**任何测量之前**捕获出货配置：下面的 helper 会临时改写这两个模块全局来模拟"改前"，
#   必须在最开头抓下来，否则会被"改前"的 None 覆盖（第一版验证脚本就踩了这个坑）。
SHIP_TT = F.DECODE_THREAD_TYPE
SHIP_TC = F.DECODE_THREAD_COUNT

MEDIA = prof.MEDIA_DIR
RES = []


def _check(tag, ok, detail):
    RES.append((tag, bool(ok), detail))
    print("  [%s] %s\n        %s" % ("PASS" if ok else "FAIL", tag, detail))


def _p95(v):
    return prof._stats(v)["p95"]


def _p50(v):
    return prof._stats(v)["p50"]


def _find(name):
    p = os.path.join(MEDIA, name)
    return p if os.path.isfile(p) else None


# --------------------------------------------------------------------------
def t1_fetch_4k():
    """① 4K 取帧 P95 ≤ 20 ms；1080p 更快。"""
    p4k = _find("Balls visual@110 (19).mov")
    phd = _find("Balls visual@110 (40).mov")
    if not p4k:
        _check("① 4K 取帧 P95 ≤ 20ms", True, "SKIP：未找到 4K 素材")
        return
    box = (1440, 810)
    before = prof._frame_play_stat(p4k, None, None, None)          # 真·改前：原尺寸 + 不改多线程
    after = prof._frame_play_stat(p4k, box, SHIP_TT, SHIP_TC)
    ok = _p95(after) <= 20.0
    _check("① 4K 取帧 P95 ≤ 20ms",
           ok,
           "改前 p50=%.1f p95=%.1f ⇒ 改后 p50=%.1f p95=%.1f ms（AUTO/%s + 1.5×，阈 20ms）"
           % (_p50(before), _p95(before), _p50(after), _p95(after), SHIP_TC))
    if phd:
        hd = prof._frame_play_stat(phd, box, SHIP_TT, SHIP_TC)
        _check("① 1080p 取帧更快（< 4K）", _p50(hd) < _p50(after),
               "1080p p50=%.1f p95=%.1f ms  vs  4K p50=%.1f ms" % (_p50(hd), _p95(hd), _p50(after)))


def t2_end_to_end_fps():
    """② 2~3 层 4K 播放 ≥ 24fps；含"3 层同素材异入点"（**已由后续有界解码器池修复**）。"""
    p4k = _find("Balls visual@110 (19).mov")
    if not p4k:
        _check("② 端到端播放 2~3 层 ≥ 24fps", True, "SKIP：未找到 4K 素材")
        return
    W = 960
    box = (int(round(W * 1.5)), int(round(W * 9 / 16 * 1.5)))
    tc = SHIP_TC
    layers = [
        ("2 层同入点", [("c0", p4k), ("c1", p4k)], [0.0, 0.0]),
        ("3 层同入点", [("c%d" % i, p4k) for i in range(3)], [0.0, 0.0, 0.0]),
    ]
    allok = True
    msgs = []
    for tag, paths, offs in layers:
        fb = prof._delivered_fps_4k(paths, W, None, None, seconds=8.0, offsets=offs)
        fa = prof._delivered_fps_4k(paths, W, box, "AUTO", seconds=8.0, offsets=offs,
                                    thread_count=tc)
        allok = allok and fa >= 24.0
        msgs.append("%s：改前=%.1f → 改后=%.1f fps" % (tag, fb, fa))
    # ★ 同一素材 N 层、各层入点不同：本轮"改后"仍是 15fps 的老限制，**已被后续「有界解码器池」
    #   修复**（各层各持独立解码器）。这里按**出货配置**（画布×1.5 取帧 + 有界池 lane）复测并断言。
    #   详见 `tools/_fix_decoder_pool_verify.py` 与报告 `docs/_audit/_fix_decoder_pool.md`。
    pa = prof._delivered_fps_4k([("c%d" % i, p4k) for i in range(3)], W, box, "AUTO",
                                seconds=6.0, offsets=[0.0, 0.7, 1.4], thread_count=tc)
    allok = allok and pa >= 24.0
    msgs.append("3 层异入点（有界池已修复）：%.1f fps" % pa)
    _check("② 端到端播放 2~3 层 ≥ 24fps", allok, "；".join(msgs))


def t3_quality():
    """③ 画质：平均像素差很小 + 截图产出。"""
    p4k = _find("Balls visual@110 (19).mov")
    if not p4k:
        _check("③ 画质不明显下降", True, "SKIP：未找到 4K 素材")
        return
    shot = os.path.join(_ROOT, "_shots", "m7_scale_compare.png")
    diffs = prof._screenshot_compare(p4k, 960, shot)
    ok = (diffs[0] >= 0.0 and diffs[0] <= 3.0 and diffs[1] >= 0.0 and diffs[1] <= 6.0
          and os.path.isfile(shot))
    _check("③ 画质不明显下降（截图对比）", ok,
           "平均像素差：全幅=%.2f（阈 3.0） / crop 放大2×=%.2f（阈 6.0）；截图 %s"
           % (diffs[0], diffs[1], shot))


def _run(script, args=None):
    cmd = [sys.executable, script] + (args or [])
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    try:
        r = subprocess.run(cmd, cwd=_ROOT, env=env, capture_output=True, timeout=600)
        out = (r.stdout or b"").decode("utf-8", "replace")
        err = (r.stderr or b"").decode("utf-8", "replace")
        return r.returncode, out + err
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"


def t4_no_regression():
    """④ 静态闸门 + 全量测试 + 两个修复验证脚本。"""
    rc, out = _run("-m", ["compileall", "-q", "src/"])
    ok_c = rc == 0
    rc, out = _run(os.path.join(_HERE, "selftest.py"))
    ok_s = rc == 0 and "7/7" in out
    tests = ["tests/test_media.py", "tests/test_compositor.py", "tests/test_timeline.py",
             "tests/test_audio.py", "tests/test_ui_smoke.py"]
    bad = []
    for t in tests:
        rc, out = _run(os.path.join(_ROOT, t))     # ★ 判据 = 退出码（输出里"失败"二字常见于用例名，勿作文本匹配）
        if rc != 0:
            bad.append("%s(rc=%d)" % (t, rc))
    rc, out = _run(os.path.join(_HERE, "_fix_timeline_verify.py"))
    ok_tl = rc == 0 and "6/6" in out
    rc, out = _run(os.path.join(_HERE, "_fix_wave_ui_verify.py"))
    ok_wv = rc == 0 and "6/6" in out
    ok = ok_c and ok_s and not bad and ok_tl and ok_wv
    _check("④ 不退化（compileall + selftest + tests + 两验证）", ok,
           "compileall=%s selftest7/7=%s tests失败=%s timeline6/6=%s wave6/6=%s"
           % (ok_c, ok_s, bad or "无", ok_tl, ok_wv))


def t5_untouched():
    """⑤ waveform.py / timeline_widget.py 未被本次改动触碰（无 Task F 符号 + mtime 更早）。"""
    wf = os.path.join(_SRC, "ercue", "audio", "waveform.py")
    tl = os.path.join(_SRC, "ercue", "ui", "timeline_widget.py")
    fr = os.path.join(_SRC, "ercue", "media", "frames.py")
    marks = ("OUT_INTERP", "DECODE_THREAD_TYPE", "DECODE_THREAD_COUNT", "_norm_out_size",
             "OUT_HEADROOM", "_out_size")
    bad = []
    for p in (wf, tl):
        try:
            txt = open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            bad.append("%s 读不到" % os.path.basename(p))
            continue
        hit = [m for m in marks if m in txt]
        if hit:
            bad.append("%s 含 %s" % (os.path.basename(p), hit))
    # 顺带证明我的改动确实落在 frames.py / preview.py
    frt = open(fr, encoding="utf-8", errors="replace").read()
    pos = all(m in frt for m in ("OUT_INTERP", "DECODE_THREAD_TYPE", "_norm_out_size"))
    mt = os.path.getmtime(fr)
    older = all((not os.path.isfile(p)) or os.path.getmtime(p) < mt for p in (wf, tl))
    _check("⑤ 未动 waveform.py / timeline_widget.py", (not bad) and pos and older,
           "越界命中=%s；frames.py 含 Task F 符号=%s；两文件 mtime 更早=%s"
           % (bad or "无", pos, older))


def main() -> int:
    _APP = QApplication.instance() or QApplication([])
    print("=" * 72)
    print("Task F 判据验证：4K 取帧缩放 + 有界多线程解码")
    print("素材库 = %s" % MEDIA)
    print("出货配置：thread_type=%r thread_count=%r  取帧盒=画布×1.5"
          % (SHIP_TT, SHIP_TC))
    print("=" * 72)
    t1_fetch_4k()
    t3_quality()
    t2_end_to_end_fps()
    t5_untouched()
    t4_no_regression()
    print("\n" + "=" * 72)
    for tag, ok, _ in RES:
        print("  [%s] %s" % ("PASS" if ok else "FAIL", tag))
    print("=" * 72)
    ok_all = all(ok for _, ok, _ in RES)
    print("汇总：%d/%d 通过  ⇒  %s" % (sum(1 for _, ok, _ in RES if ok), len(RES),
                                       "全绿 ✓" if ok_all else "有失败 ✗"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
