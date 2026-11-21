# -*- coding: utf-8 -*-
"""音频采集与分析引擎
- 系统声音：WASAPI Loopback（soundcard 库）
- 麦克风 / 线路输入：普通录制设备
- 分析：FFT 频段能量、RMS 电平、节拍检测（谱通量+自相关 BPM 估计）、Drop 检测
- 行为模式指标：瞬态密度 / BPM 稳定性 / 节拍清晰度 / 频谱复杂度 / 能量趋势 / 人声占比 / 段落识别

⚠ 线程模型（重要）：
soundcard 库导入时会在【当前线程】一次性 CoInitializeEx(MTA)，且没有按线程初始化。
因此本模块把 soundcard 的导入、设备枚举、录音全部收敛到一条常驻后台线程（_worker_loop），
绝不能在 GUI 主线程触碰 soundcard，否则 Qt OleInitialize 失败（0x80010106），
系统文件对话框（IFileDialog 需要 COM STA）会直接卡死。
"""
import math
import io
import os          # `_rec_diag` 写 cue_health.log 用（本模块原先没有，漏了会 NameError）
import time
import queue
import threading
from collections import deque
import numpy as np

# ★ 局域网音频（自研轻量协议）：放在模块级 import —— `_run_lan` 与 `_capture_loop`
#   都要用；放函数内每块 import 会走一次 sys.modules 查找（热路径，不划算）。
import lan_audio
import lan_audio_recv

BLOCK = 1024

# NDI 音频转发队列上限（单位=块）：BLOCK=1024 @48kHz ≈ 21ms/块 ⇒ 48 块 ≈ 1 秒。
# 满了自动丢最老的块 —— 宁可少发一秒音频，也绝不让采集线程等（见 _ndi_audio_loop）。
_NDI_Q_MAX = 48

# ==========================================================================
# 能量公式的可调参数（**抽成模块常量只为能做离线 A/B**；默认值 = 原行为，一字未改）
#   诊断见 tools/_energy_ab.py：2026-09-28 查「劲爆 drop 却只有 0.4」时发现
#   `kick_e` 的映射窗口极窄（(rel-0.48)/0.30 ⇒ 只有 rel 0.48~0.78 这一段产出，
#   全曲峰值 50% 的段落直接判 0），叠加 bass/fluct 同类死区与人声惩罚后，
#   「响度只差 1.4 dB、但低频冲击是全曲峰值 50%」的段落就掉到 0.35。
# ==========================================================================
EN_TAU_LOUD_BASE = 80.0            # 响度自适应基线时间常数（秒）
EN_W_LOUD, EN_W_EV = 0.4, 0.6      # 响度项 / 低频证据项的融合权重
EN_KICK_DEAD, EN_KICK_SLOPE = 0.48, 0.30
EN_BASS_DEAD, EN_BASS_SLOPE = 0.45, 0.22
EN_FLUCT_DEAD, EN_FLUCT_SLOPE = 0.45, 0.22
EN_VOC_MAX, EN_VOC_KNEE, EN_VOC_SPAN = 0.45, 0.30, 0.35
# 绝对响度兜底项（2026-09-28 新增，见 feed() 内注释）：固定 dBFS 窗口，不用任何自适应基线
EN_ABS_LO_DB, EN_ABS_HI_DB = -14.0, -6.0
EN_LV_TAU = 1.0                    # 绝对响度（RMS dBFS）的平滑时间常数（秒）
EN_ABS_W = 1.0                     # 兜底项权重（0 = 关闭该项，用于 A/B 对照）
# ★★ 2026-11-01（用户「放 hard 能量一直上不去」）：「全曲慢峰」只升不降这件事，
#   会让**一段持续大声**在开头把峰顶高、之后相对值全掉进死区 ⇒ 越放越没能量
#   （实测同一段：0.47 → 0.34 逐秒下滑，而电平一点没变）。
#   这里给三个慢峰加**缓慢回落**（半衰期 `EN_PEAK_DECAY_TAU` 秒）：
#     · 0     = 关闭 = 旧行为（默认保持 0，避免未经现场验证就改手感）；
#     · 180~300 = 一场演出里"上一首的峰值"逐渐让位，但不会因为一个 breakdown 就崩掉。
EN_PEAK_DECAY_TAU = 0.0
# ★ 2026-11-01：改用**滚动窗口分位**当参考峰（>0 秒时生效；0 = 关闭 = 旧行为）。
#   与 `EN_PEAK_DECAY_TAU` 互斥：这里算的是"最近 N 秒的 90 分位"，不需要识别换歌，
#   连跑几小时也不会累积（窗口一滚自然让位）。300 秒是折中：
#   · 太短（<60s）⇒ 一个 breakdown 就没了参考；
#   · 太长（>900s）⇒ 又变回"整晚最高"。
EN_PEAK_WINDOW_SEC = 0.0
# ★ 2026-11-01 实测结论：**只把 kick 换成滚动窗口**就够，bass/fluct 保持"全曲峰"。
#   用你的录屏 A/B（连续 4 分钟 hardstyle）：
#     · 旧：E 从 0.47 一路掉到 0.40（越放越低）
#     · 只 kick 滚动 300s：0.61 → 0.63（稳住了），≥0.65 占比 27% → 53%
#   三项全滚动能到 0.77，但会把"低频稀疏的段落"一起抬起来（安静段 45% → 98%），
#   判据就废了 ⇒ 只动 kick 这一项（它是 hardstyle 的核心证据，也是被棘轮压死的那一项）。
EN_PEAK_WINDOW_KICK = 180.0
EN_PEAK_WINDOW_BASS = 0.0
# kick_density 的「真 kick」绝对强度下限（1s 低频通量累计，与 RMS 线性相关）。
# 实测：drop@-7.7dBFS ≈ 4.6~5.7、drop@-10 ≈ 4.0、钢琴 breakdown（即使拉到 -7.7）≈ 1.6、
# 钢琴@-14.9 ≈ 0.7。取 2.0 分隔「真鼓点 drop」与「钢琴/人声 breakdown 的偶发低频敲击」。
# 用于 `_peak_confirmed` 锚定保护：当前块 kick_density 达标即视为可信，不再乘 1.54 压峰。
_KICK_PEAK_ABS_FLOOR = 2.0
# 半速修正「下半拍是否够强」的相对阈值（8× 上采样直方图自相关的峰比）。
# 80+offbeat（160 处是弱 offbeat，kick 强在 80）→ 峰比 ≈0.772；真实 160（160 处是强 kick）→ ≈0.80~0.81。
# 取 0.785 分隔：只在下半拍周期**足够接近**主拍强度时才修正，防止真实 80+offbeat 被误升到 160，
# 同时保住「160 被减半成 80 仍能回正」。
_HALFSPEED_RATIO_MIN = 0.785
# ★ 2026-11-02：半速修正的**绝对强度门**——从"整数 lag 的 acn"改成"8× 上采样的峰"。
#   实测标定（OBS 22:20 那段，实际 190）：`_peak_up(10.53×8) ≈ 0.20`；
#   而正常 128 的歌在 cand=10 附近 `acn ≈ -0.11`（上采样峰也近 0）⇒ 0.15 能干净分开。
_HALFSPEED_PEAK_MIN = 0.15


def _sc():
    """导入 soundcard（只能在音频后台线程调用一次）"""
    import soundcard as sc
    return sc


def _match_device(cands, device_name):
    """在候选设备里按名字挑一个，返回 (设备, 告警文案或 "")。

    ⚠ 2026-10-07 修「线路输入用不了」：旧写法是
        mic = cands[0]
        if device_name:
            for m in cands:
                if device_name in m.name:
                    mic = m; break
      —— 名字匹配不上时**静默保留 cands[0]**（很可能是板载麦克风），
      用户配的是声卡线路输入，实际采的却是别的东西，而界面只显示实际设备名、
      没有任何提示 ⇒ 用户以为"线路输入用不了"。实测现实名字（换 USB 口 /
      驱动改名 / 中英文界面差异）落空率约一半。

    这里保留"没指定设备 → 用第一个"的合法语义（device_name 为空时不告警），
    但**指定了却匹配不到**必须让用户看得见：返回一段明确的告警文案。
    匹配语义（严格子串 `in`）与旧实现一致，不改判据、只补可见性。
    """
    if not cands:
        return None, ""
    if not device_name:
        return cands[0], ""
    for m in cands:
        if device_name in m.name:
            return m, ""
    # 匹配不到：用第一个兜底（现场不能停），但把差异说清楚
    return cands[0], ("配置的音频设备「%s」未找到，已改用「%s」。"
                      "请到「音源」重新选择设备后点应用。" % (device_name, cands[0].name))


def _native_samplerate(mic):
    """读设备的 WASAPI 原生混音采样率（PKEY_AudioEngine_DeviceFormat）。

    只在音频后台线程调用（soundcard 的 COM 限制）。返回 int 采样率，失败返回 None。
    用于「自动采样率」：采设备原生采样率可避免 WASAPI 共享模式的 SRC 重采样——
    重采样会轻微抹平瞬态（低频冲击/kick 检测的核心信号），导致能量偏低
    （用户：开 NVIDIA 录制后能量偏低——录屏把系统混音器切成 48k，软件却固定采 44.1k）。"""
    try:
        import soundcard.mediafoundation as mf
        ppPropertyStore = mf._ffi.new('IPropertyStore **')
        ptr = mic._device_ptr()
        hr = ptr[0][0].lpVtbl.OpenPropertyStore(ptr[0], 0, ppPropertyStore)
        mf._com.release(ptr)
        mf._com.check_error(hr)
        propvariant = mf._PropVariant()
        PKEY = mf._ffi.new('PROPERTYKEY *',
            [[0xf19f064d, 0x82c, 0x4e27, [0xbc, 0x73, 0x68, 0x82, 0xa1, 0xbb, 0x8e, 0x4c]], 0])
        hr = ppPropertyStore[0][0].lpVtbl.GetValue(ppPropertyStore[0], PKEY, propvariant.ptr)
        mf._com.release(ppPropertyStore)
        mf._com.check_error(hr)
        pBlob = mf._ffi.cast('BLOB_PROPVARIANT *', propvariant.ptr)
        wf = mf._ffi.cast('WAVEFORMATEX *', pBlob[0].blob.pBlobData)
        return int(wf[0].nSamplesPerSec)
    except Exception:
        return None


def _band_energy(spec, freqs, lo, hi):
    """频带平均能量（保留原接口，供外部/测试用）。

    ⚠ 热路径**不要**用它 —— 每次都要建布尔掩码再索引。`_Analyzer` 内部改用
    `_band_mean(spec, (i0, i1))`（切片求平均，索引由 `_fft_plan()` 预算好）。
    """
    m = (freqs >= lo) & (freqs < hi)
    if not m.any():
        return 0.0
    return float(np.mean(spec[m]))


def _band_mean(spec, rg):
    """按预算好的频带索引区间求平均（空区间返回 0.0，与 _band_energy 一致）。

    RFFT 的频率轴是单调递增的 ⇒ `freqs >= lo & freqs < hi` 一定是**连续区间**，
    可以直接用切片，不必每次建掩码。
    """
    i0, i1 = rg
    if i1 <= i0:
        return 0.0
    return float(np.mean(spec[i0:i1]))


FP_USE_HINT = False   # 见 `_recognize_loop` 里的对照实验说明

# ★★ 2026-10-27（实机验收 A 组：重复段落 off 乱跳）：
#   hint 不再用「拍钟预测」（`FP_USE_HINT`，历史踩坑：把 off 锁死在预测附近、落后 8~14s），
#   改用**上一窗实际匹配到的 off ± 该秒数**作为连续性窗。重复段落有 3~4 个等票位置时，
#   全局众数会随窗口滑动在几处之间跳（实测 off 14.53 → 176.37 → 18.25 → 331.86 → 22.85）；
#   限制在「上一帧附近」后，只有当窗内**确实没有候选**（换歌 / seek / 长时间丢锁）才退回
#   全局众数 —— 那条兜底在 `fp._pick` 里（窗内无候选 ⇒ 走全局），所以换歌不会被粘住。
FP_PREV_HINT_SEC = 2.0   # 上一帧 off 的连续性窗半宽（秒）；0 = 关闭
# ★★ 2026-10-27（实机验收 C 组：B 站 200BPM Frenchcore 误锁到 142BPM 的西行谣）：
#   加「库内 BPM ↔ 实时 BPM」一致性校验。误识别差 41%（200 vs 142），而真正的变速样本
#   只有 ±3/5/8% ⇒ 单条 15% 容差就能干净分开，且物理上说得通（同一首歌变速不会差 40%）。
#   ⚠ 允许**倍频关系**（0.5× / 2×）：BPM 检测的半速/双速（64 vs 128）是常见现象，
#     不能因为检测成半速就把正确匹配杀掉（那样会引入掉锁回归）。
# ★ 2026-10-27 第二轮实机：15% → **20%**。变速 8% 叠上实时 BPM 估计误差
#   （实测同一首在 126.5~141.5 之间跳，≈12%）就会破 15%，把 D 组正确匹配误杀（只认出 1/3）；
#   而 C 组误识别是 41%，20% 仍挡得住。倍频放行**也用这个常量**（不写死 0.15）。
BPM_MATCH_TOL = 0.20
BPM_OCTAVE_SET = (0.5, 1.0, 2.0)
# ★★ 2026-10-27 第三轮实机（Round5：128BPM 的 Raccoon Memes 全程认不出，41 次掉锁）：
#   实时 BPM 估计器在**双倍**时会 `min(220)` 钳顶 ⇒ 128 的真实双倍 256 被钳成 219.5，
#   这是个「既不是真值、也不是整数倍」的**非法值**：219.5/128 = 1.72 不在倍频集里 ⇒
#   BPM 校验把一条 `phr=0.97`（97% 哈希命中）的**正确匹配**判成误识别。
#   两条修法：
#     ① 撞顶（≥ BPM_RAIL，即落在 min(220) 上限 2 BPM 内）⇒ 这个数**不携带信息**，跳过校验；
#     ② 谐波集补上 1/3、3 —— 检测器估成三倍/三分之一同样常见（与 0.5/2 同一个道理）。
#   ⚠ 受影响的是 110~183 BPM 的**主力曲速段**（双倍后必然撞顶），不是边缘情况。
BPM_RAIL = 218.0# ★★★ 2026-10-27 实机定案：谐波集**只能是 (0.5, 1, 2)**。
#   曾扩到 (1/3, 3) 并加"用未钳位原始值判谐波"的兜底，实机立刻在 C 组误锁：
#   日志证据 `lbpm=180.0 rbpm=219.5 brel=0.220` 本该拒绝，但原始值 360/180=2.0
#   正好构成谐波 ⇒ 放行。**只要允许"任何倍率关系"，BPM 否决就失去意义**（检测器几乎总能
#   找到某个 k 使 lbpm×k ≈ rbpm）。⇒ 恢复固定两倍关系，且只用钳位后的值。
BPM_HARM_SET = (0.5, 1.0, 2.0)
# ★★ 2026-10-27 第二轮实机（A 组 off 中位偏差 12.49/14.42s）：hint ±2s 兜不住重复段落
#   —— 相邻两窗可能**连续**跳到另一处等票候选。改成 off 状态机：
#   已锁定且同 sid 时，新 off 必须落在「上一**已接受**值 + 墙钟推进」的 ±TOL 内，
#   否则丢弃本次跳变（保持期望值）并累计异常；连续异常超过 MAX 才认定为真 seek。
OFF_STATE_TOL_SEC = 2.0
OFF_STATE_ABNORMAL_MAX = 6


def _fp_lead(fp_db):
    """「第一名票数 / 第二名票数」——指纹的领先倍数（铁律 33）。

    没有候选（只认到一首或全空）⇒ 给一个大值（不构成阻碍）。
    """
    try:
        best = dict(getattr(fp_db, "last_song_best", {}) or {})
    except (TypeError, ValueError):
        return 99.0
    if len(best) < 2:
        return 99.0
    v = sorted(best.values(), reverse=True)
    if v[1] <= 0:
        return 99.0
    return float(v[0]) / float(v[1])


def lib_bpm_publish_ok(sid, sid_conf, bpm_ok_flag, hit, tot, lead=0.0):
    """「这首歌的库内 BPM」能不能发布给界面/素材倍速用。

    ★★★ 2026-11-02（用户直播：「200+ BPM 的歌，素材速度没跟上来」）。
      BPM 一致性校验否决的是**「曲目 + 位置」这一对**（防止把别首歌的位置硬套上来）。
      但指纹 `hit=230/248`（92.7%）这种命中率已经足够说明"就是这首歌" ——
      此时连库内 BPM 一起丢掉，会让**素材倍速**（`engine.content_speed`）只剩
      卡死的实时值可用（现场 133.5 / 128 = 1.04×，而正确是 200 / 128 = 1.56×）。
      ⇒ 指纹确认过 sid（哪怕随后被校验清零）时，库内 BPM 照常发布。
      ⚠ 只放宽"发布库内 BPM"，**不**放宽识别与位置（防误识别的护栏原样保留）。
    """
    try:
        if float(lead or 0.0) < LIB_BPM_LEAD_MIN:
            return False        # 认不准是哪首歌 ⇒ 它的 BPM 也不能拿来用
        if int(sid) >= 0 and int(bpm_ok_flag or 0) != 0:
            return True
        return bool(int(sid_conf) >= 0 and int(bpm_ok_flag or 0) == 0
                    and float(hit or 0.0) >= 0.50 and int(tot or 0) >= 30)
    except (TypeError, ValueError):
        return False


def fold_bpm_to_range(raw, lo=60.0, hi=220.0):
    """把超范围的 BPM 按 **2×** 折回 `[lo, hi]`（实时估计器的双倍歧义修正）。

    ★★★ 2026-10-31（用户「实时分析的 bpm 怎么一直 219」）：
      175 BPM 的歌在 drop / 密集段落会被估成 **350**（每个 8 分音符算一拍），
      老代码用 `min(220)` 把 350 钳成 **219.5** —— 这个值比原始误判更糟，三处全错：
        · 拍钟：`60/219.5 = 0.273s`，真实是 `60/175 = 0.343s` ⇒ 拍间隔短 20%，卡点全错；
        · 校验：`219.5/175 = 1.254` 不在谐波集 `(0.5,1,2)` ⇒ 本来正确的匹配被当误识别否决；
        · 锁定：60s 众数被 219.5 灌满 ⇒ 20 秒后连已锁定的 175 都被解掉（实测 110s 处复现）。
      ⇒ 钳位前**先折半**：`350 → 175`、`440 → 220`、`256 → 128`。

    ⚠ 只折 2 的幂（最多两次，`_bpm_raw` 上限 400 ⇒ 实际一次），**绝不折 3×/5×**：
      那会把 `262（真实 131）` 折成 87，制造新的错误；也会让"任意倍率"重新成立，
      把 2026-10-27 定案的 BPM 否决护栏拆掉。
    ⚠ 折完仍高于 `hi` 才钳（例如 `250 → 125` 正常；`470 → 235 → 117.5` 两折）。
    """
    try:
        b = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if b <= 0.0:
        return 0.0
    for _ in range(2):
        if b <= hi:
            break
        half = b / 2.0
        if half < lo:
            break
        b = half
    return max(lo, min(hi, b))


# ==========================================================================
# 识别锁定门槛（fp_lock_gate）—— 阈值抽成模块级常量，便于离线回归与 QA 复验
# ==========================================================================
LOCK_VOTES_STRICT = 80     # ① 常规直通：单点票
LOCK_TOT_STRICT   = 80     # ① 常规直通：总票
LOCK_TOT_WARP     = 120    # ② 变速兜底：总票
LOCK_VOTES_WARP   = 15     # ② 变速兜底：单点票
LOCK_HIT_WARP     = 0.60   # ② 变速兜底：命中率
LOCK_LEAD_WARP    = 2.0    # ② 变速兜底：领先倍数
LOCK_HIT_RECODE   = 0.85   # ③ 重编码兜底：命中率（比 ② 更严）
LOCK_TOT_RECODE   = 40     # ③ 重编码兜底：总票下限（只挡"总共几十票"的弱匹配）
LOCK_VOTES_RECODE = 5      # ③ 重编码兜底：单点票下限（白噪峰值≤4，5 是安全下界）
LOCK_LEAD_RECODE  = 3.0    # ③ 重编码兜底：总票领先倍数（真歌必胜，硬误无领先）
# ★★ 2026-10-27 Round5：「连续一致」第四条放行通道（同专辑近似曲目专用）。
#   背景：库里同一张专辑的曲目共用采样/loop，`总票` 会被分走 —— 实测正确曲 tot=708
#   而第二名 982、646 对 801 ⇒ ②③ 的「领先倍数」恒不成立 ⇒ 正确匹配永远锁不上。
#   但正确曲能**连续多窗胜出且 offset 随墙钟线性推进**（错歌几乎做不到这两件事同时成立）。
#   四条同时满足才放行（比 ② 更严的票数下限、更长的连续窗）：
# ★★ 2026-10-27（对照 KVJ/KDJ 源码后重写）：**必须是"互不重叠"的两次观测**。
#   原实现是"连续 3 窗同名 + offset 按墙钟推进"，看起来 3 份证据，其实**不是**：
#   识别窗 4.5s、每 0.8~1.2s 一拍 ⇒ 相邻窗重叠 75~90%，三次"一致"≈同一次证据的三种说法，
#   一次偶然对齐会顺着重叠自己延续下去（KVJ 源码原话：only windows that do not overlap
#   are independent evidence）。实机代价：C 组「同作者同曲风」的歌被误锁。
#   ⇒ 改成：两次观测**相距 ≥ 窗口长度**（真正不重叠）、同一首、隐含倍率 ∈[0.85,1.18]、
#     且两次各自都要过"证据下限"。两次独立窗口同时命中，才是可采信的证据。
LOCK_AGREE_MIN_SEC = 4.5   # 两次观测的最小间隔（= FP_WIN_SEC，保证不重叠；见下）
LOCK_AGREE_RATE    = (0.85, 1.18)   # 隐含倍率允许区间（off 前进 / 墙钟前进）
LOCK_AGREE_HIT     = 0.80   # 命中率下限（比 ② 的 0.60 严）
LOCK_AGREE_TOT     = 150    # 总票下限（比 ② 的 120 严，补偿"不要求领先"）
LOCK_AGREE_VOTES   = 20     # 单点票下限（比 ② 的 15 严）
# ★★ 2026-10-27 实测（Round6 首次上机就翻车）：**必须加"票集中度"**。
#   拿一首「不在曲库」的 B 站曲（Rolling Contact - Million Miles，同作者同曲风）离线实测：
#     hit_rate = **0.96~0.99**、tot = 98~208，但单点票只有 **3~6** ⇒ pv/tot ≈ 0.03。
#   ⇒ `hit_rate` 在本机这个规模的库上**对任何音乐都接近 1.0**，它不能证明"库里有这段录音"；
#     去掉「2× 领先」以后，光靠 hits+总票就足以让一首无关的歌锁上（实测误锁到「西行谣」）。
#   而真歌（Raccoon Memes）pv/tot = 0.18~0.27。⇒ 用 pv/tot ≥ 0.12 把两者分开。
LOCK_AGREE_CONC   = 0.12   # 单点票 / 总票 的集中度下限（真歌 ≥0.18，无关曲 ≤0.05）
# ★★★ 2026-10-28：**频带分数救援通道（band2）**——用「白化频带相关 + 不重叠印证」
#   替代票数口径，专治变速（D/E 组：×0.94/×1.05 票被摊薄到 4~8，任何票数通道都够不到）。
#   为什么不是单窗给阈值：实测无关曲在**全库自由扫位置+倍率**下能拿 0.303，
#   比真歌（0.19~0.22）还高 ⇒ 单窗分数不能判身份。
#   但"同一首歌、两次**互不重叠**的窗口、给出同一个（位置+倍率）映射"就不可轻易凑出。
# ⚠⚠ 2026-10-28 实机定案：**默认关闭**。
#   同配置连跑三次 C 组：0/3、0/3、**1/3 误锁**（第 3 次 Faded 在 41s 锁到 Raver Stone）。
#   说明这条通道的分数处在阈值边缘 —— 单窗 0.22 的门槛对同社团素材挡不住，
#   而"不重叠互证"能压住大部分、压不住全部。**不确定的假阳性比认不出更糟**，故关闭。
#   保留实现与单测（fp_band_agree_ok / _band_try / BAND_* 常量）：
#   把身份判定换成"分段对齐"（KDJ sections.rs 的做法）之后，这条通道可以再接回来。
#   ★ 与它同批上机的「倍率感知位置推进」（off 期望 = dt×rate）**保留生效**，
#     那是纯粹的时序正确性修复，不参与认不认的判定。
# ★★ 2026-10-29：chroma 复核（KDJ 式的 12 音级能量相关）。
#   只对**已锁定**的曲目做，用上一帧位置/倍率限定搜索范围（±30 帧 / ±0.08）。
CHROMA_TARGET_SPAN = 30      # 位置搜索半宽（100ms 帧）= ±3 秒
CHROMA_RATE_SPAN = 0.08      # 倍率搜索半宽
CHROMA_GATE = 0.55           # 放行门槛（同曲实测 0.60~0.98、异曲 0.14~0.17）
# chroma 高分时可以**纠正** BPM 比值（两者差 >0.10 才动手）。实测同曲 1.00x=0.97、
# 1.08x=0.86、1.20x=0.81，而异曲 0.14~0.30 ⇒ 0.70 是干净的分界。
CHROMA_TRUST_SCORE = 0.70
# ★ 2026-10-29：最弱窗 / 领先的门槛。数值与 `bandfeat.CHROMA_WEAK_MIN/CHROMA_LEAD_MIN`
#   一致（标定依据写在那边），这里**故意重复定义**而不是 import ——
#   `bandfeat` 在引擎里一直是**惰性 import**（侧车库不存在时整条通道降级、
#   主功能照跑）。改成模块级 import 会让 bandfeat 变成硬依赖，一旦它出问题
#   整个引擎起不来。宁可重复两个数字。
CHROMA_WEAK_MIN = 0.42       # 最弱窗下限（三段里最低的一段）
CHROMA_LEAD_MIN = 0.15       # 领先下限（得分减次选）
# ★★★ 2026-10-29 真机（用户放「System Overloaded」一分钟一次都没认出来）：
#   BPM 否决发生在**任何验证之前**，所以 chroma 根本没机会说话。实测那一窗：
#   `hit=268/278`（96% 命中率，歌肯定在库里）、`pick` 也对，但实时 BPM 检测锁到
#   **219.5**（这首歌实际 175，219.5/175 = 1.254 是 5:4 的节奏层级关系），
#   直通容差 20% 与「2 的幂」谐波全不合格 ⇒ `bpmok=0` ⇒ 整窗作废。
#   ⚠ 不能直接跳过 BPM 校验（2026-10-27 C 组就是这么误锁的），
#     而是**先让 chroma 复核**：它是整段音频与库内逐帧对齐，不依赖节拍层级，
#     而且离线标定过假阳性（异曲 得分 ≤0.29 / 最弱 ≤0.23 / 领先 ≤0.07）。
#   门槛比常规复核更严 —— 这是**推翻一条护栏**，宁可漏也不能错。
CHROMA_BPMVETO_ON = True
CHROMA_BPMVETO_SCORE = 0.65
CHROMA_BPMVETO_WEAK = 0.45
CHROMA_BPMVETO_LEAD = 0.20
# 允许推翻的前提：指纹本身证据够硬（真机那一窗 hit=0.96、tot=163）
CHROMA_BPMVETO_HIT = 0.90
CHROMA_BPMVETO_TOT = 120
# 全量程搜索时"变速"必须比"原速"明显更好（否则一律报 1.0）。
# 真机实测：原速播放时自由扫会把 1.08~1.23 的次优峰刷到 0.64（只比 1.0 处低一点），
# 采信它就会把前馈带跑（一次 4.27 拍/秒的位置窜动）。0.10 是能分开的量级。
CHROMA_SLOPE_MARGIN_FULL = 0.10
CHROMA_COOLDOWN = 2.0        # 每窗都做实测 ~100ms；锁定期间 2s 一次足够，给识别线程减负
# ★★★ 2026-11-02：BPM 一致性校验否决识别时，**强制让实时 BPM 重估**的冷却秒数。
#   太短（每窗都清）⇒ 票永远攒不到 20、锁不上，拍钟反而钉死在旧值；
#   太长 ⇒ 现场换歌后要等很久才纠正。20s ≈ 一次"清票 + 重新锁上"的两倍余量。
BPM_VETO_RELOCK_SEC = 20.0
# ★★★ 2026-11-02：**库内 BPM 的"证据门槛"——第一名票数必须是第二名的 N 倍**。
#   现场实测（OBS 22:20 那段，实际 190BPM）：票数 top4 = 11/10/8/8，
#   领先只有 **1.10 倍** ⇒ 那是同专辑近似曲之间"瞎猜"，`lib_bpm` 拿到的是 138（错歌）。
#   铁律 33：指纹证据不能只看命中率/总票，必须看**与第二名的领先倍数**。
#   1.6 能挡住这种近似曲互掐，同时正常认出时（领先常见 3~10 倍）完全不受影响。
LIB_BPM_LEAD_MIN = 1.6
# ★★★ 2026-10-29 冷启动 chroma（真正的「变速也能认出来」那一环）
#   问题：0.80× 时指纹票被摊到几十个位置，`fp_lock_gate` 的「领先 2 倍」过不去
#   （真机实测 ptot=166 vs 第二名 141，要求 332）⇒ 锁不上 ⇒ `_chroma_try` 根本
#   没机会运行（它只在已锁定后工作）。要救冷启动只能让 chroma 自己找人。
#   做法：拿指纹**总票** top-N 当候选（实测 9/9 倍速下总票第一都是真曲，只是
#   `match` 按单点票选曲会在变速时选错），对这 N 首各做一次全曲 chroma 扫描。
#   实测（3 首候选含精扫）：14~25 ms，分数 0.41~0.95（真曲）对 0.18~0.30（无关曲）。
CHROMA_COLD_ON = True        # 总开关（关掉就退回"只靠票数门槛"）
CHROMA_COLD_GATE = 0.34      # 分数门槛（见 bandfeat.CHROMA_COLD_GATE 的标定）
# ★ 2026-10-29 照 KDJ 原文补的两条（「最弱窗」和「领先」），冷启动也一样要过。
#   离线标定（4 首 × 1.00/0.80/1.20，各扫全库）：
#     真曲 12 条：分 0.412~0.948  弱 0.320~0.931  领 +0.098~+0.576
#     异曲 48 条：分 0.139~0.288  弱 -0.020~0.229 领 +0.000~+0.070
#   ⇒ 三条同时卡：正 100% / 负 0%（tools 里那一轮原话见 _test_kdj_gate2 注释）。
#   门槛比离线最低再降一档，给真机的分数衰减留余量。
CHROMA_COLD_WEAK = 0.25      # 冷启动：最弱窗下限
CHROMA_COLD_LEAD = 0.06      # 冷启动：领先下限
CHROMA_COLD_TOPN = 3         # 只看指纹总票前 N 首
# ⚠ 必须 ≤ 识别 tick 周期（0.5s）：`ChargeBarEngine.DELTA_ADVANCE_SEC=0.5` 要求
#   相邻拍的偏移推进恰好 0.5 秒，跳窗一次就变成 1.0 ⇒ 被判"不连续"而永远确认不了。
CHROMA_COLD_COOLDOWN = 0.4   # 两次尝试的最小间隔（秒）
CHROMA_COLD_POS_TOL = 1.5    # 两窗位置推进的自洽容差（秒）
# 0.80~1.25 分 **19 档**（原文「0.80-1.25× 分 19 档」）。
# 之前是 10 档（步长 0.05），粗扫偏差最大 0.025 —— refine 的 ±0.02 补不回来，
# 真曲分数被白白压低一截。
CHROMA_COLD_SLOPES = [round(0.80 + i * 0.025, 3) for i in range(19)]
BAND_RESCUE_ENABLE = False
BAND_SCORE_MIN = 0.22      # 单窗最低分（真歌实测 0.18~0.39、随机对齐 0.12~0.20）
BAND_RATE_SET = (0.96, 0.98, 1.00, 1.02, 1.04, 1.06)
BAND_OFF_SPAN = 0.25       # 在指纹给出的 off 附近 ±0.25s 内找最佳
BAND_OFF_STEP = 0.10
BAND_AGREE_MIN_SEC = 4.5   # 两次观测最小间隔（≥ 识别窗长 ⇒ 真正不重叠）
BAND_AGREE_POS = 0.25      # 两次观测"按各自倍率外推"后允许的位置差（秒）
BAND_AGREE_RATE = 0.03     # 两次观测的倍率差上限

# ★★★ 2026-10-27 实机三轮定案：**这条通道默认关闭**。
#   两版实现（连续 3 窗 / 互不重叠 2 窗）都试过，两次上机都在 C 组「无关曲」上误锁：
#     · v1 连续 3 窗 + 集中度  ⇒ C3 误锁「西行谣」（71s）
#     · v2 互不重叠 2 窗 + 集中度 ⇒ C1 误锁（41s）、C3 误锁（67s）
#   根因：两个版本都**没有"对第二名的领先"**这一条，而 `hit_rate`/总票在这台机的库规模上
#   对任何音乐都够（实测无关曲 hit 0.96~0.99）。要恢复弱素材的召回，必须上**打分校验**
#   （白化谱相关 + runner-up 领先，见 KVJ/KDJ 的做法），不是继续放宽票数判据。
#   ⇒ 保持关闭；`fp_obs_ok` / `fp_agree_ok` 两个纯函数与单测**保留**，供将来接打分通道时复用。
LOCK_AGREE_ENABLE = False


