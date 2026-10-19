# -*- coding: utf-8 -*-
"""离线节拍网格 → 实时演出路径 的离线自测（`_` 前缀，已被 .gitignore 忽略）。

覆盖（对应交付要求）：
  1) r 估计        —— RateEstimator：真值 0.90/1.00/1.05/1.10 的收敛速度与精度
  2) 网格时钟精度  —— GridClock：±50ms 识别噪声下 beat_pos 的误差 / 单调性 / 无跳拍（≥60s）
  3) 八拍头预测    —— 距下一个八拍头还有几拍/几秒（换算秒必须除以 r）
  4) 零回归        —— 开关关闭 / 无网格 / 未识别：_beat_pos / _bar_off / _next_switch_due
                      与「改动前实现」逐值一致
  5) 合成输入端到端 —— 真实 FLAC（只读）算出的 grid → 合成 offset 序列 → 拍位/八拍头核对

用法：venv\\Scripts\\python.exe tools\\_grid_drive_test.py
"""
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np  # noqa: E402

from audio_engine import RateEstimator  # noqa: E402
from beatgrid import GridClock, grid_from_signal, load_mono  # noqa: E402

PASS = "PASS"
FAIL = "FAIL"
_results = []


def check(name, ok, detail=""):
    _results.append((name, ok, detail))
    print("  [%s] %s  %s" % (PASS if ok else FAIL, name, detail))
    return ok


# ==========================================================================
# 1) r 估计
# ==========================================================================
def test_rate_estimator():
    print("\n== 1) r 估计（RateEstimator）==")
    TICK = 0.5
    for r_true in (0.90, 1.00, 1.05, 1.10):
        est = RateEstimator(tau=3.0)
        rng = np.random.default_rng(int(r_true * 1000))
        # 模型：窗口起点在原曲时间轴上的偏移 off 以速率 r_true 前进；
        # off_k = r_true * t_k + c - 4.5*r_true，Δt=0.5 ⇒ Δoff/Δt = r_true。
        c = 30.0
        conv_t = None
        final = 1.0
        for k in range(0, 80):          # 40s
            t = k * TICK
            off = r_true * t + c - 4.5 * r_true + rng.normal(0.0, 0.02)
            final = est.update(1, off, t)
            if conv_t is None and abs(final - r_true) < 0.01:
                conv_t = (k + 1) * TICK
        detail = "r_true=%.2f  终值=%.4f  误差=%.4f  收敛(±0.01)=%s" % (
            r_true, final, abs(final - r_true),
            ("%.1fs" % conv_t) if conv_t is not None else "未收敛")
        check("r≈%.2f" % r_true, abs(final - r_true) <= 0.02, detail)


# ==========================================================================
# 2) 网格时钟精度 + 单调性
# ==========================================================================
def _drive_grid_clock(bl, anchor, r_true, c, dur=60.0, tick=0.5,
                      noise_sec=0.05, seed=0):
    """合成一段识别 tick 序列喂 GridClock，返回 (max_err, monotonic, max_jump, n)。"""
    clk = GridClock()
    rng = np.random.default_rng(seed)
    # 采样网格拍位误差：每个 tick 之间按 10ms 采样
    sample_dt = 0.01
    max_err = 0.0
    monotonic = True
    max_jump = 0.0
    prev = None
    sid = 1
    t = 0.0
    # 先在 t=0 出第一个参考（硬锚定）
    off0 = r_true * 0.0 + c
    clk.update(sid, bl, anchor, off0, r_true, 0.0)
    while t <= dur:
        # 到 tick 时喂一次带噪参考
        if abs((t / tick) - round(t / tick)) < 1e-9:
            off = r_true * t + c + rng.normal(0.0, noise_sec)
            clk.update(sid, bl, anchor, off, r_true, t)
        p = clk.pos(t)
        truth = (r_true * t + c - anchor) / bl
        max_err = max(max_err, abs(p - truth))
        if prev is not None:
            if p < prev - 1e-9:
                monotonic = False
            max_jump = max(max_jump, abs(p - prev))
        prev = p
        t += sample_dt
    return max_err, monotonic, max_jump


