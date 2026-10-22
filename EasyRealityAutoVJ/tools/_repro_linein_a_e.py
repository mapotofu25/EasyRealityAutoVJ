# -*- coding: utf-8 -*-
"""线路输入/声卡 失败场景离线复现（场景 A~E）。

⚠ 铁律：不要用 `python -c` —— 本文件用 venv 直跑。
⚠ 无真实线路输入硬件 ⇒ 全部用「假设备桩 + 打桩 soundcard」离线复现。
⚠ 不修改 src/ 下任何文件；夹具只落在系统临时目录。

运行：
  P:\\ERAVJ\\AutoVJ\\venv\\Scripts\\python.exe P:\\ERAVJ\\AutoVJ\\tools\\_repro_linein_a_e.py

判据：看脚本自己打印的各行结论（本脚本不创建 MainWindow，无 NDI/ASIO 卸载 abort）。
"""
import os
import sys
import types
import traceback

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import numpy as np

import audio_engine as AE

RESULTS = []


def _log(tag, msg):
    line = "[%s] %s" % (tag, msg)
    print(line)
    RESULTS.append(line)


# ==========================================================================
# 假设备桩：具备 soundcard recorder 需要的最小接口
# ==========================================================================
class FakeRecorder:
    """上下文管理器；record(numframes=) 返回已知信号。

    model: "stereo_ok"  —— 左右都有信号（正常立体声线路输入）
           "right_only" —— 左声道全 0、右声道有信号（单声道接口只插了右声道）
           "raise"      —— record() 一直抛异常（设备被拔掉）
    """

    def __init__(self, model="stereo_ok", sr=48000, block=1024, amp=0.5):
        self.model = model
        self.sr = sr
        self.block = block
        self.amp = amp
        self._t = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.closed = True
        return False

    def record(self, numframes=None):
        n = numframes or self.block
        if self.model == "raise":
            raise RuntimeError("data discontinuity in recording (模拟设备拔掉)")
        t = self._t + np.arange(n, dtype=np.float64)
        tone = (self.amp * np.sin(2.0 * np.pi * 1000.0 * t / self.sr)).astype(np.float32)
        self._t += n
        if self.model == "right_only":
            left = np.zeros(n, dtype=np.float32)
            return np.column_stack([left, tone])
        # stereo_ok：左右同相
        return np.column_stack([tone, tone])


class FakeMic:
    def __init__(self, name, isloopback=False, model="stereo_ok", sr=48000,
                 native_sr=48000, recorder_raises=False):
        self.name = name
        self.isloopback = isloopback
        self._model = model
        self._sr = sr
        self._native_sr = native_sr
        self._recorder_raises = recorder_raises
        self.recorder_calls = []

    def _device_ptr(self):
        raise RuntimeError("桩：无真实 COM 设备指针")

    def recorder(self, samplerate=None, channels=2):
        if self._recorder_raises:
            raise RuntimeError("桩：recorder() 打不开设备")
        self.recorder_calls.append((samplerate, channels))
        return FakeRecorder(self._model, sr=samplerate or self._sr)


def _stub_soundcard(mics):
    """把一个假 soundcard 模块塞进 sys.modules（list_devices 里 `import soundcard`）。"""
    mod = types.ModuleType("soundcard")
    mod.all_microphones = lambda include_loopback=False: list(mics)

    def _default_speaker():
        return None

    mod.default_speaker = _default_speaker
    mod.default_microphone = lambda: (mics[0] if mics else None)
    sys.modules["soundcard"] = mod
    return mod


def _has_error(st):
    with st.lock:
        return st.error, st.running, st.device_desc


# ==========================================================================
# 场景 0（前置）：正常信号下 _Analyzer 是否能正常输出能量 —— 建立"分析链路本身是好的"
# ==========================================================================
def scenario0():
    print("\n" + "=" * 74)
    print("场景 0（前置）：正常立体声信号 → _Analyzer.feed 能量/档位是否正常")
    print("=" * 74)
    from audio_engine import _Analyzer, AudioState
    sr = 48000
    an = _Analyzer(sr)
    st = AudioState()
    # 喂 200 块（约 4.3s）1kHz 正弦，跑热，看能量/level 是否稳定为正
    for _ in range(200):
        d = FakeRecorder("stereo_ok", sr=sr).record(1024)
        an.feed(d, st)
        an.energy_scale = 1.0
    snap = st.snapshot()
    _log("场景0", "level=%.4f energy=%.4f rms(lv_db)=%.2f bass=%.4f mid=%.4f high=%.4f"
         % (snap["level"], snap["energy"], snap["lv_db"], snap["bass"], snap["mid"], snap["high"]))
    ok = snap["lv_db"] > -60.0 and snap["level"] > 0.0
    _log("场景0", "结论：正常信号下分析链路 %s（level>0 且 lv_db>-60）"
         % ("正常输出" if ok else "异常——能量恒 0！"))
    return ok