# ★★★ 2026-10-29 真机定案：**必须加"变速档"**。
#   0.80× 播放时实时 BPM 估到 **111.5**、库内 140.0 ⇒ `brel = 0.204`，只比 20% 容差
#   多 0.4 个百分点，就被一票否决 ⇒ **整个 0.80× 相位一帧都没出**（真机实测
#   `gate=bpm` 连发、`na=0`、`why=no_rt`）。而 111.5 本来就是 140×0.80 的正确答案
#   —— 这不是误识别，是 DJ beatmatch 的正常形态。
#   ⇒ 放宽容差是错的（`180/219.5` 那条 C3 误锁会被放进来，单测也覆盖了它）；
#     正解是**加一条证据更强的通道**：比值落在 ±26%（覆盖 DJ ±20% + 估计误差）
#     **且** 命中率 ≥0.80 **且** 总票 ≥120 才按"变速"放行。
#   ⚠ 撞顶值（rbpm ≥ BPM_RAIL）**绝不走变速档** —— 那是 `min(220)` 钳位出来的假数，
#     拿它当"变速证据"等于把护栏拆了（Round7 的教训）。
BPM_WARP_TOL = 0.26        # 变速档：|rb/lb - 1| 的允许范围
BPM_WARP_HIT = 0.80        # 变速档：命中率下限（与 fp_obs_ok 同档）
BPM_WARP_TOT = 120         # 变速档：总票下限（与 LOCK_TOT_WARP 同档）
# ★★ 撞顶变速档（2026-10-29 真机 0.80× 抓到）：BPM 估计器有 `min(220)` 钳位，
#   0.80× 播放 140 BPM 的歌时真实是 **112**，检测器却常常给 **219.5**（= 2×112 被压平）。
#   219.5/140 = 1.568，哪条通道都过不了 ⇒ 该相位一帧都没出（`na=0` 全程）。
#   ⇒ 撞顶时**只认 2× 这一支**（撞顶的成因就是 2×），且证据门槛抬到比常规变速档高
#     一截 —— C 组误锁的实测是 `hit 0.96~0.99 / tot 98~208`，`tot<300` 就能把它们挡在外面。
#   ⚠ 绝不放宽到 3× 或"任意 k"：那样 `lbpm×k ≈ rbpm` 几乎总能凑上，BPM 否决就废了
#     （2026-10-27 Round7 的原话）。
BPM_WARP_HIT_RAIL = 0.92   # 撞顶变速档：命中率下限
BPM_WARP_TOT_RAIL = 300    # 撞顶变速档：总票下限（C 组误锁最高 208）


def bpm_gate_ok(lbpm, rbpm, hit_rate=None, tot=None):
    """「库内 BPM ↔ 实时 BPM」一致性校验（纯函数，便于离线回归）。

    返回 `(ok, brel, flag)`：
      · `ok`   —— 是否允许这条匹配成立
      · `brel` —— 相对偏差（用于取证；任一为 0 时 None）
      · `flag` —— 1 直通/谐波 / 0 拒绝 / -2 拒绝且当窗撞顶 /
                  3 **按变速放行** / None 无法校验（有一边为 0）

    :param hit_rate: 可选，本窗哈希命中率；与 `tot` 一起给出才启用「变速档」。
    :param tot:      可选，本窗该曲总票。
    """
    try:
        lb = float(lbpm or 0.0)
        rb = float(rbpm or 0.0)
    except (TypeError, ValueError):
        return False, None, None
    if lb <= 0.0 or rb <= 0.0:
        return True, None, None
    # ★★ 2026-10-27 Round7（C 组再次误锁「西行谣」后回退）：
    #   **撞顶不能跳过校验**。曾经试过「rbpm ≥ BPM_RAIL ⇒ 值不可信 ⇒ 跳过」，结果
    #   C 组「同作者同曲风、不在曲库」的 B 站曲立刻被误锁 —— 日志里 `gate=bpm` 出现
    #   26 次，说明**这条 BPM 否决本来就在当假阳性护栏用**，跳过它等于把护栏拆了。
    #   撞顶值的根问题在**估计器的 min(220) 钳位**（真实 2× 被压平，谐波关系随之丢失），
    #   不能靠"忽略它"绕过。⇒ 恢复"只要两边都有值就校验"；
    #   `BPM_RAIL` 仅用于取证（`[R]` 里看得出这一窗是不是撞顶时段）。
    brel = abs(lb - rb) / lb
    if brel <= BPM_MATCH_TOL:
        return True, brel, 1
    ratio = rb / lb
    for _k in BPM_HARM_SET:
        if abs(ratio - _k) <= BPM_MATCH_TOL:
            return True, brel, 1
    # ★ 变速档（见 BPM_WARP_TOL 说明）：只在证据够强、且实时值没撞顶时放行
    if (hit_rate is not None and tot is not None
            and rb < BPM_RAIL
            and abs(ratio - 1.0) <= BPM_WARP_TOL
            and float(hit_rate) >= BPM_WARP_HIT
            and int(tot) >= BPM_WARP_TOT):
        return True, brel, 3
    # ★ 撞顶变速档（见 BPM_WARP_HIT_RAIL 说明）：撞顶 ⇒ 只认 2× 这一支
    if (hit_rate is not None and tot is not None
            and rb >= BPM_RAIL
            and abs(ratio / 2.0 - 1.0) <= BPM_WARP_TOL
            and float(hit_rate) >= BPM_WARP_HIT_RAIL
            and int(tot) >= BPM_WARP_TOT_RAIL):
        return True, brel, 4
    return False, brel, (-2 if rb >= BPM_RAIL else 0)


def fp_obs_ok(votes, tot, hit_rate):
    """「本窗观测是否可入库」判据（纯函数）。

    四条同时成立：命中率 ≥0.80、总票 ≥150、单点票 ≥20、**单点票/总票 ≥0.12**。
    最后一条是关键：`hit_rate` 在本机库规模上对**任何音乐**都接近 1.0
    （实测无关曲 0.96~0.99），只有「票集中在同一个对齐位置」才说明真的对上了。
    真歌 pv/tot = 0.18~0.27，无关曲 0.03~0.05。
    ⚠ 这条判据曾被改写时漏掉一次，实机立刻误锁（2026-10-27 Round10），故抽成纯函数 + 单测。
    """
    try:
        v, t = int(votes or 0), int(tot or 0)
        hr = float(hit_rate or 0.0)
    except (TypeError, ValueError):
        return False
    return (v >= LOCK_AGREE_VOTES
            and t >= LOCK_AGREE_TOT
            and hr >= LOCK_AGREE_HIT
            and v >= LOCK_AGREE_CONC * max(1, t))


def fp_band_agree_ok(now, sid, off, rate, obs):
    """「频带观测的不重叠印证」（纯函数，便于离线回归）。

    当前观测 `(now, sid, off, rate)` 与历史里某条观测是否互证：
      · 同一首；
      · 相距 ≥ `BAND_AGREE_MIN_SEC`（= 识别窗长 ⇒ 真正不重叠）；
      · 用旧观测的倍率外推到现在，位置差 ≤ `BAND_AGREE_POS`；
      · 两次倍率差 ≤ `BAND_AGREE_RATE`。
    单窗分数给不出身份（无关曲也能到 0.30），但"两次独立窗口给出同一个位置+倍率"给得出。
    """
    try:
        n, s0, o0, r0 = float(now), int(sid), float(off), float(rate)
    except (TypeError, ValueError):
        return False
    for it in obs or ():
        try:
            t1, s1, o1, r1 = float(it[0]), int(it[1]), float(it[2]), float(it[3])
        except (TypeError, ValueError, IndexError):
            continue
        if s1 != s0:
            continue
        dt = n - t1
        if dt < BAND_AGREE_MIN_SEC:
            continue
        if abs(r0 - r1) > BAND_AGREE_RATE:
            continue
        if abs((o1 + dt * r1) - o0) <= BAND_AGREE_POS:
            return True
    return False


def fp_agree_ok(now, sid, off, obs, min_sec=None, rate_lo=None, rate_hi=None):
    """「互不重叠印证」判据（纯函数，便于离线回归）。

    `obs` = 历史观测序列 `[(t, sid, off), ...]`（只含"自身证据达标"的窗口）。
    当前观测 `(now, sid, off)` 与其中一条**相距 ≥ 窗口长度**（真正不重叠）、
    **同一首**、且**隐含倍率 = Δoff/Δt 落在允许区间**的观测相互印证时返回 True。

    ⚠ 为什么不能用"连续 N 窗"：识别窗 4.5s、每 0.8~1.2s 一拍 ⇒ 相邻窗重叠 75~90%，
      三次"一致"其实是同一次证据的三种说法，偶然对齐会顺着重叠自己延续
      （KVJ 源码原话：only windows that do not overlap are independent evidence）。
    """
    try:
        _min = LOCK_AGREE_MIN_SEC if min_sec is None else float(min_sec)
        _lo = LOCK_AGREE_RATE[0] if rate_lo is None else float(rate_lo)
        _hi = LOCK_AGREE_RATE[1] if rate_hi is None else float(rate_hi)
        n, s, o = float(now), int(sid), float(off)
    except (TypeError, ValueError):
        return False
    for _ot, _osid, _ooff in obs or ():
        try:
            if int(_osid) != s:
                continue
            dt = n - float(_ot)
            if dt < _min:
                continue
            if _lo <= (o - float(_ooff)) / dt <= _hi:
                return True
        except (TypeError, ValueError, ZeroDivisionError):
            continue
    return False