def test_grid_clock():
    print("\n== 2) 网格时钟精度（±50ms 识别噪声，60s）==")
    # 120 BPM ⇒ beat_len = 0.5s；anchor 取某个八拍头
    for r_true in (1.00, 1.05, 0.90):
        bl, anchor, c = 0.5, 2.0, 17.0
        err, mono, jump = _drive_grid_clock(bl, anchor, r_true, c,
                                            dur=60.0, noise_sec=0.05, seed=7)
        detail = "r=%.2f  max|误差|=%.3f 拍  单调=%s  最大单步=%.3f 拍" % (
            r_true, err, mono, jump)
        # 误差应远小于半拍（不跳拍），且严格单调非减
        check("网格时钟 r=%.2f" % r_true, (err < 0.30) and mono and (jump < 0.5),
              detail)


# ==========================================================================
# 3) 八拍头预测
# ==========================================================================
def test_phrase_prediction():
    print("\n== 3) 八拍头预测（距下一个八拍头：拍 / 秒，秒必须除以 r）==")
    bl, anchor, c = 0.5, 1.0, 5.0        # 120 BPM
    r_true = 1.10                        # 变速 1.10×
    clk = GridClock()
    clk.update(1, bl, anchor, c, r_true, 0.0)
    ok_all = True
    worst = 0.0
    for t in (3.0, 3.37, 10.0, 25.5, 57.3):
        p = clk.pos(t)                              # 网格拍位
        beats = clk.beats_to_phrase(t)              # 距下一个八拍头（拍）
        sec = beats * bl / r_true                   # 换算成秒（除以 r）
        # 用 anchor + 8n·beat_len 独立核对：当前原曲位置 & 下一个八拍头
        pos_orig = anchor + p * bl
        n = math.floor(pos_orig / bl - 0)  # noqa
        k = math.floor((pos_orig - anchor) / (8.0 * bl)) + 1
        head_orig = anchor + k * 8.0 * bl
        beats_ref = (head_orig - pos_orig) / bl
        sec_ref = (head_orig - pos_orig) / r_true
        d = max(abs(beats - beats_ref), abs(sec - sec_ref))
        worst = max(worst, d)
        if d > 1e-9:
            ok_all = False
    check("八拍头预测", ok_all,
          "最大偏差=%.3e（拍/秒）；例：距八拍头 %.3f 拍 = %.3f 秒" % (
              worst, beats, sec))


# ==========================================================================
# 4) 零回归（开关关 / 无网格 / 未识别）
# ==========================================================================
import time as _timemod  # noqa: E402


class _FakeTime:
    def __init__(self):
        self.t = 0.0

    def perf_counter(self):
        return self.t


# 全局共享的假时钟：real（engine.time）与 ref（下面 _ref_* 函数）都读它，保证看到同一时刻
_NOWCLOCK = _FakeTime()


def _make_fake(cls):
    """构造一个「跳过 __init__ 的 engine 实例」：继承全部方法，只塞入方法要读到的属性。"""
    f = cls.__new__(cls)
    f.cfg = {"auto": {"beat_grid": False, "cooldown_sec": 20, "seed": 0},
             "mode": {"sensitivity": 1}, "beat": {}}
    f.mode = "normal"
    f._song_grid_lookup = {}      # 无网格
    f._grid_clock = GridClock()
    f._pos_ref = None
    f._bar_off_user = None
    f._next_due = None
    f._due_off = 0.0
    f.switch_pos = 0.0
    return f


def _ref_beat_pos(self, snap):
    """改动前 _beat_pos 的逐字副本（对照基准）。"""
    if snap.get("bpm", 0) > 0 and snap.get("beat_active"):
        pos = snap["beat_count"] + snap.get("beat_phase", 0.0)
        self._pos_ref = (_NOWCLOCK.perf_counter(), pos)
        return pos
    now = _NOWCLOCK.perf_counter()
    ref = getattr(self, "_pos_ref", None)
    if ref is None or now - ref[0] > 8.0:
        cur = (ref[1] + (now - ref[0]) * (120.0 / 60.0)) if ref else 0.0
        self._pos_ref = (now, cur)
        ref = self._pos_ref
    return ref[1] + (now - ref[0]) * (120.0 / 60.0)


def _ref_bar_off(self, snap):
    u = self._bar_off_user
    if u is not None:
        return float(u)
    return float(snap.get("downbeat_off", 0) or 0)


