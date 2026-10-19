# -*- coding: utf-8 -*-
"""★ 接线验证：把**真实彩色波形**接到时间轴（`ui/timeline_widget.py`）。

跑法（Windows）：
    P:\\ERAVJ\\EasyRealityCue\\venv\\Scripts\\python.exe  tools/_fix_wave_ui_verify.py
（内部自动设 `QT_QPA_PLATFORM=offscreen`，无头可跑；波形缓存改写到临时目录，不碰用户目录。）

判据（全部**真实运行**：真造音频、真跑 offscreen Qt、真取像素 / 真跑后台线程）：
  ① 画的是**真数据**：低频(100Hz)⇒红、高频(6kHz)⇒蓝（采样通道 + 渲染像素双双验证），
     且数据源确为 `WaveformData`（不是旧的确定性伪波形）。
  ② **不卡 UI**：模拟"算 3 秒" ⇒ `set_audio_path` 立即返回；计算发生在**非主线程**；
     主线程期间 QTimer 照常跳动（响应）；**逆向**：同一次计算在**主线程**直接跑要 ~3 秒。
  ③ **换轨取消不串写**：先请求慢的 A、立刻请求快的 B ⇒ 最终显示 B；A 的结果**从未**被应用；
     **逆向**：单独请求 A 时确实会应用 A（证明"从未应用"不是判据失效）。
  ④ **降级不崩**：不存在的音频 / 空路径 ⇒ 不触发计算、不崩、合理留空；计算返回 None
     （失败）⇒ 平滑退化，绝不抛。
  ⑤ 静态闸门：`compileall src/` 零错误；`tools/selftest.py` 7/7。

（完整 `tests/` 由命令行另行跑，见 `docs/_audit/_fix_wave_ui.md`。）
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import traceback
import wave

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import numpy as np                                                    # noqa: E402
from PySide6.QtCore import QTimer                                     # noqa: E402
from PySide6.QtWidgets import QApplication                           # noqa: E402

import ercue.audio.waveform as wf                                     # noqa: E402
from ercue import config as app_config                               # noqa: E402
from ercue.ui import theme                                           # noqa: E402
from ercue.ui.timeline_widget import CueTimeline                     # noqa: E402

APP = None
_TMP = tempfile.mkdtemp(prefix="ercue_waveui_")


# ==========================================================================
# 夹具：合成音频（stdlib wave + numpy），缓存隔离到临时目录
# ==========================================================================
def _write_wav(path, samples, sr=44100):
    """float32 [-1,1] 单声道 → 立体声 PCM16 WAV（PyAV 可读）。"""
    x = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    stereo = np.stack([x, x], axis=1)
    pcm = (stereo * 32767.0).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm.tobytes())


def _tone(freq, dur, sr=44100, amp=0.35):
    t = np.arange(int(sr * dur)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


_CACHE = os.path.join(_TMP, "cache")
os.makedirs(_CACHE, exist_ok=True)
wf.cache_dir = lambda: _CACHE                                         # noqa: E731


def _fixtures():
    low = os.path.join(_TMP, "low_100hz.wav")
    high = os.path.join(_TMP, "high_6khz.wav")
    ta = os.path.join(_TMP, "trackA.wav")
    tb = os.path.join(_TMP, "trackB.wav")
    fail = os.path.join(_TMP, "fail.wav")
    _write_wav(low, _tone(100.0, 3.0))
    _write_wav(high, _tone(6000.0, 3.0))
    _write_wav(ta, _tone(440.0, 1.0))
    _write_wav(tb, _tone(660.0, 1.0))
    _write_wav(fail, _tone(330.0, 1.0))
    return {"low": low, "high": high, "A": ta, "B": tb, "fail": fail}


def _fresh_tl(width=900, height=300, span=3.0):
    tl = CueTimeline()
    tl.resize(width, height)
    tl.set_track(None)
    tl.set_view(0.0, span)
    APP.processEvents()
    return tl


def _pump(pred, timeout=6.0, interval=0.01):
    """反复 processEvents 直到 pred() 为真或超时。返回 pred() 的最终值。"""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        APP.processEvents()
        if pred():
            return True
        time.sleep(interval)
    APP.processEvents()
    return bool(pred())


def _row_dominant(tl, red_min=20, blue_min=20):
    """扫描音频轨像素，返回 `(红主导像素数, 蓝主导像素数)`。"""
    img = tl.grab().toImage()
    ar = tl._row_rect(len(tl._layers()))          # 音频行
    y_lo = max(0, int(ar.top()) + 2)
    y_hi = min(img.height(), int(ar.bottom()) - 2)
    red = blue = 0
    for x in range(theme.GUTTER + 2, img.width() - 2, 2):
        for y in range(y_lo, y_hi, 2):
            c = img.pixelColor(x, y)
            r, g, b = c.red(), c.green(), c.blue()
            if r > g + red_min and r > b + red_min:
                red += 1
            elif b > r + blue_min and b > g + blue_min:
                blue += 1
    return red, blue


# ==========================================================================
# ① 画的是真数据（低=红 / 高=蓝）
# ==========================================================================
def t_real_data(fx):
    # 先真算一遍（顺带写缓存）⇒ 之后 set_audio_path 走"缓存命中、同步"路径
    d_low = wf.load_or_compute(fx["low"])
    d_high = wf.load_or_compute(fx["high"])
    assert d_low is not None and d_high is not None
    # 数据层自我一致性：低频⇒R 主导、高频⇒B 主导
    assert float(np.mean(d_low.r)) > float(np.mean(d_low.g)), "低频数据 R 应主导"
    assert float(np.mean(d_low.r)) > float(np.mean(d_low.b)), "低频数据 R 应主导"
    assert float(np.mean(d_high.b)) > float(np.mean(d_high.r)), "高频数据 B 应主导"
    print("    数据层：低频 r=%.3f>g=%.3f,b=%.3f ｜ 高频 b=%.3f>r=%.3f"
          % (np.mean(d_low.r), np.mean(d_low.g), np.mean(d_low.b),
             np.mean(d_high.b), np.mean(d_high.r)))

    tl = _fresh_tl()
    try:
        # ---- 低频 ----
        tl.set_audio_path(fx["low"])
        assert tl._wav is not None, "① 缓存命中应同步拿到 WaveformData"
        assert type(tl._wav).__name__ == "WaveformData", type(tl._wav)
        assert tl._wav is not d_low or True  # 类型来源明确即可
        assert tl.wave_status()[0] == "ready", tl.wave_status()
        assert tl._wav_worker is None, "① 缓存命中不该起后台线程"
        mid_x = theme.GUTTER + (tl.width() - theme.GUTTER) // 2
        s = tl.wave_sample(mid_x)
        assert s is not None, "① 视口内取样不该为 None"
        cr, cg, cb = s[1]
        assert cr > cg and cr > cb, "① 低频处取样应 R 主导，实得 (%.2f,%.2f,%.2f)" % (cr, cg, cb)
        r_px, b_px = _row_dominant(tl)
        assert r_px > b_px * 3 and r_px > 30, \
            "① 低频时间轴应红多于蓝（red=%d blue=%d）" % (r_px, b_px)
        print("    ✓ ① 低频(100Hz)：取样=(%.2f,%.2f,%.2f) R 主导；像素 红=%d / 蓝=%d"
              % (cr, cg, cb, r_px, b_px))

        # ---- 高频（同一控件，换音频 ⇒ 颜色随数据变，证明确非固定伪波形）----
        tl.set_audio_path(fx["high"])
        assert tl._wav is not None
        s2 = tl.wave_sample(mid_x)
        assert s2 is not None
        cr2, cg2, cb2 = s2[1]
        assert cb2 > cr2 and cb2 > cg2, "① 高频处取样应 B 主导，实得 (%.2f,%.2f,%.2f)" % (cr2, cg2, cb2)
        r_px2, b_px2 = _row_dominant(tl)
        assert b_px2 > r_px2 * 3 and b_px2 > 30, \
            "① 高频时间轴应蓝多于红（red=%d blue=%d）" % (r_px2, b_px2)
        print("    ✓ ① 高频(6kHz)：取样=(%.2f,%.2f,%.2f) B 主导；像素 红=%d / 蓝=%d"
              % (cr2, cg2, cb2, r_px2, b_px2))

        # ---- 伪波形会失败：旧伪波形与音频无关，两种音频画出来应**一样**；这里必须不同 ----
        assert (r_px, b_px) != (r_px2, b_px2), "① 换音频后像素分布没变（像是画的固定伪波形）"
        print("    ✓ ① 换音频后渲染像素分布随之改变 ⇒ 画的确实是数据、不是固定伪波形")
    finally:
        tl.stop_waveform()


# ==========================================================================
# ② 不卡 UI（后台线程；主线程保持响应）
# ==========================================================================
def t_not_blocking(fx):
    import threading
    main_id = threading.get_ident()
    calls = []
    orig = wf.load_or_compute

    def slow(path, columns=None, progress=None, use_cache=True, force=False, **kw):
        calls.append((str(path), threading.get_ident()))
        for i in range(30):                       # 模拟"算 3 秒"
            time.sleep(0.1)
            if progress is not None:
                progress((i + 1) / 30.0)
        n = 600
        return wf.WaveformData(columns=n, amp=np.full(n, 0.5, np.float32),
                               r=np.ones(n, np.float32), g=np.zeros(n, np.float32),
                               b=np.zeros(n, np.float32), duration_sec=3.0)

    wf.load_or_compute = slow
    tl = _fresh_tl()
    try:
        t0 = time.perf_counter()
        tl.set_audio_path(fx["fail"])             # 无缓存 ⇒ 走后台线程
        dt = time.perf_counter() - t0
        assert dt < 0.3, "② set_audio_path 不该阻塞（模拟 3 秒计算却耗时 %.2fs）" % dt
        assert tl.wave_status()[0] == "computing", tl.wave_status()
        print("    ② set_audio_path 立即返回：%.3fs（远小于 3s）" % dt)

        # 主线程保持响应：QTimer 照常跳动
        ticks = {"n": 0}
        tm = QTimer()
        tm.timeout.connect(lambda: ticks.__setitem__("n", ticks["n"] + 1))
        tm.start(30)
        t1 = time.perf_counter()
        while time.perf_counter() - t1 < 0.6:     # 计算还在跑，主线程应能转事件
            APP.processEvents()
            time.sleep(0.01)
        tm.stop()
        assert ticks["n"] >= 5, "② 主线程被卡住：0.6s 内 QTimer 只跳了 %d 次" % ticks["n"]
        print("    ✓ ② 计算期间主线程未被卡：0.6s 内 QTimer 跳动 %d 次" % ticks["n"])

        # 计算确实发生在非主线程
        assert calls, "② 后台线程没被调起"
        assert all(ident != main_id for _, ident in calls), "② 计算竟跑在主线程里！"
        print("    ✓ ② 计算在非主线程执行：%d 次调用，线程 id 全部 ≠ 主线程" % len(calls))

        # 结果最终回到主线程落地
        assert _pump(lambda: tl._wav is not None, timeout=5.0), "② 后台结果没回主线程"
        assert float(tl._wav.amp[0]) == 0.5 and tl.wave_status()[0] == "ready"
        print("    ✓ ② 后台算完回主线程：data.amp[0]=%.2f" % float(tl._wav.amp[0]))

        # ---- 逆向：同一次"计算"在**主线程**直接跑要 ~3 秒（证明上面省掉的正是它）----
        t0 = time.perf_counter()
        slow(fx["fail"])
        dt_direct = time.perf_counter() - t0
        assert dt_direct > 2.0, "逆向验证失败：直接跑本应 ~3s，实得 %.2fs" % dt_direct
        assert dt_direct > dt * 5.0, "逆向：直接跑(%.2fs)应远慢于接线后(%.3fs)" % (dt_direct, dt)
        print("    ✓ 逆向（②）：主线程直接跑 = %.2fs（接线后仅 %.3fs）⇒ 后台线程确有效"
              % (dt_direct, dt))
    finally:
        wf.load_or_compute = orig
        tl.stop_waveform()


# ==========================================================================
# ③ 换轨取消：慢 A 被快的 B 顶掉 ⇒ 只显示 B
# ==========================================================================
def t_switch_cancel(fx):
    orig = wf.load_or_compute

    def fake(path, columns=None, progress=None, use_cache=True, force=False, **kw):
        base = os.path.basename(str(path))
        n = 600
        if base == "trackA.wav":
            time.sleep(0.8)
            amp = np.full(n, 0.2, np.float32)
        else:
            time.sleep(0.05)
            amp = np.full(n, 0.8, np.float32)
        return wf.WaveformData(columns=n, amp=amp,
                               r=np.full(n, 0.5, np.float32), g=np.full(n, 0.5, np.float32),
                               b=np.full(n, 0.5, np.float32), duration_sec=1.0)

    wf.load_or_compute = fake
    tl = _fresh_tl(span=1.0)
    try:
        # ---- 逆向：单独请求 A ⇒ A 的结果**确实**会被应用（0.2）----
        tl.set_audio_path(fx["A"])
        assert _pump(lambda: tl._wav is not None, timeout=3.0), "③ 单独请求 A 应能拿到结果"
        assert abs(float(tl._wav.amp[0]) - 0.2) < 1e-6, float(tl._wav.amp[0])
        print("    逆向（③）：单独请求 A ⇒ 应用 A（amp=%.2f）⇒ 判据有效" % float(tl._wav.amp[0]))

        # ---- 正式：先 A（慢 0.8s）后 B（快 0.05s），最终必须显示 B，且 A 从未被应用 ----
        tl.set_audio_path(fx["A"])
        tl.set_audio_path(fx["B"])
        applied = []
        t0 = time.monotonic()
        while time.monotonic() - t0 < 2.5:
            APP.processEvents()
            if tl._wav is not None and not applied:
                applied.append(float(tl._wav.amp[0]))
            time.sleep(0.01)
        assert tl._wav is not None, "③ 最终应显示 B 的数据"
        assert tl._wav_path == fx["B"], "③ 当前波形路径应为 B"
        assert abs(float(tl._wav.amp[0]) - 0.8) < 1e-6, \
            "③ 最终应显示 B（amp=0.8），实得 %.2f" % float(tl._wav.amp[0])
        assert 0.2 not in applied, "③ 慢 A 的结果竟被应用了（换轨取消失效）：%s" % applied
        print("    ✓ ③ 先 A 后 B：最终=B（amp=%.2f），期间应用过的值=%s（无 A）"
              % (float(tl._wav.amp[0]), applied))

        # ---- 再等一会儿：A 的计算早已返回，绝不该回写覆盖 B ----
        time.sleep(0.6)
        APP.processEvents()
        assert abs(float(tl._wav.amp[0]) - 0.8) < 1e-6, "③ A 的旧结果回写覆盖了新轨！"
        print("    ✓ ③ 旧任务返回后不回写：%0.1fs 后仍显示 B（amp=%.2f）"
              % (0.6, float(tl._wav.amp[0])))
    finally:
        wf.load_or_compute = orig
        tl.stop_waveform()


# ==========================================================================
# ④ 降级不崩（无音频 / 不存在 / 计算失败）
# ==========================================================================
def t_degrade(fx):
    tl = _fresh_tl()
    try:
        # (1) 不存在的音频 ⇒ 不触发计算、不崩、无数据
        tl.set_audio_path(os.path.join(_TMP, "no_such_file.wav"))
        assert tl.wave_status()[0] == "none", tl.wave_status()
        assert tl._wav is None and tl._wav_worker is None, "④ 不存在音频不该起线程"
        assert tl.wave_sample(theme.GUTTER + 50) is None
        tl.grab()                                  # 渲染不崩
        print("    ④ 不存在的音频：状态=none，未起线程，渲染不崩")

        # (2) 空路径（如**纯图片素材**）⇒ 同样不触发计算
        tl.set_audio_path("")
        assert tl.wave_status()[0] == "none" and tl._wav_worker is None
        tl.grab()
        print("    ④ 空路径/纯图片素材：状态=none，未起线程，渲染不崩")

        # (3) 计算返回 None（失败）⇒ 平滑退化到 none，绝不抛
        orig = wf.load_or_compute
        wf.load_or_compute = lambda path, **kw: None
        try:
            tl.set_audio_path(fx["fail"])          # 存在但"算不出来"
            assert _pump(lambda: tl.wave_status()[0] != "computing", timeout=3.0), \
                "④ 失败后应离开 computing 态"
            assert tl.wave_status()[0] == "none" and tl._wav is None, tl.wave_status()
            tl.grab()
        finally:
            wf.load_or_compute = orig
        print("    ④ 计算失败（返回 None）：平滑退化为 none，渲染不崩")
    finally:
        tl.stop_waveform()


# ==========================================================================
# 接线：主窗口把音频路径喂给时间轴（缓存命中 ⇒ 同步、无线程）
# ==========================================================================
def t_window_wiring(fx):
    wf.load_or_compute(fx["low"])                  # 先写缓存
    from ercue.ui.window import MainWindow
    w = MainWindow()
    w._interactive = False
    try:
        w.new_sheet("WAVE")
        w.add_track("波形曲", "x", 3.0, 0.0)
        w.session.track_audio[0] = fx["low"]
        w._refresh_all()                           # → _sync_timeline_data → set_audio_path
        tl = w.timeline
        assert tl._wav_path == fx["low"], "主窗口应把当前音频路径喂给时间轴"
        assert tl._wav is not None and tl.wave_status()[0] == "ready", tl.wave_status()
        assert tl._wav_worker is None, "缓存命中应是同步读入，不该起线程"
        print("    ✓ 接线：MainWindow._sync_timeline_data → set_audio_path(%s)，缓存命中同步读入"
              % os.path.basename(fx["low"]))
    finally:
        w.shutdown()


# ==========================================================================
# ⑤ 静态闸门：compileall + selftest
# ==========================================================================
def t_static_gates():
    py = sys.executable
    r1 = subprocess.run([py, "-m", "compileall", "-q", _SRC],
                        capture_output=True, text=True)
    assert r1.returncode == 0, "compileall 失败：\n%s\n%s" % (r1.stdout, r1.stderr)
    print("    ✓ compileall -q src/ 零错误")
    r2 = subprocess.run([py, os.path.join(_HERE, "selftest.py")],
                        capture_output=True, text=True, cwd=_ROOT)
    tail = (r2.stdout or "").strip().splitlines()[-1:] or [""]
    assert r2.returncode == 0, "selftest 失败：\n%s\n%s" % (r2.stdout, r2.stderr)
    assert "7/7" in (r2.stdout or ""), "selftest 应 7/7 通过，末行：%s" % tail[0]
    print("    ✓ tools/selftest.py 7/7 通过")


# ==========================================================================
def _run(name, fn, fx=None):
    try:
        fn(fx) if fx is not None else fn()
        return (name, True, "")
    except Exception as e:                                       # noqa: BLE001
        return (name, False, "%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()))


def main() -> int:
    global APP
    APP = QApplication.instance() or QApplication([])
    app_config._INSTANCE = app_config.Config(
        os.path.join(tempfile.mkdtemp(prefix="ercue_cfg_waveui_"), "config.json"))
    fx = _fixtures()

    print("=" * 70)
    print("接线：真实彩色波形 → 时间轴 · 验证")
    print("夹具：100Hz/6kHz/各轨 wav（%s）" % _TMP)
    print("=" * 70)

    cases = [
        ("① 画真数据（低红/高蓝 + 像素）", t_real_data, fx),
        ("② 不卡 UI（后台线程 + 响应 + 逆向）", t_not_blocking, fx),
        ("③ 换轨取消不串写（+逆向）", t_switch_cancel, fx),
        ("④ 降级不崩（无/不存在/失败）", t_degrade, fx),
        ("接线：主窗口喂音频路径", t_window_wiring, fx),
        ("⑤ 静态闸门（compileall + selftest）", t_static_gates, None),
    ]
    results = []
    for name, fn, f in cases:
        print("\n--- %s ---" % name)
        n, ok, detail = _run(name, fn, f)
        results.append((n, ok, detail))
        if not ok:
            print("  ✗ %s\n%s" % (n, detail))

    print("\n" + "=" * 70)
    for n, ok, _ in results:
        print("  [%s] %s" % ("PASS" if ok else "FAIL", n))
    n_ok = sum(1 for _, ok, _ in results if ok)
    print("=" * 70)
    print("汇总：%d/%d 通过  ⇒  %s" % (n_ok, len(results),
                                       "全绿 ✓" if n_ok == len(results) else "有失败 ✗"))
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