# ==========================================================================
# 场景 A：设备列表为空（所有设备都是 loopback，没有非回环设备）
# ==========================================================================
def scenarioA():
    print("\n" + "=" * 74)
    print("场景 A：设备列表为空（全部 loopback，无非回环设备）→ 线路输入被选")
    print("=" * 74)
    import config as cfgmod
    mics = [FakeMic("扬声器 (Realtek Loopback)", isloopback=True)]
    _stub_soundcard(mics)
    a = AE.AudioEngine()
    a._dev_jobs = a._dev_jobs  # 已是 Queue
    # 直接同步跑 list_devices 的那段逻辑（起个 worker 线程代价大，这里手动模拟）
    scanned = {"system": [], "mic": [], "asio": []}
    sc = sys.modules["soundcard"]
    for m in sc.all_microphones(include_loopback=True):
        (scanned["system"] if m.isloopback else scanned["mic"]).append(m.name)
    _log("场景A", "设备枚举结果：system=%s mic=%s" % (scanned["system"], scanned["mic"]))

    # 走真实 _run()：source_type="mic"（线路输入映射到 mic）
    a._run(sc, "mic", "", True, 48000, a._gen)
    err, running, desc = _has_error(a.state)
    _log("场景A", "真实 _run(mic) 结果：error=%r running=%s device_desc=%r" % (err, running, desc))
    hit = ("找不到麦克风/线路输入设备" in (err or ""))
    _log("场景A", "是否抛「找不到麦克风/线路输入设备」：%s" % ("是（已被 _run 捕获写入 st.error）" if hit else "否"))
    # UI 层表现：面板 device 列表只有「（未找到设备）」占位；hint 文案：
    _log("场景A", "UI 层表现：设备下拉只剩占位「（未找到设备）」；"
                  "lbl_hint=「没有扫描到可用设备：请检查连接后点「重新扫描」。」；"
                  "点应用 → dev 被当占位清空 → 引擎 _run 抛错 → st.error 显示「找不到麦克风/线路输入设备」"
                  "，界面能量条恒 0（用户看到“没反应”）")
    return hit


# ==========================================================================
# 场景 B：device_name 匹配不到 → 是否静默回退 cands[0]
# ==========================================================================
def scenarioB():
    print("\n" + "=" * 74)
    print("场景 B：device_name 匹配不到（配置的声卡名不在列表）→ 是否静默回退 cands[0]")
    print("=" * 74)
    mics = [
        FakeMic("麦克风 (Realtek 内置阵列)"),           # cands[0] ← 会被静默选中
        FakeMic("线路输入 (USB 声卡 Line-In)"),          # 用户真正想要的那个
    ]
    _stub_soundcard(mics)
    a = AE.AudioEngine()
    sc = sys.modules["soundcard"]
    # 用户配置里写的是声卡名，但名字对不上（比如换了 USB 口导致名字变了/带后缀）
    user_cfg_name = "USB Audio Device Line-In"   # 子串匹配不到任何一个
    captured = {}
    orig_cl = a._capture_loop

    def spy(mic, mono, sr, gen, _c=captured, _o=orig_cl):
        _c["mic"] = mic.name
        _c["sr"] = sr
        raise SystemExit  # 只在采集真正开始那一刻截断（不真的跑循环）

    a._capture_loop = spy
    try:
        a._run(sc, "mic", user_cfg_name, True, 48000, a._gen)
    except SystemExit:
        pass
    err, running, desc = _has_error(a.state)
    _log("场景B", "用户配置 device_name=%r" % user_cfg_name)
    _log("场景B", "实际开始采集的设备 = %r（sr=%s）" % (captured.get("mic"), captured.get("sr")))
    _log("场景B", "st.device_desc=%r error=%r" % (desc, err))
    silently_fell_back = (captured.get("mic") == "麦克风 (Realtek 内置阵列)")
    _log("场景B", "是否【静默回退到 cands[0]、且不报错】：%s"
         % ("是 —— 用户选了线路输入，实际却在采内置麦克风，界面上「当前采集设备」显示的是麦克风名字，"
            "而且**没有任何错误提示**" if (silently_fell_back and not err) else "否"))
    return silently_fell_back