def test_zero_regression():
    print("\n== 4) 零回归（开关关 / 无网格 / 未识别）==")
    try:
        import engine as engmod
        from engine import AutoVJEngine
        # 用可替换的 time 模块，保证 real/ref 两条路径看到同一时刻
        ft = _NOWCLOCK
        orig_time = engmod.time
        engmod.time = ft

        class _FE(AutoVJEngine):
            """继承全部方法、但跳过 __init__ 的引擎替身：用来直接调 _beat_pos 等纯逻辑。"""
            pass
    except Exception as e:                                            # noqa: BLE001
        check("零回归（需 engine）", False, "无法导入 engine：%s" % e)
        return

    try:
        # ---- 4a) _beat_pos ----
        snaps = []
        # 确定分支：bpm>0 且 beat_active
        for bc in (0, 3, 17, 250):
            for ph in (0.0, 0.25, 0.5, 0.87):
                snaps.append({"bpm": 128.0, "beat_active": True,
                              "beat_count": bc, "beat_phase": ph})
        # 回退分支：bpm=0（用受控时钟驱动）
        snaps.append({"bpm": 0.0, "beat_active": False})

        fa = _make_fake(_FE)
        fb = _make_fake(_FE)
        ok = True
        worst = 0.0
        for i, sn in enumerate(snaps):
            ft.t = 100.0 + i * 0.3
            va = fa._beat_pos(sn)
            vb = _ref_beat_pos(fb, sn)
            worst = max(worst, abs(va - vb))
            if abs(va - vb) > 1e-12:
                ok = False
        # 回退分支多跑几步，验证 _pos_ref 演进一致
        for i in range(40):
            ft.t = 200.0 + i * 0.2
            va = fa._beat_pos(snaps[-1])
            vb = _ref_beat_pos(fb, snaps[-1])
            worst = max(worst, abs(va - vb))
            if abs(va - vb) > 1e-12:
                ok = False
        check("_beat_pos 逐值一致", ok, "最大偏差=%.3e" % worst)

        # ---- 4b) _bar_off（含用户手动相位：新版存绝对拍位 / 旧版存 % 4）----
        fa2 = _make_fake(_FE)
        fb2 = _make_fake(_FE)
        ok = True
        worst = 0.0
        for down in (0, 1, 2, 3):
            fa2._bar_off_user = None
            fb2._bar_off_user = None
            sn = {"downbeat_off": down}
            va = fa2._bar_off(sn)
            vb = _ref_bar_off(fb2, sn)
            worst = max(worst, abs(va - vb))
            if abs(va - vb) > 1e-12:
                ok = False
        for beat in (0, 5, 37, 258, 1023):
            fa2._bar_off_user = float(beat)      # 新版 next_scene 存的绝对拍位
            fb2._bar_off_user = beat % 4         # 旧版存的相位
            sn = {"downbeat_off": 0}
            va = fa2._bar_off(sn)
            vb = _ref_bar_off(fb2, sn)
            worst = max(worst, abs(va - vb))
            if abs(va - vb) > 1e-12:
                ok = False
        check("_bar_off 逐值一致", ok, "最大偏差=%.3e" % worst)

        # ---- 4c) _next_switch_due（对照：改动前的 4 拍对齐实现）----
        def ref_next(self, snap):
            iv = self._current_interval(snap)
            if iv is None:
                self._next_due = None
                return None
            off = self._bar_off(self)
            if self._next_due is None:
                target = self.switch_pos + iv
                near = off + math.floor((target - off) / 4.0 + 0.5) * 4.0
                self._next_due = near if abs(near - target) >= self.ALIGN_MIN_OFFSET else target
                self._due_off = off
            elif off != getattr(self, "_due_off", off):
                near = off + round((self._next_due - off) / 4.0) * 4.0
                if abs(near - self._next_due) >= self.ALIGN_MIN_OFFSET:
                    self._next_due = near
                self._due_off = off
            if self._next_due <= self.switch_pos:
                self._next_due += 4.0
            return self._next_due

        fa3 = _make_fake(_FE)
        fb3 = _make_fake(_FE)
        fa3.ALIGN_MIN_OFFSET = AutoVJEngine.ALIGN_MIN_OFFSET
        fb3.ALIGN_MIN_OFFSET = AutoVJEngine.ALIGN_MIN_OFFSET
        ok = True
        worst = 0.0
        for mode in ("normal", "fast"):
            fa3.mode = mode
            fb3.mode = mode
            for start in (0.0, 7.4, 100.3):
                fa3.switch_pos = start
                fb3.switch_pos = start
                fa3._next_due = None
                fb3._next_due = None
                fa3._bar_off_user = None
                fb3._bar_off_user = None
                for down in (0, 1, 2, 3):
                    sn = {"downbeat_off": down, "energy": 0.5, "energy_trend": 0.0}
                    va = fa3._next_switch_due(sn)
                    # ref 用 _bar_off（旧版直接返回相位）——用 unbound ref
                    fb3._bar_off = lambda s, sn=sn: _ref_bar_off(s, sn)
                    vb = ref_next(fb3, sn)
                    if (va is None) != (vb is None):
                        ok = False
                    elif va is not None:
                        worst = max(worst, abs(va - vb))
                        if abs(va - vb) > 1e-12:
                            ok = False
        check("_next_switch_due 逐值一致", ok, "最大偏差=%.3e" % worst)

        engmod.time = orig_time
    finally:
        try:
            engmod.time = orig_time
        except Exception:
            pass


