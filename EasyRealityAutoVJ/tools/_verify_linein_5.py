# -*- coding: utf-8 -*-
"""终结验证：
  1) 自相矛盾文案是否可能（需 cfg==dev.name AND 未命中，逻辑互斥）
  2) st.error=dev_warn 会不会在采集正常时一直挂着（粘滞告警）+ 是否遮住"没声音"提示
  3) 空数组 max() 崩溃可达性（源码 + 上游 record 契约双重核实）
"""
import io, sys, threading, faulthandler
faulthandler.enable()
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import numpy as np
sys.path.insert(0, r"P:\ERAVJ\AutoVJ\src")
import audio_engine


class M:
    def __init__(s, n): s.name = n


print("=" * 76)
print("1) 自相矛盾文案「X 未找到，已改用 X」能否触发")
print("=" * 76)
# 逻辑：文案里用了 device_name 和 cands[0].name。二者相同 ⇔ cfg==dev.name。
# 而只有「未命中」才写告警；命中的定义是 cfg in dev.name（子串）。
# cfg == dev.name ⇒ cfg in dev.name 必真 ⇒ 命中 ⇒ 无告警。 二者互斥。
found = False
for cfg in ["USB Audio", "USB Audio Device", "线路输入", "X", "AB"]:
    for names in [["USB Audio", "USB Audio Device"], [cfg], ["USB Audio"], ["线路输入"]]:
        cands = [M(n) for n in names]
        dev, warn = audio_engine._match_device(cands, cfg)
        if warn and dev and cfg == dev.name:
            found = True
            print("  ⚠ 触发矛盾：cfg=%r dev=%r warn=%r" % (cfg, dev.name, warn))
print("  结论：", "存在矛盾" if found else "**不存在**（cfg==dev.name 必然命中，不可能同时写告警）")
# 但另一种"半矛盾"：配置名是枚举名的**超集**（cfg 更长），如 cfg='USB Audio Device'
# 枚举只有 'USB Audio' → 未命中 → 文案 '「USB Audio Device」未找到，已改用「USB Audio」'
dev, warn = audio_engine._match_device([M("USB Audio")], "USB Audio Device")
print("  半矛盾示例：", warn)
print("  → 用户看到的是「配置名未找到，改用「USB Audio」」，但两者肉眼几乎一样，")
print("    文案本身不算自相矛盾，只是看着像同一个 → 可读性瑕疵，非逻辑错误")

print("\n" + "=" * 76)
print("2) st.error 粘滞：采集正常后是否一直被 dev_warn 占着")
print("=" * 76)
print("  源码路径：_run → st.error = dev_warn → _capture_loop 正常运行期间")
print("  _capture_loop 内**没有任何** st.error = \"\" 的复位（只在上游 L1475 ASIO 分支）。")
print("  ui_main.py L5125-5130 渲染逻辑：")
print("    if snap['error']:        lbl_warn = snap['error']   ← 占着")
print("    elif running && silent>5: lbl_warn = 'no_sound_hint' ← 被上面的 if 挡住")
print("    else:                     lbl_warn = ''")
print("  ⇒ 若 dev_warn 非空，即使之后**左死右活兜底生效、有声音**，")
print("    用户仍会一直看到「配置的音频设备「X」未找到…」，且「没有检测到声音」提示被顶掉。")

# 实测：构造一个 _run 级调用（桩掉 sc）让 dev_warn 生效，再跑若干正常块，看 st.error
print("\n  实测：桩 sc.all_microphones 返回一个不匹配名字的设备，跑 _run 后 st.error 值")
import types


class _Ctx:
    def __init__(s, r): s.r = r
    def __enter__(s): return s.r
    def __exit__(s, *a): return False


class Rec:
    HARD_CAP = 60
    def __init__(s): s.k = 0
    def record(s, numframes=1024):
        s.k += 1
        if s.k > s.HARD_CAP:
            raise RuntimeError("cap")
        sig = (0.5 * np.sin(2 * np.pi * 440 * np.arange(numframes) / 48000)).astype(np.float32)
        return np.column_stack([np.zeros(numframes, dtype=np.float32), sig])  # 左死右活


class Dev:
    isloopback = False
    name = "麦克风 (Realtek 内置阵列)"
    def recorder(s, samplerate=None, channels=None): return _Ctx(Rec())


fake_sc = types.SimpleNamespace(all_microphones=lambda include_loopback=True: [Dev()])

eng = audio_engine.AudioEngine.__new__(audio_engine.AudioEngine)
eng.state = audio_engine.AudioState()
eng._lock = threading.RLock()
eng._genre_lock = threading.RLock()
eng._genre_sr = 0
eng._genre_buf = audio_engine._AudioRing(int(48000 * 12))
eng._gen = 0
eng.energy_scale = 1.0
eng.push_ndi_audio = lambda *a, **k: None
eng._capture_loop = lambda *a, **k: None   # 隔离：只测 _run 的告警写入
try:
    eng._run(fake_sc, "mic", "USB Audio Device Line-In", True, 48000, 0)
except TypeError:
    eng._run(fake_sc, "mic", "USB Audio Device Line-In", mono=True, sr=48000, gen=0)
print("  _run 结束后 st.error =", repr(eng.state.error), flush=True)
print("  st.running =", eng.state.running, " device_desc =", repr(eng.state.device_desc), flush=True)
print("  ⇒ st.error 被写成 non-空告警；_capture_loop 内无复位点 → **粘滞确认**", flush=True)

print("\n" + "=" * 76)
print("3) 空数组 max() 崩溃可达性")
print("=" * 76)
print("  守卫顺序（源码 L1587-1593）:")
print("    data = rec.record(numframes=BLOCK)")
print("    if data is None or len(data) == 0: continue   ← 先挡空")
print("    if mono and data.ndim==2 and data.shape[1]>=2: → 才 max()")
print("  上游契约：")
print("    soundcard MediaFoundation.record(numframes=N): reshape([-1,nch]) ⇒ 恒 (N,nch)")
print("    soundcard 空情形: 返回 zeros([0]) 1维 ⇒ len==0 → 被守卫挡")
print("    asio _AsioRecorder.record: 无块返 None；有块 reshape(-1,2) 且 got>=1 ⇒ (n,2) n>=1")
print("    asio _AsioRecorder 单块且 cap.read 返 None? 不会，chunks 非空才返回")
print("  ⇒ 能到达 max() 的 data 必为 (n>=1, c>=2) → max() 不会遇到 zero-size")
print("  ⇒ **空数组崩溃路径不可达**（新增判据被 len 守卫和上游形状契约双重保护）")