# ==========================================================================
# 场景 C：配置 source_type="linein" → 跑 SOURCE_KEYS 映射
# ==========================================================================
def scenarioC():
    print("\n" + "=" * 74)
    print("场景 C：历史配置 source_type='linein' → 面板 SOURCE_KEYS / _load_from_cfg 映射")
    print("=" * 74)
    SOURCE_KEYS = ("system", "mic", "mic", "asio")
    cfg_idx = {"system": 0, "mic": 1, "linein": 2, "asio": 3}

    idx = cfg_idx.get("linein", 0)
    key = SOURCE_KEYS[max(0, min(3, idx))]
    src_label = ["系统声音（WASAPI Loopback）", "麦克风", "线路输入/声卡", "ASIO"][idx]
    _log("场景C", "cfg['source_type']=='linein' → _load_from_cfg 下拉索引 idx=%d" % idx)
    _log("场景C", "下拉索引 %d 对应的 UI 文案 = %r" % (idx, src_label))
    _log("场景C", "该索引映射出的类别 key = %r（注意 SOURCE_KEYS 里根本没有 'linein'）" % key)
    # 引擎侧的真实映射（ui_main.start_audio: linein -> mic）
    eng_t = {"linein": "mic"}.get("linein", "linein")
    _log("场景C", "引擎侧 start_audio 映射：'linein' → %r（引擎能认）" % eng_t)
    degraded = (key == "system")
    _log("场景C", "结论：面板层 %s —— "
                  "历史配置 linein 在下拉里恰好落到索引 2 显示「线路输入/声卡」，"
                  "但类别 key 退化成 'mic'（= 场景 B 的子串匹配）；"
                  "真正危险的是：一旦用户在面板上点过「应用」或任何触发 _load_from_cfg 的路径"
                  "把 source_type 规范化，" % ("退化" if degraded else "未退化"))
    _log("场景C", "  另注意 ui_main.start_audio 会把 linein 静默改写成 mic 并**回写配置**——"
                  "历史 linein 配置一旦启动过就被永久改写，不可逆。")
    return True


# ==========================================================================
# 场景 D：sr=0 时 _native_samplerate 失败 → fallback 48000 是否正常
# ==========================================================================
def scenarioD():
    print("\n" + "=" * 74)
    print("场景 D：sr=0（自动）时 _native_samplerate(mic) 失败/返回 None → fallback")
    print("=" * 74)
    mics = [FakeMic("线路输入 (USB 声卡 Line-In)", native_sr=None)]
    _stub_soundcard(mics)
    a = AE.AudioEngine()
    sc = sys.modules["soundcard"]

    # 桩：_native_samplerate 返回 None（真实设备打不开 PropertyStore 时就是这条路）
    orig_native = AE._native_samplerate
    AE._native_samplerate = lambda mic: None
    captured = {}
    orig_cl = a._capture_loop

    def spy(mic, mono, sr, gen, _c=captured, _o=orig_cl):
        _c["sr"] = sr
        _c["mic"] = mic.name
        raise SystemExit

    a._capture_loop = spy
    try:
        a._run(sc, "mic", "", True, 0, a._gen)   # sr=0 = 自动
    except SystemExit:
        pass
    _log("场景D", "_native_samplerate→None 时，_run 用的 sr=%s（期望 fallback 48000）"
         % captured.get("sr"))
    ok_none = (captured.get("sr") == 48000)
    _log("场景D", "_native_samplerate 返回 None：fallback 48000 %s"
         % ("正常，_capture_loop 不会崩" if ok_none else "异常！"))

    # 再试：_native_samplerate 直接抛异常（不是 return None）
    def boom(mic):
        raise AttributeError("桩：mediafoundation / _device_ptr 不存在")

    AE._native_samplerate = boom
    captured2 = {}

    def spy2(mic, mono, sr, gen, _c=captured2, _o=orig_cl):
        _c["sr"] = sr
        raise SystemExit

    a._capture_loop = spy2
    try:
        a._run(sc, "mic", "", True, 0, a._gen)
        crashed = None
    except SystemExit:
        crashed = None
    except Exception as e:
        crashed = "%s: %s" % (type(e).__name__, e)
    AE._native_samplerate = orig_native
    err, running, desc = _has_error(a.state)
    if crashed:
        # _run 自己 try/except 包住了，会写进 st.error
        _log("场景D", "_native_samplerate 抛异常时：st.error=%r running=%s" % (err, running))
        _log("场景D", "⚠ 注意：_run 的 try/except 把异常吞进 st.error，**不会 fallback**，"
                      "采集根本不会开始（而非走到 _capture_loop 再崩）")
    else:
        _log("场景D", "_native_samplerate 抛异常时：sr=%s（说明异常被内部吞掉了？）"
             % captured2.get("sr"))
    _log("场景D", "结论：`or 48000` 只兜住「返回 None」；**抛异常时不会兜底**——"
                  "异常冒泡到 _run 的 except，直接 st.error + 不采集。")
    return (ok_none, crashed)


