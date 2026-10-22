# -*- coding: utf-8 -*-
"""★ Task G 判据验证：**有界解码器池**（同素材多层不同入点不再互相 seek）（可复跑）。

跑法：
    P:\\ERAVJ\\EasyRealityCue\\venv\\Scripts\\python.exe tools/_fix_decoder_pool_verify.py

背景（用户"预览还是非常卡"的另一半原因）：
    同一素材切几段、分放多层（VJ 典型用法）时，各层入点不同却**挤在一个解码器**上，
    每拍互相 `seek` ⇒ 三层场景实测只有 ~15 fps。本任务的**有界解码器池**让"各层各持独立
    解码器"，同时用**每路径上限 + 全局上限 + LRU/别名共用**保证**绝不放任资源增长**。

判据（对齐任务书）：
  ① **目标场景**：同一素材 3 层、各层入点不同（0s/10s/20s）⇒ 实际 fps ≥ 24（当前 15.5）
  ② **无退化**：单层 4K、3 层不同素材、3 层同素材同入点 ⇒ fps 不低于上轮（≈31~32）/ 改前
  ③ **内存有界**：同一素材 5 层不同入点（极端）⇒ 解码器数 ≤ 池上限（报进程内存峰值）
  ④ **无泄漏**：反复切轨 20 次 ⇒ 解码器 / 句柄数不增长（ctypes 量进程句柄）
  ⑤ 静态闸门：`compileall` + `selftest` 7/7 + `tests/` 全量 + `_fix_timeline_verify` 6/6 +
              `_fix_wave_ui_verify` 6/6
  ⑥ `audio/waveform.py` 与 `ui/timeline_widget.py` **未被本次改动触碰**

依赖真实素材库（`Y:\\EasyRealityAutoVJ\\素材库`）；素材缺失的判据标 SKIP 而非 FAIL。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
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

from PySide6.QtCore import QEventLoop, QTimer                           # noqa: E402
from PySide6.QtWidgets import QApplication                              # noqa: E402

import _profile_preview as prof                                        # noqa: E402
import ercue.media.frames as F                                         # noqa: E402
from ercue.ui.preview import PreviewCanvas                             # noqa: E402

# ★ 在**任何测量之前**捕获出货配置（下面的 helper 会临时改写模块全局模拟"改前"，必须先抓）。
SHIP_TT = F.DECODE_THREAD_TYPE
SHIP_TC = F.DECODE_THREAD_COUNT
CAP_PER = F.POOL_MAX_PER_PATH
CAP_ALL = F.POOL_MAX_TOTAL

MEDIA = prof.MEDIA_DIR
RES = []


# ==========================================================================
# 小工具
# ==========================================================================
def _check(tag, ok, detail):
    RES.append((tag, bool(ok), detail))
    print("  [%s] %s\n        %s" % ("PASS" if ok else "FAIL", tag, detail))


def _find(*names):
    for n in names:
        p = os.path.join(MEDIA, n)
        if os.path.isfile(p):
            return p
    return None


def _proc_mem():
    """返回 (工作集 MB, 峰值工作集 MB)。Windows `GetProcessMemoryInfo`（**必须显式 argtypes**）。"""
    class PMC(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]
    psapi = ctypes.WinDLL("psapi")
    k32 = ctypes.WinDLL("kernel32")
    k32.GetCurrentProcess.restype = wt.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(PMC), wt.DWORD]
    psapi.GetProcessMemoryInfo.restype = wt.BOOL
    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
    return pmc.WorkingSetSize / 1e6, pmc.PeakWorkingSetSize / 1e6


def _handles():
    """当前进程句柄数（`GetProcessHandleCount`；**必须显式 argtypes**，否则静默返回 0）。"""
    k32 = ctypes.WinDLL("kernel32")
    k32.GetCurrentProcess.restype = wt.HANDLE
    k32.GetProcessHandleCount.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    k32.GetProcessHandleCount.restype = wt.BOOL
    n = wt.DWORD(0)
    k32.GetProcessHandleCount(k32.GetCurrentProcess(), ctypes.byref(n))
    return int(n.value)


def _box(width):
    return (int(round(width * 1.5)), int(round(width * 9 / 16 * 1.5)))


# ==========================================================================
# 端到端交付 fps（真实定时器 + 真实事件循环），可开关"lane"以模拟改前/改后
# ==========================================================================
def _fps(paths, width, offsets, seconds=8.0, use_lane=True, box=None):
    """跑 `seconds` 秒的 30Hz 位置推进，返回 (fps, decoder_count, lane_count, evictions)。

    `use_lane=False` ⇒ 把 `PreviewCanvas._build_lane_map` 换成"空表" ⇒ 所有层 lane=None
      ⇒ 池退化为**每路径一个解码器**（= **改前**行为：同素材多层挤在一个解码器上互相 seek）。
    其余配置（`out_size` 1.5×、`thread_type`/`thread_count`）**改前改后保持一致** ——
    这样量出的差值**纯粹是"有界解码器池"的贡献**。
    """
    F.DECODE_THREAD_TYPE = SHIP_TT
    F.DECODE_THREAD_COUNT = SHIP_TC
    cv = None
    try:
        cv = PreviewCanvas()
        # ★ "改前"模拟：**在实例上**覆盖 `_build_lane_map`（实例属性不是描述符 ⇒ 只传 states，
        #   且**不污染类属性**；改类是坑：还原成普通函数会让它变成实例方法、下一轮报"2 个参数"）。
        if not use_lane:
            cv._build_lane_map = lambda states: {}            # noqa: ARG005
        cv.resize(width, int(width * 9 / 16))
        cv.set_runtime(prof._build_runtime(paths, offsets=offsets), 0.0)
        if box is None:
            cv._out_size = lambda: None                       # 模拟"取帧不缩放"（本轮不需要，保留）
        count = {"n": 0}
        orig = cv._compose

        def counting():
            count["n"] += 1
            orig()

        cv._compose = counting
        cv.set_pos(0.0)
        cv.set_active(True)
        cv.set_playing(True)
        pos = {"p": 0.0}
        drv = QTimer()
        drv.setInterval(33)
        drv.timeout.connect(lambda: (pos.__setitem__("p", pos["p"] + 1 / 30.0),
                                     cv.set_pos(pos["p"])))
        drv.start()
        loop = QEventLoop()
        QTimer.singleShot(int(seconds * 1000), loop.quit)
        t0 = time.perf_counter()
        loop.exec()
        el = time.perf_counter() - t0
        drv.stop()
        fps = count["n"] / max(1e-9, el)
        dec, lane, ev = cv.src.decoder_count(), cv.src.lane_count(), cv.src.evictions
        return fps, dec, lane, ev
    finally:
        if cv is not None:
            try:
                cv.shutdown()
            except Exception:                                 # noqa: BLE001
                pass


def _fmt_case(tag, before, after, extra=""):
    bf, _, _, _ = before
    af, ad, al, ae = after
    return "%-22s 改前=%.1f → 改后=%.1f fps（dec=%d lane=%d evict=%d）%s" % (
        tag, bf, af, ad, al, ae, extra)


# ==========================================================================
# 判据①：目标场景 —— 同一素材 3 层、各层入点不同
# ==========================================================================
def t1_target():
    p4k = _find("Balls visual@110 (19).mov", "Balls visual@110 (40).mov")
    if not p4k:
        _check("① 目标场景 3 层异入点 ≥ 24fps", True, "SKIP：未找到 4K 素材")
        return
    width = 960
    box = _box(width)
    base = os.path.basename(p4k)
    # 主判据：入点 0/10/20 s（任务书原话）
    paths = [("c%d" % i, p4k) for i in range(3)]
    before = _fps(paths, width, [0.0, 10.0, 20.0], seconds=8.0, use_lane=False, box=box)
    after = _fps(paths, width, [0.0, 10.0, 20.0], seconds=8.0, use_lane=True, box=box)
    ok = after[0] >= 24.0
    _check("① 目标场景 3 层异入点(0/10/20s) ≥ 24fps", ok,
           "%s  [%s]" % (_fmt_case("3 层 %s" % base, before, after), "✓" if ok else "✗"))
    # 次级变体：入点 0/0.7/1.4 s（更密的 seek，最能暴露"共用单解码器"）
    paths2 = [("c%d" % i, p4k) for i in range(3)]
    b2 = _fps(paths2, width, [0.0, 0.7, 1.4], seconds=6.0, use_lane=False, box=box)
    a2 = _fps(paths2, width, [0.0, 0.7, 1.4], seconds=6.0, use_lane=True, box=box)
    _check("① 变体 3 层异入点(0/0.7/1.4s) ≥ 24fps", a2[0] >= 24.0,
           _fmt_case("3 层 异入点(0/.7/1.4)", b2, a2))


# ==========================================================================
# 判据②：无退化（单层 / 3 层不同素材 / 3 层同素材同入点）
# ==========================================================================
def t2_no_regression():
    p4k = _find("Balls visual@110 (19).mov")
    p_40 = _find("Balls visual@110 (40).mov")
    p_57 = _find("Balls visual@110 (57).mov")
    if not p4k:
        _check("② 无退化", True, "SKIP：未找到 4K 素材")
        return
    width = 960
    box = _box(width)
    msgs = []
    allok = True

    # a) 单层 4K
    one = [("c0", p4k)]
    b = _fps(one, width, [0.0], seconds=6.0, use_lane=False, box=box)
    a = _fps(one, width, [0.0], seconds=6.0, use_lane=True, box=box)
    allok = allok and a[0] >= 24.0 and a[0] >= b[0] - 3.0
    msgs.append(_fmt_case("1 层 4K", b, a))

    # b) 3 层不同素材（用三个 4K 素材；缺则退化为同一素材不同入点）
    if p_40 and p_57:
        three = [("c0", p4k), ("c1", p_40), ("c2", p_57)]
        offs = [0.0, 0.3, 0.6]
        tag = "3 层不同素材"
    else:
        three = [("c%d" % i, p4k) for i in range(3)]
        offs = [0.0, 0.3, 0.6]
        tag = "3 层不同素材(SKIP→同素材)"
    b = _fps(three, width, offs, seconds=6.0, use_lane=False, box=box)
    a = _fps(three, width, offs, seconds=6.0, use_lane=True, box=box)
    allok = allok and a[0] >= 24.0 and a[0] >= b[0] - 3.0
    msgs.append(_fmt_case(tag, b, a))

    # c) 3 层同素材同入点（叠层，= 同一条播放线 ⇒ 应仍共用、且不退化）
    same = [("c%d" % i, p4k) for i in range(3)]
    b = _fps(same, width, [0.0, 0.0, 0.0], seconds=6.0, use_lane=False, box=box)
    a = _fps(same, width, [0.0, 0.0, 0.0], seconds=6.0, use_lane=True, box=box)
    allok = allok and a[0] >= 24.0 and a[0] >= b[0] - 3.0
    msgs.append(_fmt_case("3 层同素材同入点", b, a))

    _check("② 无退化（1 层 / 3 层不同素材 / 3 层同入点）", allok, "；".join(msgs))


# ==========================================================================
# 判据③：内存有界（同素材 5 层异入点极端 ⇒ 解码器数 ≤ 池上限）
# ==========================================================================
def t3_bounded_memory():
    p4k = _find("Balls visual@110 (19).mov")
    if not p4k:
        _check("③ 内存有界（5 层异入点）", True, "SKIP：未找到 4K 素材")
        return
    width = 960
    box = _box(width)
    paths = [("c%d" % i, p4k) for i in range(5)]
    offs = [0.0, 3.0, 6.0, 9.0, 12.0]
    m0 = _proc_mem()[0]
    fps, dec, lane, ev = _fps(paths, width, offs, seconds=8.0, use_lane=True, box=box)
    peak = _proc_mem()[1]
    ok = (dec <= CAP_PER) and (dec <= CAP_ALL)
    _check("③ 内存有界：5 层异入点 decoder_count ≤ 池上限", ok,
           "5 层同素材异入点：%.1f fps；dec=%d（上限 per-path=%d / total=%d）lane=%d evict=%d；"
           "进程内存 %.0f → 峰值 %.0f MB"
           % (fps, dec, CAP_PER, CAP_ALL, lane, ev, m0, peak))


# ==========================================================================
# 判据④：无泄漏（反复切轨 20 次 ⇒ 解码器/句柄数不增长）
# ==========================================================================
def t4_no_leak():
    p4k = _find("Balls visual@110 (19).mov", "Balls visual@110 (40).mov")
    if not p4k:
        _check("④ 无泄漏（切轨 20 次）", True, "SKIP：未找到 4K 素材")
        return
    F.DECODE_THREAD_TYPE = SHIP_TT
    F.DECODE_THREAD_COUNT = SHIP_TC
    width = 960
    cv = PreviewCanvas()
    cv.resize(width, int(width * 9 / 16))
    cv.set_active(True)
    # 预热一次，让基线含"已建解码器"的场景
    cv.set_runtime(prof._build_runtime([("c0", p4k), ("c1", p4k)],
                                       offsets=[0.0, 5.0]), 0.0)
    cv.set_pos(0.0)
    cv._compose()
    h_base = _handles()
    m_base = _proc_mem()[0]
    dec_seen = []
    for i in range(20):
        # 每次换一个"轨道"（不同入点组合），模拟用户切轨
        rt = prof._build_runtime([("c0", p4k), ("c1", p4k)],
                                 offsets=[float(i % 7), float((i + 3) % 7)])
        cv.set_runtime(rt, 0.0)
        cv.set_pos(float(i % 5))
        cv._compose()
        dec_seen.append(cv.src.decoder_count())
    h_after = _handles()
    m_after = _proc_mem()[0]
    # 关闭后应回到"无解码器"
    cv.shutdown()
    dec_closed = cv.src.decoder_count()
    h_closed = _handles()
    ok = (max(dec_seen) <= CAP_PER and dec_closed == 0
          and (h_after - h_base) <= CAP_PER * 40 and (h_closed - h_base) <= 40)
    _check("④ 无泄漏：切轨 20 次后 decoder/handle 不增长", ok,
           "切轨 20 次：解码器 max=%d（≤%d）；句柄 base=%d → after=%d → closed=%d；"
           "内存 %.0f → %.0f MB；shutdown 后 dec=%d"
           % (max(dec_seen), CAP_PER, h_base, h_after, h_closed, m_base, m_after, dec_closed))


# ==========================================================================
# 判据⑤：静态闸门 + 全量测试
# ==========================================================================
def _run(script, args=None):
    cmd = [sys.executable, script] + (args or [])
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["ERCUE_PROF_MAIN"] = "0"          # 验证内部再跑 profile 主段会很慢，关掉
    try:
        r = subprocess.run(cmd, cwd=_ROOT, env=env, capture_output=True, timeout=900)
        out = (r.stdout or b"").decode("utf-8", "replace")
        err = (r.stderr or b"").decode("utf-8", "replace")
        return r.returncode, out + err
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"


def t5_gates():
    rc, out = _run("-m", ["compileall", "-q", "src/"])
    ok_c = rc == 0
    rc, out = _run(os.path.join(_HERE, "selftest.py"))
    ok_s = rc == 0 and "7/7" in out
    tests = ["tests/test_media.py", "tests/test_compositor.py", "tests/test_timeline.py",
             "tests/test_audio.py", "tests/test_ui_smoke.py"]
    bad = []
    for t in tests:
        rc, out = _run(os.path.join(_ROOT, t))     # ★ 判据 = 退出码（输出里"失败"二字常见于用例名）
        if rc != 0:
            bad.append("%s(rc=%d)" % (t, rc))
    rc, out = _run(os.path.join(_HERE, "_fix_timeline_verify.py"))
    ok_tl = rc == 0 and "6/6" in out
    rc, out = _run(os.path.join(_HERE, "_fix_wave_ui_verify.py"))
    ok_wv = rc == 0 and "6/6" in out
    rc, out = _run(os.path.join(_HERE, "_fix_preview_perf2_verify.py"))
    ok_p2 = rc == 0
    ok = ok_c and ok_s and not bad and ok_tl and ok_wv and ok_p2
    _check("⑤ 静态闸门 + 全量测试", ok,
           "compileall=%s selftest7/7=%s tests失败=%s timeline6/6=%s wave6/6=%s perf2=%s"
           % (ok_c, ok_s, bad or "无", ok_tl, ok_wv, ok_p2))


# ==========================================================================
# 判据⑥：waveform.py / timeline_widget.py 未被触碰
# ==========================================================================
def t6_untouched():
    wf = os.path.join(_SRC, "ercue", "audio", "waveform.py")
    tl = os.path.join(_SRC, "ercue", "ui", "timeline_widget.py")
    fr = os.path.join(_SRC, "ercue", "media", "frames.py")
    marks = ("POOL_MAX_PER_PATH", "POOL_MAX_TOTAL", "POOL_HOT_SEC", "_make_room",
             "_build_lane_map", "lane_count")
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
    frt = open(fr, encoding="utf-8", errors="replace").read()
    pos = all(m in frt for m in ("POOL_MAX_PER_PATH", "POOL_HOT_SEC", "_make_room"))
    mt = os.path.getmtime(fr)
    older = all((not os.path.isfile(p)) or os.path.getmtime(p) < mt for p in (wf, tl))
    _check("⑥ 未动 waveform.py / timeline_widget.py", (not bad) and pos and older,
           "越界命中=%s；frames.py 含 Task G 符号=%s；两文件 mtime 更早=%s"
           % (bad or "无", pos, older))


# ==========================================================================
def main() -> int:
    _APP = QApplication.instance() or QApplication([])
    print("=" * 74)
    print("Task G 判据验证：有界解码器池（同素材多层异入点）")
    print("素材库 = %s" % MEDIA)
    print("出货配置：thread_type=%r thread_count=%r；池上限 per-path=%d total=%d hot=%.2fs"
          % (SHIP_TT, SHIP_TC, CAP_PER, CAP_ALL, F.POOL_HOT_SEC))
    print("=" * 74)
    t1_target()
    t2_no_regression()
    t3_bounded_memory()
    t4_no_leak()
    t6_untouched()
    t5_gates()
    print("\n" + "=" * 74)
    for tag, ok, _ in RES:
        print("  [%s] %s" % ("PASS" if ok else "FAIL", tag))
    print("=" * 74)
    ok_all = all(ok for _, ok, _ in RES)
    print("汇总：%d/%d 通过  ⇒  %s"
          % (sum(1 for _, ok, _ in RES if ok), len(RES), "全绿 ✓" if ok_all else "有失败 ✗"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