# ==========================================================================
# 5) 合成输入端到端（真实 FLAC + 真实 grid）
# ==========================================================================
def test_endtoend_flac():
    print("\n== 5) 合成输入端到端（真实 FLAC grid → 拍位/八拍头）==")
    folder = r"W:\临时结界相关\2of\tofu"
    flacs = []
    try:
        for fn in sorted(os.listdir(folder)):
            if fn.lower().endswith(".flac"):
                flacs.append(os.path.join(folder, fn))
    except Exception as e:                                            # noqa: BLE001
        check("FLAC 端到端", False, "目录不可读：%s（跳过）" % e)
        return
    if not flacs:
        check("FLAC 端到端", False, "无 FLAC（跳过）")
        return
    path = flacs[0]
    try:
        mono, sr = load_mono(path, maxsec=90.0)
        g = grid_from_signal(mono, sr, vdj=None, path=path, dur=None, maxsec=90.0)
    except Exception as e:                                            # noqa: BLE001
        check("FLAC 端到端", False, "分析失败：%s（跳过）" % e)
        return
    if not g:
        check("FLAC 端到端", False, "grid 为空（跳过）")
        return
    bl = g["beat_len"]; anchor = g["anchor"]
    src = g.get("src")
    print("   网格：%s  bpm=%.2f  beat_len=%.4f  anchor=%.4f  conf=%.3f  src=%s"
          % (os.path.basename(path), g["bpm"], bl, anchor, g["conf"], src))
    # 合成"识别 tick 序列"：把一首歌按 r=1.03 播放，每 0.5s 出一次 off（窗口末端原曲秒）
    #   ⚠ 本用例用 vdj=None 生成本地来源网格，其 anchor 相位**不可信**（见阻塞项 2：
    #     21 首有 VDJ 真值的样例里本地相位只对 1/21）。因此**只做内部自洽核对**
    #     （pos 是否等于 (off-anchor)/bl —— 这是 GridClock 的定义，anchor 错也成立），
    #     **不做相位校验**；相位校验仅对 src=="vdj" 的真值网格进行。
    r_true = 1.03
    c0 = 8.0
    clk = GridClock()
    t0 = 0.0
    clk.update(1, bl, anchor, r_true * t0 + c0, r_true, t0)
    worst_beat = 0.0
    worst_head = 0.0
    t = 0.0
    while t <= 60.0:
        if abs((t / 0.5) - round(t / 0.5)) < 1e-9:
            clk.update(1, bl, anchor, r_true * t + c0, r_true, t)
        p = clk.pos(t)
        truth = (r_true * t + c0 - anchor) / bl
        worst_beat = max(worst_beat, abs(p - truth))     # 内部自洽（恒成立）
        # 八拍头：下一个"在或之后"的八拍头（恰好踩头=0，与 beats_to_phrase 约定一致）
        beats = clk.beats_to_phrase(t)
        pos_orig = anchor + p * bl
        hk = math.ceil((pos_orig - anchor) / (8.0 * bl) - 1e-9)
        head_orig = anchor + hk * 8.0 * bl
        worst_head = max(worst_head, abs((head_orig - pos_orig) / bl - beats))
        t += 0.01
    if src == "vdj":
        # 真值来源：做完整相位校验
        check("FLAC 端到端拍位/八拍头(vdj 相位校验)",
              (worst_beat < 0.2 and worst_head < 1e-6),
              "src=vdj  最大拍位误差=%.4f 拍；八拍头偏差=%.2e" % (worst_beat, worst_head))
    else:
        # 本地来源：不做相位校验（仅内部自洽 + 八拍头换算核对）
        check("FLAC 端到端(本地来源·跳过相位校验)", worst_beat < 0.2,
              "src=%s（本地相位不可信，跳过相位校验；内部自洽最大误差=%.4f 拍）"
              % (src, worst_beat))