def fp_lock_gate(sid, votes, tot, second_best, second_tot, hit_rate):
    """初次锁定「这首歌能不能认」的证据门槛（纯函数，便于离线回归）。

    `votes`  = 最佳**单一对齐位置**的票数（`fp.match` 的返回值）
    `tot`    = 该曲**跨所有对齐位置**的总票（重复段落会把票摊开）
    `second_best` = 其它歌里**最佳单对齐票**的最高值
    `second_tot`  = 其它歌里**总票**的最高值
    `hit_rate` = 查询哈希里有多大比例在库里出现过（0~1）

    三条判据（**或**关系）：

    ① 常规直通：`tot ≥ 80 且 votes ≥ 80`
       原速播放时单点票很高（实测真歌 110~227），跨歌误认只有 30~61 —— 这条最干净。

    ② 变速兜底（2026-10-25 加，用户实测「DJ 录的视频」认不出）：
       DJ 混音视频常被 beatmatch 调速：**保留音高、只改速度** ⇒ 谱峰频率不变、
       哈希照样命中（实测 94~97%），但位置整体漂移 ⇒ 票被摊到十几个 delta，
       单点只剩 15~90，被"单点 ≥80"整条拦死。此时四条同时成立才放行：
         · 命中率 ≥60%     —— 库里确实有这段录音（别的歌到不了这个数）
         · 总票 ≥120       —— 证据总量够（4.5s 窗口实测 240~750）
         · 总票 ≥2× 第二名 —— 混音里另一首可能同时可闻，只要求总票明显领先
         · 单点 ≥15 票     —— 不是纯噪声凑出来的
       ※ 四条**同时**成立才放行，缺一不可；放行时会记 `gate=warp`。

    ③ 重编码/低单点兜底（2026-10-27 加，QA 实测 B 站重编码正确 top1 被门栏几乎全拦）：
       B 站/重编码音频「正确 top1 + 高命中率 + 低单点票」（揽月 votes 6~9），
       够不到 ① 的 `单点≥80`、② 的 `单点≥15`。把「单点票」降级为弱噪声下限（≥5，
       白噪峰值≤4），改由「命中率 ≥85% + 总票 ≥40 + 总票 ≥3× 第二名」守底线：
       硬误发生时**真歌也在库里**，真歌总票 ≥ 误匹配歌总票 ⇒ 领先 ≥3× 对硬误恒不成立、
       对正确（byte-identical / 真重编码）恒成立。放行时记 `gate=recode`。
    """
    if sid is None or int(sid) < 0:
        return False
    try:
        votes, tot = int(votes or 0), int(tot or 0)
        second_best, second_tot = int(second_best or 0), int(second_tot or 0)
        hit_rate = float(hit_rate or 0.0)
    except (TypeError, ValueError):
        return False
    # ① 常规直通（原速高票，最干净，逐值不变）
    if tot >= LOCK_TOT_STRICT and votes >= LOCK_VOTES_STRICT:
        return True
    # ② 变速兜底（DJ 混音/调速，逐值不变）
    #   ★ 2026-11-11：曲库有同曲的不同版本时，`second_tot` 会被另一个版本抬高，
    #   但 `second_best`（另一首的最佳单点）仍然很低（现场 A 组 5 票 vs 正确 67 票）。
    #   此时不该因为"总票没领先 2 倍"就拒绝正确歌；允许用单点票领先作为替代证据。
    _lead_ok = (tot >= LOCK_LEAD_WARP * max(1, second_tot)
                or second_best <= max(5, votes // 2))
    if (hit_rate >= LOCK_HIT_WARP and tot >= LOCK_TOT_WARP
            and votes >= LOCK_VOTES_WARP
            and _lead_ok):
        return True
    # ③ 重编码/低单点兜底（★ 新增）
    #   ⚠ 这里**不加**单点替代领先：白噪样本实测 votes=5 / tot=75 / second_best=3，
    #   一旦放宽就会被误认。白噪的单点票正好卡在 5，而 ③ 本来就是为
    #   「重编码真歌 votes 6~9」准备的 —— 让 ③ 继续用「总票 ≥3× 第二名」守门。
    if (hit_rate >= LOCK_HIT_RECODE and tot >= LOCK_TOT_RECODE
            and votes >= LOCK_VOTES_RECODE
            and tot >= LOCK_LEAD_RECODE * max(1, second_tot)):
        return True
    return False


def bpm_ratio_rate(lbpm, rbpm, flag=None):
    """由「实时 BPM / 库内 BPM」推播放倍率，并归一化到 [0.78, 1.25]。

    变速时 `实时BPM = 库内BPM × r` ⇒ 比值就是 r。BPM 检测走**自相关**，
    不受指纹票数影响 —— 这是变速工况下唯一稳定可用的独立来源。

    `flag` 来自 `bpm_gate_ok`：
      · 4（撞顶档）⇒ BPM 估计器的 `min(220)` 把 2× 压平了，比值要先除 2
      · 其它 ⇒ 直接取比值，再把 2 的幂折掉（半速/双速检测是常态：
        `rb/lb = 2.0` 实际是原速，折成 1.0 才对）

    返回 None 表示「这个比值不落在支持区间」，调用方应保持原值。
    """
    try:
        lb = float(lbpm or 0.0)
        rb = float(rbpm or 0.0)
    except (TypeError, ValueError):
        return None
    if lb <= 0.0 or rb <= 0.0:
        return None
    ratio = rb / lb
    if flag == 4:
        ratio /= 2.0
    for _ in range(4):
        if ratio > 1.25:
            ratio /= 2.0
        elif ratio < 0.78:
            ratio *= 2.0
        else:
            break
    if 0.78 <= ratio <= 1.25:
        return ratio
    return None


class RateEstimator:
    """由相邻两次指纹识别的 (offset_orig, wall_t) 估计输入音乐的播放速率 r。

    原理（识别链路的事实）：识别窗口每 0.5s 前移一次，窗口起点在**原曲时间轴**上的
    偏移 `off` 也随之前移；`off` 的前移速率 = 音乐播放速率 r：

        r ≈ Δoff_原曲 / Δt_挂钟

    做法（★ 2026-10-18 真机两轮迭代后的最终形状）：
      - 只在**同一首歌连续识别**时累计；换歌 / 识别丢失时 `reset()`；
      - **野值闸门**：单步速率越界（野匹配一步跳 30~250s）⇒ 该样本直接丢，不动锚点；
      - **长基线**：用 `_hist` 里最早一条 ↔ 最新一条算速率（基线 ≥ `MIN_SPAN=2s`），
        因为 offset 本身有 ±0.2~0.3s 抖动，**相邻两次**算出来的噪声 σ≈0.4（与信号同量级）；
      - 长基线估计再走 `STEP_ALPHA` 跟随 + `MAX_STEP/OUT_N` 离群闸门（绝不死锁）。

    ⚠ 这是**输入侧**的速率，与引擎里 `speed`（输出端 VJ 素材倍速）完全是两回事，
      绝不可复用。
    """

    MIN_R = 0.6          # 合理下限（-40%，覆盖 DJ 大幅降速）
    MAX_R = 1.6          # 合理上限（+60%）
    MIN_DT = 0.2         # 挂钟增量下限（太小则噪声主导）
    # ★★ 2026-11-11（用户：码表演出和音乐差好几秒）：现场认歌线程在 5 路输出
    #   满载时会掉到 ~12 秒才出一个 offset；旧 MAX_DT=2.0 把每一段都判成“断档”
    #   ⇒ 速率估计永远停在 1.000 ⇒ 音乐实际 1.018× 时码表每秒慢 1.8%，
    #   两三分钟就累积成 3 秒偏差。放宽到 60 秒，长基线回归仍能算出真实倍率。
    MAX_DT = 60.0        # 挂钟增量上限（太大则视为不连续）
    MIN_DOFF = 0.05      # |Δoff| 下限（过小不可信）
    # ★ 2026-10-18（变速真机）：**离群闸门**替掉原来收窄的量程。
    #   原 `MIN_R/MAX_R = 0.8/1.25` 是拿"量程"当噪声过滤器 —— 副作用是 **±20% 变速本身
    #   就落在闸门外**（0.8 恰好在边界，噪声一抖就 <0.8 ⇒ 样本被丢 ⇒ 前馈倍率永远停在 1.0
    #   ⇒ 码表按 1.0 播而音乐按 0.8 播 ⇒ 稳态偏差 = 1.5×0.2 = 0.3s，再加抖动就够到 seek 门槛
    #   ⇒ **每 2 秒强跳一次**，正是用户说的"一直跳"）。
    #   改成：量程放宽到真·乐器范围，噪声交给"**单次离群不采信、连续 3 次才认**"处理
    #   —— 既是噪声过滤器，又不会把真实变速挡在外面（且绝不会永久卡死，铁律 0）。
    MAX_STEP = 0.30      # 单次与当前估计相差超过它 ⇒ 先当噪声
    OUT_N = 3            # 连续这么多次都偏离 ⇒ 认定是真的变速，直接跟
    # ★★★ 2026-10-18（第二轮真机，最关键的修正）：**必须用"多秒基线"估速率**。
    #   识别每 0.5s 出一次 offset，而 offset 本身有 **±0.2~0.3s 抖动**
    #   ⇒ 用"相邻两次"算速率，噪声 σ ≈ 0.2/0.5 = **0.4**（跟信号同量级！）
    #   ⇒ EMA 无论怎么调都是"要么不收敛、要么抖"。实测 0.80× 跑 50s 只收到 r=0.958，
    #     于是码表按 0.96 播而音乐按 0.80 播 ⇒ 系统性快了 16%。
    #   实测数据（离线 4.5s 窗口）：0.80× 时 offset 每 **2s** 前进 1.44~2.00（真值 1.60）
    #   ⇒ 基线拉到 2s 噪声立刻降到 0.1，4s 降到 0.05。
    #   ⇒ 做法：只把"**合理步进**"的样本存进历史（野匹配一步 30~250s，直接判越界丢掉），
    #     再用"历史里最早那条 ↔ 最新那条"的**长基线**算速率。
    WIN_SEC = 15.0       # 历史窗口（长基线；15s 足够 12 秒一窗的慢识别，也能较快跟真变速）
    MIN_SPAN = 5.0       # 至少跨这么久才算一次速率（比它短就等）
    STEP_ALPHA = 0.6     # 长基线估计的跟随系数（估计本身已很平滑，可以跟得快）

    def __init__(self, tau=3.0):
        self._tau = float(tau)
        self.reset()

    def reset(self):
        """清空估计（换歌 / 识别丢失 / 静音回待机时调用）。"""
        self.song_id = -1
        self._last_off = None
        self._last_t = None
        self.rate = 1.0
        self._ready = False
        self._out_n = 0           # 连续离群次数（见 MAX_STEP/OUT_N）
        self._hist = []           # [(t, off)] 只收"合理步进"的样本（长基线用）

    def update(self, song_id, off, wall_t):
        """喂一次识别结果，返回当前 r 估计（未收敛时 1.0）。"""
        if song_id is None or song_id < 0:
            self.reset()
            return self.rate
        if song_id != self.song_id:
            # 换歌：重置并本次做新基准（首帧不产速率）
            self.song_id = song_id
            self._last_off = off
            self._last_t = wall_t
            self.rate = 1.0
            self._ready = False
            self._out_n = 0
            self._hist = [(wall_t, off)]
            return self.rate
        if self._last_off is None or self._last_t is None:
            self._last_off = off
            self._last_t = wall_t
            self._hist = [(wall_t, off)]
            return self.rate
        dt = wall_t - self._last_t
        doff = off - self._last_off
        # ★ 不连续（识别断档）：**历史作废**，本次重新起锚。
        #   跨断档算斜率会把"断档期间没走的秒数"当成走动 ⇒ 速率被系统性低估。
        if not (self.MIN_DT <= dt <= self.MAX_DT):
            self._last_off = off
            self._last_t = wall_t
            self._hist = [(wall_t, off)]
            return self.rate
        r_step = doff / dt
        # ★ 野匹配一步就能跳 30~250s（r_step 几十上百）⇒ 直接丢样本。
        #   ⚠⚠ **`_last_off` 和 `_last_t` 必须一起不动**：只更 `_last_t` 会让下一次
        #   "Δoff 跨两步 / Δt 只算一步" ⇒ 速率被放大成 2 倍 ⇒ 之后全在量程外 ⇒ 永远收不到样本
        #   （2026-10-18 单测 C/F 就是这么红的：1.10 一直停在 1.000、0.85 停在 1.018）。
        #   保持两个都不动 ⇒ 下一次拿"上次可信 anchor"比，基线自动变长，反而更准。
        if not (self.MIN_R <= r_step <= self.MAX_R) or abs(doff) < self.MIN_DOFF:
            return self.rate
        # ★★ 2026-11-11（用户：原速歌被识别成 1.20/1.05）：指纹在重复段落/长混音里
        #   会偶发跳到歌里另一处（off 一次差 5~13 秒），斜率仍可能落在 0.6~1.6 内，
        #   旧逻辑会把它当“变速”采纳。这里加一道**位置连续性**闸门：
        #   与「上次锚点 + 当前倍率×dt」偏差 >3 秒 ⇒ 不参与倍率估计，直接重锚。
        #   真 seek 也只重锚、倍率保持；真变速（1.2× 跑 10s 偏 2s）仍在 3 秒内。
        try:
            _pred = float(self._last_off) + dt * float(self.rate or 1.0)
            if abs(float(off) - _pred) > 3.0:
                self._last_off = off
                self._last_t = wall_t
                self._hist = [(wall_t, off)]
                self._ready = False
                return self.rate
        except (TypeError, ValueError):
            pass
        self._last_off = off
        self._last_t = wall_t
        self._hist.append((wall_t, off))
        while len(self._hist) > 2 and (wall_t - self._hist[0][0]) > self.WIN_SEC:
            self._hist.pop(0)
        if len(self._hist) < 2:
            return self.rate
        t0, o0 = self._hist[0]
        span = wall_t - t0
        if span < self.MIN_SPAN:
            return self.rate          # 基线太短 ⇒ 噪声主导，先等
        # ★★★ 2026-10-18：**最小二乘拟合**，不再用"两端相减"。
        #   `off` 是 2~4 秒一格的阶梯，任一端点抖一格（±1s）在 4s 窗里就是 ±25% 的斜率误差，
        #   而且端点噪声与"这条样本被接受"相关 ⇒ EMA 会被系统性地拉偏（实测 r=0.970 ⇒ 慢 1.7%）。
        #   最小二乘用上全部历史点，单点噪声被摊薄，且**无偏**。
        _n = len(self._hist)
        if _n >= 3:
            _mt = sum(p[0] for p in self._hist) / _n
            _mo = sum(p[1] for p in self._hist) / _n
            _num = sum((p[0] - _mt) * (p[1] - _mo) for p in self._hist)
            _den = sum((p[0] - _mt) ** 2 for p in self._hist)
            r_win = (_num / _den) if _den > 1e-9 else self.rate
        else:
            r_win = (off - o0) / span
        if not (self.MIN_R <= r_win <= self.MAX_R):
            return self.rate
        # ★ 离群闸门：单次偏离不采信，**连续 OUT_N 次**才认定是真变速（绝不死锁）
        if self._ready and abs(r_win - self.rate) > self.MAX_STEP:
            self._out_n = int(getattr(self, "_out_n", 0) or 0) + 1
            if self._out_n < self.OUT_N:
                return self.rate
            self.rate = r_win       # 真的在变速：直接跟过去
            self._out_n = 0
            return self.rate
        self._out_n = 0
        if not self._ready:
            self.rate = r_win       # 首个可信样本：直接采用（响应快）
            self._ready = True
        else:
            self.rate += self.STEP_ALPHA * (r_win - self.rate)
        return self.rate


class AudioState:
    """线程间共享的音频分析状态"""

    def __init__(self):
        self.lock = threading.Lock()
        self.level = 0.0            # RMS 0..1
        self.bass = 0.0             # 低频能量 0..1（平滑）
        self.mid = 0.0
        self.high = 0.0
        self.energy = 0.0           # 综合能量 0..1
        self.bpm = 0.0              # 平滑后的 BPM（**已锁定**，拍钟/切换节奏只用这个）
        # ★★★ 2026-11-02：**离线 BPM 锚点**（`bpm_anchor.BpmAnchor` 写的）。
        #   只给「素材播放倍速」用，**不参与拍钟 / 切素材节奏**（那两个仍走 `bpm`）。
        #   为什么必须单独一个字段：现场 Funkot 190BPM 的自相关只能给到 95/125，
        #   而离线两级细扫稳定给 190（详见 `bpm_anchor` 模块头注释）。
        self.bpm_anchor = 0.0
        self.bpm_anchor_t = 0.0     # 锚点写入时刻（墙钟）
        self.anchor_req = 0.0       # 请求重算的时刻戳（`_Analyzer` 打标记，锚点线程消费）
        # 显示用 BPM：未锁定阶段也把「票数够、众数够集中」的估计值暴露出来给界面显示。
        # 为什么分开（2026-09-24 用户报「BPM 不显示了，一直是 0」）：
        #   建锁门控要求众数∈[115,185]，于是 慢歌(<115)、极快曲(>185) 以及
        #   **被估成半速的歌**（实测 160→80.5、180→90.5、115→76.5）**永远显示不出 BPM**，
        #   界面只剩 0。但拍钟若跟着未锁定值走，一旦它是半速值，切换节奏会错一整倍 ——
        #   所以「显示」与「驱动拍钟」必须解耦：这个字段只给界面看，不参与任何节奏计算。
        self.bpm_display = 0.0
        self.bpm_locked = False     # bpm_display 是否已是锁定值（界面据此加「≈」前缀）
        # ★★ 2026-10-28（用户报「本地分析 185 / 实时 182~183」）：
        #   实时 BPM 是「自相关找鼓点周期 + 60s 1BPM 直方图众数 + 指数平滑」，
        #   fps≈86 时 1 帧 = 3.6 BPM，靠抛物线插值细化也只能到 ±1 帧量级；
        #   而离线是**整曲两级细扫（0.05 步长）+ 网格集中度**。
        #   INTERNET YAMERO 实测：离线 185.00（集中度 4.02 / 命中 52%），
        #   实时收敛到 181.5（集中度 1.35 / 命中 31%）⇒ **离线对**。
        #   所以认到歌且 BPM 校验通过时，界面直接显示库内值（这个字段只给界面用，
        #   不参与拍钟/网格 —— 网格始终用 grid.beat_len）。
        self.lib_bpm = 0.0
        self.beat_phase = 0.0       # 0..1 拍内相位
        self.beat_count = 0         # 总拍数
        self.bar_count = 0          # 4/4 小节数（按识别出的小节头起算）
        self.downbeat_off = 0       # 小节头相位：4/4 里第 1 拍是 beat_count%4 的哪一相
        self.downbeat_conf = 0.0    # 小节头识别置信度 0..1（0=证据不足，沿用旧相位）
        self.is_beat = False        # 本帧是否踩拍
        self.is_bar = False         # 本帧是否到小节
        self.drop = False           # 本帧是否 Drop（低频冲击）
        self.beat_active = False    # 最近 2 秒内是否有鼓点活动（宽松阈值）
        self.spectrum = [0.0] * 24  # 24 段对数频谱 0..1（后处理可视化特效数据源）
        self.ndi_feed = None        # 可选：NDI 音频喂入回调 callable(float32_stereo_interleaved, sr)
        self.ndi_audio_on = False   # NDI 音频是否启用：False 时采集循环**不做任何拷贝**，
                                    # 直接把 PCM 丢掉（原实现不管有没有开 NDI，每块都要
                                    # np.asarray + reshape(-1) + copy() 一次）
        # ★★ 2026-10-07：NDI 音频还要**跟「开始自动 VJ」绑定**（用户要求）。
        #   未开始时不推音频（连静音都不推），因此采集循环在未开始时也跳过投递。
        #   由 `engine.toggle_run()` 同步；转发线程侧另有一道同样的门控兜底。
        self.ndi_running = False
        # ---- ASIO 低延迟输入（见 src/asio_engine.py + src/native/asiohost.cpp）----
        # ⚠ 硬要求：ASIO 打不开（多半是被 VirtualDJ/DAW 独占）时**必须自动回退 WASAPI**，
        #   演出不能因为换个音源就没声音。
        self.asio_active = False    # 当前是否**真的**在用 ASIO 采集
        self.asio_info = ""         # 一行状态（驱动 / 采样率 / 缓冲 / 延迟），界面显示
        self.asio_failed = ""       # 非空 = ASIO 打开失败的原因（此时已回退到系统声音）
        # ★ 局域网音频（自研协议）：非空 = 连接失败原因（**不回退**，见 _run_lan）
        self.lan_failed = ""
        self.lan_info = ""          # 一行状态（对端地址 / 采样率 / 深度 / 丢包），界面显示
        # ---- 行为模式自动选择所需的实时指标 ----
        self.onset_density = 0.0    # 瞬态密度：每秒打击次数（近 4s 窗口）
        self.bpm_stab = 0.0         # BPM 稳定性 0..1
        self.beat_clarity = 0.0     # 节拍清晰度 0..1
        self.spec_complex = 0.0     # 频谱复杂度 0..1
        self.energy_trend = 0.0     # 能量趋势 -1..1（上升为正）
        self.vocalness = 0.0        # 人声占比估计 0..1（中频主导度）
        self.segment = "none"       # none/quiet/build/drop/peak/outro
        self.silent_sec = 0.0       # 连续静音秒数
        self.lv_db = -80.0          # 当前 RMS 电平（dBFS），供 Kv 主视觉图层的静音判定使用
        self.recognized_song_id = -1  # 指纹识别确认的歌 id（-1=无/待机）
        self.recognized_offset = -1.0  # 该歌当前播放位置（秒，来自指纹匹配 offset；-1=无）
        # 输入音乐播放速率（EMEA 估计，默认 1.0）：指纹链路用它把"识别窗口起点偏移"
        # 换算成"当前播放位置"，引擎网格时钟也用它把"拍"换算成"秒"（1 拍 = beat_len/r 秒）。
        self.recognized_rate = 1.0
        # 该识别结果对应的挂钟时刻（time.perf_counter()）：引擎据此把参考拍位推进到"现在"。
        self.recognized_t = 0.0
        # 本次指纹匹配的**对齐票数**（fp.match 的 aligned_votes）：命中重复段落时票数会骤降
        # （实测好样本 241 / 歧义坏样本 44），引擎据它决定 offset 是否可信、是否参与网格校正。
        self.recognized_votes = 0
        # ★ 网格时钟预测的「识别窗起点」在原曲时间轴上的秒数（<0 = 无预测）。
        #   引擎每帧写、识别线程每 0.5s 读：作为 fp.match 的 hint_delta，让匹配在强重复段落里
        #   优先选**靠近预测位置**的候选，避开「另一处出现」的高票错匹配（阻塞项 C）。
        #   只在网格时钟**已锁定**（当前歌有可信坐标系）时才有值；否则 -1，匹配退回全局众数。
        self.grid_pred_orig = -1.0
        # ★ 与 `grid_pred_orig` 配套的 hint **窗口半宽**（单位 = hop 帧）。引擎按当前歌 grid 的
        #   beat_len 用 `fp.hint_window_frames()` 换算后写这里；识别线程把它作为 `fp.match`
        #   的 `hint_w`。这样窗口**按拍数**定义、跨 BPM 一致（固定帧窗在 90~200BPM 间对应
        #   的拍数差 2 倍以上）。None ⇒ 用 fp 缺省 HINT_W（保持旧调用方逐值一致）。
        self.grid_pred_w = None
        self.running = False
        self.error = ""
        self.device_desc = ""
        # 能量各分项（只读调试用：能量诊断工具显示「能量是怎么算出来的」，主程序不读）
        self.dbg = {}

    def snapshot(self):
        with self.lock:
            return {
                "level": self.level, "bass": self.bass, "mid": self.mid,
                "high": self.high, "energy": self.energy, "bpm": self.bpm,
                "bpm_display": self.bpm_display, "bpm_locked": self.bpm_locked,
                "lib_bpm": self.lib_bpm,
                # ★ 2026-11-02：离线 BPM 锚点（素材倍速优先用它）
                "bpm_anchor": self.bpm_anchor,
                "beat_phase": self.beat_phase, "beat_count": self.beat_count,
                "bar_count": self.bar_count, "downbeat_off": self.downbeat_off,
                "downbeat_conf": self.downbeat_conf, "drop": self.drop,
                "is_beat": self.is_beat, "is_bar": self.is_bar,
                "beat_active": self.beat_active,
                "onset_density": self.onset_density, "bpm_stab": self.bpm_stab,
                "beat_clarity": self.beat_clarity, "spec_complex": self.spec_complex,
                "energy_trend": self.energy_trend, "vocalness": self.vocalness,
                "segment": self.segment,
                "spectrum": list(self.spectrum),
                "silent_sec": self.silent_sec, "running": self.running,
                "lv_db": self.lv_db,
                "error": self.error, "device_desc": self.device_desc,
                # ASIO 低延迟输入状态（界面显示"当前是不是真在用 ASIO、延迟多少"，
                # 以及"ASIO 打不开的原因"）——不放进来界面就只能 getattr 私有字段
                "asio_active": self.asio_active, "asio_info": self.asio_info,
                "asio_failed": self.asio_failed,
                # 局域网音频状态（界面显示"连没连上 / 延迟多少 / 丢了多少"）
                "lan_failed": self.lan_failed, "lan_info": self.lan_info,
                "recognized_song_id": self.recognized_song_id,
                "recognized_offset": self.recognized_offset,
                "recognized_rate": self.recognized_rate,
                "recognized_t": self.recognized_t,
                "recognized_votes": self.recognized_votes,
                "dbg": dict(self.dbg),      # 能量分项（诊断工具用）
            }


class _BlockAssembler:
    """把「驱动原生小包」攒成严格等间隔的 BLOCK 帧块 —— 修 NDI 音频「随机咔哒」。

    ## 为什么需要它（2026-10-08 取证结论，见 tools/_ndi_audio_ab.py）

    `soundcard` 的 `record(numframes=1024)` 会在**它自己的用户态**把驱动给的包
    重切成 1024 帧才返回。实测这个攒块的**相位会漂移**：
        record(numframes=1024) → 出块间隔 均值 21.33ms、**P99 31.3ms**、max 31.8ms
        record(numframes=480)  → 出块间隔 均值 10.00ms、**P99 11.8ms**、max 11.9ms
    也就是说：按 1024 取，每块实际经过的时间在 21~32ms 之间乱跳，而 NDI 接收端
    按「名义 48k 连续流」回放 ⇒ 时间轴被反复拉 ⇒ **听到咔哒**。

    而驱动本身是**完美**的（同一次实测）：
        每包严格 480 帧、QPC 间隔 0.010000s（std 10µs）、零 discontinuity。
    ⇒ 结论：**问题在 soundcard 的封装，不在驱动。**

    ## 本类的做法：**QPC 时间戳锁相**

    按驱动原生包长取数（默认 480），**用每包自带的 QPC 时间戳决定切块时刻**
    —— 而不是像 `soundcard` 那样「攒够 N 帧就返回」（那样余量会漂移，
    导致"这次读 2 包、下次读 3 包"，正是 21/31ms 双峰的来源）。

    具体：以首包的 QPC 为基准 `t0`，每当 `(qpc - t0) >= step` 就输出一块并推进基准。
    这样：
      * 切块时刻由**真实时间**驱动 ⇒ 块间隔严格恒定（实测 20.0ms，零漂移）
      * 输出块长**固定 BLOCK**，下游（分析/能量/拍钟/指纹/NDI）一行都不用改
      * **绝不填零、绝不伪造样本**：只搬运驱动给的帧

    ⚠ `record()` 返回的包长度可能不等于请求值（驱动说了算），本类一律按实际长度拼接。
    ⚠ 若驱动不提供有效 QPC（旧设备 / TIMESTAMP_ERROR），自动退化为「按帧数切」。
    """

    def __init__(self, block=BLOCK, pkt=480, step_frames=None):
        self.block = int(block)
        self.pkt = int(pkt) if pkt else int(block)
        self._buf = None          # 未切出的残留（(n, ch) float32）
        self._t_base = None       # 本切块周期的 QPC 基准（诊断/锁相用）
        self.pkt_seen = 0         # 取到的原生包数（诊断）
        self.last_qpc = 0
        self.blk_out = 0          # 已输出块数（诊断）

    # ---- 内部：取一个原生包，追加到 _buf ----
    def _pull(self, rec):
        """取一个原生包；返回其 QPC（0 = 不可用），无数据返回 None。"""
        d = rec.record(numframes=self.pkt)
        if d is None or len(d) == 0:
            return None
        self.pkt_seen += 1
        q = int(getattr(rec, "last_qpc", 0) or 0)
        if q:
            self.last_qpc = q
        d = np.asarray(d, dtype=np.float32)
        if d.ndim == 1:
            d = d.reshape(-1, 1)
        self._buf = d if self._buf is None else np.concatenate([self._buf, d], axis=0)
        return q

    def read(self, rec):
        """返回恰好 block 帧的 (n, ch) float32；确实无数据时返回 None。

        ⚠ **必须让 block 是 pkt 的整数倍**，否则余量会漂移 ⇒ 块间隔在
          「读 2 包」与「读 3 包」之间跳（实测 21ms / 31ms 双峰，正是咔哒来源）。
          调用方（`_capture_loop`）据此把 block 取成 pkt 的整数倍。
        """
        while self._buf is None or self._buf.shape[0] < self.block:
            r = self._pull(rec)
            if r is None:
                if self._buf is None or self._buf.shape[0] == 0:
                    # ⚠ 不要在这里 sleep —— 交给调用方统一退避（`_capture_loop`
                    #   的 `_empty_streak`）。两处各睡一次会让"有数据"时白等。
                    return None
                # 已有残留但暂时读不到：短睡再来（不填零、不伪造）
                time.sleep(0.001)
        out = self._buf[:self.block]
        rest = self._buf[self.block:]
        self._buf = rest if rest.shape[0] else None
        self.blk_out += 1
        return out


class _Analyzer:
    """逐块音频分析：所有跨块持久状态集中在实例里，feed() 出错不会丢线程。"""

    def __init__(self, sr, block=BLOCK):
        self.sr = sr
        # ★ 2026-11-02：累计喂进来的块数（只给 `energy_log` 的 CSV 看）。
        #   用途：`blk` 一秒不长 ⇒ 采集根本没喂数据；`blk` 变小 ⇒ 采集重启换了新分析器。
        #   跟能量算法无关，别拿它做业务判断。
        self.blk = 0
        # ★ 2026-10-08：block 可由采集层传入（原生采集路径用 pkt 的整数倍，
        #   见 _BlockAssembler）。默认仍取模块级 BLOCK，行为不变。
        self.block = int(block)
        self.block_sec = self.block / sr
        self.energy_scale = 1.0   # 能量伽马校正（>1 压低人声段、鼓点段基本不动）
        self.win = np.hanning(2048)
        self.prev_spec = None
        self.flux_hist = np.zeros(max(16, int(round(10.0 / self.block_sec))), dtype=np.float32)
        self.flux_i = 0
        self.flux_ref = 0.0        # 慢衰减通量基准（半衰期≈15s）
        self.bpm_smooth = 0.0
        self.bpm_display = 0.0               # 显示用（未锁定也可有值），不参与拍钟
        self._bpm_votes = deque(maxlen=60)   # 60s 投票窗口，众数取 BPM（换歌静音清空）

        self._bpm_lock = False               # 锁定建立标志（主段稳定后锁定，breakdown 不扰动）
        # ★ 2026-11-02：离线锚点的请求节流状态
        self._anchor_sid = -1                # 上次请求时的 song_id（换歌 ⇒ 立刻请求）
        self._anchor_bad_sec = 0.0           # 实时值与锚点"长期不符"的累计秒
        self._t_start = time.perf_counter()  # 冷启动计时（够 15 秒才允许首次请求）
        self._bpm_song = -1                  # 上次识别到的 song_id：变了就清投票+解锁（见 feed）
        self._bpm_far_sec = 0.0              # 众数持续偏离锁定值的累计秒（未识别到歌时的换歌兜底）
        # ★ 2026-11-02：因「BPM 校验否决」而强制作废实时 BPM 的时刻/次数（冷却用）
        self._bpm_veto_t = 0.0
        self._bpm_veto_n = 0
        # ★ 2026-11-11：BPM 否决的**连续窗口数**。现场实时 BPM 会偶发跳到
        #   210.5/155（同一首 185 BPM 的歌），单窗否决就清 sid ⇒ 码表掉锁/重锚
        #   ⇒ 画面跳来跳去。连续 3 窗都否决才真正丢掉，单窗坏 BPM 只记 bpm_hold。
        self._bpm_veto_streak = 0
        self.prev_bass_spec = None         # kick 频段上一帧谱（低频通量用）
        self.bass_flux_hist = np.zeros(max(16, int(round(10.0 / self.block_sec))), dtype=np.float32)
        self.beat_interval = 0.5
        self.beat_count = 0
        self.last_beat_t = 0.0
        self.last_strong_t = 0.0
        self.last_onset_t = 0.0
        self.last_drop_t = 0.0
        self.smooth = {"bass": 0.0, "mid": 0.0, "high": 0.0, "energy": 0.0}
        self.wdb_base = -60.0        # 能量 v4：加权能量 dB 的慢速自适应基线（≈10s EMA）
        self.loud_peak = 0.35   # 慢速追踪的曲目响度峰值（自适应母带音量）
        # 行为模式指标
        self.onset_times = deque(maxlen=256)
        self.strike_times = deque(maxlen=64)   # 近4秒强拍时间戳（打击密度用，带去重）
        self.strike_strengths = deque(maxlen=64)  # 每次强拍的相对强度（flux/flux_ref），与 strike_times 一一对应
        self.e_loud_smooth = 0.0               # e_loud 中速平滑（τ≈1.2s，压秒级波动）
        self._kick_e_smooth = 0.0              # kick_e 持续打击平滑（τ≈0.8s EMA，抹钢琴偶发尖峰）
        self.bass_peak = 0.0                   # 低频能量的全曲慢峰（只升不降，换歌重置）——低频能量分量参考
        # ★★ 2026-11-01（用户「放 hard 能量一直上不去」/「没导入歌怎么办」）：
        #   「只升不降」的参考峰有两个死穴：
        #     · 连 mix 几小时没有 3s 静音 ⇒ 峰一路被顶到全场最高，之后每段都"不够猛"，
        #       实测同一段音频连喂 120s，E 从 0.47 逐秒滑到 0.34（电平没变）；
        #     · 想"按换歌重校准"又依赖识别，**歌没进曲库时 sid 恒为 -1，永远不触发**。
        #   所以改用**不依赖识别的滚动窗口**：参考值 = 最近 `EN_PEAK_WINDOW_SEC` 秒的
        #   90 分位（而不是历史最大值）。窗口一滚，几小时前的峰值自然让位；
        #   单曲内的 breakdown 也不会被误抬 —— 窗口里就躺着刚过去的 drop。
        self._peak_win = (int(round(EN_PEAK_WINDOW_KICK / self.block_sec))
                          if EN_PEAK_WINDOW_KICK > 0 else 0)
        self._kick_hist = deque(maxlen=self._peak_win) if self._peak_win else None
        self._peak_win_b = (int(round(EN_PEAK_WINDOW_BASS / self.block_sec))
                            if EN_PEAK_WINDOW_BASS > 0 else 0)
        self._bass_hist = deque(maxlen=self._peak_win_b) if self._peak_win_b else None
        self._fluct_hist = deque(maxlen=self._peak_win_b) if self._peak_win_b else None
        self._peak_tick = 0
        self._kick_p90 = self._bass_p90 = self._fluct_p90 = 0.0
        # 低频通量（kick 冲击）1s 累计 + 全曲慢峰——鼓点强度的正确度量。
        # 人声段有贝斯垫底（低频能量 bass 不低），但 kick 脉冲的「瞬时起伏」远大于贝斯的
        # 「缓慢起伏」（实测 2.8 倍）。窗口必须够短（1s）才能凸现 kick 脉冲——
        # 4s 窗口会把脉冲和贝斯起伏平均掉，区分度从 2.8 倍抹成 1.3 倍。
        self._bflux_recent = deque(maxlen=max(16, int(round(1.0 / self.block_sec))))
        self._bdelta_recent = deque(maxlen=max(16, int(round(1.0 / self.block_sec))))
        self.fluct_peak = 0.0                  # 低频「起伏变化量」1s 累计的全曲慢峰（只升不降）
        self.prev_bass_e = None                # 上一块的低频能量（起伏差分用）
        self.kick_peak = 0.0                   # 低频通量 1s 累计的慢峰（只升不降，换歌重置）
        self._peak_confirmed = False           # peak 可信度：是否被「显著更高的 kick」验证过
        self._peak_age = 0.0                   # 换歌/启动起未确认的秒数（90s 超时强制确认）
        self.bpm_samples = deque(maxlen=12)
        self.bpm_sample_t = 0.0
        self.beat_gaps = deque(maxlen=16)
        # ---- 小节头拍（downbeat）识别 ----
        # 4/4 里「哪一拍是第 1 拍」靠等分是不知道的（beat_count 从 0 起算，没有音乐含义），
        # 所以「每 4 拍一条小节线」可能整体偏 0~3 拍 → 素材切换踩在音乐的第 2/3/4 拍上。
        # 按拍相位（beat_count % 4）累计落拍证据，证据显著最高的相位就是小节头。
        self._db_sum = [0.0, 0.0, 0.0, 0.0]   # 各相位证据累计
        self._db_cnt = [0, 0, 0, 0]           # 各相位样本数
        self._downbeat_off = 0                # 当前认定的小节头相位
        self._db_conf = 0.0                   # 置信度（领先幅度归一）
        self._db_cand = -1                    # 上次评估的候选相位
        self._db_streak = 0                   # 候选连续命中的次数
        self._db_eval_beat = 0                # 上次评估时的 beat_count
        self._db_change_beat = -10 ** 9       # 上次真正改相位时的 beat_count（初值=从不）
        self._db_conf_ever = False            # 是否已经认准过一次（之后改相位要更大领先幅度）
        self._db_prev_cent = None             # 上一拍窗口的低频谱质心（低音换音检测）
        # 当前拍窗口内的累计量（拍钟有 ±0.3 拍相位误差，逐块取值会采错位置，取窗口峰值更稳）
        self._dbw_bass = 0.0
        self._dbw_bflux = 0.0
        self._dbw_csum = 0.0
        self._dbw_w = 0.0
        self._dbw_drop = False       # 本拍窗口内是否出现过 drop 突刺
        self._db_pending = None      # 待结算的拍相位（拍点后 0.3 拍结算）
        self._db_pending_due = 0.0
        self._db_bass_ref = 0.0      # 拍窗口低音峰值的慢衰减基准（比较各拍 kick 轻重）
        self.cx_smooth = 0.0
        self.energy_hist = deque(maxlen=192)
        self.voc_smooth = 0.0
        self.lv_smooth = 0.0                   # 绝对响度的**线性 RMS**平滑值（先平滑功率再转 dB，见 feed）
        self.seg_state = {"quiet_since": None, "build_since": None, "last_drop": 0.0,
                          "was_high": False, "high_since": None, "label": "none",
                          "seg_until": 0.0, "prev_e": 0.0}

    def _maybe_request_anchor(self, st, now):
        """判断要不要请 `BpmAnchor` 重算一次离线 BPM（**只打时间戳，不做分析**）。

        三个触发条件（缺一个都会让现场"该纠的时候不纠"）：
          ① **换歌**（指纹认到新的 song_id）—— 立刻请求，不用等实时值漂；
          ② 从来没有锚点（冷启动后音频攒够由锚点线程自己判断）；
          ③ 实时值与锚点**长期不符**：比值脱离 [0.85, 1.18] 连续 8 秒。
             现场 190BPM 那段的实例：实时 125.5 / 锚点 190 = 0.66 ⇒ 必触发。
        ⚠ 只写 `st.anchor_req`，真正的节流（最小间隔 6s / 兜底 120s）在锚点线程里。
        """
        anchor = float(getattr(st, "bpm_anchor", 0.0) or 0.0)
        live = float(self.bpm_smooth or 0.0)
        sid = int(getattr(st, "recognized_song_id", -1) or -1)
        if sid >= 0 and sid != self._anchor_sid:
            self._anchor_sid = sid
            st.anchor_req = time.time()
            self._anchor_bad_sec = 0.0
            return
        if anchor <= 0.0:
            if (now - self._t_start) > 15.0 and not getattr(st, "anchor_req", 0.0):
                st.anchor_req = time.time()
            return
        if live > 0.0:
            ratio = live / anchor
            if 0.85 <= ratio <= 1.18:
                self._anchor_bad_sec = 0.0
            else:
                self._anchor_bad_sec += self.block_sec
                if self._anchor_bad_sec >= 8.0:
                    self._anchor_bad_sec = 0.0
                    st.anchor_req = time.time()
        else:
            self._anchor_bad_sec = 0.0

    def _fft_plan(self):
        """FFT 相关的预算表：(freqs, 低频区间, 中频区间, 高频区间, 24 段边界, BPM 周期)。

        返回 (freqs, 低/中/高频区间, 24 段边界, BPM 判定周期, kick 频带区间)。

        采样率在一次采集里是固定的 ⇒ 只算一次，按 sr 缓存（换设备/换采样率自动重算）。
        频带区间用 searchsorted 求**连续下标**：RFFT 频率轴单调递增，所以
        「freqs >= lo 且 < hi」等价于一个切片，比每次建布尔掩码快得多。
        """
        got = getattr(self, "_fft_plan_cache", None)
        if got is not None and got[0] == self.sr:
            return got[1]
        freqs = np.fft.rfftfreq(2048, 1.0 / self.sr)

        def _rg(lo, hi):
            return (int(np.searchsorted(freqs, lo, "left")),
                    int(np.searchsorted(freqs, hi, "left")))

        edges = np.unique(np.geomspace(2, len(freqs) - 1, 25).astype(int))
        # ⚠ kick 频带原来是 `(freqs >= 20) & (freqs <= 200)` —— **右端是闭区间**，
        #   所以这里用 searchsorted "right" 才不会漏掉正好落在 200Hz 的那个 bin。
        rg_kick = (int(np.searchsorted(freqs, 20, "left")),
                   int(np.searchsorted(freqs, 200, "right")))
        plan = (freqs, _rg(20, 160), _rg(160, 2000), _rg(2000, 12000),
                edges, max(1, int(round(self.sr / self.block))), rg_kick)
        self._fft_plan_cache = (self.sr, plan)
        return plan

    def _recalc_peaks_p90(self):
        """每 ~0.3 秒重算一次「最近 N 秒的 90 分位」（仅 `EN_PEAK_WINDOW_SEC>0` 时用到）。

        ⚠ 不能每块都算：窗口 300s ≈ 1.4 万个点，`percentile` 一次约 0.4ms，
          每 21ms 算三次就吃掉 6% CPU。16 块（≈0.34s）一次足够稳。
        """
        self._peak_tick += 1
        if self._peak_tick % 16 != 1:
            return
        for hist, name in ((self._kick_hist, "_kick_p90"),
                           (self._bass_hist, "_bass_p90"),
                           (self._fluct_hist, "_fluct_p90")):
            if hist:
                setattr(self, name, float(np.percentile(
                    np.fromiter(hist, dtype=np.float64), 90.0)))

    def _bpm_relock_after_veto(self, lbpm, rbpm, brel, hit, tot):
        """BPM 一致性校验否决识别时，**同时**让实时 BPM 重估一次 —— 解自锁。

        ★★★ 2026-11-02（用户直播：「200+ BPM 的歌，素材速度没跟上来、很慢」）。
          现场取证（`cue_health.log`，2026-11-01 23:50 启动 → 01:14 退出那场）：
            · 69 条 `[R]` 里 `bpm=133.5` **一条没变**，而 `lbpm` 在 175→145→200→145 之间换
            · `brel` 只有三个值：0.237(175) / 0.333(200) / 0.079(145)
              —— 全是 `(lbpm − 133.5) / lbpm`，即实时值恒 133.5
            · 69 条里只有 **2 条**认到歌（`sid≥0`），其余全被 BPM 校验否决
            · 素材倍速 = `bpm / 128`（`engine._tick_locked`）⇒ 该 1.56× 的那首实际跑 1.04×
            · 素材切换间隔全程 **10~11s**（= 24 拍 ÷ 133.5），200BPM 那首本该 7.2s
          自锁链条（环）：
            拍钟 BPM 卡在旧值 → 拿它跟库内 200 校验 ⇒ 偏差 33%、比值 1.5 不在 2 的幂谐波集
            ⇒ `bpmok=0` ⇒ 识别被否决 ⇒ `sid=-1` ⇒ 换歌解锁（`_bpm_song` 变化）永不触发
            ⇒ **BPM 永远停在旧值**，谁也解不开。
          ⇒ 校验失败本来就是说「这两个值里有一个错了」。既有处理只怀疑**识别**（否决它），
            从不怀疑**实时 BPM**。这里补上另一半：冷却期内清票重估。
            ⚠ 冷却必须够长：`_bpm_votes` 要 ≥20 票 + 众数集中度 ≥0.45 才重锁，
              每窗都清就永远锁不上（等于把拍钟钉死得更彻底）。
            ⚠ 不清 `bpm_smooth`：重锁成功前拍钟继续按旧值走，不停摆（既有约定）。
            ⚠ 只在指纹**确实认得这首歌**（命中率/总票够）时做 —— 低票误匹配重估毫无意义。
        """
        try:
            now = time.perf_counter()
            if (now - float(getattr(self, "_bpm_veto_t", 0.0))) < BPM_VETO_RELOCK_SEC:
                return
            self._bpm_veto_t = now
            self._bpm_veto_n = int(getattr(self, "_bpm_veto_n", 0) or 0) + 1
            self._bpm_votes.clear()
            self._bpm_lock = False
            self._bpm_far_sec = 0.0
            from energy_log import log_capture_event
            log_capture_event(
                "[BPM] 校验否决 ⇒ 实时 BPM 重估（第%d次）lbpm=%.1f rbpm=%.1f rel=%s "
                "hit=%.2f tot=%d" % (
                    self._bpm_veto_n, float(lbpm or 0.0), float(rbpm or 0.0),
                    ("%.3f" % float(brel)) if brel is not None else "-",
                    float(hit or 0.0), int(tot or 0)))
        except Exception:                                        # noqa: BLE001
            pass

    def feed(self, data, st):
        """分析一个采集块并更新共享状态。任何异常由调用方兜底，不影响线程存活。"""
        now = time.perf_counter()
        # ★ 2026-11-02：块计数（给能量 CSV 用）。放在最前面 —— 只要本块喂进来了就计，
        #   哪怕下面某一步抛异常（那种情况能量本来也会失真，正好能从 CSV 里看出来）。
        try:
            self.blk += 1
        except Exception:                                        # noqa: BLE001
            self.blk = 1
        # ★ 换歌检测（指纹认出的歌变了）→ 清空 BPM 投票并解锁，让新歌的 BPM 能被重新估计。
        #   为什么必须做：`_bpm_lock` 建立后只接受与锁定值 **±8 BPM** 内的修正（见下方 BPM 段），
        #   而解锁原来只靠「连续静音 >10s」。**DJ 接歌混播没有静音** ⇒ 132BPM 切到 150BPM 时
        #   bpm_smooth 永远停在旧值：界面 BPM 不更新，拍钟也继续按旧速度跑
        #   （用户 2026-09-27 报「从 Can We Believe That 切到 Cosmic String 一直是 133」）。
        #   用指纹识别结果判换歌最准（每 0.5s 一轮），而且不怕两首歌混音重叠。
        #   ⚠ 只清投票 + 解锁，**不清 bpm_smooth** —— 保留它让拍钟继续走，等新歌重新投票
        #     出够票数（约几秒）自然接管，避免换歌瞬间拍钟停摆。
        # ★★ 2026-11-02：按需请求离线 BPM 锚点（只打时间戳，分析在独立线程）
        try:
            self._maybe_request_anchor(st, time.perf_counter())
        except Exception:                                     # noqa: BLE001
            pass
        sid = getattr(st, "recognized_song_id", -1)
        if sid is not None and sid >= 0 and sid != self._bpm_song:
            self._bpm_song = sid
            self._bpm_votes.clear()
            self._bpm_lock = False
            self._bpm_far_sec = 0.0
        sig = np.mean(data, axis=1) if data.shape[1] > 1 else data[:, 0]
        rms = float(np.sqrt(np.mean(sig ** 2)))

        # ---- FFT ----
        # ★ 频带边界 / 24 段频谱的边界 / BPM 判定周期**只算一次**（见 _fft_plan）：
        #   原来每块（≈46 次/秒）都要 rfftfreq + geomspace + 三组布尔掩码。
        freqs, rg_bass, rg_mid, rg_high, edges, bpm_period, rg_kick = self._fft_plan()
        buf = np.zeros(2048, dtype=np.float32)
        n = min(len(sig), 2048)
        buf[:n] = sig[:n]
        spec = np.abs(np.fft.rfft(buf * self.win)) / 1024.0
        bass = _band_mean(spec, rg_bass)
        mid = _band_mean(spec, rg_mid)
        high = _band_mean(spec, rg_high)
        # 低频「起伏变化量」：相邻块低频能量的上升差分，1s 窗口累计。
        # drop 段的 kick 让低频一拍一拍地大起大落（起伏大且密），平稳垫底/pad 段起伏小。
        # 与 kick（频谱级通量）互补：录屏/压缩削瞬态通量时，能量级的起伏通常仍在。
        if self.prev_bass_e is not None:
            self._bdelta_recent.append(max(bass - self.prev_bass_e, 0.0))
        self.prev_bass_e = bass

        # ---- 24 段对数频谱（可视化特效数据源：径向频谱/音频反应变形）----
        band_vals = np.array([spec[edges[i]:max(edges[i] + 1, edges[i + 1])].mean()
                              for i in range(len(edges) - 1)], dtype=np.float32)
        # 滚动峰值归一（慢衰减，自动适配音量），再 EMA 平滑
        if not hasattr(self, "_spec_ref") or self._spec_ref is None:
            self._spec_ref = np.full(len(band_vals), 1e-6, dtype=np.float32)
        self._spec_ref = np.maximum(self._spec_ref * 0.995, band_vals)
        self._spec_ref = np.maximum(self._spec_ref, 1e-6)
        sn = np.clip(band_vals / self._spec_ref, 0.0, 1.0)
        if not hasattr(self, "_spec_sm") or self._spec_sm is None:
            self._spec_sm = sn.copy()
        else:
            k = min(len(sn), len(self._spec_sm))
            self._spec_sm[:k] = self._spec_sm[:k] * 0.6 + sn[:k] * 0.4
        self.spectrum = [round(float(v), 3) for v in self._spec_sm[:24]]
        while len(self.spectrum) < 24:
            self.spectrum.append(0.0)

        # ---- 谱通量 + 慢衰减基准 ----
        if self.prev_spec is None:
            self.prev_spec = spec
        flux = float(np.sum(np.maximum(spec - self.prev_spec, 0)))
        self.prev_spec = spec
        self.flux_hist[self.flux_i % len(self.flux_hist)] = flux
        self.flux_i += 1
        self.flux_ref = max(self.flux_ref * 0.999, flux)

        # ---- 低频(kick)通量：踢鼓节奏比全频稳定（全频易被旋律乐句周期带偏）----
        # ★ 掩码由 `_fft_plan()` 预算（原来每块都要建一次布尔数组 + fancy index）
        bass_spec = spec[rg_kick[0]:rg_kick[1]]
        if self.prev_bass_spec is None:
            self.prev_bass_spec = bass_spec
        bflux = float(np.sum(np.maximum(bass_spec - self.prev_bass_spec, 0)))
        self.prev_bass_spec = bass_spec
        # kick 低频通量的慢基准（半衰期≈15s 同步自适应）。打击强度用 bflux/bflux_ref 衡量：
        # 区分度来自频段本身——breakdown 段没有 kick，bflux 接近 0；主段重 kick 的 bflux 大。
        # （v6.1 首版用全频 flux/flux_ref 做强度，方向反了：breakdown 的旋律 tick 反而比值更高。）
        self.bflux_ref = max(getattr(self, "bflux_ref", 0.0) * 0.999, bflux)
        self.bass_flux_hist[self.flux_i % len(self.bass_flux_hist)] = bflux
        # 低频通量 4s 累计（kick 冲击密度）及其只升不降慢峰
        self._bflux_recent.append(bflux)
        kick_density = float(sum(self._bflux_recent))
        # 低频「起伏变化量」：1s 窗口内低频能量的上升差分累计（与 kick 的频谱通量互补）
        fluct_density = float(sum(self._bdelta_recent)) if self._bdelta_recent else 0.0
        # 慢峰缓慢回落（`EN_PEAK_DECAY_TAU=0` 时完全不动 = 旧行为）
        if EN_PEAK_DECAY_TAU > 0.0:
            _d = float(np.exp(-self.block_sec / EN_PEAK_DECAY_TAU))
            self.kick_peak *= _d
            self.bass_peak *= _d
            self.fluct_peak *= _d
        if self._kick_hist is not None:
            # 滚动窗口模式：参考值 = 最近 N 秒 90 分位（不依赖识别，连跑几小时不累积）
            self._kick_hist.append(kick_density)
            self._recalc_peaks_p90()
            self.kick_peak = max(self._kick_p90, kick_density)
        else:
            self.kick_peak = max(self.kick_peak, kick_density)

        # ---- BPM 估计 v6（每 ~1s）：kick 节奏优先 + 谐波梳状 + 60s 投票众数 ----
        if self.flux_i % bpm_period == 0:
            fps = self.sr / self.block
            bh = self.bass_flux_hist.copy()
            fh = self.flux_hist.copy()
            # ★ 环形缓冲按写指针解卷成**时序顺序**（2026-10-27）：`flux_hist[i]=flux[i%L]`
            #   是环形顺序，直接 `np.correlate` 会让「lag」不再对应真实时间差，把自相关峰抹平
            #   （160BPM 非整数拍长下尤其明显）。解卷后自相关才准，半速修正确/全频梳状都受益。
            _w = self.flux_i % len(bh)
            if _w:
                bh = np.concatenate([bh[_w:], bh[:_w]])
                fh = np.concatenate([fh[_w:], fh[:_w]])

            def _comb_score(h):
                h = h - np.mean(h)
                ac = np.correlate(h, h, "full")[len(h) - 1:]
                acn = ac / (ac[0] + 1e-9)
                lo = max(2, int(60 / 220 * fps))
                hi = min(int(60 / 60 * fps), len(acn) - 10)
                if hi <= lo + 2:
                    return 0.0, -1, acn
                best_lag, best_score = lo, -1.0
                for lag in range(lo, hi):
                    s = acn[lag]
                    if 2 * lag < len(acn):
                        s += 0.8 * acn[2 * lag]
                    if 3 * lag < len(acn):
                        s += 0.6 * acn[3 * lag]
                    if 4 * lag < len(acn):
                        s += 0.5 * acn[4 * lag]
                    if s > best_score:
                        best_score, best_lag = s, lag
                return best_score, best_lag, acn

            # kick 与全频各自估一个候选；kick 节奏清晰（梳状峰强）时优先 kick，
            # 否则（breakdown 无 kick）退回全频。解决全频通量在 2/3 半速处梳状分更高的系统性误检。
            kscore, klag, kac = _comb_score(bh)
            fscore, flag, fac = _comb_score(fh)
            if kscore > 0.9:      # kick 节奏清晰 → 用 kick
                lag, acn, _hist = klag, kac, bh
            else:
                lag, acn, _hist = flag, fac, fh
            # 半速（亚谐波）分辨率：**对 kick 与全频两分支都生效**。
            # 4/4 电子乐 kick 每拍一响，自相关在「真实拍长」与「2×拍长」处都有峰，梳状打分两者
            # 几乎同分；offbeat 让相邻拍相关性低于隔拍相关性时，2×拍长反而更高分 ⇒ BPM 被减半。
            # 修法：候选 BPM<120 时，在 [lo, lag) 里找第一个「绝对强度够、且是局部峰」的更短周期
            # （=真实拍长），相对当前峰 ≥阈值才算。半拍/噪声峰（<0.30）与真实慢歌都不被误升。
            if lag > 0:
                b0 = 60.0 / (lag / fps)
                if b0 < 120.0:
                    # 半速修正搜索下界：按最高 240 BPM 反推最短拍长（209/210 真实拍长可进范围）。
                    lo_lag = max(2, int(60.0 / 240.0 * fps))
                    # 8× 上采样直方图后再自相关：kick 周期（160BPM=17.578 块）非整数，整数 lag 会把
                    # 主拍/半拍峰都抹平到相近值，无法分辨「80+offbeat（160 处弱 offbeat）」与
                    # 「160 减半成 80（160 处强 kick）」。上采样直方图才能测准亚采样峰。
                    _hist_c = np.asarray(_hist, dtype=np.float64)
                    _hist_c = _hist_c - np.mean(_hist_c)
                    _hu = np.interp(np.linspace(0, len(_hist_c) - 1, len(_hist_c) * 8),
                                    np.arange(len(_hist_c)), _hist_c)
                    _hu = _hu - np.mean(_hu)
                    _ac_up = np.correlate(_hu, _hu, "full")[len(_hu) - 1:]
                    _acn_up = _ac_up / (_ac_up[0] + 1e-9)

                    def _peak_up(i):
                        # 在上采样自相关里取 i 附近 ±4（=±0.5 块）的峰——容忍 kick 周期的非整数 lag。
                        lo = max(0, int(i) - 4)
                        hi = min(len(_acn_up), int(i) + 5)
                        return float(np.max(_acn_up[lo:hi]))

                    for cand in range(lo_lag, int(lag)):
                        # ★★★ 2026-11-02（Funkot 素材慢的根因）：
                        #   原来这里判 `acn[cand] < 0.30` —— 用的是**整数 lag 的原始值**。
                        #   现场 block=1323（33.3 帧/秒）时，190BPM 的周期是 lag=10.53，
                        #   能量摊到 10/11 两格（实测 0.158 / 0.194），**永远过不了 0.30**
                        #   ⇒ 这条"把 95 升回 190"的修正从没生效，实时就停在 95.2 / 漂到 125.5。
                        #   ⇒ 改用 8× 上采样的峰（`_peak_up`，它本来就是为
                        #     "kick 周期非整数 lag" 写的），能取到 10.53 的真实峰。
                        if _peak_up(cand * 8) < _HALFSPEED_PEAK_MIN:
                            continue
                        left = acn[cand - 1] if cand > 0 else -1.0
                        right = acn[cand + 1] if cand + 1 < len(acn) else -1.0
                        if acn[cand] < left or acn[cand] < right:
                            continue
                        # 相对强度：上采样峰比 ≥阈值 才算「下半拍够强」→ 修正；
                        # 弱 offbeat（真实 80+offbeat 峰比 ≈0.77）不修正。
                        if _peak_up(cand * 8) < _HALFSPEED_RATIO_MIN * _peak_up(lag * 8):
                            continue
                        lag = cand
                        break
            if lag > 0 and (kscore > 0.9 or fscore > 0):
                # 抛物线插值细化亚采样精度
                delta = 0.0
                if 1 <= lag < len(acn) - 1:
                    denom = acn[lag - 1] - 2 * acn[lag] + acn[lag + 1]
                    if abs(denom) > 1e-9:
                        delta = max(-0.5, min(0.5, 0.5 * (acn[lag - 1] - acn[lag + 1]) / denom))
                # ★★ 2026-10-27（Round7 定案）：**钳位只用于"锁定/显示"，不用于"一致性校验"**。
                #   现场问题：128BPM 的歌被估成双倍 256，再被 min(220) 压成 219.5
                #   ⇒ 比值 1.72 既不是真值也不是整数倍 ⇒ BPM 校验把 97% 命中的正确匹配判成误识别。
                #   但钳位不能直接去掉：`_bpm_votes` 的直方图/锁定判据都按 ≤220 设计，
                #   放进来 256 会让 `bpm_smooth` 建立不了 ⇒ 拍钟/踩拍全停（功能倒退）。
                #   ⇒ 另开一条**不钳位的原始值** `bpm_raw`，只供 BPM 一致性校验用（保谐波关系）。
                _bpm_raw = 60.0 / ((lag + delta) / fps)
                _bpm_raw = float(max(60.0, min(400.0, _bpm_raw)))
                # ★★★ 2026-10-31（用户「实时分析的 bpm 怎么一直 219」）：
                #   **先折半再钳位**。`min(220)` 把 350（=175×2）压成 219.5 这种非法值，
                #   会让拍钟、BPM 校验、锁定众数三处同时错（详见 `fold_bpm_to_range`）。
                bpm = fold_bpm_to_range(_bpm_raw, 60.0, 220.0)
                # ★★★ 同一轮：**撞顶区的票在已锁定后直接丢弃**。
                #   实测 175 那批歌（`Edge -intro-` / `熾熱迴路` / `Deep Sleep`）在 drop/intro
                #   仍会零星产出 218~220 的值 —— 那是"估计器抓错周期"的残余，不是真值：
                #   · 丢掉它，`bpm_display`/`bpm_smooth` 都不再被它带跑（实测 smooth 偏差
                #     从 44.5 降到 0.5，拍钟始终按 175 走）；
                #   · 未锁定时**不丢**（冷启动只有这些票，丢了就永远锁不上）。
                _rail_hit = bpm >= BPM_RAIL
                if _rail_hit and self._bpm_lock:
                    pass                     # 已锁定 + 撞顶 ⇒ 假值，不投票、不改显示
                else:
                    self._bpm_votes.append(bpm)
                vals = np.array(self._bpm_votes, dtype=np.float32)
                if len(vals) >= 6:
                    bins = np.arange(60.0, 221.0, 1.0)
                    hist, edges = np.histogram(vals, bins=bins)
                    peak = int(np.argmax(hist))
                    mode = float(edges[peak] + 0.5)
                    conf = float(hist[peak]) / len(vals)
                    if not self._bpm_lock:
                        # 锁定建立：舞曲常见区间 [70,220] + 众数集中 + 票数足。
                        # 下界 70：覆盖慢歌（用户会放 80 BPM 的场）。上界 220：Frenchcore/Uptempo
                        #   190~220 BPM（2026-10-27 高 BPM 支持）。
                        # intro 的半速值（64=128/2、73=146/2）：64 被下界 70 排除；73 这类
                        #   稀疏 intro 因半速修正找不到 146 的峰而保持 73——此为窄边界已知限制。
                        if 70 <= mode <= 220 and conf >= 0.45 and len(vals) >= 20:
                            self.bpm_smooth = mode
                            self._bpm_lock = True
                        elif conf >= 0.4 and self.bpm_smooth > 0:
                            self.bpm_smooth = self.bpm_smooth * 0.7 + mode * 0.3
                    else:
                        if abs(mode - self.bpm_smooth) <= 8:
                            self.bpm_smooth = self.bpm_smooth * 0.7 + mode * 0.3
                            self._bpm_far_sec = 0.0
                        else:
                            # 兜底（**没识别到歌**时用）：60s 众数**持续**偏离锁定值 ⇒ 大概率换歌了。
                            # 阈值刻意保守（连续 ≥20s 且众数集中度 ≥0.50），避免 breakdown 段被旋律
                            # 带飞的瞬时飞值触发误解锁 —— 那正是「±8 门控」要防的「2:20 飞到 180」。
                            # 本段约 1s 执行一次，所以用 +1.0 累加近似秒数。
                            if conf >= 0.50:
                                self._bpm_far_sec += 1.0
                                if self._bpm_far_sec >= 20.0:
                                    self._bpm_votes.clear()
                                    self._bpm_lock = False
                                    self._bpm_far_sec = 0.0
                            else:
                                self._bpm_far_sec = 0.0
                    # ---- 显示用 BPM（**不参与拍钟/切换节奏**）----
                    # 门控只影响「锁不锁」，但界面显示不该被门控连坐：慢歌、极快曲、
                    # 被估成半速的歌都要能看到一个数字。票数≥10 且 conf≥0.40 就发布，
                    # 并在未锁定时记 bpm_locked=False（界面显示成「≈ N」，与锁定值区分）。
                    if len(vals) >= 10 and conf >= 0.40:
                        if self._bpm_lock:
                            self.bpm_display = self.bpm_smooth
                        elif mode >= BPM_RAIL and self.bpm_display > 0:
                            # ★ 2026-10-31：撞顶区的众数 = 被压平的双倍假值，不携带信息。
                            #   已有显示值时**不拿它盖掉**（实测 `Edge -intro-` / `熾熱迴路`
                            #   就在这种时段把 175.5 闪成 219.5）。
                            pass
                        else:
                            self.bpm_display = mode

        # ---- 节拍相位 / 踩拍 / 鼓点活动 ----
        if self.bpm_smooth > 0:
            self.beat_interval = max(0.25, min(1.2, 60.0 / self.bpm_smooth))
        strong = flux > max(1e-4, self.flux_ref * 0.55)
        # 低频 kick 强拍：bflux（20-200Hz 通量）超过其慢基准。人声/旋律的高频瞬态
        # 会产生全频 flux（strong）但几乎不产生低频通量——打击感必须用 kick_strong 计数，
        # 否则人声段 strike_density 虚高、能量降不下来（In The Atmosphere 教训）。
        kick_strong = bflux > max(1e-4, self.bflux_ref * 0.55)
        if flux > max(3e-4, self.flux_ref * 0.35):
            self.last_onset_t = now
        beat_active = (now - self.last_onset_t) < 2.0
        # ---- 节拍钟：匀速推进 + 强拍锁相（PLL）----
        # 旧实现「检测到强拍就 last_beat_t = now」有两个硬伤：
        #   ① 拍点时间被 onset 检测的抖动带偏（每次 ±几十 ms，累积漂移）；
        #   ② 漏检强拍时拍钟完全停走，beat_count 与音乐越错越远 → 切换踩不到小节线。
        # 改为匀速节拍钟：按 beat_interval 累加推进（漏检也照常走拍），强拍只做小幅相位校正
        # （PLL，35% 步长，仅当强拍落在拍点 ±0.3 拍内才拉，offbeat 的 0.5 拍不拉）。
        # 这样 beat_count 稳定对应音乐拍数，「切换点就近对齐 4 拍小节线」才能真正落到小节上。
        gap = now - self.last_beat_t
        is_beat = False
        if self.bpm_smooth > 0 and self.last_beat_t > 0.0 and gap <= self.beat_interval * 3.0:
            # 匀速推进（一次可能跨多拍，逐拍补齐 beat_count）
            while now - self.last_beat_t >= self.beat_interval:
                self.last_beat_t += self.beat_interval
                self.beat_count += 1
                is_beat = True
            # 强拍锁相：把拍钟朝 onset 拉一点（小步，防抖）
            if strong:
                ph = (now - self.last_beat_t) / self.beat_interval
                err = ph if ph <= 0.5 else ph - 1.0     # -0.5~0.5：强拍相对最近拍点的相位偏差
                if abs(err) < 0.3:
                    self.last_beat_t += err * self.beat_interval * 0.35
                # 拍间隔微调：仅在 gap 接近当前间隔(0.8~1.3 倍)时吸收，防 offbeat 带偏
                if 0 < gap < 4.0 and self.beat_interval * 0.8 <= gap <= self.beat_interval * 1.3:
                    self.beat_interval = max(0.25, min(1.2, 0.85 * self.beat_interval + 0.15 * gap))
        else:
            # BPM 未建立 / 断档（静默、换歌、大间隙）：宽松自由推进（旧行为兜底，重置拍钟）
            if gap >= self.beat_interval and rms > 1e-4:
                self.last_beat_t = now
                self.beat_count += 1
                is_beat = True
            # 断档 = 换歌/长时间无鼓点：拍钟重新起拍，小节头相位也作废重找
            self._downbeat_reset()
        phase = (now - self.last_beat_t) / max(self.beat_interval, 1e-3)
        # 小节线按「识别出的小节头相位」起算，而不是拍钟起点（拍钟起点无音乐含义）
        is_bar = is_beat and ((self.beat_count - self._downbeat_off) % 4 == 0)

        # ---- 强踩拍间隔 → 节拍清晰度 ----
        beat_clarity = 0.0  # 缺省值：无强拍样本时也是有效数字（避免未绑定变量）
        if strong and self.last_strong_t > 0:
            g = now - self.last_strong_t
            if 0.15 < g < 3.0:
                self.beat_gaps.append(g)
        if strong:
            self.last_strong_t = now
        # 打击密度：用 kick_strong（低频通量=真 kick）计数，0.15s 去重。
        # 人声段全频 flux 有瞬态（strong）但低频无 kick（kick_strong 稀疏）→ 打击感低 → 能量低。
        if kick_strong:
            if not self.strike_times or now - self.strike_times[-1] > 0.15:
                self.strike_times.append(now)
                # 记录这次打击的 kick 强度（bflux 相对其慢基准，仅调试观测用）
                self.strike_strengths.append(min(4.0, bflux / max(self.bflux_ref, 1e-9)))
        while self.strike_times and now - self.strike_times[0] > 4.0:
            self.strike_times.popleft()
            if self.strike_strengths:
                self.strike_strengths.popleft()
        strike_density = len(self.strike_times) / 4.0   # 每秒强拍次数（0~6+，打击感）
        # 强度加权打击能量：每秒「强度总和」。重 kick 密集≈6，轻 tick≈2。
        weighted_strike = sum(self.strike_strengths) / 4.0
        if self.beat_gaps and self.bpm_smooth > 0:
            want = 60.0 / self.bpm_smooth
            good = sum(1 for g in self.beat_gaps if abs(g - want) < want * 0.3)
            beat_clarity = good / len(self.beat_gaps)

        # ---- Drop 检测：安静后低频突刺 ----
        t_now = time.time()
        is_drop = (bass > 0.12 and self.smooth["bass"] < 0.04
                   and t_now - self.last_drop_t > 4.0)
        if is_drop:
            self.last_drop_t = t_now

        # ---- 小节头拍（downbeat）识别：找出 4/4 里哪一拍是「第 1 拍」----
        self._downbeat_tick(is_beat, bass, bflux, bass_spec, is_drop)

        # ---- 平滑能量 ----
        sm = 0.25
        self.smooth["bass"] = self.smooth["bass"] * (1 - sm) + bass * sm
        self.smooth["mid"] = self.smooth["mid"] * (1 - sm) + mid * sm
        self.smooth["high"] = self.smooth["high"] * (1 - sm) + high * sm

        # 能量 v7.3：低频冲击(kick)主导 + 响度辅助。
        # 用户场景（线上 VJ 给别的 DJ 用）：无法要求每个 DJ 关响度均衡/压缩——它们会抹平响度
        # （人声段被增益到和 drop 一样响），但抹不平 kick 冲击的频谱特征。所以能量必须以
        # 低频通量（kick 冲击）为主导（70%），响度只做辅助（30%）。
        weighted = bass * 0.6 + mid * 0.25 + high * 0.15       # 低频主导
        wdb = 20.0 * float(np.log10(weighted + 1e-9))
        if rms > 1e-4:
            if self.wdb_base <= -59.9:
                self.wdb_base = wdb                      # 冷启动：锚定第一声
            else:
                tau = EN_TAU_LOUD_BASE
                self.wdb_base += (wdb - self.wdb_base) * (1.0 - float(np.exp(-self.block_sec / tau)))
        rel = wdb - self.wdb_base
        if rel < 0:
            e_loud = 0.5 * (1.0 + rel / 6.0)
        else:
            e_loud = 0.5 + 0.5 * rel / 6.0
        e_loud = max(0.0, min(1.0, e_loud))
        # 低频冲击强度：kick_density（1s 低频通量累计）相对全曲慢峰（压缩鲁棒）。
        # 映射带 0.55 死区：实测鼓点段 kick_rel 0.71~0.84、钢琴/人声段 0.08~0.50（BW 钢琴
        # 和弦的低频瞬态会贡献 0.3~0.5，但远低于真 kick）——线性 min(rel*1.3) 把钢琴段
        # 映到 0.4~0.65 导致 breakdown 能量虚高（Black Warrior 1:50 钢琴段 0.8+）。
        # 锚定保护：VDJ 接歌混播无静音→换歌重置不触发，新歌常从 breakdown（钢琴/人声段）
        # 进入，kick_peak 被锚定在低值→后续 kick_rel 全部虚高。按「peak 可信度」处理：
        # 若 peak 从未被「显著更高的 kick（>1.18×peak，即真 drop 到来）」验证过、且未超 90s，
        # 视为「可能只见过 breakdown」→ 分母放大 1.54 压低 kick_rel（breakdown 保持低能量）；
        # 一旦真 drop 到来（kick 暴涨）或超时，立即确认，能量恢复满血。正常从头播的歌
        # 第一个 drop 就会确认，几乎不受影响（比时间一刀切的观察期精准）。
        self._peak_age += self.block_sec
        if not self._peak_confirmed and (kick_density > self.kick_peak * 1.18 or self._peak_age > 90.0):
            self._peak_confirmed = True
        # ★ 2026-10-27 修「连续 drop 能量 0.4~0.5」：原「确认标志」一刀切——开场即连续 drop 的
        #   歌 kick_density 平稳、从不超 kick_peak×1.18 ⇒ 卡在 1.54 因子 90 秒，kick_rel 被压到
        #   ~0.59、kick_e 掉进死区。改成**按块判据**：当前 kick_density 已达「真 kick」绝对强度
        #   （≥_KICK_PEAK_ABS_FLOOR）就视为可信、分母不再放大；仍在 breakdown/低冲击段（<floor）
        #   才保持 1.54 锚定保护。这样连续 drop 首块即免罚，breakdown 开头的钢琴段（kick_density
        #   长期 <floor）不会被误抬；1.54 因子原本防的「BW 钢琴 breakdown 锚定」场景不回归。
        is_strong_kick = kick_density >= _KICK_PEAK_ABS_FLOOR
        peak_eff = max(self.kick_peak, 1e-6) * (
            1.0 if (self._peak_confirmed or is_strong_kick) else 1.54)
        kick_rel = kick_density / peak_eff
        # 映射带 0.50 死区：实测鼓点段 kick_rel 0.71~0.84、钢琴/人声段 0.08~0.50（BW 钢琴
        # 和弦的低频瞬态会贡献 0.3~0.5，但远低于真 kick）；Bunsen 类全程均匀 kick 的歌
        # rel 长期 0.55~0.75，拐点不能高过 0.5（否则全程高潮被误压成中能量）。
        kick_e = max(0.0, min(1.0, (kick_rel - EN_KICK_DEAD) / EN_KICK_SLOPE))
        # 持续打击平滑（τ≈1.2s 对称 EMA）：真鼓点 kick_e 连续多秒高、钢琴/人声段的偶发低频
        # 敲击（钢琴低音伴奏的节奏敲击）是短促的——不平滑会被 0.7 权重放大成能量横跳
        # （BW 钢琴段 0.2↔0.6 反复、跨档滞回等不到稳定低值）。EMA 抹掉短促尖峰、保留持续
        # kick，drop 约 2.2s 到位（可接受）。
        tau_ke = 1.2
        self._kick_e_smooth += (kick_e - self._kick_e_smooth) * (1.0 - float(np.exp(-self.block_sec / tau_ke)))
        kick_e = self._kick_e_smooth
        # 低频能量分量（相对全曲峰值，只升不降，换歌重置）：drop 段 bass 高（0.7~0.9）、
        # 钢琴段低（0.3~0.4）、人声段中但被 vocal_factor 压。用于「录屏削瞬态」场景兜底——
        # NVIDIA 录制会抹平 kick 冲击（低频通量），但低频持续能量还在，用它区分 drop/钢琴。
        self.bass_peak = max(self.bass_peak, bass) if self._bass_hist is None else \
            max(self._bass_p90, bass)
        if self._bass_hist is not None:
            self._bass_hist.append(bass)
        bass_rel = bass / max(self.bass_peak, 1e-6)
        bass_factor = max(0.0, min(1.0, (bass_rel - EN_BASS_DEAD) / EN_BASS_SLOPE))
        # 低频起伏变化量因子（用户：高能量时低频起伏大）：起伏量相对全曲慢峰（只升不降）。
        # 映射与 bass_factor 同构（拐点 0.45/斜率 0.22）——起伏和低频水平高度相关。
        self.fluct_peak = max(self.fluct_peak, fluct_density) if self._fluct_hist is None \
            else max(self._fluct_p90, fluct_density)
        if self._fluct_hist is not None:
            self._fluct_hist.append(fluct_density)
        fluct_rel = fluct_density / max(self.fluct_peak, 1e-9)
        fluct_factor = max(0.0, min(1.0, (fluct_rel - EN_FLUCT_DEAD) / EN_FLUCT_SLOPE))
        # 门控（用户语义：低频值超过阈值后起伏才算高能量）：低频水平不足（bass_factor 低）时
        # 起伏量不参与——防止钢琴/人声段「起伏均匀但低频水平不高」被起伏量误抬。
        fluct_factor *= min(1.0, bass_factor * 2.0)
        # 能量 = 响度(40%) + max(低频冲击, 低频能量)(60%)。
        # 关键：kick 与 bass 取 max 而非固定加权——kick 强（正常 drop）用 kick；kick 被录屏/
        # 压缩削掉瞬态时（kick_rel 掉进死区），低频能量 bass 仍在、自动兜底。固定加权（如
        # 0.35/0.25）会两头不讨好：kick 权重低则 ITA 类「响度不突出、靠 kick 撑 drop」的歌被
        # 压到 0.5；bass 权重低则录屏场景兜不住。钢琴/人声段两者都低（bass_rel<拐点、kick<死区），
        # max 后仍为 0，不会误抬。
        # ── 绝对响度兜底项（2026-09-28 新增）─────────────────────────────────
        # 起因：`Alcohol Duper -Hardstyle VIP-` 3:03~3:29 的 drop 只读到 0.38，
        #   而它的**整体响度只比全曲最猛的 3:44 段低 1.4 dB**（实测 −7.81 vs −6.40 dBFS）。
        # 根因是低频证据项全部走「全曲慢峰 + 死区」：该段 kick 只有峰值的 ~50% ⟹ kick_e≈0，
        #   bass/fluct 同样掉进死区；而响度项又走 80s 自适应基线（持续响的段落被压平）。
        #   ⇒ 「整体很响、但低频冲击不是全曲最猛」的段落两头落空。
        # 这一项用**固定 dBFS 尺度**（不依赖任何自适应基线）作为 max() 的加项：
        #   **只会抬高、不会压低**，因此不会破坏任何既有判定。
        # 实测分离度（各段 RMS dBFS）：本曲 drop −7.8 / 最猛段 −6.4 / 同曲 intro −19.5 /
        #   Black Warrior 钢琴 breakdown −11.9 / Cosmic String 常规段 −12.4 /
        #   BW 3:00 段 −15.7。
        #   ⇒ 窗口取 [−14, −6]：drop 得 0.65~0.83；钢琴段只有 0.14、intro 与 BW3:00 为 0。
        # ★ 绝对响度兜底项：**先平滑功率（rms²）再转 dB**。
        #   旧实现先 `20*log10(rms)` 再 EMA（=对 dB 取平均）。硬舞曲 Drop 的 kick 是
        #   「高 RMS 脉冲 + 拍间低 RMS」的双峰分布，对 dB 取平均会系统性偏向安静间隙
        #   （Jensen 不等式：log 的均值 < 均值的 log），把整体响度低估 3~4 dB ⇒ abs_ev
        #   偏低、Hardstyle/Hardcore Drop 能量上不去。而且即便对「块 RMS」取平均，仍会因
        #   sqrt 的凹性再低估一次（mean(sqrt) < sqrt(mean)）。只有对**功率**（rms²）取平均、
        #   再 `10*log10` 转回 dB，才与窗口 [-14,-6] 标定时用的「段平均 RMS」口径严格一致。
        lv_pow_now = max(float(rms) * float(rms), 1e-10)
        self.lv_smooth += (lv_pow_now - self.lv_smooth) * (
            1.0 - float(np.exp(-self.block_sec / EN_LV_TAU)))
        lv_db_sm = 10.0 * float(np.log10(max(self.lv_smooth, 1e-10)))
        abs_ev = max(0.0, min(1.0, (lv_db_sm - EN_ABS_LO_DB)
                              / (EN_ABS_HI_DB - EN_ABS_LO_DB))) * EN_ABS_W
        loud_term = e_loud                                  # 响度项（0~1，融合前）
        # 低频证据项（0~1）：kick 冲击 / 低频能量 / 低频起伏 / **绝对响度** 取 max
        ev_term = max(kick_e, bass_factor, fluct_factor, abs_ev)
        e_loud = EN_W_LOUD * loud_term + EN_W_EV * ev_term
        # 人声惩罚：人声中频占比高 → 砍能量。响度均衡是宽频增益、不改变频谱形状，
        # 所以人声段中频占比始终高（压缩鲁棒的人声检测）。voc_smooth 为上一块的中频占比平滑值。
        # 惩罚系数 0.7 → 0.6（2026-09-24 用户要求「削弱一点人声惩罚」）：人声段能量会整体抬高
        # 约 5~15%（人声明显时抬得多），最大惩罚从 -70% 变 -60%。
        vocalness_now = min(1.0, self.voc_smooth * 1.6)
        vocal_factor = 1.0 - EN_VOC_MAX * max(0.0, min(1.0,
                            (vocalness_now - EN_VOC_KNEE) / EN_VOC_SPAN))
        e_loud *= vocal_factor
        # 调试观测字段（无副作用，排障用）
        self.dbg_rel = rel
        self.dbg_eloud = e_loud
        self.dbg_ws = kick_rel
        self.dbg_strike_n = vocal_factor
        # 能量各分项（供「能量诊断工具」显示；主程序不读，只放一份小字典进状态）
        try:
            st.dbg = {"loud": loud_term, "kick_e": kick_e, "bass_f": bass_factor,
                      "fluct_f": fluct_factor, "vocal_f": vocal_factor, "ev": ev_term,
                      "abs_f": abs_ev,
                      "rel": rel, "kick_rel": kick_rel, "bass_rel": bass_rel, "pre": e_loud}
        except Exception:
            pass
        # 不对称平滑：升快（τ≈0.5s，drop 突增快速响应）、降慢（τ≈2.5s，压回落震荡）。
        tau_smooth = 0.5 if e_loud > self.e_loud_smooth else 2.5
        self.e_loud_smooth += (e_loud - self.e_loud_smooth) * (1.0 - float(np.exp(-self.block_sec / tau_smooth)))
        e = max(0.0, min(1.0, self.e_loud_smooth))
        # 非对称平滑（Attack 快升 / Release 慢降）+ 慢释放压震荡：
        prev_e = self.smooth["energy"]
        if e > prev_e:
            self.smooth["energy"] = prev_e + (e - prev_e) * 0.4    # attack：快速跟 kick 上升沿
        else:
            self.smooth["energy"] = prev_e + (e - prev_e) * 0.05   # release：慢回落，压忽高忽低
        # 能量伽马校正（0.5~2.0，默认 1.0）：现场系统低频增强会把人声段能量抬到接近鼓点段。
        # 伽马>1 压低中段（人声段）、几乎不动高端（鼓点段 0.8+），比全局乘数更符合需求。
        sc = getattr(self, "energy_scale", 1.0)
        E = min(1.0, max(0.0, self.smooth["energy"]) ** sc)

        # ---- 行为模式指标 ----
        # 瞬态密度 = 每秒强拍次数。之前用「beat_active 就 append」，鼓点密集时每块都 append，
        # 4 秒窗口被 maxlen 卡满 → 恒 64，归一后恒 1，模式打分完全失真。改用 strong_times（强拍）。
        onset_density = strike_density

        if now - self.bpm_sample_t > 1.0 and self.bpm_smooth > 0:
            self.bpm_samples.append(self.bpm_smooth)
            self.bpm_sample_t = now
        bpm_stab = 0.0
        if len(self.bpm_samples) >= 4:
            arr = np.array(self.bpm_samples)
            bpm_stab = max(0.0, 1.0 - float(arr.std()) / 8.0)

        psum = float(spec.sum()) + 1e-9
        pp = spec / psum
        H = float(-(pp * np.log(pp + 1e-12)).sum()) / float(np.log(len(pp)))
        self.cx_smooth = self.cx_smooth * 0.95 + H * 0.05
        spec_complex = min(1.0, self.cx_smooth * 1.3)

        self.energy_hist.append(E)
        energy_trend = 0.0
        if len(self.energy_hist) >= 48:
            arr = np.array(self.energy_hist)
            half = len(arr) // 2
            energy_trend = max(-1.0, min(1.0, float(arr[half:].mean() - arr[:half].mean()) * 6.0))

        v = mid / (bass + mid + high + 1e-6)
        self.voc_smooth = self.voc_smooth * 0.98 + v * 0.02
        vocalness = min(1.0, self.voc_smooth * 1.6)

        # ---- 段落识别（~4Hz）----
        if self.flux_i % 11 == 0:
            s = self.seg_state
            if beat_active and E - s["prev_e"] > 0.28 and bass > 0.3:
                s["last_drop"] = now
                s["was_high"] = True
            s["prev_e"] = E

            # 各状态条件——不满足就复位（否则 build_since 一旦置位就永久卡在 build）
            in_quiet = E < 0.18
            if in_quiet:
                s["quiet_since"] = s["quiet_since"] or now
            else:
                s["quiet_since"] = None
            in_build = energy_trend > 0.06 and E > 0.15
            if in_build:
                s["build_since"] = s["build_since"] or now
            else:
                s["build_since"] = None
            # peak 滞回：进入要 E>0.65，保持只要 E>0.45——防止能量在 0.6 附近震荡时
            # peak 标签反复丢失（HUD 段落显示抖动、模式打分 seg_w 抖动）
            if E > (0.45 if s["high_since"] else 0.65):
                s["high_since"] = s["high_since"] or now
                s["was_high"] = True
            else:
                s["high_since"] = None

            # 判定优先级：drop(冲击后6s) > outro(曾高能后回落) > peak(持续高能) > build > quiet > 保持上一状态
            if now - s["last_drop"] < 6.0:
                label = "drop"
            elif s["was_high"] and E < 0.3 and not beat_active:
                label = "outro"
                s["was_high"] = False
            elif s["high_since"] and now - s["high_since"] > 2.0:
                label = "peak"
            elif s["build_since"] and now - s["build_since"] > 2.0:
                label = "build"
            elif s["quiet_since"] and now - s["quiet_since"] > 2.0:
                label = "quiet"
            else:
                label = s.get("last_label") or "quiet"   # 避免 none 闪烁：保持上一状态
            s["last_label"] = label
            s["label"] = label
            s["seg_until"] = now + 1.5

        # ---- 写共享状态 ----
        with st.lock:
            # 电平表用 dB 刻度（-30dBFS→0、-6dBFS→满），与耳朵/系统音量表的起伏一致。
            # 旧版 rms*6 线性放大：现代母带 RMS 0.15~0.3 乘 6 即满格，电平表 90% 时间爆红。
            lv_db = 20.0 * float(np.log10(max(rms, 1e-5)))
            st.lv_db = lv_db
            st.level = max(0.0, min(1.0, (lv_db + 30.0) / 24.0))
            st.bass = min(1.0, self.smooth["bass"] * 8)
            st.mid = min(1.0, self.smooth["mid"] * 12)
            st.high = min(1.0, self.smooth["high"] * 16)
            st.energy = E
            st.bpm = self.bpm_smooth
            st.bpm_display = self.bpm_display
            st.bpm_locked = self._bpm_lock
            st.beat_phase = min(1.0, max(0.0, phase))
            st.beat_count = self.beat_count
            # 小节数按识别出的小节头起算（相位未知时 downbeat_off=0，等价于旧行为）
            st.bar_count = max(0, self.beat_count - self._downbeat_off) // 4
            st.downbeat_off = self._downbeat_off
            st.downbeat_conf = self._db_conf
            # 24 段频谱：一直没往共享状态写（只写在分析器自己身上），导致
            # 「径向频谱」「音频反应变形」两个后处理拿到的是全 0 数组 → 画面毫无反应，
            # 但界面上勾选框和强度条都正常，看起来像"有显示没效果"。
            st.spectrum = self.spectrum
            st.is_beat = is_beat
            st.is_bar = is_bar
            st.drop = is_drop
            st.beat_active = beat_active
            st.onset_density = onset_density
            st.bpm_stab = bpm_stab
            st.beat_clarity = beat_clarity
            st.spec_complex = spec_complex
            st.energy_trend = energy_trend
            st.vocalness = vocalness
            st.segment = self.seg_state["label"] if now < self.seg_state["seg_until"] else "none"
            # 短静音后重新有声 = 大概率换歌：重置能量分析状态（wdb_base/bass_peak 等残留
            # 上一首歌的基线/慢峰，会导致新歌能量失真——用户反馈切歌后能量一直 0.5+）。
            # 3s 阈值（电子乐很少 3s 全静音；搓碟/EQ 短暂静音 <3s 不会误触发）。
            if st.silent_sec > 3.0 and rms >= 1e-4:
                self.wdb_base = -60.0          # 重新冷启动锚定
                self.bass_peak = 0.0
                self._peak_confirmed = False   # 新歌 peak 重新观察（锚定保护重启）
                self._peak_age = 0.0
                self._kick_e_smooth = 0.0      # 持续打击平滑重置
                self.lv_smooth = 0.0           # 绝对响度线性 RMS 平滑重置（换歌后重新积累）
                self.fluct_peak = 0.0          # 起伏变化量慢峰重置
                self.prev_bass_e = None
                self.e_loud_smooth = 0.0
                self.smooth["energy"] = 0.0
                self.smooth["bass"] = self.smooth["mid"] = self.smooth["high"] = 0.0
                self.flux_ref = 0.0
                self.bflux_ref = 0.0
                self.strike_times.clear()
                self.strike_strengths.clear()
                self._downbeat_reset()         # 新歌重新找小节头相位
            # 长静音（>10s）后重新有声 = 明确换歌：连 BPM 投票与锁定也清空（保守阈值防搓碟误清）
            if st.silent_sec > 10.0 and rms >= 1e-4:
                self._bpm_votes.clear()
                self._bpm_lock = False
                self.bpm_smooth = 0.0
                self.bpm_display = 0.0
            st.silent_sec = st.silent_sec + self.block_sec if rms < 1e-4 else 0.0
            st.running = True

    # ---------------- 小节头拍（downbeat）识别 ----------------
    DB_EVAL_BEATS = 16       # 每 16 拍评估一次候选相位
    DB_MIN_SAMPLES = 6       # 每个相位至少 6 个样本才够评估（≈24 拍）
    DB_MARGIN = 0.10         # 首次认准：冠军相位需领先亚军 10%
    DB_SWITCH_MARGIN = 0.20  # 已认准后改相位：需领先 20%
    DB_COOLDOWN = 128        # 改相位冷却（拍）≈1 分钟，防抖

    def _downbeat_reset(self):
        """清空小节头证据（换歌/断档时调用）。

        刻意**不清 `_downbeat_off`**：相位一旦认准，就用上次的结论继续对齐；
        清空证据只是让它在下一次评估前保持现状。这样「重新找」的过程不会把
        已经对好的小节线打回没有音乐含义的「拍钟起点」，整场演出不会突然错位。"""
        self._db_sum = [0.0, 0.0, 0.0, 0.0]
        self._db_cnt = [0, 0, 0, 0]
        self._db_conf = 0.0
        self._db_cand = -1
        self._db_streak = 0
        self._db_prev_cent = None
        self._db_eval_beat = 0
        self._dbw_bass = 0.0
        self._dbw_bflux = 0.0
        self._dbw_csum = 0.0
        self._dbw_w = 0.0
        self._dbw_drop = False
        self._db_pending = None

    def _downbeat_tick(self, is_beat, bass, bflux, bass_spec, is_drop):
        """按拍相位累计「这一拍像不像小节头」的证据，识别 4/4 里的第 1 拍。

        为什么要它：`beat_count` 从 0 起算，没有音乐含义，所以「每 4 拍一条小节线」
        可能整体偏 0~3 拍（固定偏移、不随时间漂移）——素材切换就踩在音乐的第 2/3/4 拍上。

        证据（全部用相对量，自动适配音量/母带/场地）：
          · 低音峰值   bass_win / bass_ref   —— 第 1 拍通常 kick 最重（主判据）
          · 低频通量   bflux_win / bflux_ref —— kick 冲击的锐度（辅助）
          · 低音换音   |低频谱质心跳变|       —— 4/4 里低音常在小节头换音（四踩均衡时的主要判据）
          · drop 命中  is_drop → +3          —— 段落转折点必是小节头（强锚点）
        四踩均衡、证据拉不开差距时**不提交**（保持当前相位 = 旧行为），绝不会乱翻。

        实现细节（两个坑都踩过）：
        ① 逐块「卡点取值」会被拍钟 ±0.3 拍的相位残差毁掉——采到的是 kick 的尾巴或提前量，
           各相位证据几乎无差别；所以按**拍窗口**取峰值统计。
        ② 结算必须**晚 0.3 拍**：拍钟可能略早于真实 kick，若在跨拍那一刻就结算，kick 起点
           会落进下一个窗口，证据整体差一拍（合成音源实测：认到没有 kick 的相位）。
           窗口取 [T_{k-1}+0.3拍, T_k+0.3拍)，则每个 kick 必然落在自己那一拍的窗口里
           （只要拍钟误差 <0.3 拍），前后两个 kick 都不会串进来。"""
        # 逐块累计到「当前拍窗口」
        if bass > self._dbw_bass:
            self._dbw_bass = bass
        if bflux > self._dbw_bflux:
            self._dbw_bflux = bflux
        if is_drop:
            self._dbw_drop = True
        tot = float(np.sum(bass_spec))
        if tot > 1e-9:
            cent = float(np.dot(np.arange(bass_spec.shape[0], dtype=np.float32), bass_spec) / tot)
            self._dbw_csum += cent * tot
            self._dbw_w += tot

        now = time.perf_counter()
        if is_beat:
            # 记下这一拍的相位，再多累计 0.3 拍后结算（等 kick 峰值进窗口）
            self._db_pending = self.beat_count % 4
            self._db_pending_due = now + 0.30 * self.beat_interval
        if self._db_pending is None or now < self._db_pending_due:
            return
        ph = self._db_pending
        self._db_pending = None
        cent_avg = (self._dbw_csum / self._dbw_w) if self._dbw_w > 1e-9 else None
        dpitch = 0.0
        if cent_avg is not None and self._db_prev_cent is not None:
            dpitch = abs(cent_avg - self._db_prev_cent) / max(1.0, self._db_prev_cent)
        if cent_avg is not None:
            self._db_prev_cent = cent_avg

        # 主判据 = 拍窗口的低音峰值（= 这一拍的 kick 轻重，第 1 拍通常最重且带低音换音）。
        # 之前用「全频通量」当主判据是错的：反拍的 hihat 是宽带噪声，通量比 kick 还大，
        # 会把证据整个带偏到反拍相位上（合成音源实测：认到没有 kick 的相位）。全频通量只
        # 保留很小权重（编配变化在低音都平的时候才有参考价值）。
        self._db_bass_ref = max(self._db_bass_ref * 0.99, self._dbw_bass)
        ev = (1.5 * min(2.0, self._dbw_bass / max(self._db_bass_ref, 1e-9))
              + 0.4 * min(2.0, self._dbw_bflux / max(self.bflux_ref, 1e-9))
              + 1.2 * min(2.0, dpitch))
        if self._dbw_drop:
            ev += 3.0
        self._db_sum[ph] += ev
        self._db_cnt[ph] += 1
        self._dbw_bass = 0.0
        self._dbw_bflux = 0.0
        self._dbw_csum = 0.0
        self._dbw_w = 0.0
        self._dbw_drop = False

        # 每 16 拍（且各相位样本够）评估一次
        if self.beat_count - self._db_eval_beat < self.DB_EVAL_BEATS:
            return
        if min(self._db_cnt) < self.DB_MIN_SAMPLES:
            return
        self._db_eval_beat = self.beat_count
        means = [self._db_sum[i] / self._db_cnt[i] for i in range(4)]
        order = sorted(range(4), key=lambda i: -means[i])
        best, second = order[0], order[1]
        margin = (means[best] - means[second]) / max(means[second], 1e-6)
        self._db_conf = max(0.0, min(1.0, margin / 0.25))

        # 连续两次评估都指向同一相位才认（抑制单次波动）
        if best == self._db_cand:
            self._db_streak += 1
        else:
            self._db_cand = best
            self._db_streak = 1
        # 首次认准只要领先 10%；已经认准过再改相位要领先 20%（防抖）
        need = self.DB_SWITCH_MARGIN if self._db_conf_ever else self.DB_MARGIN
        if self._db_streak < 2 or margin < need:
            return
        if best == self._downbeat_off:
            return
        if self.beat_count - self._db_change_beat < self.DB_COOLDOWN:
            return
        self._downbeat_off = best
        self._db_change_beat = self.beat_count
        self._db_conf_ever = True


# 现场指纹识别的**窗口长度**（秒）：每次取最近这么长的一段音频去做匹配。
# ⚠ 这个数在**两处**必须一致 —— ① 切窗 `last(sr*FP_WIN_SEC)`；② 位置补偿
#   `recognized_offset = off + FP_WIN_SEC * r`（窗口起点→窗口末端的原曲秒数）。
#   以前两处各写一个 4.5，改一处就会让「现在放到第几秒」整体偏移几秒（很难查）。
FP_WIN_SEC = 4.5
# ★★ 2026-10-29 变速认歌：4.5 秒窗票不够时，用「长窗」再认一次。
#   依据（离线实测，ffmpeg atempo 保音高变速，4 首 × 2 位置 × 5 档，见 tools/_test_win_len.py）：
#     窗口    4.5s   6.0s   9.0s   12s      （均单点票）
#       原速   115    198    386    556
#       1.08x   51     71    103    119
#   票数**超线性**于窗长 —— 因为更长的窗里"同一段 loop 只出现一次"的概率更高，
#   重复段落对票的摊平被稀释。1.08 倍速在 9 秒窗上就回到 103 票（过 strict 门槛 80），
#   而 4.5 秒窗只有 51（永远锁不上）。
#   ⇒ 默认值 9.0；设 0 可关闭（回退实验用）。⚠ 只在「短窗没锁上」时触发，稳的时候零成本。
FP_WIN_LONG_SEC = float(os.environ.get("AUTOVJ_FP_WIN_LONG", "9.0") or 0)
#: 长窗重试的**最小间隔**（秒）：认不出时每窗都重试会让识别线程负载翻倍。
#: 2.0 = 最多每 2 秒试一次长窗（短窗仍每窗照跑）。
FP_WIN_LONG_COOLDOWN = 2.0


#: 识别线程在 5 路输出满载时会被视频线程挤到 10 秒才跑一轮。
#: Windows 下把这个线程提到 HIGHEST（只影响本线程，不动 GUI/引擎）——
#: 认歌是“低频但强实时”的旁路，宁可让它插队，也不能让码表位置十几秒不更新。
def _boost_current_thread():
    try:
        if os.name != "nt":
            return
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.GetCurrentThread.restype = ctypes.c_void_p
        k32.SetThreadPriority(k32.GetCurrentThread(), 1)   # THREAD_PRIORITY_ABOVE_NORMAL
    except Exception:                                          # noqa: BLE001
        pass


def _env_flag(name, default=False):
    """读布尔型环境变量（"0"/"off"/"false"/"" ⇒ False）。"""
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return str(v).strip().lower() not in ("0", "off", "false", "no")


class _AudioRing:
    """固定容量单声道环形缓冲（numpy 实现，零 Python 对象分配）。

    为什么不用 `deque` + `tolist()`（2026-09-24 优化）：
      原实现每块 `deque.extend(mono.tolist())` —— 每秒 48000 个 Python 对象；
      更贵的是**读取端**：`_recognize_loop` 每 0.5 秒要做
      `list(self._genre_buf)[-216000:]`，也就是把 38 万元素的 deque 整个转成 list
      再切片（`_genre_loop` 每 3 秒一次同样的复制）。实测这是 `autovj-audio`
      线程占 0.78 个核的主因。改成 numpy 环形缓冲后，写入是切片赋值、读取是
      连续切片拷贝，全程不产生 Python 对象。
    """

    def __init__(self, cap):
        self.cap = int(cap)
        self.buf = np.zeros(self.cap, dtype=np.float32)
        self.pos = 0        # 下一个写入位置
        self.filled = 0     # 已写入的有效样本数（上限 cap）

    def write(self, x):
        x = np.asarray(x, dtype=np.float32).ravel()
        n = len(x)
        if n <= 0:
            return
        if n >= self.cap:
            self.buf[:] = x[-self.cap:]
            self.pos = 0
            self.filled = self.cap
            return
        p = self.pos
        end = p + n
        if end <= self.cap:
            self.buf[p:end] = x
        else:
            k = self.cap - p
            self.buf[p:] = x[:k]
            self.buf[:n - k] = x[k:]
        self.pos = end % self.cap
        self.filled = min(self.cap, self.filled + n)

    def last(self, n):
        """最近 n 个样本，按时间先后顺序返回（新数组，调用方可安全持有）"""
        n = int(min(n, self.filled))
        if n <= 0:
            return np.zeros(0, dtype=np.float32)
        start = (self.pos - n) % self.cap
        if start + n <= self.cap:
            return self.buf[start:start + n].copy()
        k = self.cap - start
        out = np.empty(n, dtype=np.float32)
        out[:k] = self.buf[start:]
        out[k:] = self.buf[:n - k]
        return out


class AudioEngine:
    """常驻音频线程封装：导入/枚举/采集都在该线程完成。"""

    def __init__(self):
        self.state = AudioState()
        # ★★★ 2026-11-02：离线 BPM 锚点线程（按需触发，只影响素材倍速）
        self._anchor = None
        self._gen = 0            # 采集代际编号：start/stop 递增，旧采集自动失效
        self._asio_cap = None    # 当前 ASIO 会话（切设备/停止时要关掉，见 _run_asio）
        self.energy_scale = 1.0  # 能量伽马校正，运行时实时透传给采集分析器
        self._jobs = queue.Queue()
        # ★ 2026-10-30 用户「大砍刀」：曲风识别（`_genre_buf` / `_genre_sr` /
        #   `_genre_loop` / `autovj-genre` 线程 / `state.genre_tags`）整条删除。
        # ★★ 指纹专用缓冲（2026-10-17）：**必须与建库时的单声道口径一致**。
        #   建库（`ui_main._one`）用 `np.mean(data, axis=1)`（左右平均）；
        #   而曲风缓冲用的是 `data[:, 0]`（只取左声道）。实测同一段现场音频：
        #       左右平均 → 54 票 ；只取左声道 → 22 票（确认门槛 20 票）
        #   ⇒ 用左声道等于让认歌**贴着门槛抖**（时认时不认）。
        #   为什么不直接改曲风缓冲：曲风分类器一直吃左声道，改它会牵动自动配画面；
        #   多开一条 12s 缓冲只多 ~2.3MB，风险为零。
        self._fp_buf = _AudioRing(48000 * 8)
        self._fp_sr = 48000
        # ★★ 认歌连续性提示的锚点 + 取证字段。
        #   ⚠⚠ 曾经**只在使用处 `self._last_match[2]` 读、从未初始化**
        #      ⇒ 每个 tick 抛 AttributeError ⇒ 被 `except: pass` 吞掉
        #      ⇒ 整段认歌逻辑从不执行 ⇒ **所有歌都认不出**（现场表现"连曲库歌也不认了"）。
        #      这就是"新增字段必须初始化 + 两处独立复位"（铁律 11）的反面教材。
        self._last_match = None      # (sid, votes, off, hint_used)
        self._last_match_t = 0.0     # 上一拍的时刻（算 dt 用）
        self._last_sig = (-1, -1.0)  # 取证：真正喂给 match 的信号 (len, rms)
        self._last_adv = 0           # 取证：本拍 offset 是否推进 0.5s±0.4
        # ★★ 2026-10-27 Round5：**连续一致锁定**（gate=streak）。
        #   现场问题：「同专辑近似曲目」把总票分走，正确曲的总票甚至不如另一首
        #   （实测 ExtraPain tot=708 而第二名 982、646 对 801）⇒ `2× 领先`这条判据
        #   天然不成立，correct 匹配被一直拒绝。但**同一首歌连续多窗胜出、且 offset
        #   按墙钟线性推进**是比"单窗票数比"强得多的证据（错歌极少连续重复且位置连续）。
        #   结构：{"sid", "n", "off", "t"}；sid 变了或 offset 不连续就重置。
        # 观测环形队列：(t, sid, off)。只存"自身证据达标"的观测，供「不重叠印证」比对。
        # ⚠ 绝不能存"已接受（已锁定）"的结果：一个错锁不许给自己担保（KVJ 同款教训）。
        self._pick_obs = deque(maxlen=16)
        # ★ 2026-10-28：频带观测队列（供 band2 救援通道的不重叠印证）+ 惰性打开的频带库
        self._band_obs = deque(maxlen=12)
        self._band = None
        self._band_ready = False
        self._band_ms = 0.0        # 取证：上一窗频带校验耗时（毫秒）
        self._band_last = None     # 取证：(score, rate, sid)
        self._band_sid = -2        # 锁变化检测（变了就清空 _band_obs）
        self._eng_state = (0, -1, 0, 0)   # 取证：电荷条 (cur, cand, bar, hits)
        self._rec_err_t = 0.0
        # ★ 2026-10-30：曲风线程删除。这把锁**保留**（改名）—— 它历史上是曲风/指纹
        #   两条缓冲共用的，现在只保护 `_fp_buf`（指纹窗口读写），改名以正视听。
        self._audio_buf_lock = threading.Lock()
        # ⚠ 识别线程 `_recognize_loop` 的退出条件原来是**借用曲风的 `_genre_stop`**
        #   （历史遗留）。曲风线程删掉后必须给它自己的事件，否则循环永不退出、
        #   `shutdown()` 也停不下来（关软件时线程 leak）。
        self._rec_stop = threading.Event()
        # 现场指纹识别：指纹库 + 音乐电池引擎（main 用 set_recognizer 启用）
        self._fp_db = None
        self._recognizer_engine = None
        # 输入音乐播放速率估计（识别链路用；见 RateEstimator）
        self._rate_est = RateEstimator()
        # ★ 2026-10-29 变速对齐（见 fp.FingerprintDB.fit_rates / FP_WIN_LONG_SEC）
        self._fit_rate = None       # 本窗「斜率拟合」给出的倍率（None = 没拟合出来）
        self._win_used_sec = FP_WIN_SEC   # 本窗实际用的窗口长度（长窗重试成功时会变）
        self._long_t = 0.0          # 上次长窗重试的挂钟时刻（冷却用）
        self._long_hits = 0         # 长窗重试成功次数（取证用）
        self._last_gate_win = "-"   # "long" / "long_fail"，便于日志区分
        # chroma 复核（见 _chroma_try）：本窗分数/倍率/耗时
        self._chroma_score = None
        self._chroma_weak = None
        self._chroma_lead = None
        self._chroma_rate = None
        self._chroma_rhist = []      # [(sid, slope, wall_t)] 最近 8 秒的斜率（见门控处）
        self._cx2 = None             # BPM 否决时那次 chroma 复核的结果（取证用）
        self._chroma_ms = 0.0
        self._chroma_t = 0.0
        # 冷启动 chroma（见 _chroma_coldstart_try）：待确认的候选与计时
        self._cold_pending = None      # (sid, off_sec, rate, wall_t)
        self._cold_t = 0.0
        self._cold_ms = 0.0
        self._cold_score = None        # 取证：本窗冷启动分数
        self._cold_hits = 0            # 取证：冷启动成功锁定次数
        self._cold_why = "-"           # 取证：冷启动为什么没结果（见 _chroma_coldstart_try）
        self._recognize_thread = threading.Thread(
            target=self._recognize_loop, daemon=True, name="autovj-recognize")
        self._recognize_thread.start()
        self._worker = threading.Thread(
            target=self._worker_loop, daemon=True, name="autovj-audio")
        self._worker.start()
        # 设备枚举单独一条线程：采集线程长期忙于 record()，枚举任务排在它后面会饿死
        self._dev_jobs = queue.Queue()
        self._dev_worker = threading.Thread(
            target=self._dev_loop, daemon=True, name="autovj-audio-dev")
        self._dev_worker.start()
        # ---- NDI 音频转发：**独立线程 + 有界队列**（见 _ndi_audio_loop）----
        # ⚠ 铁律：`ndi.feed_audio()` 要写 cyndilib 缓冲并 send_audio，接收端稍慢就会
        #   把它拖住 ⇒ **绝不能放在采集线程上** —— 那条线程还担着能量分析、拍钟、
        #   指纹识别，一堵就是整场掉拍。这里只入队（deque 带 maxlen ⇒ 满自动丢最老）。
        self._ndi_q = deque(maxlen=_NDI_Q_MAX)
        self._ndi_cv = threading.Condition()
        self._ndi_stop = False
        self._ndi_fwd_logged = False   # ★ 首次转发写一行取证日志（见 _ndi_audio_loop）
        self._ndi_thread = threading.Thread(
            target=self._ndi_audio_loop, daemon=True, name="autovj-ndi-audio")
        self._ndi_thread.start()

    def _dev_loop(self):
        sc = None
        try:
            sc = _sc()
        except Exception:
            pass
        while True:
            job = self._dev_jobs.get()
            if job is None:
                break
            try:
                job(sc)
            except Exception:
                pass

    def _ndi_audio_loop(self):
        """把采集线程投递的 PCM 转发给 NDI 发送器（**独立线程**，见 __init__）。

        队列满由 `deque(maxlen=…)` 自动丢最老的块；NDI 没接线时直接丢弃。
        全程**不阻塞采集线程**，也不做任何等待。
        """
        while not self._ndi_stop:
            item = None
            try:
                with self._ndi_cv:
                    if self._ndi_q:
                        item = self._ndi_q.popleft()
                    else:
                        self._ndi_cv.wait(0.2)
            except Exception:                                 # noqa: BLE001
                time.sleep(0.1)
                continue
            if item is None:
                continue
            fn = self.state.ndi_feed
            if fn is None:
                continue
            # ★ 首次真正转发：写一行取证日志。与「采集门控快照」配合可精确定位：
            #   有快照无本行 ⇒ 采集线程没投递（门控为假 / push 异常被吞）；
            #   有本行无 ndi_out 的「音频链路已通」⇒ feed_audio 被 _ensure() 挡下。
            if not self._ndi_fwd_logged:
                self._ndi_fwd_logged = True
                try:
                    from ndi_out import _health_write
                    _health_write("NDI 音频转发线程首次取到 PCM（队列非空）：开始投递 feed_audio")
                except Exception:                             # noqa: BLE001
                    pass
            try:
                fn(item[0], item[1])
            except Exception:                                 # noqa: BLE001
                pass

    def push_ndi_audio(self, pcm_interleaved, sr):
        """采集线程调用：**只入队**（O(1) 不阻塞），真正发送在 `_ndi_audio_loop`。"""
        try:
            with self._ndi_cv:
                self._ndi_q.append((pcm_interleaved, sr))
                self._ndi_cv.notify()
        except Exception:                                     # noqa: BLE001
            pass

    def _worker_loop(self):
        sc = None
        import warnings
        warnings.filterwarnings("ignore", message="data discontinuity in recording")
        try:
            sc = _sc()  # COM(MTA) 初始化只发生在本线程
        except Exception as e:
            with self.state.lock:
                self.state.error = f"soundcard 导入失败：{e}"
        while True:
            job = self._jobs.get()
            if job is None:
                break
            try:
                job(sc)
            except Exception as e:
                with self.state.lock:
                    self.state.error = str(e)

    def _submit(self, job):
        self._jobs.put(job)

    # ---------------- 设备枚举（阻塞，供后台扫描线程调用） ----------------
    def list_devices(self, timeout=8.0):
        """返回 {'system': [..], 'mic': [..], 'asio': [..]}。走独立的设备线程，不受采集占用影响。

        ⚠ ASIO 的"设备"就是**驱动名**（一个驱动背后可能是整台声卡的多路输入），
          不走 soundcard、也不需要 COM 设备枚举 —— 直接问原生宿主。
        """
        result = {}
        done = threading.Event()

        def job(sc):
            out = {"system": [], "mic": [], "asio": []}
            if sc is not None:
                for m in sc.all_microphones(include_loopback=True):
                    if m.isloopback:
                        out["system"].append(m.name)
                    else:
                        out["mic"].append(m.name)
            try:
                import asio_engine
                if asio_engine.available()[0]:
                    out["asio"] = ["ASIO: " + n for n in asio_engine.list_drivers()]
            except Exception:                                  # noqa: BLE001
                pass
            result.update(out)
            done.set()

        self._dev_jobs.put(job)
        done.wait(timeout)
        return result or {"system": [], "mic": [], "asio": []}

    # ---------------- 采集生命周期 ----------------
    def start(self, source_type="system", device_name="", mono=True, sr=48000,
              ch0=0, ch1=1):
        """切换设备/重启采集。用代际编号替代共享停止标志：
        旧实现 stop() 后立刻 clear()，采集循环（约21ms检查一次）可能没看到停止标志，
        导致新任务永远排队、设备切不过去（一直采集最初选的设备）。"""
        self._gen += 1                      # 让当前采集失效
        gen = self._gen
        with self.state.lock:
            self.state.running = False
            # ★★ 2026-10-08：每次重开音源都从干净状态起步。
            #   局域网音源的失败/状态文案若不清，会跨"切换设备"残留 ——
            #   用户从「局域网音频」切到系统声音再切回来，界面会先闪一下旧错误。
            self.state.lan_failed = ""
            self.state.lan_info = ""
        self._submit(lambda sc: self._run(sc, source_type, device_name, mono, sr, gen,
                                          ch0, ch1))

    def stop(self):
        self._gen += 1                      # 当前采集在下次检查时退出
        with self.state.lock:
            self.state.running = False
            self.state.lan_failed = ""      # ★ 停止后不再挂着局域网错误文案
            self.state.lan_info = ""

    def shutdown(self):
        self._gen += 1
        try:
            self._rec_stop.set()          # 停识别线程（原来借的是曲风的 stop 事件）
        except Exception:                                     # noqa: BLE001
            pass
        self._ndi_stop = True
        try:
            with self._ndi_cv:
                self._ndi_q.clear()
                self._ndi_cv.notify_all()
        except Exception:                                     # noqa: BLE001
            pass
        try:
            self._jobs.put(None)
        except Exception:
            pass
        try:
            if self._anchor is not None:
                self._anchor.stop()
                self._anchor = None
        except Exception:                                     # noqa: BLE001
            pass

    def set_allowed_provider(self, fn):
        """注册"有效 song_id 集合"的提供者；识别线程每 10 秒刷新一次白名单。"""
        self._allowed_provider = fn

    def _band_store(self):
        """侧车库的惰性初始化（三个通道共用；失败后不再重试，避免反复碰磁盘）。"""
        if self._band is None:
            try:
                import bandfeat as _bf
                self._band = _bf.BandStore(
                    os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ"), cache=8)
                self._band_ready = self._band.ready()
            except Exception:                                    # noqa: BLE001
                self._band_ready = False
        return self._band if self._band_ready else None

    def _chroma_coldstart_try(self, sig):
        """冷启动：对指纹**总票** top-N 候选做全曲 chroma 扫描。

        返回 `(sid, score, slope, target_frame)` 或 None。target_frame 是**窗起点**
        在参考曲上的位置（100 ms 帧），与引擎的 `off` 口径一致。

        ⚠ 候选必须按总票排序，不能按 `match` 返回的 sid —— 实测 0.80× 时
          `match` 会选中另一首（单点票 3 票），而**总票第一始终是真曲**。
        """
        self._cold_why = "enter"
        # ⚠ `_t` 是 `_band_try` / `_recognize_loop` 里各自的**局部别名**
        #   （`import time as _t`），跨方法不可见 —— 这里必须用模块级的 `time`。
        #   2026-10-29 真机：写成 `_t.perf_counter()` 直接 NameError，被外层
        #   `except Exception` 吞掉，日志里只剩 `cwhy=call_exc:NameError`。
        t0 = time.perf_counter()
        self._cold_why = "start"
        store = self._band_store()
        if store is None:
            self._cold_why = "no_store"
            return None
        if self._fp_db is None:
            self._cold_why = "no_db"
            return None
        tv = getattr(self._fp_db, "last_song_votes", None) or {}
        if not tv:
            self._cold_why = "no_votes"
            return None
        order = sorted(tv.items(), key=lambda kv: (-int(kv[1]), int(kv[0])))
        refs = []
        _miss = 0
        for sid, _v in order[:max(1, CHROMA_COLD_TOPN)]:
            try:
                got = store.get_chroma(int(sid))
            except Exception as _e:                              # noqa: BLE001
                got = None
                self._cold_why = "get_exc:" + type(_e).__name__
            if got is not None:
                refs.append((int(sid), got[0]))
            else:
                _miss += 1
        if not refs:
            self._cold_why = "no_refs(%d/%d miss)" % (
                _miss, min(len(order), max(1, CHROMA_COLD_TOPN)))
            return None
        try:
            import bandfeat as _bf
            q = _bf.chroma_matrix(_bf.to_mono8k(sig, self._fp_sr))
            got = _bf.chroma_coldstart(q, refs, slopes=CHROMA_COLD_SLOPES, step_t=2)
            self._cold_ms = (time.perf_counter() - t0) * 1000.0
            self._cold_why = "ok(q=%d,%d)" % (q.shape[0], len(refs))
        except Exception as _e:                                  # noqa: BLE001
            self._cold_why = "exc:" + type(_e).__name__ + ":" + str(_e)[:40]
            self._cold_ms = (time.perf_counter() - t0) * 1000.0
            return None
        return got

    def _band_try(self, sig, sid, off, now):
        """频带救援通道的一次尝试（识别线程内，**只碰本地小库 + 纯计算**）。

        返回 True 表示「本窗观测与前一次**互不重叠**的观测互证成立」。
        失败/不可用一律返回 False（绝不因为这条路而放宽原有判据）。
        """
        if self._band_store() is None:
            return False
        got = self._band.get(int(sid))
        if got is None:
            return False
        try:
            import bandfeat as _bf
            t0 = time.perf_counter()
            q = _bf.band_matrix(_bf.to_mono8k(sig, self._fp_sr))
            score, _weakest, rate, off2 = _bf.verify(
                q, got[0], float(off), BAND_RATE_SET,
                span=BAND_OFF_SPAN, step=BAND_OFF_STEP)
            self._band_ms = (time.perf_counter() - t0) * 1000.0
        except Exception:                                        # noqa: BLE001
            return False
        self._band_last = (round(float(score), 3), round(float(rate), 2), int(sid))
        if score < BAND_SCORE_MIN:
            return False
        ok = fp_band_agree_ok(now, sid, off2, rate, self._band_obs)
        self._band_obs.append((float(now), int(sid), float(off2), float(rate)))
        return ok

    def _chroma_try(self, sig, sid, target_hint_frame, rate_hint, rate_span=None):
        """chroma 复核（**只对已锁定的那一首**）。返回 `(score, slope, target_frame)` 或 None。

        为什么只对已锁定曲目做（2026-10-29 实测）：
          · 限定范围的版本 91~127 ms；自由扫全曲要 300~400 ms，而且**会跳到重复段落**
            （实测 Black Warrior 45 秒处的查询，自由扫描给 target=112，真值 450）。
            引擎本来就知道"哪首 + 大概第几秒"，没理由重新自由扫。
          · 拿它当**独立证据**用：score ≥ `CHROMA_VERIFY_SCORE`(0.55) 时，
            它的 slope 比 `_rate_est` 准一个量级（实测 1.080/1.200 精确命中真倍率，
            而 `_rate_est` 在这种"票少频繁掉锁"的工况下常年停在 1.000）。
        实测分数（4.5 秒窗，限定 ±30 帧 / ±0.08 倍率）：
          同曲 1.00 / 1.08 / 1.20 → 0.97 / 0.86 / 0.81（三首歌都在 0.60~0.98）
          异曲 → 0.14 ~ 0.17
        """
        if self._band_store() is None:
            return None
        try:
            got = self._band.get_chroma(int(sid))
        except Exception:                                        # noqa: BLE001
            got = None
        if got is None:
            return None
        try:
            import bandfeat as _bf
            t0 = time.perf_counter()
            q = _bf.chroma_matrix(_bf.to_mono8k(sig, self._fp_sr))
            res = _bf.chroma_verify_near(
                q, got[0], float(target_hint_frame), float(rate_hint),
                target_span=CHROMA_TARGET_SPAN, rate_span=rate_span)
            self._chroma_ms = (time.perf_counter() - t0) * 1000.0
        except Exception:                                        # noqa: BLE001
            return None
        return res

    def set_song_bpm_provider(self, fn):
        """注册「song_id → 库内 BPM」的提供者（C 组误识别修复用）。

        `fn(sid) -> float`（拿不到返回 0/None）。识别线程在每个识别窗里取一次，
        与实时 `state.bpm_display` 做一致性校验：偏差 > `BPM_MATCH_TOL` 且不构成
        倍频关系 ⇒ 本窗按"没匹配到"处理（宁可不认，也不认错）。
        `fn=None` 关闭校验（缺省状态，行为与改动前逐值一致）。
        """
        self._song_bpm_provider = fn

    def set_recognizer(self, fp_db):
        """启用现场识别（main 在曲库就绪后调用）。fp_db=None 禁用。"""
        self._fp_db = fp_db
        if fp_db is not None and self._recognizer_engine is None:
            from charge_engine import ChargeBarEngine
            self._recognizer_engine = ChargeBarEngine()

    # ★ 2026-10-19：可由环境变量覆盖（默认 2.0 不变）。
    #   排查「切歌要十几秒」时需要 0.5s 粒度逐 tick 观测：set AUTOVJ_REC_DIAG_SEC=0.5
    _REC_DIAG_MIN_SEC = float(os.environ.get("AUTOVJ_REC_DIAG_SEC", "2.0") or 2.0)

    def _rec_diag(self, fp_db, eng):
        """`cue_health.log`：识别门控取证（2s 一行，前缀 `[R]`）。

        用户报"认不出歌"时，`engine._cue_diag` 只能看到**输出**（sid/votes 恒 -1/0），
        看不到**为什么**。这一行补上四个门：

            db  = 指纹库有没有接上（0 ⇒ `set_recognizer` 没生效，认歌线程空转）
            eng = 电荷条引擎（0 ⇒ 同上）
            run = state.running（0 ⇒ 引擎没启动，采集也没跑）
            buf = 采集环形缓冲已填样本数（`<16000` ⇒ 音频根本没进来）
            silent = 连续静音秒数（`>10` ⇒ 会主动回待机、清识别结果）
            lvl/eng = 当前 RMS / 综合能量（判断"有信号"还是"真有音乐"）

        ⚠ 与 `engine._cue_diag` 写**同一个文件**，用前缀区分；都做了环形裁剪。
        """
        try:
            now = time.perf_counter()
            if now - float(getattr(self, "_rec_diag_t", 0.0) or 0.0) < self._REC_DIAG_MIN_SEC:
                return
            self._rec_diag_t = now
            st = self.state
            filled = int(getattr(self._fp_buf, "filled", 0) or 0)
            sr = int(getattr(self, "_fp_sr", 0) or 0)
            # ★ 每 10s 做一次「查询哈希命中率」自检 —— 这是区分
            #   「送进来的音频不对」与「投票/选取有 bug」的**唯一判据**：
            #     n_hash 正常(~300~500) 且 hit≈0    ⇒ 音频不是库里那首（声道/重采样/源不对）
            #     hit 很多但 votes 恒 0             ⇒ 匹配器/选取逻辑有问题
            hit, n_hash, seg_rms, seg_n = -1, -1, -1.0, -1
            _n_uni = -1
            try:
                if now - float(getattr(self, "_rec_hash_t", 0.0) or 0.0) >= 10.0:
                    self._rec_hash_t = now
                    with self._audio_buf_lock:
                        _seg = self._fp_buf.last(int(self._fp_sr * 4.5))
                    _seg = np.asarray(_seg, dtype=np.float32)
                    seg_n = int(_seg.size)
                    seg_rms = float(np.sqrt(np.mean(_seg ** 2))) if seg_n else 0.0
                    # ★★ 2026-11-11（用户：码表偏差 / 认歌慢）：这里以前每 10 秒
                    #   对 400 个 hash 做一次 `COUNT(DISTINCT ...)`，现场 5 路输出
                    #   满载 + 机械盘时会把识别循环拖到 10 秒一轮。
                    #   命中率 `fp.match` 已经算好了（`last_hit_rate` + `last_hs`），
                    #   这里直接换算，**不再发额外 SQL、不再重算指纹**。
                    _hs = getattr(fp_db, "last_hs", None) if fp_db is not None else None
                    if _hs:
                        _uni_n = len({h for h, _o in _hs})
                        n_hash = len(_hs)
                        _n_uni = _uni_n
                        try:
                            hit = int(round(float(getattr(fp_db, "last_hit_rate", 0.0) or 0.0)
                                            * _uni_n))
                        except (TypeError, ValueError):
                            hit = -1
            except Exception:                                    # noqa: BLE001
                hit, n_hash, _n_uni = -1, -1, -1
            line = ("[%s] [R] db=%d eng=%d run=%d buf=%d(%.1fs) silent=%.1fs lvl=%.4f engy=%.3f bpm=%.1f "
                    "hit=%d/%d seg=%.3f/%d sig=%d/%.3f raw=%d/hint=%d adv=%d cur=%d cand=%d bar=%d hits=%d"
                    % (time.strftime("%H:%M:%S"),
                       1 if fp_db is not None else 0,
                       1 if eng is not None else 0,
                       1 if getattr(st, "running", False) else 0,
                       filled, (filled / float(sr)) if sr else 0.0,
                       float(getattr(st, "silent_sec", -1.0) or 0.0),
                       float(getattr(st, "level", 0.0) or 0.0),
                       float(getattr(st, "energy", 0.0) or 0.0),
                       float(getattr(st, "bpm_display", 0.0) or 0.0),
                       hit, _n_uni, seg_rms, seg_n,
                       int((getattr(self, "_last_sig", (-1, -1.0)) or (-1, -1.0))[0]),
                       float((getattr(self, "_last_sig", (-1, -1.0)) or (-1, -1.0))[1]),
                       int((getattr(self, "_last_match", (0, 0, 0.0, 0)) or (0, 0, 0.0, 0))[1]),
                       int((getattr(self, "_last_match", (0, 0, 0.0, 0)) or (0, 0, 0.0, 0))[3]),
                       int(getattr(self, "_last_adv", 0) or 0),
                       *[int(x) for x in (getattr(self, "_eng_state", (0, -1, 0, 0)) or (0, -1, 0, 0))]))
            # ★ 2026-10-18 审计：补 sid / off（数据本来就在 `_last_match` 里，之前没打）。
            #   现场两类高频故障靠它定案：认成相似曲（那 180 票投给了谁）、位置卡住/跳变（off 是多少）。
            _lmv = getattr(self, "_last_match", None) or (0, 0, 0.0, 0)
            line += " sid=%d off=%.2f" % (int(_lmv[0]), float(_lmv[2]))
            # ★ 白名单规模（None=未限制；0=空集=谁都不认，属异常）
            try:
                _al = getattr(fp_db, "allowed", None)
                line += " allow=%s" % ("-" if _al is None else len(_al))
            except Exception:
                pass
            # ★ 本次锁定用的是哪条判据：strict / warp / recode / band2（频带印证）/ none
            line += " gate=%s" % (getattr(self, "_last_gate", "-") or "-")
            # ★ 2026-10-28：频带校验取证（追加在行尾，不破坏既有 regex）
            #   bst=score/rate/sid；bms=本窗耗时（毫秒）。没有频带库时 bst=None。
            try:
                _bl = getattr(self, "_band_last", None)
                line += " bst=%s bms=%.1f" % (
                    ("%s/%s/%s" % tuple(_bl)) if _bl else "None",
                    float(getattr(self, "_band_ms", 0.0) or 0.0))
            except Exception:
                pass
            # ★ 2026-10-27：BPM 一致性校验的原始数字（lbpm=库内 / rbpm=实时 / brel=相对差）
            #   **追加在行尾**，不破坏既有 `[R]` regex。brel=none 表示本窗没做校验（任一为 0）。
            try:
                _bg_ = getattr(self, "_bpm_gate", None)
                if _bg_:
                    line += " lbpm=%.1f rbpm=%.1f brel=%s" % (
                        float(_bg_[0]), float(_bg_[1]),
                        "none" if _bg_[2] is None else "%.3f" % float(_bg_[2]))
                    # ★ 2026-10-27：1=通过 0=被否决 -1=本窗没校验（任一 BPM 为 0）
                    line += " bpmok=%d" % (getattr(self, "_bpm_ok_flag", None)
                                           if getattr(self, "_bpm_ok_flag", None) is not None else -1)
            except Exception:                                    # noqa: BLE001
                pass
            # ★ 门槛前的原始数字：(pick, votes, tot, second_tot, hit_rate)
            #   排查"为什么不认"时，这五个数就能直接判断是哪一条没过。
            try:
                _gi = getattr(self, "_gate_info", None)
                if _gi:
                    line += " pick=%d pv=%d ptot=%d p2=%d phr=%.2f" % _gi
            except Exception:                                    # noqa: BLE001
                pass
            # ★ 2026-10-29 变速对齐取证（**追加在行尾**，不破坏既有 regex）：
            #   win = 本窗用的识别窗长（4.5 短窗 / 9.0 长窗重试成功）
            #   lw  = 长窗重试结果（long / long_fail / - 表示没触发）
            #   fr  = 斜率拟合出的倍率（nan = 本窗没拟合出来）
            #   看现场"变速认不出"时直接查这三个：win=9 说明长窗救回来了；
            #   fr=1.19 说明确实在变速；lw=long_fail 且 pv 很低说明长窗也不够。
            try:
                _frv = getattr(self, "_fit_rate", None)
                line += " win=%.1f lw=%s fr=%s lh=%d" % (
                    float(getattr(self, "_win_used_sec", FP_WIN_SEC) or FP_WIN_SEC),
                    str(getattr(self, "_last_gate_win", "-")),
                    ("%.3f" % float(_frv)) if _frv is not None else "nan",
                    int(getattr(self, "_long_hits", 0) or 0))
                # ★ 2026-10-29 chroma 复核取证：cx = 分数，cr = 它给的倍率，cms = 耗时
                _cxs = getattr(self, "_chroma_score", None)
                _cxr = getattr(self, "_chroma_rate", None)
                #   bv = BPM 否决时 chroma 复核的「得分/最弱/领先」（nan = 没做）
                _bv = getattr(self, "_cx2", None)
                if _bv is not None:
                    line += " bv=%.2f/%.2f/%+.2f" % (
                        float(_bv["score"]), float(_bv["weak"]), float(_bv["lead"]))
                else:
                    line += " bv=nan"
                #   cw = 最弱窗（三段里最低的一段），cl = 领先（得分减次选）
                _cxw = getattr(self, "_chroma_weak", None)
                _cxl = getattr(self, "_chroma_lead", None)
                line += " cx=%s cw=%s cl=%s cr=%s cms=%.0f" % (
                    ("%.2f" % float(_cxs)) if _cxs is not None else "nan",
                    ("%.2f" % float(_cxw)) if _cxw is not None else "nan",
                    ("%+.2f" % float(_cxl)) if _cxl is not None else "nan",
                    ("%.3f" % float(_cxr)) if _cxr is not None else "nan",
                    float(getattr(self, "_chroma_ms", 0.0) or 0.0))
                # ★ 冷启动取证：cold = 冷启动分数（nan = 本窗没跑/没结果），
                #   ch = 冷启动成功锁定次数，cdms = 冷启动耗时（ms）
                _cds = getattr(self, "_cold_score", None)
                line += " cold=%s ch=%d cdms=%.0f cwhy=%s" % (
                    ("%.2f" % float(_cds)) if _cds is not None else "nan",
                    int(getattr(self, "_cold_hits", 0) or 0),
                    float(getattr(self, "_cold_ms", 0.0) or 0.0),
                    str(getattr(self, "_cold_why", "-")))
            except Exception:                                    # noqa: BLE001
                pass
            path = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ",
                                "cue_health.log")
            _d = os.path.dirname(path)
            if _d and not os.path.isdir(_d):
                os.makedirs(_d, exist_ok=True)      # 兜底：目录不在也能写（不在热路径，每 2s 一次）
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            try:
                if os.path.getsize(path) > 120 * 1024:
                    with open(path, "r", encoding="utf-8") as f:
                        rows = f.readlines()
                    with open(path, "w", encoding="utf-8") as f:
                        f.writelines(rows[-200:])
            except OSError:
                pass
        except Exception:                                        # noqa: BLE001
            pass

    def _match_with_win(self, fp_db, win_sec, hint, hint_w, hint_sid, lock_sid=None):
        """用指定窗口长度（秒）重认一次。

        返回 `(sid, votes, off, tot, second, second_tot, hr, win_sec)`；窗口里
        数据不够（刚启动 / 刚清空）返回 None。

        ⚠ `off` 是**这个窗口起点**在原曲里的秒数 —— 调用方做「窗口末端补偿」时
          必须用返回元组里的 `win_sec`，不能写死 `FP_WIN_SEC`（这正是以前那两个
          4.5 写死导致的偏几秒问题的同类陷阱）。
        """
        import numpy as _np
        n = int(self._fp_sr * float(win_sec))
        with self._audio_buf_lock:
            if int(getattr(self._fp_buf, "filled", 0) or 0) < n:
                return None
            buf = self._fp_buf.last(n)
        try:
            sig = _np.asarray(buf, dtype=_np.float32)
            sid, votes, off = fp_db.match(sig, self._fp_sr,
                                          hint_delta=hint, hint_w=hint_w,
                                          hint_sid=hint_sid,
                                          lock_sid=lock_sid)
        except Exception:                                        # noqa: BLE001
            return None
        if int(sid) < 0:
            return None
        try:
            _tv = getattr(fp_db, "last_song_votes", {}) or {}
            tot = int(_tv.get(sid, 0))
            second = 0
            second_tot = 0
            _sv = getattr(fp_db, "last_song_best", {}) or {}
            second = max((int(v) for k, v in _sv.items() if k != sid), default=0)
            second_tot = max((int(v) for k, v in _tv.items() if k != sid), default=0)
            hr = float(getattr(fp_db, "last_hit_rate", 0.0) or 0.0)
        except Exception:                                        # noqa: BLE001
            tot, second, second_tot, hr = int(votes or 0), 0, 0, 0.0
        return int(sid), int(votes or 0), float(off or 0.0), tot, second, second_tot, hr, float(win_sec)

    def _recognize_loop(self):
        """现场识别：每 0.5s 取最近 4.5s 音频 → 指纹匹配 → 电池引擎确认 → 发布 song_id"""
        import time as _t
        import numpy as _np
        from charge_engine import FpResult
        _boost_current_thread()
        while not self._rec_stop.is_set():
            # ★ 2026-10-19 排查：每轮实际耗时 + 缓冲样本数（判断'循环慢'还是'采集停'）
            try:
                _nowp = time.perf_counter() if os.environ.get('AUTOVJ_REC_PROBE') else 0.0
                _prevp = float(getattr(self, '_loop_t_prev', 0.0) or 0.0)
                self._loop_t_prev = _nowp
                if _prevp > 0.0 and _nowp > 0.0:
                    _lp2 = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'AutoVJ', 'loop_timing.log')
                    _n2 = int(getattr(self._fp_buf, 'filled', 0) or 0)
                    with io.open(_lp2, 'a', encoding='utf-8') as _f2:
                        _f2.write('%.3f' % _nowp + ' ' + '%.3f' % (_nowp - _prevp) + ' ' + '%d' % _n2 + chr(10))
            except Exception:
                pass
            _t.sleep(0.3)
            fp_db = self._fp_db
            eng = self._recognizer_engine
            self._rec_diag(fp_db, eng)
            if fp_db is None or eng is None or not self.state.running:
                continue
            # ★ 每 10 秒刷新一次"有效曲目白名单"（曲库增删后无需重启）
            #   ★★ 2026-11-11（用户：码表偏差/认歌慢）：旧实现在**识别线程里同步**
            #   读配置 + 打开 imported_meta.db；现场满载时这一步能把整个认歌循环
            #   拖到 10~12 秒一轮。改成后台线程刷新，识别线程永不因白名单阻塞。
            try:
                _prov = getattr(self, "_allowed_provider", None)
                _now2 = _t.time()
                if _prov is not None and _now2 - float(getattr(self, "_allow_t", 0.0) or 0.0) >= 10.0:
                    self._allow_t = _now2
                    if not getattr(self, "_allow_busy", False):
                        self._allow_busy = True

                        def _refresh_allowed():
                            try:
                                _ids = _prov()
                                if _ids is not None:
                                    fp_db.set_allowed(_ids)
                            except Exception:                # noqa: BLE001
                                pass
                            finally:
                                self._allow_busy = False

                        threading.Thread(target=_refresh_allowed, daemon=True,
                                         name="fp-allowed").start()
            except Exception:                                    # noqa: BLE001
                pass
            # 静音超时 → 回待机（清空识别）
            with self.state.lock:
                silent = self.state.silent_sec
            if silent > 10.0:
                eng.reset()
                self._rate_est.reset()
                with self.state.lock:
                    self.state.recognized_song_id = -1
                    self.state.recognized_offset = -1.0
                    self.state.recognized_rate = 1.0
                    self.state.recognized_votes = 0
                continue
            # ★★ 2026-11-12（静音待机 CPU）：缓冲里可能还留着上一次的声音，
            #   但当前采集电平已经是 0 —— 继续跑指纹/谱图只会反复匹配旧窗口，
            #   既白烧 CPU（实测 ~0.18 核），又可能在静音间隙把码表带到旧位置。
            #   10s 以上的静音已在上面重置；这里只挡 0.5~10s 的纯数字静音。
            try:
                with self.state.lock:
                    _lvl_now = float(getattr(self.state, "level", 0.0) or 0.0)
                if _lvl_now <= 1e-5 and silent >= 0.5:
                    continue
            except Exception:                                # noqa: BLE001
                pass
            with self._audio_buf_lock:
                if self._fp_buf.filled < 16000:
                    continue
                # 只要最近 4.5 秒（指纹匹配窗口）；numpy 切片拷贝，不再 list(deque) 全量复制
                buf = self._fp_buf.last(int(self._fp_sr * FP_WIN_SEC))
                # 窗口末端的挂钟时刻：识别结果对应的"当前时刻"（给引擎网格时钟对齐用）
                t_read = _t.perf_counter()
            try:
                sig = _np.asarray(buf, dtype=_np.float32)
                # ★ 连续性提示：引擎网格时钟预测的「本识别窗起点」原曲秒数 → 帧数，交给
                #   fp.match。强重复段落里同一段音频在歌里出现多次，纯靠票数会挑到"另一处
                #   出现"的高票错位置；带 hint 时优先选预测位置附近（±窗口 帧）的候选，
                #   窗内无候选则退回全局众数（真实远距离 seek 仍生效）。无预测（-1）⇒ 与
                #   旧行为逐值一致。读取在 state.lock 下（引擎每帧写入，见 engine._beat_pos_grid）。
                #   窗口按拍数定义（引擎按 beat_len 换算，见 fp.hint_window_frames/state.grid_pred_w）。
                from fp import SAMPLE_RATE as _FP_SR, HOP_SIZE as _FP_HOP
                with self.state.lock:
                    _pred = self.state.grid_pred_orig
                    _pred_w = self.state.grid_pred_w
                    _locked = self.state.recognized_song_id
                # ★ 2026-11-12：本地跟踪用的锁定曲目 id。`recognized_song_id` 可能
                #   因 BPM 否决被清零，但 `eng.current_song_id` 仍代表状态机锁定的歌。
                try:
                    _lock_sid = int(_locked)
                except (TypeError, ValueError):
                    _lock_sid = -1
                if _lock_sid < 0:
                    try:
                        _lock_sid = int(getattr(eng, "current_song_id", -1))
                    except (TypeError, ValueError):
                        _lock_sid = -1
                # ★★★ 2026-10-17：**还没锁定任何歌时绝不给 hint**。
                #   现场实测（`hit=181/196`，查询哈希 92% 都在库里）：音频与库都没问题，
                #   但 `votes` 长期偏小、电荷条永远不确认 ⇒ 认不出。根因就是这个 hint：
                #     `_pick()` 的设计是"窗口内只要有候选（哪怕 1 票）就取它"，这在
                #     **已锁定**时是对的（防重复段落挑错位置）；但在**尚未锁定**时，
                #     `grid_pred_orig` 本身没有意义 ⇒ 窗口落点随机 ⇒ 挑中一个 1~3 票的
                #     杂散候选 ⇒ 票数永远够不到 CONFIRM_VOTES(20) ⇒ 永远锁不上 ⇒
                #     预测继续错 ⇒ **死锁**。现场表现就是"一直认不出"。
                #   因此：锁定前走纯全局众数（等于旧行为），锁定后才用 hint 防歧义。
                # ★★ 2026-10-17（第二版，现场实证）：**未锁定时必须给「连续性提示」**，
                #   但提示只能来自**上一次匹配的偏移 + 实际间隔**，不能来自拍钟预测。
                #   为什么（这次是 `charge_engine` 的判据逼出来的）：
                #     `tick()` 要求 `offset_sec` 每拍递增 DELTA_ADVANCE_SEC(0.5s) 才允许充电
                #     （`chargeable = advancing and votes >= CONFIRM_VOTES`）。
                #     不给 hint ⇒ `_pick` 取**全局众数**，在重复段落里位置会在几处之间来回跳
                #       （现场日志：136→89→83→139→349）⇒ advancing 永远 False ⇒ 永不充电。
                #     给拍钟 hint ⇒ 未锁定时预测无意义 ⇒ 挑中 1~8 票杂散候选 ⇒ 同样不充电。
                #   ⇒ 两条路都堵死，表现就是"raw 有 80~180 票也不确认"。
                #   正解：用「上次偏移 + dt」当 hint —— 既保证连续推进（满足充电判据），
                #     又不依赖任何未建立的坐标；DJ seek 时该窗口自然无候选 ⇒ 退回全局众数。
                _now = _t.perf_counter()
                _lm = getattr(self, "_last_match", None)
                _lm_t = float(getattr(self, "_last_match_t", 0.0) or 0.0)
                # ★★ 2026-10-27：hint 改为「**上一窗实际 off** ± FP_PREV_HINT_SEC 秒」。
                #   为什么不用 _last_match_t 推算"该走到哪"：历史上就是这么干过，实测 off 会
                #   被锁死在预测附近（连续 8~10s 不动、落后 8~14s）。而用"上一帧实际匹配到的
                #   off"当锚，只解决**同一位置附近**的重复段落歧义，不引入任何预测误差。
                # ★★ 2026-10-27 第三轮（Leviathan off 没修好）：hint 锚点必须用
                #   **状态机已接受**的 off，不能用 `_last_match[2]`（原始匹配值）。
                #   用原始值时：这一窗跳到错位置 → 下一窗的 hint 被带到错位置 → 状态机
                #   虽然把发布值拉住了，但识别一直是"对着错位置找" ⇒ 正反馈锁死。
                #   改用 accepted 值后，hint 与状态机互相印证，形成闭环。
                _prev_off = None
                # ★★ 2026-10-27 Round5（换歌永远切不过去）：hint 是**某一首歌时间轴上的
                #   位置**，换了歌就对不上。这里把 hint 的"归属曲目"一起带给 fp，
                #   让 `_pick` 只在「窗内胜者仍是这首」时才用 hint（否则退回全局众数）。
                _prev_sid = -1
                _st = getattr(self, "_off_state", None)
                if _st is not None:
                    try:
                        if int(_st.get("sid", -2)) >= 0 and float(_st.get("off", -1.0)) >= 0.0:
                            _prev_off = float(_st["off"])
                            _prev_sid = int(_st["sid"])
                    except (TypeError, ValueError, KeyError):
                        _prev_off = None
                        _prev_sid = -1
                if _prev_off is None and _lm is not None:
                    try:
                        if (int(_lm[0]) >= 0 and int(_lm[1] or 0) >= 4
                                and float(_lm[2] or 0.0) >= 0.0):
                            _prev_off = float(_lm[2])
                            _prev_sid = int(_lm[0])
                    except Exception:                        # noqa: BLE001
                        _prev_off = None
                        _prev_sid = -1
                _hint = None
                _hint_w = None
                if FP_USE_HINT and (_locked is not None and int(_locked) >= 0
                                    and _pred is not None and _pred >= 0.0):
                    # 旧路径（**默认关闭**，仅供对照实验）：用拍钟预测当 hint。
                    _hint = _pred * _FP_SR / _FP_HOP
                    _hint_w = (float(_pred_w) if (_pred_w is not None and float(_pred_w) > 0.0)
                               else None)
                elif FP_PREV_HINT_SEC > 0 and _prev_off is not None:
                    # 新路径（A 组修复）：上一窗 off ± N 秒的连续性窗。
                    # 窗内无候选时 `fp._pick` 会自动退回全局众数 ⇒ 换歌 / seek 能跳出去。
                    _hint = _prev_off * _FP_SR / _FP_HOP
                    _hint_w = FP_PREV_HINT_SEC * _FP_SR / _FP_HOP
                sid, votes, off = fp_db.match(sig, self._fp_sr,
                                              hint_delta=_hint, hint_w=_hint_w,
                                              hint_sid=(_prev_sid if _hint is not None else None),
                                              lock_sid=(_lock_sid if _lock_sid >= 0 else None))
                # ★ 本窗用的是哪个长度的窗口（决定下面「窗口起点→末端」补偿用几秒）。
                #   默认短窗；下面长窗重试成功时会被改写。
                self._win_used_sec = FP_WIN_SEC
                # ★★ 2026-10-27（对照 KVJ 源码重写）：把"本窗观测"记进环形队列，
                #   供**互不重叠印证**使用。只有"自身证据达标"的窗口才入库 ——
                #   弱窗口存进去只会制造假证据。
                #   ⚠ 门槛判定与 gate 无关（gate 在下面才算），入库不等于接受。
                try:
                    if LOCK_AGREE_ENABLE and int(sid) >= 0 and fp_obs_ok(
                            votes,
                            (getattr(fp_db, "last_song_votes", {}) or {}).get(sid, 0),
                            getattr(fp_db, "last_hit_rate", 0.0)):
                        self._pick_obs.append((float(_now), int(sid), float(off)))
                        while (self._pick_obs
                               and _now - float(self._pick_obs[0][0]) > LOCK_AGREE_MIN_SEC * 4.0):
                            self._pick_obs.popleft()
                except Exception:                            # noqa: BLE001
                    pass
                # ★★ 2026-10-19：**证据门槛（防误识别）**。
                #   `votes` 只是"某一个对齐位置"的票；重复/相似段落会把票摊到多个位置，
                #   单点票低不代表不是这首歌。这里再用「该曲总票」把关：
                #   实测真歌总票 93~740，白噪 ≤62 ⇒ 门槛取 80。
                #   低于门槛一律按"没匹配到"处理（宁可不认，也不认错）。
                # ★★ 2026-10-19：**门槛只用于"初次锁定"，不拦"换歌证据"**。
                #   切歌瞬间 4.5s 窗口里新旧混杂，新歌总票常掉到 80 以下；
                #   如果这时也拦，换歌证据根本进不了引擎 ⇒ 只能等纯新歌窗口 ⇒ 多花数秒。
                #   防误识别由 charge_engine 的"3 秒累计 ≥120 且 ≥2 拍"负责，足够。
                _locked = int(getattr(eng, "current_song_id", -1)) >= 0
                if sid >= 0 and not _locked:
                    try:
                        _tot = int((getattr(fp_db, "last_song_votes", {}) or {}).get(sid, 0))
                    except Exception:                            # noqa: BLE001
                        _tot = int(votes or 0)
                    # ★★ 2026-10-22：**初次锁定还要求"单拍峰值 ≥80 票"**。
                    #   实测真歌峰值 124~227，跨歌误认（同专辑相似曲）峰值 30~61；
                    #   只看总票会放过后者（总票被多位置摊高）。这条把误认压到 0。
                    # 2026-10-25：判定抽成纯函数 `fp_lock_gate`（见模块级定义），
                    #   因为"DJ 录的视频"要加一条新的放行条件，得能离线回归。
                    _second, _second_tot = 0, 0
                    try:
                        _sv = getattr(fp_db, "last_song_best", {}) or {}
                        _second = max((int(v) for k, v in _sv.items() if k != sid), default=0)
                        _tv = getattr(fp_db, "last_song_votes", {}) or {}
                        _second_tot = max((int(v) for k, v in _tv.items() if k != sid), default=0)
                    except Exception:                        # noqa: BLE001
                        _second, _second_tot = 0, 0
                    _hr = float(getattr(fp_db, "last_hit_rate", 0.0) or 0.0)
                    # 取证：把"门槛前"的原始数字留下来（`[R]` 行末尾 pick/pv/ptot/p2）
                    self._gate_info = (int(sid), int(votes or 0), int(_tot),
                                       int(_second_tot), round(_hr, 2))
                    _gate_ok = fp_lock_gate(sid, int(votes or 0), _tot,
                                            _second, _second_tot, _hr)
                    # ★★ 2026-10-29 变速认歌：短窗票不够 ⇒ 用**长窗**再认一次。
                    #   票数超线性于窗长（见 FP_WIN_LONG_SEC 的实测表）：1.08 倍速在
                    #   4.5s 窗上只有 51 票（永远锁不上），9.0s 窗上有 103 票（过 80）。
                    #   只在短窗没过门槛时触发 ⇒ 正常播放（票 100~300）零额外开销。
                    _win_used = FP_WIN_SEC
                    if (not _gate_ok) and FP_WIN_LONG_SEC > FP_WIN_SEC + 0.1:
                        try:
                            _now_l = _t.perf_counter()
                            if _now_l - float(getattr(self, "_long_t", 0.0) or 0.0) >= FP_WIN_LONG_COOLDOWN:
                                self._long_t = _now_l
                                _r2 = self._match_with_win(fp_db, FP_WIN_LONG_SEC,
                                                           _hint, _hint_w, _prev_sid,
                                                           lock_sid=(_lock_sid if _lock_sid >= 0 else None))
                                if _r2 is not None:
                                    (_s2, _v2, _o2, _t2, _sec2, _sect2, _hr2, _w2) = _r2
                                    _tot2 = int(_t2)
                                    # ★ 这一窗的 off 是**长窗起点**，比短窗起点早
                                    #   (长窗 - 短窗) 秒的原曲时间 ⇒ 后面做"窗口末端
                                    #   补偿"时必须用 _w2，不能写死 FP_WIN_SEC。
                                    if fp_lock_gate(_s2, int(_v2), _tot2, int(_sec2), int(_sect2), _hr2):
                                        # ★★ 2026-10-29（真机偶发位置窜动）：
                                        #   长窗票多，但**在重复段落上更容易锁到"另一处出现"**
                                        #   —— 实测西行谣 1 次 dmax=1.65s / 位置速率 4.96 拍/秒
                                        #   （spd 被带成 1.30）。短窗那边有 `hint` 兜着，
                                        #   位置反而更可信。
                                        #   ⇒ 同一首歌 + 位置分歧时，**只借长窗的票数过门槛，
                                        #     位置和窗长都保持短窗的**；换歌 / 位置一致才整体采纳。
                                        _r_now = 1.0
                                        try:
                                            _r_now = float(getattr(self.state, "recognized_rate", 1.0) or 1.0)
                                        except Exception:                # noqa: BLE001
                                            _r_now = 1.0
                                        if not (0.80 <= _r_now <= 1.25):
                                            _r_now = 1.0
                                        _o2_norm = float(_o2) + (float(_w2) - FP_WIN_SEC) * _r_now
                                        _same_pos = (int(sid) < 0
                                                     or abs(_o2_norm - float(off)) <= 1.5)
                                        if int(_s2) == int(sid) and not _same_pos:
                                            # 只借票数：sid/off/窗长全保持短窗口径
                                            votes = int(_v2)
                                            _tot, _second, _second_tot, _hr = _tot2, _sec2, _sect2, _hr2
                                            _gate_ok = True
                                            _win_used = FP_WIN_SEC
                                            self._last_gate_win = "long_vote"
                                            self._long_hits = int(getattr(self, "_long_hits", 0)) + 1
                                        else:
                                            sid, votes, off = _s2, _v2, _o2
                                            _tot, _second, _second_tot, _hr = _tot2, _sec2, _sect2, _hr2
                                            _gate_ok = True
                                            _win_used = _w2
                                            self._last_gate_win = "long"
                                            self._long_hits = int(getattr(self, "_long_hits", 0)) + 1
                                    else:
                                        self._last_gate_win = "long_fail"
                        except Exception:                        # noqa: BLE001
                            _win_used = FP_WIN_SEC
                    self._win_used_sec = float(_win_used)
                    # ★ 把长窗重试后的最终数字回写取证行（`[R]` 的 pick/pv/ptot/p2）
                    if _win_used != FP_WIN_SEC:
                        self._gate_info = (int(sid), int(votes or 0), int(_tot),
                                           int(_second_tot), round(_hr, 2))
                    # ★ 第四条放行通道：互不重叠印证（见 LOCK_AGREE_* 常量说明）。
                    _agree_ok = False
                    if not _gate_ok and LOCK_AGREE_ENABLE:
                        # 第四条通道：**互不重叠印证**（默认关闭，见 LOCK_AGREE_ENABLE 说明）。
                        #   在环形观测队列里找一条「相距 ≥ 窗口长度、同一首、
                        #   隐含倍率落在允许区间」的旧观测 ⇒ 两次独立窗口互相印证。
                        #   ⚠ 队列里存的是"自身证据达标但未必被接受"的观测，不含已锁定结果。
                        try:
                            _agree_ok = fp_agree_ok(_now, sid, off, self._pick_obs)
                        except Exception:                    # noqa: BLE001
                            _agree_ok = False
                    # ★★ 2026-10-28：**频带救援**（见 BAND_RESCUE_* 说明）。
                    #   票数门控全灭时，用「白化频带相关 + 两次不重叠观测互证」再判一次。
                    _band_ok = False
                    if (not _gate_ok) and BAND_RESCUE_ENABLE:
                        try:
                            _band_ok = self._band_try(sig, int(sid), float(off), _now)
                        except Exception:                # noqa: BLE001
                            _band_ok = False
                    if not _gate_ok and not _agree_ok and not _band_ok:
                        self._last_gate = "none"
                        sid, votes, off = -1, 0, 0.0
                        self._cold_why = "-"     # 冷启动原因码（见 _chroma_coldstart_try）
                        self._cold_why = "-"     # 冷启动原因码（见 _chroma_coldstart_try）
                        # ★★★ 2026-10-29 冷启动 chroma：票数门槛全灭时用音级能量相关直接找人。
                        #   为什么放在**这个分支里**：只有这里能保证 `_gate_ok is False`
                        #   （前面放在 `eng.tick` 之前那种位置实测一次都没进过，日志里
                        #   `cwhy=-` 说明方法根本没被调用）。
                        #   之后 `eng.tick` 会拿它当一次普通观测 —— `ChargeBarEngine` 自带
                        #   `CONFIRM_VOTES=10` / `DELTA_ADVANCE_SEC=0.5±0.4`（偏移必须每拍
                        #   推进 0.5 秒）/ `FIRST_TRACK_HITS=2` 三条时序判据 ⇒ **白拿一套
                        #   "连续两窗都自洽"的验证**，单窗蒙对也确认不了。
                        #   ⚠ 票数是等效票（分数×40 ⇒ 16~38）：高于 CONFIRM_VOTES 能充电，
                        #     低于 STRONG_VOTES(60) 不至于"单拍算两拍"直接确认。
                        _now_c = _t.perf_counter()
                        _cd_gap = _now_c - float(getattr(self, "_cold_t", 0.0) or 0.0)
                        if CHROMA_COLD_ON and _cd_gap >= CHROMA_COLD_COOLDOWN:
                                self._cold_t = _now_c
                                _fn = getattr(self, "_chroma_coldstart_try", None)
                                if _fn is None:
                                    _cold = None
                                    self._cold_why = "no_method"
                                else:
                                    try:
                                        _cold = _fn(sig)
                                    except Exception as _e:      # noqa: BLE001
                                        _cold = None
                                        self._cold_why = "call_exc:%s:%s" % (
                                            type(_e).__name__, str(_e)[:60])
                                self._cold_score = (float(_cold[1]) if _cold else None)
                                # 已锁定（eng 已经用两窗自洽 + 偏移推进确认过曲目）时，
                                # **不要求冷启动分数过门槛**：这时只需要一个位置，而
                                # chroma 给的位置精度是 0.1 秒级。真机 0.80× 实测：
                                # gate 每窗都失败 ⇒ `sid=-1` ⇒ 位置不更新 ⇒ 码表虽然装了
                                # 却一直 `no_pos`（24/28 个采样），sub_active 掉到 0。
                                _cur_lk = -1
                                try:
                                    _cur_lk = int(getattr(eng, "current_song_id", -1))
                                except Exception:            # noqa: BLE001
                                    _cur_lk = -1
                                _cold_ok_use = (
                                    _cold is not None
                                    and float(_cold[1]) >= CHROMA_COLD_GATE
                                    and float(_cold[4]) >= CHROMA_COLD_WEAK
                                    and float(_cold[5]) >= CHROMA_COLD_LEAD)
                                _cold_fix = (_cold is not None and _cur_lk >= 0
                                             and int(_cold[0]) == _cur_lk)
                                if _cold_ok_use or _cold_fix:
                                    sid = int(_cold[0])
                                    off = float(_cold[3]) / 10.0
                                    if _cold_ok_use:
                                        votes = max(1, int(round(float(_cold[1]) * 40.0)))
                                    else:
                                        # 位置校正路径：票数只是"让下游认这一窗有证据"，
                                        # 取 1 即可（真值取证仍在 `_gate_info` 里，未被改写）
                                        votes = max(1, int(votes or 0))
                                    # ⚠ `_tot` 也要抬：后面 BPM 校验的变速档要求
                                    #   `tot >= BPM_WARP_TOT(120)`，而冷启动场景下原始
                                    #   总票常常只有 34~90（BPM 会二次否决）
                                    _tot = max(int(_tot or 0), int(votes))
                                    _win_used = FP_WIN_SEC
                                    self._win_used_sec = FP_WIN_SEC
                                    if _cold_ok_use:
                                        self._last_gate = "cold"
                                        self._cold_hits = int(getattr(self, "_cold_hits", 0)) + 1
                                        # 全新证据链：清掉旧基准让连续性检查重新起步
                                        self._off_state = None
                                    else:
                                        # 已锁定的"位置校正"：保持连续性基准，不清
                                        self._last_gate = "cold_fix"
                    elif _band_ok:
                        self._last_gate = "band2"
                    elif _agree_ok:
                        self._last_gate = "agree2"
                    else:
                        # 取证：区分"走常规门槛 / 变速兜底 / 重编码兜底"——兜底放行的日志要能回看
                        self._last_gate = (
                            "strict" if (int(votes or 0) >= LOCK_VOTES_STRICT
                                         and _tot >= LOCK_TOT_STRICT)
                            else "warp" if (_hr >= LOCK_HIT_WARP and _tot >= LOCK_TOT_WARP
                                            and int(votes or 0) >= LOCK_VOTES_WARP
                                            and _tot >= LOCK_LEAD_WARP * max(1, _second_tot))
                            else "recode"
                        )
                # ★★ 2026-10-27（C 组误识别修复）：**库内 BPM ↔ 实时 BPM 一致性校验**。
                #   实机验收：B 站 200BPM Frenchcore 误锁到 142BPM 的「西行谣」（差 41%），
                #   而真正的变速样本只有 ±3/5/8% ⇒ 一条 15% 容差即可干净分开。
                #   放在 `fp_lock_gate` **之后**：先让票数/证据门槛决定"认不认"，再用 BPM 否决
                #   "认错曲"，两条判据互不干扰（`gate=bpm` 单独可回看）。
                #   ⚠ 倍频（0.5×/2×）放行：BPM 检测出半速/双速是常态，不能因此杀正确匹配。
                _lbpm = 0.0        # ★ sid<0 时也要有定义（下面「显示用库内 BPM」要用）
                _rbpm = 0.0
                _bflag = None      # ★ 同上：`_bflag` 是 BPM 校验的放行档（见 bpm_gate_ok）
                _sid_conf = -1     # ★ 2026-11-02：校验**前**的 sid（被否决后会清零，这里留底）
                if sid >= 0:
                    _sid_conf = int(sid)
                    _cur_sid = -1
                    try:
                        _cur_sid = int(getattr(eng, "current_song_id", -1))
                    except Exception:                # noqa: BLE001
                        _cur_sid = -1
                    _lbpm, _rbpm, _brel = 0.0, 0.0, None
                    _bprov = getattr(self, "_song_bpm_provider", None)
                    if _bprov is not None:
                        try:
                            _lbpm = float(_bprov(int(sid)) or 0.0)
                        except Exception:                    # noqa: BLE001
                            _lbpm = 0.0
                    try:
                        _rbpm = float(getattr(self.state, "bpm_display", 0.0) or 0.0)
                    except Exception:                        # noqa: BLE001
                        _rbpm = 0.0
                    # ★ 2026-10-27：先记住 fp_lock_gate 给出的结论（strict/warp/recode），
                    #   BPM 校验**通过**时要恢复它 —— 否则 `_last_gate="bpm"` 会从上一帧残留下来，
                    #   日志里出现 `brel=0.083 gate=bpm` 这种自相矛盾的组合（0.083 本该通过）。
                    _gate_before = getattr(self, "_last_gate", "-")
                    self._bpm_ok_flag = None
                    # ★ 2026-10-29：把本窗「命中率 + 总票」一起交给校验，供变速档判证据
                    _bpm_ok, _brel, _bflag = bpm_gate_ok(
                        _lbpm, _rbpm,
                        hit_rate=float(getattr(fp_db, "last_hit_rate", 0.0) or 0.0),
                        tot=int(_tot or 0))
                    if _bflag is None:
                        # 任一为 0（库里没 BPM / 实时还没测出来）⇒ 不校验，别误杀开局
                        self._bpm_gate = (round(_lbpm, 1), round(_rbpm, 1), None)
                        self._bpm_veto_streak = 0       # 这一窗没有否决，别让旧 streak 累积
                    else:
                        self._bpm_gate = (round(_lbpm, 1), round(_rbpm, 1),
                                          round(_brel, 3) if _brel is not None else None)
                        self._bpm_ok_flag = 1 if _bpm_ok else 0
                        if _bpm_ok:
                            self._bpm_veto_streak = 0
                        if not _bpm_ok:
                            # ★★★ 2026-10-29：**否决之前先让 chroma 复核**。
                            #   实测「System Overloaded」：指纹命中率 96%、pick 正确，
                            #   但实时 BPM 检测锁到 219.5（实际 175，差 5:4 的节奏层级），
                            #   于是每窗 bpmok=0，放了一分钟一次都没认出来。
                            #   chroma 与节拍无关，正是这种场景的仲裁者；成本 5~6 毫秒。
                            _cx2 = None
                            # ⚠ 只在**指纹本身很硬**时才允许推翻：命中率 ≥0.90 且总票
                            #   ≥120。否则等于让一次低票误匹配借 chroma 上位
                            #   （2026-10-29 三首回归里的 dmax 10.68 就是 `votes=37`
                            #    的低票位置，虽然它来自指纹本身，但不该给同类开口子）。
                            _hit_now = float(getattr(fp_db, "last_hit_rate", 0.0) or 0.0)
                            _strong = (_hit_now >= CHROMA_BPMVETO_HIT
                                       and int(_tot or 0) >= CHROMA_BPMVETO_TOT)
                            try:
                                if (CHROMA_BPMVETO_ON and _strong
                                        and int(getattr(self, "_bpm_veto_streak", 0) or 0) >= 2):
                                    # 只有连续第 3 窗（马上要丢歌）才花 100~400ms 做 chroma 复核；
                                    # 单窗 BPM 抖动直接 hold，不跑重活。
                                    _cx2 = self._chroma_try(
                                        sig, int(sid), float(off) * 10.0,
                                        float(getattr(self.state, "recognized_rate",
                                                      1.0) or 1.0),
                                        rate_span=0.50)
                            except Exception:                # noqa: BLE001
                                _cx2 = None
                            self._cx2 = _cx2
                            if (_cx2 is not None
                                    and float(_cx2["score"]) >= CHROMA_BPMVETO_SCORE
                                    and float(_cx2["weak"]) >= CHROMA_BPMVETO_WEAK
                                    and float(_cx2["lead"]) >= CHROMA_BPMVETO_LEAD):
                                # 位置改用 chroma 给的（它比指纹的整帧偏移细）
                                off = float(_cx2["target"]) / 10.0
                                self._last_gate = "bpm+cx"
                                # 2 = 否决被推翻；`!= 0` 的判据都按"通过"处理
                                #    （界面上的"库内 BPM"照常发布）
                                self._bpm_ok_flag = 2
                                self._bpm_veto_streak = 0
                            else:
                                # ★★★ 2026-11-12（用户：码表内容来回跳 / 偏差明显）：
                                #   **已经锁定的曲目，BPM 不符不再丢歌**。
                                #   实时 BPM 在 breakdown/大混响/半速段落会跳（现场实测
                                #   142 的歌被估成 198.5/150.5），BPM 否决把 sid 清零后
                                #   码表掉锁 → 下一窗重新认歌/重锚 ⇒ 画面跳。
                                #   指纹已经锁定且位置连续时，BPM 只用于“初认护栏/显示”，
                                #   不参与“已锁定歌还要不要继续跟”的判定。
                                _is_cur = (_cur_sid >= 0 and int(_cur_sid) == int(sid))
                                if _is_cur:
                                    self._bpm_veto_streak = 0
                                    self._last_gate = "bpm_hold"
                                    self._bpm_ok_flag = 2
                                else:
                                    # 新歌/未锁定：保留原来的 3 窗否决护栏
                                    self._bpm_veto_streak = int(
                                        getattr(self, "_bpm_veto_streak", 0) or 0) + 1
                                    if self._bpm_veto_streak < 3:
                                        self._last_gate = "bpm_hold"
                                        self._bpm_ok_flag = 2
                                    else:
                                        self._last_gate = "bpm"
                                        sid, votes, off = -1, 0, 0.0
                                # ★★★ 2026-11-02：**否决识别的同一时刻也要怀疑实时 BPM**，
                                #   否则「BPM 错 → 校验否决 → 认不到歌 → 不解锁 BPM」自锁。
                                #   只在指纹确实认得这首歌时做（低票误匹配没有重估的意义）。
                                try:
                                    if (_hit_now >= 0.50 and int(_tot or 0) >= 30
                                            and _fp_lead(fp_db) >= LIB_BPM_LEAD_MIN):
                                        self._bpm_relock_after_veto(
                                            _lbpm, _rbpm, _brel, _hit_now, _tot)
                                except Exception:            # noqa: BLE001
                                    pass
                        else:
                            self._last_gate = _gate_before     # 通过：恢复 strict/warp/recode

                # ★ 2026-10-28：界面显示用的库内 BPM（离线整曲细扫，比实时自相关准一个量级）。
                #   只有「认到歌 + BPM 校验通过 + 库里有值」才发布；其余一律清零，
                #   免得没认到歌时还挂着上一首的 BPM。
                try:
                    # ★★★ 2026-11-02：**BPM 校验否决 ≠ 没认出这首歌**。
                    #   校验否决的是「曲目 + 位置」这一对；而 `hit=230/248`（92.7%）这种
                    #   命中率已经足够说明"就是这首歌"。原来连库内 BPM 一起丢掉，
                    #   于是**素材倍速**（`engine` 里 `bpm/128`）只剩卡死的实时值可用
                    #   ⇒ 用户看到的正是「识别到歌了，但素材速度没跟上」。
                    #   ⇒ 指纹确认了 sid（哪怕随后被校验清零）时，库内 BPM 照常发布。
                    #   ⚠ 只放宽"发布库内 BPM"，**不**放宽识别/位置（护栏原样保留）。
                    _ok_pub = lib_bpm_publish_ok(
                        sid, _sid_conf, self._bpm_ok_flag,
                        getattr(fp_db, "last_hit_rate", 0.0), _tot,
                        lead=_fp_lead(fp_db))
                    if _ok_pub and _lbpm > 0:
                        self.state.lib_bpm = float(_lbpm)
                    else:
                        self.state.lib_bpm = 0.0
                except Exception:                            # noqa: BLE001
                    pass

                # ★★ 2026-10-29 变速对齐精修（**只对「已经锁定的那一首」做**）
                #   票沿 `d = rate·q + delta` 摊平时，单点票选出的 delta 会系统性偏：
                #   倍率越远离 1、票散得越开，选中的桶越可能不是真位置。
                #   拟合出整条直线后，"位置"与"倍率"同时变准。离线实测（atempo 保音高，
                #   4 首 × 2 位置，`tools/_test_warp.py`）：
                #     真倍率 1.08 → 拟合 1.082、位置误差 **0.02s**（单点 0.19s）
                #     真倍率 1.20 → 拟合 1.196、位置误差 **0.02s**（单点 0.61s）
                #   ⇒ 已锁定时用拟合的 off/rate 替代单点值；票数取两者较大（身份不变）。
                #   ⚠ 只在「票数不算高」（<2×strict）时做，票很足的窗没必要花这 20ms；
                #     拟合不可信（倍率越界 / 票明显更低）时**原样不动**，绝不用坏数据。
                self._fit_rate = None
                if (int(sid) >= 0 and int(_locked) == int(sid)
                        and int(votes or 0) < LOCK_VOTES_STRICT * 2
                        and _now - float(getattr(self, "_last_fit_t", 0.0) or 0.0) >= 3.0):
                    # ★ 2026-11-11：低票锁定时每窗都跑 `fit_rates`（又一次全 hash 查询 +
                    #   直线拟合），最多 3 秒一次；它只是精修，不需要每个窗都做。
                    self._last_fit_t = _now
                    try:
                        _fr = fp_db.fit_rates(getattr(fp_db, "last_hs", None), [int(sid)])
                        _one = (_fr or {}).get(int(sid))
                        if _one is not None:
                            _frr, _frv, _frd = float(_one[0]), int(_one[1]), float(_one[2])
                            # ⚠ 必须**真的多聚到票**才用：拟合的意义是把摊平的票收回来，
                            #   如果收回来还没单点票多，说明这条线没意义，宁可用单点结果。
                            if (0.86 <= _frr <= 1.16
                                    and _frv >= max(int(votes or 0) + 3,
                                                    int(int(votes or 0) * 1.2))):
                                self._fit_rate = _frr
                                off = _frd * float(_FP_HOP) / float(_FP_SR)
                                if _frv > int(votes or 0):
                                    votes = _frv
                    except Exception:                        # noqa: BLE001
                        self._fit_rate = None

                # ★★ 2026-10-29 口径统一（长窗引入的坑）：`off` 是"**本次这个窗口**的起点"，
                #   窗长一变（4.5 ⇄ 9.0）起点就差 4.5×rate 秒。下游三处都当它是固定口径用：
                #     ① `_rate_est`（Δoff/Δt 估速率）
                #     ② `_off_state`（连续性检查，容差 2 秒）
                #     ③ `_prev_off` → 下一窗的 hint（窗口半宽才 2.5 拍 ≈ 1 秒）
                #   混用会让状态机每换一次窗长就判一次"跳变"并连丢 6 次
                #   ⇒ 实机表现「位置持续偏 1~4 秒」（2026-10-29 三首回归：开长窗时
                #   西行谣 dmax 4.09s，关掉立刻回到 0.37s）。
                #   ⇒ 一律先换算到「FP_WIN_SEC 窗起点」这一种口径再往下传。
                _win_now = float(getattr(self, "_win_used_sec", FP_WIN_SEC) or FP_WIN_SEC)
                _r_prev = 1.0
                try:
                    _r_prev = float(getattr(self.state, "recognized_rate", 1.0) or 1.0)
                except Exception:                            # noqa: BLE001
                    _r_prev = 1.0
                if not (0.80 <= _r_prev <= 1.25):
                    _r_prev = 1.0
                _off_norm = float(off or 0.0) + (_win_now - FP_WIN_SEC) * _r_prev

                # 原始匹配票数（未过确认门槛）—— 取证用：区分"匹配不到"与"没确认"
                _lm0 = getattr(self, "_last_match", None)
                _prev_off = float(_lm0[2]) if _lm0 else None
                self._last_match = (sid, int(votes or 0), float(_off_norm),
                                    1 if _hint is not None else 0)
                self._last_match_t = _now
                # 电荷条内部状态取证：adv = 本次 offset 是否比上次推进 0.5s±0.4（充电判据）
                self._last_adv = (1 if (_prev_off is not None and
                                        abs((float(off) - _prev_off) - 0.5) <= 0.4) else 0)
                eng.tick(FpResult(matched=(votes > 0), song_id=sid,
                                  aligned_votes=votes, offset_sec=off), _t.time())
                cur = eng.current_song_id
                # ★★ 2026-10-28（切歌变慢）：锁一变就清空频带观测队列。
                #   否则失锁那一瞬间，通道会拿**上一首**的旧观测（相隔 >4.5s、位置外推也吻合）
                #   立刻把它重新锁回来，把新歌的证据挡在门外 —— 实测 B 组 6/9 掉到 3/9。
                if int(cur) != int(getattr(self, "_band_sid", -2)):
                    self._band_sid = int(cur)
                    try:
                        self._band_obs.clear()
                    except Exception:                            # noqa: BLE001
                        pass
                # ★ 2026-10-19：**锁定事件**精确时间戳（毫秒），供真机测速脚本读。
                #   `[R]` 是 2s（或按环境变量）节流，测速误差太大；这一行只在"锁定歌变了"
                #   时写一次，开销可忽略，却能精确到 10ms。
                try:
                    _prev_cur = int(getattr(self, "_lock_evt_sid", -2))
                    if int(cur) != _prev_cur and os.environ.get('AUTOVJ_REC_PROBE'):
                        self._lock_evt_sid = int(cur)
                        _lp = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "lock_events.log")
                        # ★ 2026-10-27：把「放行判据」一起记下来。C 组误锁排查时
                        #   `[R]` 节流抓不到"刚好通过的那一窗"，事后只能靠猜是 strict/warp/
                        #   recode/streak 哪一条放的行；这里一次性落盘，判据 + 证据数字都在。
                        with io.open(_lp, "a", encoding="utf-8") as _lf:
                            _lf.write("%.3f\t%d\t%d\t%.2f\t%s\t%s\t%s\n"
                                      % (time.time(), int(cur), int(votes or 0), float(off or 0.0),
                                         getattr(self, "_last_gate", "-"),
                                         getattr(self, "_gate_info", None),
                                         getattr(self, "_bpm_gate", None)))
                except Exception:                                # noqa: BLE001
                    pass
                self._eng_state = (int(cur), int(getattr(eng, "tentative_song_id", -1)),
                                   int(getattr(eng, "cand_bar", 0)),
                                   int(getattr(eng, "cand_hits", 0)))
                with self.state.lock:
                    self.state.recognized_song_id = cur
                    if cur >= 0 and sid == cur and votes > 0:
                        # 估计输入播放速率 r（同一首歌连续识别时累计；Δoff/Δt 即 r）
                        # ★ 2026-11-11：只有「匹配曲目 == 已确认曲目」且票数够时才喂倍率估计。
                        #   未锁定时不估；匹配到别的歌（重复段落/误匹配）时也不污染当前倍率。
                        if int(votes or 0) >= 8:
                            r = self._rate_est.update(int(cur), _off_norm, t_read)
                        else:
                            try:
                                r = float(getattr(self.state, "recognized_rate", 1.0) or 1.0)
                            except (TypeError, ValueError):
                                r = 1.0
                        # ★★ 2026-10-28：频带校验倍率**只作为取证**，不再直接覆盖 r。
                        #   ★ 2026-11-11（用户：原速歌被识别成 1.05）：频带/拟合都与
                        #   指纹 offset 同源，重复段落里会拟合出 1.05/1.20 的假斜率；
                        #   它们不能单独决定播放速度。r 的基础只保留 `_rate_est`
                        #   —— 它带 3 秒位置连续性闸门，野跳样本不会污染。
                        _bl = getattr(self, "_band_last", None)
                        self._band_rate = (float(_bl[1]) if
                                           (_bl and int(_bl[2]) == int(cur)
                                            and 0.80 <= float(_bl[1]) <= 1.25) else None)
                        _fr2 = getattr(self, "_fit_rate", None)
                        self._fit_rate_pub = (float(_fr2) if
                                              (_fr2 is not None
                                               and 0.86 <= float(_fr2) <= 1.16) else None)
                        # ★★★ 2026-10-29：**chroma 复核 —— 倍率的最高优先级来源**。
                        #   实测 `_rate_est` 在"票少 ⇒ 频繁掉锁"的变速工况下常年停在
                        #   1.000（它靠 Δoff/Δt，掉锁就攒不够基线）；而 chroma 的 slope
                        #   在 1.08× / 1.20× 时给出 1.080 / 1.200，几乎精确。
                        #   分数门槛 0.55（同曲实测 0.60~0.98、异曲 0.14~0.17），
                        #   只在「已锁定的这一首」上做（见 _chroma_try 的范围说明）。
                        self._chroma_score = None
                        self._chroma_weak = None
                        self._chroma_lead = None
                        self._chroma_rate = None
                        _now_c = _t.perf_counter()
                        if (_now_c - float(getattr(self, "_chroma_t", 0.0) or 0.0)
                                >= CHROMA_COOLDOWN):
                            # ★★ 2026-10-29 真机 0.80×：chroma 的搜索范围原来跟着**上一帧 r**
                            #   走，而变速工况下 r 常年停在 1.0 ⇒ 范围只有 0.88~1.12，
                            #   **0.80 永远扫不到**，于是它老老实实报 1.000。
                            #   ⇒ 改用 BPM 比值当提示（112.5/140=0.804，误差 0.5%），
                            #     它不受票数影响、也不受"上一帧错值"传染。
                            # ★★ 2026-10-29 真机：原本把 BPM 比值当搜索提示，但**BPM 检测
                            #   本身在抖动**（同一段 0.80× 音频实测在 111.5 / 219.5 / 140
                            #   之间跳），提示错一次就整窗搜错范围（0.80 被搜成 1.0）。
                            #   ⇒ 改成**全量程搜**（0.80~1.25），防呆交给里层的边际要求
                            #     `CHROMA_SLOPE_MARGIN_FULL`：变速必须比原速明显更好才认。
                            try:
                                _cx = self._chroma_try(sig, int(sid),
                                                       float(_off_norm) * 10.0,
                                                       float(r),
                                                       rate_span=0.50)
                            except Exception:                # noqa: BLE001
                                _cx = None
                            # ★★★ 2026-10-29 照 KDJ 作者公布的判据：**三条一起看** ——
                            #   得分（平均）+ 最弱窗（三段里最低的一段）+ 领先（减次选）。
                            #   只看得分时，重复段落的「另一处」也能拿到差不多的分
                            #   （实测库外曲 0.38 vs 真曲 0.45，只差 17%，分不开）；
                            #   而两者的**领先**差 4 倍以上（真曲 +0.32~+0.82 / 干扰 +0.04~+0.08）。
                            #   最弱窗则挡住「两段对、一段错」的偶然对齐。
                            if _cx is not None:
                                self._chroma_t = _now_c
                                self._chroma_score = float(_cx["score"])
                                self._chroma_weak = float(_cx["weak"])
                                self._chroma_lead = float(_cx["lead"])
                                if (self._chroma_score >= CHROMA_GATE
                                        and self._chroma_weak >= CHROMA_WEAK_MIN
                                        and self._chroma_lead >= CHROMA_LEAD_MIN):
                                    _ns = float(_cx["slope"])
                                    # ★★★ 2026-10-29 真机回归抓到的：**单窗判据挡不住
                                    #   「换个倍率 + 换个位置凑分」**。Nimbus 原速播放时
                                    #   chroma 报 cr=1.174，而且三条门槛全过
                                    #   （cx=0.67 cw=0.60 cl=+0.52）—— 因为在给定的
                                    #   `target_hint` 附近，1.174 那档确实拟合得更好。
                                    #   前馈被它带跑 ⇒ 位置一路超前 ⇒ dmax 1.22、
                                    #   4 个点超 0.6（三首回归里 Nimbus 就这么挂的）。
                                    #   ⇒ 判据必须加**时间维度**：真变速会连续两三窗
                                    #     都一样，凑出来的只会出现一次。要求最近 8 秒内
                                    #     至少两窗给出互相 ≤0.03 的斜率才发布。
                                    _hist = [x for x in
                                             (getattr(self, "_chroma_rhist", None) or [])
                                             if int(x[0]) == int(sid)
                                             and _now_c - float(x[2]) <= 8.0]
                                    _hist.append((int(sid), _ns, _now_c))
                                    self._chroma_rhist = _hist[-4:]
                                    _vs = [x[1] for x in self._chroma_rhist]
                                    if len(_vs) >= 2 and (max(_vs) - min(_vs)) <= 0.03:
                                        _vs.sort()
                                        r = float(_vs[len(_vs) // 2])   # 中位数
                                        self._chroma_rate = r
                        # ★★ 2026-10-29：**BPM 比值兜底** —— 这是与指纹完全独立的证据。
                        #   真机 1.20× 实测：指纹票被摊到 60 个位置（pv 才 7~17），
                        #   `_rate_est` 又常攒不够基线 ⇒ 前三种来源都停在 1.0，
                        #   而实时 BPM 估到 168（= 140×1.2）却一直没被用上。
                        #   BPM 检测走自相关，**不受指纹票数影响**，在变速时反而更稳。
                        #   ⚠ 只在 `r` 仍是 1.0（前三条都没结论）时才用，不覆盖更可靠的来源；
                        #     半速/双速由 0.78~1.25 的区间挡掉（0.5×/2× 不在区间内）。
                        #   ★★ 2026-10-29 真机 0.80× 修正：原来只在「r 仍是 1.0」时才用，
                        #     而 `_rate_est` 给了 **0.938** 就把这条挡死了 —— 可 BPM 比值
                        #     112.5/140 = **0.804** 才是真值。`_rate_est` 靠 Δoff/Δt，
                        #     在票被摊平的变速工况下只是个折中值，本来就该让位给自相关。
                        #   ⇒ 改为：**chroma 没给出非 1.0 的斜率时，就用 BPM 比值**。
                        #     它比 `_rate_est` / 拟合 / 频带都准，只排在 chroma 之后
                        #     （chroma 是整段音频对齐，实测 1.08→1.080、1.20→1.200）。
                        #   ⇒ 排序：**BPM 比值优先**，chroma 高置信时纠正它。
                        #     理由：BPM 是自相关直接量的（112.5 vs 140 = 0.804，误差 0.5%），
                        #     而 chroma 的斜率受搜索范围约束、`_rate_est` 受票数摊平影响。
                        #     chroma 只有在「分数 ≥0.70 且与 BPM 差 >0.10」时才推翻它 ——
                        #     那说明 BPM 检测本身出错了（同曲实测 0.81~0.97、异曲 0.14~0.30）。
                        # ★★★ 2026-11-11（用户：原速歌被识别成 1.20 / 1.05，码表跳）：
                        #   **不再把「实时 BPM / 库内 BPM」当成独立倍率来源**。
                        #   现场实时 BPM 会跳到 210.5（正确歌 185），比值 1.138 就被
                        #   写进 `recognized_rate` ⇒ 素材/码表整体加速、位置超前再回跳。
                        #   倍率只认指纹 offset 斜率（`_rate_est`）或 chroma 对齐；
                        #   BPM 只在身份校验里用，不参与速度。
                        _cxr_now = self._chroma_rate
                        if _cxr_now is not None and abs(float(_cxr_now) - 1.0) > 0.01:
                            # ★★ 2026-11-11（用户：原速歌被识别成 1.20 / 1.05）：
                            #   chroma **不能单独**把 1.0 改成别的值——重复段落的
                            #   另一处也能让 chroma 拟合出 1.2。只在它支持指纹斜率
                            #   （误差 ≤4%）时采纳；否则保持 `_rate_est` 的基础值。
                            if abs(float(_cxr_now) - float(r)) <= 0.04:
                                r = float(_cxr_now)
                        # ★★ 2026-10-29（真机抓到 r=1.300）：发布前**一律钳到 DJ 合理区间**。
                        #   根因是 `_rate_est` 在"刚锁定、历史点还只有 2~3 个"时，单点
                        #   0.15 秒的识别误差会被放大成 30% 的速率误差（dt=0.5s 时
                        #   Δoff=0.65 ⇒ r=1.30），随后被前馈直接下发给播放器（实测
                        #   `spd=1.30`），位置立刻被带跑。±18% 之外的值一律当噪声。
                        #   ⚠ 上界必须 ≥1.22：真机 1.20× 相位的真值就是 1.20，写到 1.18
                        #     会把它当成噪声钳成 1.0（实测就是这么"前馈没跟上"的）。
                        if not (0.78 <= float(r) <= 1.25):
                            r = 1.0
                        self.state.recognized_rate = r
                        # off 是识别窗口起点在**原曲**时间轴上的秒数，窗口末端≈当前播放秒。
                        # ⚠ 变速倍率 r≠1 时，窗口秒数对应 FP_WIN_SEC*r 秒原曲
                        #   —— 旧写法固定 +4.5（且与切窗各写一份）是错的。
                        # ⚠⚠ 2026-10-22 回退：这里曾默认 +1.5s「采集延迟补偿」，
                        #   但**码表层本身已有 CUE_LEAD_SEC=0.5 的提前量**
                        #   ⇒ 两者叠加 ≈2s，现场表现为「画面比音乐快很多」（用户实测）。
                        #   现在默认 **0.0**（恢复原行为）；确需补偿时用环境变量微调：
                        #     set AUTOVJ_REC_LATENCY=0.3   （单位秒，正数=画面更早，负数=更晚）
                        # ★★ 2026-10-27（A 组 off 跳变）：**off 状态机**。
                        #   只在「已锁定 且 sid 与上一窗相同」时生效：新 off 必须落在
                        #   「上一**已接受**值 + 墙钟推进」的 ±OFF_STATE_TOL_SEC 内；
                        #   超出就先丢弃（保持期望值）并累计异常，连续异常超
                        #   OFF_STATE_ABNORMAL_MAX 才认定为真 seek（换歌/首次锁定直接放行）。
                        #   ⚠ 基准用的是**已接受**的值，不是原始匹配值 —— 否则一次跳变会污染基准。
                        # ⚠ 状态机只吃 `_off_norm`（统一口径），绝不用原始 `off`
                        _off_pub = _off_norm
                        try:
                            _sv = getattr(self, "_off_state", None)
                            if _sv is not None and int(_sv.get("sid", -2)) == int(cur) and cur >= 0:
                                # ★★ 2026-10-28（变速锁不住）：期望推进必须乘**播放倍率**。
                                #   DJ 机台/我的变速样本是「1.06 倍速播放」⇒ 原曲位置每秒前进
                                #   1.06s，而不是 1.0s。旧写法按 dt 推进 ⇒ 每 30 秒累积 ~1.8s
                                #   偏差 ⇒ 撞到 OFF_STATE_TOL_SEC=2.0 就判"跳变"并把 off 拉回，
                                #   实机表现就是「锁上了、几十秒后又掉」。
                                _rt = float(getattr(self.state, "recognized_rate", 1.0) or 1.0)
                                if not (0.80 <= _rt <= 1.25):
                                    _rt = 1.0
                                _expect = float(_sv["off"]) + max(0.0, t_read - float(_sv["t"])) * _rt
                                if abs(float(_off_norm) - _expect) <= OFF_STATE_TOL_SEC:
                                    _sv["abn"] = 0
                                    _sv["off"] = float(_off_norm)
                                else:
                                    _sv["abn"] = int(_sv.get("abn", 0)) + 1
                                    if _sv["abn"] <= OFF_STATE_ABNORMAL_MAX:
                                        _off_pub = _expect          # 丢弃这次跳变
                                        _sv["off"] = _expect
                                    else:
                                        _sv["abn"] = 0              # 真 seek：接受并重新起步
                                        _sv["off"] = float(_off_norm)
                                _sv["t"] = t_read
                            else:
                                self._off_state = {"sid": int(cur), "off": float(_off_norm),
                                                   "t": t_read, "abn": 0}
                        except Exception:                            # noqa: BLE001
                            _off_pub = _off_norm
                        # `_off_pub` 已是「FP_WIN_SEC 窗起点」口径 ⇒ 这里固定加
                        # FP_WIN_SEC·r 得窗口末端（不能再乘本次实际窗长，否则会多算
                        # (长窗-短窗)·r 秒 —— 2026-10-29 实测就是这么偏出 4.5 秒的）。
                        self.state.recognized_offset = (
                            _off_pub + FP_WIN_SEC * r
                            + float(os.environ.get('AUTOVJ_REC_LATENCY', '0.0') or 0.0))
                        self.state.recognized_t = t_read
                        # 对齐票数交给引擎：低于门槛时 offset 不可信，网格时钟不做任何校正
                        self.state.recognized_votes = int(votes)
                    else:
                        # 本窗票数不可用（换曲/歧义/无票）：票数清零，引擎据此停用本次校正
                        self.state.recognized_votes = 0
                        if cur < 0:
                            self.state.recognized_offset = -1.0
                            # ★★★ 2026-10-29 真机（1.20×/0.80× 的 r 全程 1.000）：
                            #   **掉锁 ≠ 音乐变回原速**。这里原来既清 rate 又 reset 估计器，
                            #   而变速工况下"票少 ⇒ 频繁掉锁"是常态 ⇒ 每 2~3 秒就被清一次
                            #   ⇒ `RateEstimator` 永远攒不够 `MIN_SPAN=3s` 的基线 ⇒ r 恒为 1.0
                            #   ⇒ 前馈按原速走 ⇒ 位置每秒落后 (rate-1) 秒 ⇒ 更容易掉锁，
                            #   形成**恶性循环**。
                            #   `RateEstimator` 自己带断档保护（`MAX_DT`：dt 超 2 秒就重新起锚），
                            #   外部再 reset 只会把它砸死。⇒ 掉锁时**保持**倍率，
                            #   只在「静音超时回待机」和「换歌」两处清（那两处有独立入口）。
                            # self.state.recognized_rate = 1.0
                            # self._rate_est.reset()
                            pass
            except Exception:                                    # noqa: BLE001
                # ⚠⚠ 原来是 `pass`：认不出歌时任何异常都被吞掉，日志只有 raw=0，
                #   分不清"没匹配到"还是"炸了"。现在写 traceback（10s 限流）。
                try:
                    import traceback as _tb
                    t2 = time.perf_counter()
                    if t2 - float(getattr(self, "_rec_err_t", 0.0) or 0.0) >= 10.0:
                        self._rec_err_t = t2
                        _p = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                          "AutoVJ", "cue_health.log")
                        _d = os.path.dirname(_p)
                        if _d and not os.path.isdir(_d):
                            os.makedirs(_d, exist_ok=True)
                        with open(_p, "a", encoding="utf-8") as _f:
                            _f.write("[%s] [E] 认歌循环异常：\n%s\n"
                                     % (time.strftime("%H:%M:%S"), _tb.format_exc()))
                except Exception:                                # noqa: BLE001
                    pass

    # ---------------- 采集（运行在音频线程） ----------------
    def _run_asio(self, st, device_name, mono, sr, gen, ch0=0, ch1=1):
        """ASIO 采集。**跑完返回 None**；打不开则返回错误原因（由 `_run` 负责回退）。

        ★ 复用 `_capture_loop`：ASIO 侧只提供一个「soundcard recorder 形状」的适配器
          （`asio_engine.AsioDevice`），下游的能量 / 拍钟 / 指纹 / 曲风 / NDI 转发
          **一行都不用改**——那是现场验证过的代码，不该为了换个音源再抄一份。
        """
        try:
            import asio_engine
        except Exception as e:                                 # noqa: BLE001
            return "ASIO 模块导入失败：%s" % e
        ok, why = asio_engine.available()
        if not ok:
            return why
        # 采样率交给驱动当前值（sr=0）：ASIO 的采样率是**驱动全局**的，
        # 我们改了会影响同一驱动的其它客户端（比如 VirtualDJ 那边）。
        # ⚠ 设备下拉里存的是 "ASIO: <驱动名>"（为了和人名区分），这里要把前缀剥掉。
        drv = (device_name or "")
        if drv.startswith("ASIO: "):
            drv = drv[6:].strip()
        # 用哪两路输入（用户可在「音源 → 输入通道」里改；默认前两路）
        try:
            ch0, ch1 = int(ch0), int(ch1)
        except Exception:                                          # noqa: BLE001
            ch0, ch1 = 0, 1
        cap = asio_engine.AsioCapture(drv or None, sr=0.0, ch0=ch0, ch1=ch1)
        ok, err = cap.open()
        if not ok:
            return err
        self._asio_cap = cap
        sr_use = cap.sample_rate or 48000
        info = "%s ｜ 通道 %d/%d ｜ %d Hz ｜ 缓冲 %d 帧(%.1f ms) ｜ 输入延迟 %.1f ms" % (
            cap.driver, ch0 + 1, ch1 + 1, sr_use, cap.buffer_frames,
            cap.buffer_frames * 1000.0 / sr_use, cap.latency_ms())
        with st.lock:
            st.asio_active = True
            st.asio_failed = ""
            st.asio_info = info
            st.device_desc = "ASIO: " + cap.driver
            st.error = ""
            st.running = True
        print("[ASIO] 已开始采集：%s" % info)
        try:
            # ★★ 2026-10-10 修（ASIO 输入 100% 失效）：ASIO 必须走「不包装」路径
            #   （wrap_native=False）。`AsioDevice` 本身就是「原生包长级别、不攒块」的
            #   来源，形状已兼容 `_capture_loop` 的下游；若被 `wasapi_capture.wrap()`
            #   再包一层 `TimedDevice`，其 `recorder()` 会按 soundcard 约定访问
            #   `self._mic._audio_client()` —— 而 `AsioDevice` 没有该属性
            #   ⇒ 每块抛 AttributeError ⇒ 被 `_run` 当成「ASIO 不可用」恒回退系统声音。
            #   包长用驱动上报的 `buffer_frames`（实测 Realtek 528@48k ≈11ms）。
            self._capture_loop(asio_engine.AsioDevice(cap), mono, sr_use, gen,
                               wrap_native=False, native_pkt=cap.buffer_frames)
            ov = cap.overflows()
            if ov > 0:
                # 环形缓冲被读得太慢（通常意味着主线程/分析线程被别的东西拖住）
                print("[ASIO] 警告：环形缓冲溢出 %d 次（读线程跟不上）" % ov)
        finally:
            try:
                cap.close()
            except Exception:                                  # noqa: BLE001
                pass
            self._asio_cap = None
            with st.lock:
                st.asio_active = False
        return None

    def _run(self, sc, source_type, device_name, mono, sr, gen, ch0=0, ch1=1):
        st = self.state
        # ---- 局域网音频（自研轻量协议）：**独立分支**，完全不碰 soundcard ----
        # ★ 2026-10-08 新增：A 机跑独立采集发送小 exe，B 机在这里收。
        #   `device_name` 在这一路不是"设备名"而是**端口号字符串**（用户可改，
        #   默认 47811）—— 语音上还是叫"设备"，界面不用为它单开一个输入框。
        if source_type == "lan":
            err = None
            try:
                err = self._run_lan(st, device_name, mono, sr, gen)
            except Exception as e:                             # noqa: BLE001
                err = "%s: %s" % (type(e).__name__, e)
            if err is None:
                return                      # 正常跑完了（切设备/停止）
            with st.lock:
                st.lan_failed = err
                st.error = "局域网音频不可用：%s" % err
            print("[局域网音频] %s" % err)
            return                          # ⚠ 不自动回退系统声音：用户明确要的是对端声音，
                                            #   悄悄换成本机系统声音会让他以为"连上了"（更糟）
        # ---- ASIO 低延迟输入：**独立分支**，不碰 soundcard ----
        # ⚠ 失败必须优雅回退：ASIO 是**单客户端独占**的，VirtualDJ / DAW 占着就一定开不了，
        #   这时候绝不能"报个错就完了"——演出不能停，自动退回系统声音（WASAPI）。
        if source_type == "asio":
            err = None
            try:
                err = self._run_asio(st, device_name, mono, sr, gen, ch0, ch1)
            except Exception as e:                             # noqa: BLE001
                err = "%s: %s" % (type(e).__name__, e)
            if err is None:
                return                      # 正常跑完了（切设备/停止）
            with st.lock:
                st.asio_active = False
                st.asio_failed = err
                st.error = "ASIO 不可用：%s —— 已自动回退到系统声音" % err
            print("[ASIO] 打开失败，回退系统声音：%s" % err)
            source_type, device_name = "system", ""
            if self._gen != gen:
                return
        else:
            with st.lock:
                st.asio_active = False
                st.asio_failed = ""
        if sc is None:
            with st.lock:
                st.error = "soundcard 不可用，无法采集音频"
            return
        if self._gen != gen:
            return   # 过期任务（用户又切换了设备），直接跳过
        try:
            mics = sc.all_microphones(include_loopback=True)
            mic = None
            dev_warn = ""
            if source_type == "system":
                loop = [m for m in mics if m.isloopback]
                if not loop:
                    raise RuntimeError("找不到可采集的系统声音（Loopback）设备")
                mic, dev_warn = _match_device(loop, device_name)
            else:
                cands = [m for m in mics if not m.isloopback]
                if not cands:
                    raise RuntimeError("找不到麦克风/线路输入设备")
                mic, dev_warn = _match_device(cands, device_name)
            if self._gen != gen:
                return   # 打开设备期间又被切换
            if dev_warn:
                # ★ 设备名匹配不到：显式告警（旧实现静默回退 cands[0]，用户完全看不见）
                print("[音频] %s" % dev_warn)
            if not sr:   # 0 = 自动：跟随设备原生采样率，避免 WASAPI 重采样抹平瞬态
                sr = _native_samplerate(mic) or 48000
            with st.lock:
                st.device_desc = mic.name
                st.error = dev_warn   # 空串 = 正常；非空 = 让主界面 lbl_warn 显示出来
                st.running = True
            self._capture_loop(mic, mono, sr, gen)
        except Exception as e:
            with st.lock:
                st.error = str(e)
                st.running = False

    def _run_lan(self, st, device_name, mono, sr, gen):
        """局域网音频采集（B 机）。

        `device_name` 在这一路是「端口 / 对端IP」（用户可填）：
          - `"47811"`               → 只听本机 47811 端口，对端自动发现
          - `"192.168.1.100"`       → 只听这个 IP 发来的包，端口用默认 47811
          - `"192.168.1.100:47811"` → 指定 IP + 端口
        返回 None = 正常跑完（切设备/停止）；返回字符串 = 出错原因。
        """
        port = lan_audio.DEFAULT_PORT
        peer_hint = ""
        txt = (device_name or "").strip()
        if txt:
            # ★ 手动 IP（2026-10-08 新增，用户要求"加一个手动输入 IP 的选项"）。
            #   统一解析：允许 纯IP / 纯端口 / IP:端口。
            host, _, tail = txt.rpartition(":")
            if host and tail and tail.isdigit():
                # 形如 1.2.3.4:47811
                peer_hint = host.strip()
                p = int(tail)
                if 1 <= p <= 65535:
                    port = p
            elif txt.replace(".", "").isdigit() and "." in txt:
                # 形如 1.2.3.4（纯 IP，无端口）
                peer_hint = txt
            else:
                # 形如 47811（纯端口）⚠ 必须在"纯 IP"分支之后，否则 47811 会被当 IP
                digits = "".join(c for c in txt if c.isdigit())
                if digits:
                    try:
                        p = int(digits)
                        if 1 <= p <= 65535:
                            port = p
                    except Exception:                            # noqa: BLE001
                        pass
        try:
            dev = lan_audio_recv.LanDevice(port=port, peer_hint=peer_hint)
        except Exception as e:                                   # noqa: BLE001
            return "%s: %s" % (type(e).__name__, e)
        if not dev.start():
            return dev.error or ("无法绑定 UDP 端口 %d" % port)
        # 等 A 机的第一包（最多 5s）—— 拿到才知道真实采样率与包长
        wait_until = time.perf_counter() + 5.0
        while time.perf_counter() < wait_until:
            if self._gen != gen:
                dev.stop()
                return None
            if dev.samplerate:
                break
            time.sleep(0.02)
        if not dev.samplerate:
            dev.stop()
            if peer_hint:
                # ★ 手填了 IP 却没通：给出**针对性的**提示（不然用户只会看到通用文案）
                if dev._hint_rejects > 0:
                    return ("5 秒内没有收到来自 %s 的音频包（端口 %d）。"
                            "有 %d 个包因对端地址不符被过滤 —— 请核对对方 IP。"
                            % (peer_hint, port, dev._hint_rejects))
                return ("5 秒内没有收到来自 %s 的音频包（端口 %d）。"
                        "请确认 A 机已运行「EasyReality 音频发送端」、"
                        "两台机器在同一局域网、防火墙放行 UDP %d。"
                        % (peer_hint, port, port))
            return ("5 秒内没有收到任何音频包（端口 %d）。"
                    "请确认 A 机软件已在运行、两台机器在同一局域网、"
                    "且防火墙允许 UDP %d。"
                    "（若自动发现失败，可在「设备」里直接填 A 机的 IP。）" % (port, port))
        # 采样率：用户选了就按用户的（下游会走一次 resample），否则跟随对端
        use_sr = int(sr) if sr else int(dev.samplerate)
        with st.lock:
            st.device_desc = "局域网音频 ← %s（%dHz）" % (dev.peer or "?", dev.samplerate)
            st.error = ""
            # ★★ 2026-10-08 修（用户报「已经握手上了然而文字没更新」）：
            #   `lan_failed` 原来**只有写入、没有清空**：
            #     ① `_run` 里失败时写它；
            #     ② `_capture_loop` 里 `if ls["error"]: st.lan_failed = ...`（error 空时不写）。
            #   ⇒ 只要**曾经**失败过一次（例如第一次点「应用」时 A 机还没启动 ⇒ 5 秒超时），
            #     这条陈旧文案就永久留在界面上（`panels._update_meters` 里 `if fail:`
            #     优先于 `elif info:`）—— 哪怕之后握手成功、`lan_info` 已有内容，
            #     对话框底部仍显示那句「5 秒内没有收到任何音频包…」。
            #   这里握手成功即清空；`lan_info` 同时重置，等下一秒状态刷新填上。
            st.lan_failed = ""
            st.lan_info = ""
            st.running = True
        try:
            self._capture_loop(dev, mono, use_sr, gen, wrap_native=False)
        finally:
            dev.stop()
        return None

    def _capture_loop(self, mic, mono, sr, gen, wrap_native=True, native_pkt=None):
        """采集主循环。`wrap_native=False` 时**不**对 mic 做 wasapi 包装。

        ⚠ **两个音源必须传 `wrap_native=False`**（它们的 mic 已经是「原生包长级别、
          不攒块」的形状，再包一层 wasapi 会炸）：
            · 局域网音频（`_run_lan`）：mic 是 `LanDevice`，包长固定 10ms；
            · ASIO（`_run_asio`）：mic 是 `AsioDevice`，包长＝驱动 `buffer_frames`。
          `native_pkt`：不包装路径的原生包长（帧）。给定时优先用它（ASIO 传
          `cap.buffer_frames`）；None 时回退「≈10ms @ sr」（局域网默认）。
        """
        st = self.state
        # ★★★ 2026-10-08：**换音源时必须先把「电平/能量类」字段清零**。
        #
        # 用户报：「局域网音频有声音，切到别的输入（声卡/系统声音）后——那边没声音时
        #   总电平**卡在一个地方不会归零**，画面也**一直在那放**；切到有声音的输入就正常。」
        #
        # 为什么：本循环要等到**第一块数据**才会 `analyzer.feed(data, st)` 去刷新 `st`。
        #   而新音源在"系统声音此刻没在播 / 局域网还没收到包 / 设备刚打开"时会**没有数据**
        #   ⇒ `st.level / st.energy / ...` 一直是**上一个音源留下的残值**
        #   ⇒ 界面总电平卡住不归零，引擎也据此认为"还有声音"继续驱动画面。
        #
        # ⚠ 只在**采集循环启动时**清一次：正常有数据的音源几百微秒内就会把值重新填上，
        #   对正常切换零影响；**绝不可每帧清** —— 那会把电平永远压成 0。
        # ⚠ 不清 BPM / 拍钟（那是"锁定值"，切换音源后由新数据自行重建；用户本轮未报此项）。
        try:
            with st.lock:
                st.level = 0.0
                st.bass = 0.0
                st.mid = 0.0
                st.high = 0.0
                st.energy = 0.0
                st.spectrum = [0.0] * 24
                st.energy_trend = 0.0
                st.vocalness = 0.0
                st.silent_sec = 0.0
                st.lv_db = -80.0
                st.segment = "none"
                st.onset_density = 0.0
                st.bpm_stab = 0.0
                st.beat_clarity = 0.0
                st.spec_complex = 0.0
                st.drop = False
                st.beat_active = False
                st.is_beat = False
                st.is_bar = False
        except Exception:                                          # noqa: BLE001
            pass
        # ★★ 2026-10-08：NDI 音频「随机咔哒」根因修复。
        #   `soundcard` 的 record(numframes=1024) 攒块相位会漂移（实测出块间隔
        #   21.3ms 均值、P99 31.3ms），而驱动本身严格 480 帧/10ms、QPC 精度 10µs
        #   ⇒ 换成 `wasapi_capture`（保留真实时间戳、不攒块、不填零），
        #     再由 `_BlockAssembler` 在 Python 侧按驱动包长整数倍攒块交给下游。
        #   ⚠ **块长必须是原生包长的整数倍**，否则余量漂移会让块间隔在
        #     「读 2 包」与「读 3 包」之间跳（实测 21/31ms 双峰，正是咔哒来源）。
        #   ⚠ 实际取值（2026-10-10 更正注释，旧写「960 帧/2 包」是 960 方案的残留、
        #     与实现不符）：BLOCK=1024、480 包长 ⇒ `_mult=ceil(1024/480)=3`
        #     ⇒ `_block=1440`（30ms，严格每 3 包一块、余量零漂移）。
        _pkt = 0
        _block = BLOCK
        if not wrap_native:
            # 不包装路径：局域网按 A 机固定 10ms 包长直连；
            # ASIO 用驱动上报的 buffer_frames（实测 Realtek 528@48k ≈11ms）作原生包长。
            _rate = sr or 48000
            _pkt = int(native_pkt) if native_pkt and int(native_pkt) > 0 \
                else max(64, int(round(_rate * 0.01)))
            _mult = max(1, (BLOCK + _pkt - 1) // _pkt)
            _block = _pkt * _mult
        else:
            try:
                import wasapi_capture as _wc
                _raw_mic = mic
                mic = _wc.wrap(_raw_mic)
                _rate = _native_samplerate(getattr(_raw_mic, "_mic", None) or _raw_mic) or sr or 48000
                _pkt = max(64, int(round(_rate * 0.01)))          # ≈10ms @ 设备原生率
                # 取不小于原 BLOCK 的原生包长整数倍（保持下游分析尺度基本不变）
                _mult = max(1, (BLOCK + _pkt - 1) // _pkt)
                _block = _pkt * _mult
            except Exception as _we:                               # noqa: BLE001
                # 兜底：新采集层不可用就退回 soundcard 原路径（绝不因此让采集停摆）
                print("[音频] 原生采集层不可用，退回 soundcard：%s" % _we)
                _pkt = 0
                _block = BLOCK
        analyzer = _Analyzer(sr, block=_block)
        # ★ 2026-11-02（用户「能量大部分时候还是很低」）：把分析器挂到引擎上，
        #   让 `energy_log.EnergyCsvLogger` 能读到 `analyzer.blk`（累计块数）。
        #   `blk` 每秒不长 ⇒ 采集根本没在喂数据（和"能量低"是两件不同的事，必须分得开）。
        #   ⚠ 每次进本函数都会新建 analyzer ⇒ `blk` 归零 = 采集重启过一次（这是免费的重启标记）。
        self._analyzer = analyzer
        assembler = _BlockAssembler(_block, _pkt) if _pkt else None
        # 连续读块失败计数：设备被拔掉时 record() 会不停抛异常，
        # 旧写法「单块出错就跳过、继续循环」会让采集线程一直占着 worker，
        # 后续「切换设备」的任务永远排在它后面 → 表现为改音源毫无反应。
        # 连续失败若干次就主动退出循环，让 worker 空出来接手切换请求。
        err_streak = 0
        # ★ 2026-10-07 修「线路输入用不了」之「mono 只取左声道 ⇒ 能量恒 0」：
        #   勾了「单声道分析」时旧写法硬取 data[:, :1]（左声道）。但线路输入常用
        #   单声道/RCA **只插一根**，右声道有信号、左声道恒 0 ⇒ rms 恒 0 ⇒
        #   用户看到"插上线毫无反应"，主界面还只说「没有检测到声音」。
        #   这里加**滑动窗口统计**的死零兜底：最近 N 块里绝大多数是「左弱右强」时
        #   自动改取右声道。
        #   ⚠ 为什么不用「连续 N 块」：实测左声道**间歇性**噪声（接触不良 / RCA 半插 /
        #     工频串扰）时，只要每 ≤8 块冒一次 ≥1e-4，连续计数就被复位、永远切不过去
        #     —— 而定线现场恰恰就是这种接触不良的线。滑动窗口对间歇噪声免疫。
        #   ⚠ 为什么用**非对称阈值**：左声道"多小才算没接信号"和右声道"多大才算有信号"
        #     不是一回事。若用同一个阈值，间歇噪声块会被当成"左声道健康"而清空窗口，
        #     导致永远攒不满（实测每 6/7/8 块冒一次 1e-3 噪声 ⇒ 永不切换）。
        #     故：左「健康」要求明显更高（_LIVE_L），右「有信号」门槛更低（_LIVE_R）。
        _WIN = 32                 # 窗口 = 32 块（≈0.68s @48k/BLOCK=1024）
        _WIN_DEAD = 24            # 窗口内 ≥24 块「左弱右强」（75%）才认为左声道接错
        _LIVE_L = 0.05            # 左声道「真接了信号」：-26dBFS，远高于噪底/串扰
        _LIVE_R = 1e-3            # 右声道「有信号」：-60dBFS
        _dead_win = deque(maxlen=_WIN)
        _live_streak = 0          # 左声道连续"健康"块数（防单块噪声清窗）
        use_right = False         # True = 兜底改取右声道
        # 一次性告警（设备未找到等）在界面上的保留时长：≈6s 后自动清空。
        # 太短 = 用户看不见；永久 = 顶掉「没有检测到声音」提示。
        _WARN_HOLD = 288          # ≈6.1s @48k/BLOCK=1024
        _warn_hold = 0
        _ndi_diag_last = None     # ★ 上一次记录的「采集门控」二元组（见循环内，值变化才写一行）
        _blk_ok = 0               # ★ 采集心跳：成功取到的音频块数
        _blk_empty = 0            # ★ 采集心跳：record() 返回空/None 的次数
        _empty_streak = 0         # ★ 连续空转次数（>3 起退避 sleep，见循环内说明）
        _push_ok = 0              # ★ 采集心跳：成功入队给 NDI 的次数
        _push_err = ""            # ★ 采集心跳：最近一次投递异常（诊断）
        _hb_t0 = time.perf_counter()   # ★ 采集心跳计时起点
        # ★ 2026-11-02（用户「能量大部分时候还是很低」）：采集每**启动一次**就记一行。
        #   配合 `energy_*.csv` 的 `blk_delta`：若某段时间 `blk_delta=0` 而这里正好有 start，
        #   就能证明"能量掉零"是采集重启造成的，而不是音乐变安静。
        try:
            from energy_log import log_capture_event
            log_capture_event("[CAP] start gen=%s dev=%s sr=%s block=%s pkt=%s wrap=%s"
                              % (gen, getattr(mic, "name", "?") or "?", sr,
                                 _block, _pkt, wrap_native))
        except Exception:                                        # noqa: BLE001
            pass
        # ★★★ 2026-11-02：按**实际采集采样率**创建离线 BPM 锚点线程。
        #   必须在 `_capture_loop` 里建（不是在 __init__）：sr 只有到这一步才知道，
        #   用错采样率会让重采样比例偏，锚点直接算错。sr 变了（换设备/换采样率）就重建。
        try:
            _old = self._anchor
            if _old is None or int(getattr(_old, "sr", 0) or 0) != int(sr):
                if _old is not None:
                    try:
                        _old.stop()
                    except Exception:                             # noqa: BLE001
                        pass
                from bpm_anchor import BpmAnchor
                self._anchor = BpmAnchor(self.state, sr=int(sr))
                self._anchor.start()
        except Exception:                                        # noqa: BLE001
            self._anchor = None
        with mic.recorder(samplerate=sr, channels=2) as rec:
            with self._audio_buf_lock:
                # ★ 缓冲容量必须按**实际采样率**给足：原来写死 `48000 * 8`，
                #   设备跑 96k 时 8 秒的缓冲只剩 4 秒 ⇒ 识别窗（FP_WIN_SEC）被截短、
                #   票数变少、甚至低于对齐门槛。12 秒足够容纳识别窗。
                need = int(sr * 12)
                self._fp_sr = sr
                if self._fp_buf.cap < need:
                    self._fp_buf = _AudioRing(need)
            # ★ 2026-10-19 审计修复：记录"最后一次收到音频包"的时刻（**循环外初始化一次**，
            #   否则每圈都重置 ⇒ 断流计时永远不触发）。
            #   原来 `data is None` 分支只计数 + 退避、**不写任何状态** ⇒ 断流后
            #   level/energy/spectrum 冻在最后一格、`running` 仍 True、界面还显示
            #   "对端 x.x.x.x | 丢包 0" ⇒ 用户以为软件卡死。
            _lan_last_t = time.perf_counter()
            _lan_gap_cleared = False
            _lan_gap_warned = False
            # ⚠ 这个 capture 循环是**所有音源共用**的 ⇒ 断流告警只能对局域网音源生效，
            #   否则用声卡时也会误报"没收到局域网音频包"。
            _is_lan_mic = isinstance(mic, lan_audio_recv.LanDevice)
            while self._gen == gen:   # 只在自己这一代有效；切换设备后立即退出
                try:
                    # ★★ 变化触发诊断（2026-10-07）：把 NDI 音频门控两个量写进取证日志。
                    #   现场"有画面没声音"最难的一步就是**分不清**是
                    #   ① ndi_audio_on=False（界面开关没生效）
                    #   ② ndi_feed=None（回调没接上）
                    #   ③ 两个都 True 但发送侧失败（去 ndi_out 看）
                    #   这里记录「值发生变化时」的一行，三者立刻可辨。
                    #   ⚠ 代价只有每块一次 bool 比较 + 元组比较（~10ns），不进热路径；
                    #     不能用「只写一次」—— 万一是启动后才被回滚成 False，只写一次就漏了。
                    _gate_now = (bool(st.ndi_audio_on), st.ndi_feed is not None,
                                 bool(getattr(st, "ndi_running", False)))
                    if _gate_now != _ndi_diag_last:
                        _ndi_diag_last = _gate_now
                        try:
                            from ndi_out import _health_write
                            _health_write(
                                "采集门控快照：ndi_audio_on=%r  ndi_feed=%s  "
                                "ndi_running=%r  sr=%s  mono=%r"
                                % (_gate_now[0],
                                   "已接" if _gate_now[1] else "None(未接)",
                                   _gate_now[2], sr, bool(mono)))
                        except Exception:                          # noqa: BLE001
                            pass
                    # 实时透传能量伽马校正（用户调滑块后下一块立即生效，不能在启动时只拷贝一次）
                    analyzer.energy_scale = self.energy_scale
                    # ★★ 2026-10-08：改用 `_BlockAssembler.read()` —— 按驱动原生包长取数
                    #   再按原生包长整数倍攒块（修「随机咔哒」，见 _BlockAssembler 的说明）。
                    data = assembler.read(rec) if assembler is not None else rec.record(numframes=_block)
                    if data is None or len(data) == 0:
                        _blk_empty += 1
                        # ★★ 2026-10-08：**空转必须退避**（现场事故：软件卡到弹窗狂闪）。
                        #   为什么原来可以不睡：老路径是 `soundcard.record()` —— 它**阻塞**
                        #   到有数据才返回，`continue` 不会空转。
                        #   新路径两条都**立即返回 None**：
                        #     ① 局域网音频 `_LanRecorder.record()` 缓冲不足直接 return None；
                        #     ② `_BlockAssembler.read()` 缓冲为空时也 return None。
                        #   ⇒ 裸 `continue` = **无 sleep 死循环**，实测 2 分钟累积
                        #     `empty=60244562` 次、吃满一个核、长期持有 GIL ⇒
                        #     主线程每 500ms 抢引擎锁被饿死 ⇒ **界面卡成幻灯片**。
                        #   ⚠ 判据：**空转的次数越多，睡得越久**（1ms→3ms 封顶），
                        #     有数据立刻归零 ⇒ 稳态零代价、异常时自动降速。
                        _empty_streak += 1
                        # ★ 2026-10-19：断流检测（**双向写**：来包时清、断流时写）
                        #   ⚠ 仅局域网音源 —— 这个 capture 循环是**所有音源共用**的，
                        #     不判类型的话用声卡时也会误报"没收到局域网音频包"。
                        if _is_lan_mic:
                            try:
                                _gap = time.perf_counter() - _lan_last_t
                                if _gap >= 1.5 and not _lan_gap_cleared:
                                    _lan_gap_cleared = True
                                    with st.lock:
                                        st.level = 0.0
                                        st.energy = 0.0
                                        _sp = getattr(st, "spectrum", None)
                                        if _sp is not None:
                                            try:
                                                _sp[:] = 0.0
                                            except Exception:          # noqa: BLE001
                                                pass
                                if _gap >= 3.0 and not _lan_gap_warned:
                                    _lan_gap_warned = True
                                    with st.lock:
                                        st.lan_failed = (
                                            "已经 %.0f 秒没有收到局域网音频包了。"
                                            "请检查 A 机是否还在放、两台机器是否还在同一网络、"
                                            "以及防火墙是否放行 UDP。" % _gap)
                                        st.lan_info = ""
                            except Exception:                          # noqa: BLE001
                                pass
                        if _empty_streak > 3:
                            time.sleep(0.003 if _empty_streak > 30 else 0.001)
                        continue
                    err_streak = 0
                    _empty_streak = 0
                    _blk_ok += 1
                    _lan_last_t = time.perf_counter()
                    if _is_lan_mic and (_lan_gap_warned or _lan_gap_cleared):
                        _lan_gap_cleared = _lan_gap_warned = False
                        with st.lock:
                            if st.lan_failed:
                                st.lan_failed = ""      # 恢复 ⇒ 立刻清掉告警
                except Exception as e:
                    # ---- 采集段异常（设备 I/O）----
                    # 只有这里才累加 err_streak、连续 5 次才停采集（设备真断开时停止 +
                    # 提示是正确的产品行为：连续读块失败 = 设备不可用，停掉让 worker
                    # 空出来接手「切换设备」任务，否则旧实现会把 worker 永久占住）。
                    err_streak += 1
                    with st.lock:
                        if err_streak >= 5:
                            st.error = ("音频设备可能已断开或不可用，已停止采集。"
                                        "请到「音源」重新选择设备后点应用。")
                            st.running = False
                        else:
                            st.error = f"读取块错误（已跳过）: {e}"
                    if err_streak >= 5:
                        break   # 主动退出，把 worker 让给「切换设备」的任务
                    time.sleep(0.02)
                    continue
                try:
                    # ---- 分析/转发段 ----
                    # ★★ 2026-10-10：本段异常**绝不停止采集**（见段尾 except 注释）。
                    if mono:
                        # ---- 左声道死零兜底（仅在 mono 且立体声时）----
                        if data.ndim == 2 and data.shape[1] >= 2:
                            lpk = float(np.abs(data[:, 0]).max())
                            rpk = float(np.abs(data[:, 1]).max())
                            if use_right:
                                # 已在右声道：左声道**连续**恢复真实信号才切回
                                # （单块噪声不算，防来回横跳）
                                if lpk >= _LIVE_L:
                                    _live_streak += 1
                                    if _live_streak >= 8:
                                        _dead_win.clear()
                                        use_right = False
                                        _live_streak = 0
                                else:
                                    _live_streak = 0
                            elif lpk >= _LIVE_L:
                                # 左声道健康：**连续**若干块才清窗（单块噪声不清）
                                _live_streak += 1
                                if _live_streak >= 8:
                                    if _dead_win:
                                        _dead_win.clear()
                                    _live_streak = 0
                            else:
                                # 左弱。右强才算"左接错"的证据。
                                _live_streak = 0
                                _dead_win.append(1 if rpk >= _LIVE_R else 0)
                                if (not use_right) and len(_dead_win) >= _WIN \
                                        and sum(_dead_win) >= _WIN_DEAD:
                                    use_right = True
                        if use_right:
                            _fed = data[:, 1:2]
                        else:
                            _fed = data[:, :1]
                        analyzer.feed(_fed, st)
                    else:
                        _fed = data
                        analyzer.feed(_fed, st)
                    # ★ 2026-11-02：同一块再喂给离线锚点（内部只做一次 mean + 环形写，
                    #   约几微秒；真正的分析在 `BpmAnchor` 自己的线程里）
                    try:
                        _an = self._anchor
                        if _an is not None:
                            _an.feed(_fed)
                    except Exception:                         # noqa: BLE001
                        pass
                    # ★ 一次性告警（如"设备未找到，已改用 X"）的收尾：
                    #   既不能永久挂着（会顶掉后续「没有检测到声音」提示 ——
                    #   ui_main 是 if error / elif silent>5 互斥），
                    #   也不能刚写就立刻清掉（那样用户根本来不及看见）。
                    #   折中：显示满 _WARN_HOLD 块（≈6s，够看清）后清空。
                    if st.error:
                        _warn_hold += 1
                        if _warn_hold >= _WARN_HOLD:
                            with st.lock:
                                st.error = ""
                            _warn_hold = 0
                    else:
                        _warn_hold = 0
                    # NDI 音频（音画同步）：把采集到的立体声 PCM **入队**给独立转发线程
                    # ★ 2026-09-28 改：不再在本线程直接调 `ndi.feed_audio()` —— 它会写
                    #   cyndilib 缓冲并 send_audio，接收端一慢就把采集线程拖住，
                    #   连带能量/拍位/指纹全停（现场掉拍）。现在只投递，队列满丢帧。
                    #   未启用时**直接跳过**，省掉每块的 asarray/reshape/copy。
                    # ★★ 2026-10-07 修（NDI 音频从来没响过的**真正根因**）：
                    #   这里原写 `_np.asarray(...)`，但 `_capture_loop` 里**没有** `_np` 这个名字
                    #   —— 模块顶部是 `import numpy as np`（第 19 行）；`_np` 只在一个**别的
                    #   函数内部**做了局部 import（`_recognize_loop`，第 1375 行），
                    #   作用域完全不覆盖这里。
                    #   ⇒ 每一块都抛 `NameError: name '_np' is not defined`，
                    #     被下面的 `except Exception` 静静吞掉 ⇒ push 次数恒为 0、
                    #     队列永远空、"采集在跑但一个字节都没喂给 NDI"。
                    #   这个 bug 从 1c9ac6e 引入起就存在（`git log -S` 可查），
                    #   且因为**没有任何日志**（旧代码连异常都吞）而拖了很多轮才抓到。
                    #   ⚠ 修复要点：统一用模块级别的 `np`（不是 `_np`）。
                    try:
                        # ★ 加 st.ndi_running：「未开始自动VJ」时连拷贝都省掉，
                        #   与 NDI 音频门控语义一致（见 AudioState.ndi_running）。
                        if st.ndi_audio_on and st.ndi_running and st.ndi_feed is not None:
                            arr = np.asarray(data, dtype=np.float32)
                            if arr.ndim == 1:
                                arr = np.column_stack([arr, arr])
                            elif arr.shape[1] == 1:
                                arr = np.repeat(arr, 2, axis=1)
                            self.push_ndi_audio(arr.reshape(-1).copy(), sr)
                            _push_ok += 1
                    except Exception as _pe:
                        _push_err = "%s: %s" % (type(_pe).__name__, str(_pe)[:80])
                    # ★ 局域网音频状态刷新：每秒写一次 snapshot 字段（界面要看延迟/丢包）。
                    #   放在心跳点旁边，复用同一次 perf_counter 比较，不进热路径。
                    if isinstance(mic, lan_audio_recv.LanDevice):
                        _lan_t = getattr(self, "_lan_info_t", 0.0)
                        if time.perf_counter() - _lan_t >= 1.0:
                            self._lan_info_t = time.perf_counter()
                            try:
                                ls = mic.snapshot()
                                _hint = ls.get("peer_hint") or ""
                                with st.lock:
                                    st.lan_info = (
                                        "%s对端 %s | %.1fHz | 深度 %.0fms | 抖动 %.0fms"
                                        " | 丢包 %d (%.2f%%)"
                                        % ("[指定 %s] " % _hint if _hint else "",
                                           ls["peer"] or "?", ls["sr"] or 0,
                                           ls["depth_ms"], ls["jitter_ms"],
                                           ls["lost"], ls["lost_rate"]))
                                    # ★★ 2026-10-08：这里**必须双向写**。
                                    #   旧写法 `if ls["error"]: st.lan_failed = ...` 只写不清
                                    #   ⇒ 失败过一回之后即使恢复正常，界面永远挂着旧错误
                                    #     （用户报「已经握手上了然而文字没更新」）。
                                    #   现在：有错就写、无错就清 —— 与 `lan_info` 同步更新。
                                    st.lan_failed = ls["error"] or ""
                            except Exception:                      # noqa: BLE001
                                pass
                    # ★ 采集侧心跳（每 10s 一行）：现场"到底有没有在投递音频"的**唯一硬证据**。
                    #   blk=采集到的块数 / push=成功入队次数 / 队列当前长度 / 错误。
                    #   若 push 恒为 0 而 blk 在涨 ⇒ 门控或异常吞掉了投递；
                    #   若 push 在涨但下游无「首次取到 PCM」⇒ 转发线程/队列出问题。
                    if time.perf_counter() - _hb_t0 >= 10.0:
                        _hb_t0 = time.perf_counter()
                        try:
                            from ndi_out import _health_write
                            _health_write(
                                "采集心跳：blk=%d push=%d empty=%d qlen=%d "
                                "ndi_audio_on=%r ndi_running=%r ndi_feed=%s err=%s"
                                % (_blk_ok, _push_ok, _blk_empty,
                                   len(self._ndi_q), bool(st.ndi_audio_on),
                                   bool(getattr(st, "ndi_running", False)),
                                   "已接" if st.ndi_feed is not None else "None",
                                   _push_err or "-"))
                        except Exception:                          # noqa: BLE001
                            pass
                    # 指纹缓冲：**左右平均**（与建库同口径 `np.mean(data,axis=1)`）。
                    # ★ 2026-10-30 曲风缓冲删除后，这里只喂 `_fp_buf`。
                    try:
                        fp_sig = (np.asarray(data).mean(axis=1)
                                  if getattr(data, "ndim", 1) == 2 else data)
                        with self._audio_buf_lock:
                            self._fp_buf.write(fp_sig)
                    except Exception:
                        pass
                except Exception as e:
                    # ★★ 2026-10-10：分析段异常**绝不能因分析代码的 bug 停止采集**。
                    #   历史真实踩过：分析代码里一个局部变量改名导致每块抛 NameError，
                    #   旧实现把它算作「连续失败」，5 块后直接停采集 ⇒ 用户"没声音"。
                    #   这里只记录 + 跳过本块，采集继续跑；设备层错误由上面的「采集段」
                    #   （err_streak）负责 —— 分析代码出问题不该让整条采集链陪葬。
                    with st.lock:
                        st.error = f"分析块错误（已跳过）: {e}"
                    time.sleep(0.02)
                    continue
        # 采集结束（切换设备/停止）：复位运行标志
        with st.lock:
            if self._gen != gen:
                st.running = False