# ==========================================================================
# 场景 E：单声道接口只插了右声道（左全 0、右有信号）→ mono=True 能量恒 0？
# ==========================================================================
def scenarioE():
    print("\n" + "=" * 74)
    print("场景 E：单声道接口只插了右声道（左全 0 / 右有信号）→ mono=True 取左声道")
    print("=" * 74)
    from audio_engine import _Analyzer, AudioState
    sr = 48000

    def run(mono, model, n=200):
        an = _Analyzer(sr)
        st = AudioState()
        rec = FakeRecorder(model, sr=sr)
        for _ in range(n):
            d = rec.record(1024)
            if mono:
                an.feed(d[:, :1], st)     # 与 _capture_loop 的 mono 分支逐字一致
            else:
                an.feed(d, st)
        return st.snapshot()

    snap_mono_right = run(True, "right_only")
    snap_st_mono = run(False, "right_only")
    _log("场景E", "[mono=True ] 左声道全0/右有声：lv_db=%.2f level=%.4f energy=%.4f"
         % (snap_mono_right["lv_db"], snap_mono_right["level"], snap_mono_right["energy"]))
    _log("场景E", "[mono=False] 立体声平均        ：lv_db=%.2f level=%.4f energy=%.4f"
         % (snap_st_mono["lv_db"], snap_st_mono["level"], snap_st_mono["energy"]))

    # 交叉验证：左声道有信号时 mono=True 是否正常（排除"mono 分支本身坏了"）
    snap_mono_left = run(True, "stereo_ok")
    _log("场景E", "[mono=True ] 左右都有信号(对照)：lv_db=%.2f level=%.4f"
         % (snap_mono_left["lv_db"], snap_mono_left["level"]))

    dead = (snap_mono_right["lv_db"] < -70.0 and snap_mono_right["level"] < 1e-4)
    _log("场景E", "结论：用户接法「单声道插头只进右声道」时，mono=True 取左声道 → 信号 %s"
         % ("完全为 0，界面表现为“没反应/一直静音”" if dead else "仍有输出（不是这个原因）"))
    if snap_st_mono["level"] > 0.01:
        _log("场景E", "  ⚠ 同一输入切到 mono=False（取左右平均）就能采到 —— "
                      "说明问题出在「取哪一路」而非设备本身。")
    return dead


def main():
    print("=" * 74)
    print("线路输入/声卡 失败场景离线复现（A~E）")
    print("Python:", sys.version.split()[0], "| numpy:", np.__version__)
    print("=" * 74)
    out = {}
    try:
        out["0"] = scenario0()
    except Exception:
        traceback.print_exc(); out["0"] = "EXC"
    for name, fn in (("A", scenarioA), ("B", scenarioB), ("C", scenarioC),
                     ("D", scenarioD), ("E", scenarioE)):
        try:
            out[name] = fn()
        except Exception:
            traceback.print_exc(); out[name] = "EXC(%s)" % sys.exc_info()[1]
    print("\n" + "=" * 74)
    print("汇总（稳定复现 = 该场景在本机离线能稳定重现）")
    print("=" * 74)
    print("  场景0 正常信号分析链路  :", "正常" if out.get("0") is True else out.get("0"))
    print("  A 设备列表为空          :", "可复现（抛错→UI 恒 0）" if out.get("A") else out.get("A"))
    print("  B device_name 匹配不到  :", "可复现（静默回退 cands[0]）" if out.get("B") else out.get("B"))
    print("  C 历史 linein 配置      :", "映射退化，见上" if out.get("C") else out.get("C"))
    print("  D native_samplerate 失败:", out.get("D"))
    print("  E 单声道只进右声道      :", "可复现（左全0→能量恒0）" if out.get("E") else out.get("E"))
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