def test_grid_clock_threadsafe():
    """GridClock 多线程压力测试：3~4 个读线程狂调 pos()/beats_to_phrase()，
    另一个线程每 0.5s 喂识别 tick，跑 GRID_STRESS_SEC（默认 10s）。

    断言：① 无异常；② 每个读线程**自身**观测到的拍位序列严格非减（单调性不被并发破坏）。"""
    dur = float(os.environ.get("GRID_STRESS_SEC", "10"))
    print("\n== 6) GridClock 多线程压力测试（%gs）==" % dur)
    import threading
    bl, anchor, r_true, c0 = 0.5, 2.0, 1.05, 11.0
    clk = GridClock()
    clk.update(1, bl, anchor, c0, r_true, 0.0)
    stop = threading.Event()
    errors = []
    iter_counts = [0] * 3
    mono_viol = [0] * 3          # 各读线程观测到的「后退」次数
    lock = threading.Lock()

    def reader(idx):
        prev = None
        n = 0
        try:
            while not stop.is_set():
                p = clk.pos(_timemod.perf_counter())
                if prev is not None and p < prev - 1e-12:
                    with lock:
                        mono_viol[idx] += 1
                prev = p
                n += 1
        except Exception as e:                                        # noqa: BLE001
            with lock:
                errors.append("reader%d: %r" % (idx, e))
        finally:
            with lock:
                iter_counts[idx] = n

    def reader_phrase(idx):
        prev = None
        n = 0
        try:
            while not stop.is_set():
                b = clk.beats_to_phrase(_timemod.perf_counter())
                # 同一线程内 beats_to_phrase 也随拍位单调（拍位非减 ⇒ beats 非增）→ 这里只查范围
                if not (0.0 <= b <= 8.0):
                    with lock:
                        errors.append("phrase out of range: %r" % b)
                prev = b
                n += 1
        except Exception as e:                                        # noqa: BLE001
            with lock:
                errors.append("phrase%d: %r" % (idx, e))
        finally:
            with lock:
                iter_counts[idx] = n

    def feeder():
        t = 0.0
        try:
            while not stop.is_set():
                _timemod.sleep(0.5)
                t += 0.5
                off = r_true * t + c0
                clk.update(1, bl, anchor, off, r_true, t)
        except Exception as e:                                        # noqa: BLE001
            with lock:
                errors.append("feeder: %r" % e)

    ths = [threading.Thread(target=reader, args=(i,)) for i in range(2)]
    ths.append(threading.Thread(target=reader_phrase, args=(2,)))
    ths.append(threading.Thread(target=feeder))
    for th in ths:
        th.start()
    _timemod.sleep(dur)
    stop.set()
    for th in ths:
        th.join(timeout=2.0)

    total_iter = sum(iter_counts)
    ok = (not errors) and all(v == 0 for v in mono_viol) and total_iter > 1000
    detail = "线程=%d  总调用=%d  单调违例=%s  异常=%s" % (
        len(ths), total_iter, mono_viol, (errors or "无"))
    check("多线程单调/无异常", ok, detail)


def main():
    print("=" * 78)
    print("离线节拍网格 → 实时演出路径 自测")
    print("=" * 78)
    test_rate_estimator()
    test_grid_clock()
    test_phrase_prediction()
    test_zero_regression()
    test_endtoend_flac()
    test_grid_clock_threadsafe()
    print("\n" + "=" * 78)
    n_ok = sum(1 for _, ok, _ in _results if ok)
    n_all = len(_results)
    print("结果：%d/%d 通过" % (n_ok, n_all))
    for name, ok, _ in _results:
        if not ok:
            print("   FAILED: %s" % name)
    print("=" * 78)
    return 0 if n_ok == n_all else 1


if __name__ == "__main__":
    sys.exit(main())
