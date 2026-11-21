# -*- coding: utf-8 -*-
"""软件版本与作者信息（「关于」界面读取这里）。

版本号规则（用户 2026-09-23 定）：**日期 + 当日序号**，每生成一次新文件序号 +1。
    exe 内显示：2026.09.23.07（= 2026-09-23 当天第 7 次生成）
    压缩包名  ：EasyRealityAutoVJ_测试版092307.zip（与上面同一个号）
序号由 `tools/build_version.py` 在打包前递增并写成 `src/_build_ver.py`，
这里读它；**源码直接运行（没打包过）时显示「开发版」**。
想知道现在是第几次：`venv\\Scripts\\python tools\\build_version.py show`
"""

try:
    from _build_ver import BUILD_DATE, BUILD_SEQ, BUILD_VER, ZIP_NAME
except Exception:                      # 源码模式 / 未经打包
    BUILD_DATE, BUILD_SEQ, BUILD_VER, ZIP_NAME = "", 0, "dev", ""

# ★ 2026-11-21：1.0 正式版。对外只显示「1.0」；
#   内部兼容比较（码表包 min_app_version）用 APP_VERSION_NUM（仍是构建日期号）。
APP_OFFICIAL = True
APP_CHANNEL = ""
APP_VERSION = "1.0"
APP_VERSION_NUM = BUILD_VER or "dev"     # 仅供版本比较/日志，不直接展示
APP_BUILD = BUILD_DATE or ""
APP_SEQ = BUILD_SEQ
APP_ZIP_NAME = ZIP_NAME

TAGLINE = ("把音乐变成画面的实时 VJ 引擎。",
           "A real-time VJ engine that turns music into visuals.")

INTRO = ("它听正在放的歌，认出是哪一首，再按提前做好的码表，以及曲库里预分析的节拍和"
         "能量段落，决定“现在该是什么画面”。素材切换、跟拍、跟情绪都由它自己完成——"
         "DJ 只管放歌，你不用整场盯着电脑点下一首。",
         "It listens to the track that is playing, recognizes it, and uses your cue sheet "
         "together with the pre-analyzed beat grid and energy sections to decide what should "
         "be on screen. Clip changes, beat sync and mood are handled for you — the DJ just "
         "plays, and you are not glued to the laptop cueing the next clip.")

# 「关于」里的 5 条能力：(中文标题, 英文标题, 中文说明, 英文说明)
ABOUT_FEATURES = [
    ("跟着歌走", "Follows the track",
     "用音频指纹认出正在播放的曲目，读出 BPM、八拍网格和能量段落。进 drop 的那一帧就换"
     "素材池，不用等能量慢慢爬，也不用你手动卡点；歌中途变速也跟得住。",
     "Identifies the playing track by audio fingerprint and reads its BPM, 8-beat grid and "
     "energy sections. The clip pool switches on the exact frame you enter the drop — no "
     "waiting for measured energy, no manual cueing — and tempo changes are followed."),
    ("自动选素材", "Picks clips for you",
     "素材先按动态强弱、前景/背景、铺满度打好标。现场按当前能量和角色自动挑：高动态留给 "
     "drop，背景素材铺底，前景元素叠在上面，不用提前给每首歌排片。",
     "Clips are tagged by motion level, foreground/background role and coverage. At show time "
     "the right pool is chosen from the current energy and role: high-motion for drops, "
     "backgrounds as a base, foreground elements on top — no per-track programming needed."),
    ("稳定输出", "Solid output",
     "画面作为 NDI 源或 Spout 源直出，OBS、Resolume 里直接选，不用采集卡。DXV 素材走 GPU "
     "解码，4K 也不靠 CPU 硬扛；带透明通道的素材保留 Alpha，多屏输出各走各的分辨率。",
     "Publishes the picture as an NDI or Spout source — pick it directly in OBS or Resolume, "
     "no capture card. DXV clips are decoded on the GPU, so 4K does not hammer the CPU; alpha "
     "is preserved, and each screen in multi-screen mode keeps its own resolution."),
    ("现场可控", "Stay in control",
     "自动之外，KV 主视觉、调色、多屏、黑场/冻结/下一素材、手动接管都在。想自己来的时候"
     "随时接手，松手再交回自动。",
     "Beyond automation: KV main visual, color grading, multi-screen, blackout/freeze/next "
     "clip and manual takeover are all there. Step in whenever you want, hand it back to "
     "auto when you are done."),
    ("素材准备", "Prep tools",
     "导入、扫描打标、悬浮胶片预览、码表、曲库 BPM 与段落分析都在一个软件里。哪些素材参与"
     "自动匹配、哪些只当 logo 固定不动，也能单独设。",
     "Import, scanning/tagging, hover filmstrip previews, cue sheets, library BPM and "
     "section analysis — all in one app. You can also choose which clips join auto matching "
     "and which stay as fixed logos."),
]

# 作者与反馈渠道
AUTHOR = "麻婆豆腐"
CONTACT = "QQ 1793170435"
# 项目主页（「关于」界面显示为可点击链接）
PROJECT_URL = "https://github.com/mapotofu25/EasyRealityAutoVJ"
# 测试群（关于界面显示，链接可点击直接拉起 QQ 加群）
GROUP_NAME = "ERAVJ测试"
GROUP_NO = "543643838"
GROUP_URL = "https://qm.qq.com/q/o5m9jjmkIC"

# 本版亮点（「关于」界面展示；中英对照，按语言取用）
HIGHLIGHTS = [
    ("Kv 主视觉图层", "Kv Main Visual (idle) layer",
     "没声音时把待机画面带进来，有声音时带出去；完全独立于其它设置",
     "Brings an idle visual in when silent and out when audio returns; "
     "fully independent of every other setting"),
    ("颜色渲染", "Color rendering",
     "一键调色，让同一批素材换个颜色就不一样，降低对素材数量的依赖",
     "One-click grading so the same clips look different — less reliance on clip quantity"),
    ("听歌识曲", "Song ID",
     "靠音频指纹认出正在播放的歌，认出后按这首歌的能量段落走",
     "Identifies the playing track by audio fingerprint and follows its energy sections"),
    ("NDI 输出", "NDI output",
     "画面作为 NDI 源「Easy Reality Output」直出：本机走 TCP 回环，局域网走直连，"
     "OBS 里直接选它就行",
     "Publishes the picture as the NDI source 'Easy Reality Output' — loopback over TCP "
     "on the same machine, direct on a LAN; just pick it in OBS"),
    ("曲目信息与详情合一", "Track info merged into details",
     "曲库右键只剩一个入口：文件/歌名/艺人/曲风/时长/采样率/指纹 ID 与波形、"
     "BPM 分段在同一页；BPM 一改即生效（不用再勾「手动覆盖」），"
     "多段 BPM 在波形上右键编辑",
     "One right-click entry in the library: file / title / artist / genre / duration / "
     "sample rate / fingerprint ID together with the waveform and BPM segments; "
     "editing BPM applies immediately, and multi-tempo tracks are edited by "
     "right-clicking the waveform"),
    ("本地网格分析与 BPM", "Local beat grid & BPM",
     "自己算拍长和八拍头，不依赖外部软件：拿 VDJ 曲库当标准答案对照 59 首，"
     "52 首 BPM 一致（88%）、44 首小节相位对齐（75%）、42 首乐句对齐（71%）；"
     "200~210 BPM 的 Hardcore / 东方同人不再被判成半速；"
     "可选神经网络只用来指认「哪一拍是小节头」；详情页还能整段微调或半速/倍速",
     "Computes beat length and downbeats on its own: against 59 rekordbox/VDJ tracks, "
     "52 agree on BPM (88%), 44 on bar phase (75%), 42 on phrase (71%); 200-210 BPM "
     "hardcore no longer collapses to half speed; an optional neural model only "
     "decides which beat starts the bar; the details page can shift or halve/double it"),
    ("长曲不再崩", "Long tracks no longer crash",
     "6 分钟以上的曲目做节拍分析时会因索引越界直接中断（float32 在 1600 万以上"
     "精度只剩 2，末端采样点被舍入到数组外）；改成 float64 并显式钳位",
     "Beat analysis aborted on tracks longer than ~6 minutes (float32 loses precision "
     "above 16.7 million, rounding the last sample past the end); now uses float64 "
     "with an explicit clamp"),
    ("多 BPM 段自动识别", "Auto multi-tempo segments",
     "一首歌里中途变速也能分开：逐段测「当前网格对不对得上」，对不上才局部重估 BPM；"
     "恒速歌多花不到 0.1 秒、结果不变。实测 74 首恒速曲零误判，"
     "152→175、144→128 两种拼接样本都能分对",
     "Handles tracks that change tempo: each window is scored against the current grid "
     "and only failing windows re-estimate BPM locally. Constant-tempo tracks cost "
     "under 0.1 s and are unchanged; 74 real tracks produce zero false splits, while "
     "152→175 and 144→128 spliced samples are split correctly"),
    ("拍网铺满整首", "Beat grid covers the whole track",
     "八拍竖线从中段锚点往前往后都画 —— 修掉了「左半段没有刻度」的问题",
     "Downbeat lines are drawn both directions from a mid-track anchor — "
     "fixes the reported 'no grid on the left half'"),
    ("变速跟得住", "Follows tempo changes",
     "画面位置按实测播放倍率推进（不再每 30 秒判一次假跳变）",
     "Playback position advances by the measured rate instead of a fixed 1.0x "
     "(no more false seek every 30 s)"),
    ("能量算法", "Energy engine",
     "响度 / 低频冲击 / 起伏量多信号融合，扛得住系统音效与压缩",
     "Fuses loudness, low-frequency punch and fluctuation — robust against "
     "system audio effects and compression"),
    ("渲染开销大幅下降", "Much lower render cost",
     "调色与后处理改成共用一次全画布读回/写回，胶片颗粒换用更快的随机数生成器。"
     "同机同曲实测（1280×720 / 30fps）：本进程 19~22% → 12~14%；"
     "右下角那个 CPU 读数是**整机**占用（GetSystemTimes），不是本软件的",
     "Color grading and post-processing now share one full-frame read-back/write-back, "
     "and film grain uses a faster RNG. Measured on the same machine and track "
     "(1280×720 / 30fps): 19-22% → 12-14% of the process; the CPU figure in the "
     "status bar is the whole system (GetSystemTimes), not this app"),
    ("能量段落（到点直接切池）", "Energy sections (instant pool switch)",
     "给每首歌标好「哪一段到哪一段、目标能量和档位」（详情页波形右键 → 编辑能量段落）。"
     "演出时指纹一对齐就按这张表走：进 drop 那一帧直接换素材池，不再等实测能量慢慢爬、"
     "也不等 2 秒稳定和 8/16 拍排期。段落边界、素材切换、效果切换、颜色轮换全部对齐"
     "波形图的八拍网格（anchor + 8n×beat_len）；没标段落的歌仍然按原来的实测能量走",
     "Mark each track's sections (time range + target energy / tier) from the waveform "
     "right-click menu. Once the fingerprint locks, playback follows that table: the "
     "pool switches on the exact frame you enter the drop, instead of waiting for the "
     "measured energy to climb plus 2s of debounce and the 8/16-beat schedule. Section "
     "boundaries, clip switching, effect switching and colour rotation all snap to the "
     "same 8-beat grid as the waveform (anchor + 8n×beat_len); tracks without sections "
     "keep the old measured-energy behaviour"),
    ("认歌校验（得分 / 最弱窗 / 领先）", "Match verification: score / weakest / lead",
     "照 KDJ 作者公开的判据把认歌的「确认」从一条门槛改成三条：得分（平均）、"
     "最弱窗（把查询窗分三段，取最低的一段）、领先（得分减去别的位置的次高分）。"
     "光看得分挡不住两类错：重复段落的另一处能拿到 0.965 的分（实测只需换个位置就凑出来），"
     "以及「两段对、一段错」的偶然对齐。加了最弱窗和领先之后，这两类都能被识别出来。"
     "另外把倍率搜索改成「粗扫 0.02 → 精扫 0.001 且位置一起细扫」，"
     "变速时报出来的倍率误差从 ±0.025 降到 ±0.011，同时单次耗时从 30~47 毫秒降到 5~6 毫秒",
     "Match verification now uses three criteria instead of one threshold: the mean score, "
     "the weakest of three sub-windows, and the lead (score minus the best competing "
     "position). A single score cannot reject two failure modes: a repeated passage at a "
     "different position scores 0.965 in practice, and 'two segments right, one wrong' "
     "averages out. The weakest-window and lead criteria catch both. Speed search is now "
     "coarse (0.02) then fine (0.001, position refined together), cutting the reported "
     "rate error from +-0.025 to +-0.011 while dropping the per-call cost from 30-47 ms "
     "to 5-6 ms"),
    ("变速倍率不再被单窗带跑", "Speed no longer hijacked by a single window",
     "原速播放时偶尔会有某一窗给出 1.17 的倍率（因为换个倍率凑分确实能拟合得更好），"
     "前馈被它一带就跑偏。现在倍率必须最近 8 秒内至少两窗给出互相相差 0.03 以内的值才发布，"
     "取中位数。真变速会持续出现，凑出来的只出现一次",
     "At normal speed a single window could occasionally report a 1.17 rate (shifting both "
     "rate and position genuinely fits better locally), which then dragged the feed-forward "
     "off. The rate is now published only when at least two windows within the last 8 s "
     "agree to within 0.03, taking the median. A real speed change persists; a spurious one "
     "appears once")
]

# 用到的开源项目（「关于」界面展示，链接可点击直接打开浏览器）
#   (名称, 用途中文, 用途英文, 项目地址, 许可)
# 链接取自各包安装元数据里的官方 Homepage/Repository 字段，不用手猜。
PROJECTS = [
    ("PySide6 (Qt for Python)", "界面框架", "GUI framework",
     "https://github.com/qtproject/pyside-pyside-setup", "LGPL v3"),
    ("OpenCV (opencv-python)", "视频解码、图像处理", "video decoding, image processing",
     "https://github.com/opencv/opencv-python", "Apache 2.0"),
    ("NumPy", "数值计算、频谱分析", "numerical & spectral math",
     "https://github.com/numpy/numpy", "BSD 3-Clause"),
    ("PyAV (FFmpeg)", "带 alpha 通道的视频解码", "alpha-channel video decoding",
     "https://github.com/PyAV-Org/PyAV", "BSD 3-Clause / LGPL"),
    ("FFmpeg", "PyAV 内置的底层多媒体库", "underlying media library for PyAV",
     "https://github.com/FFmpeg/FFmpeg", "LGPL / GPL"),
    ("SoundCard", "系统回环、麦克风采集", "system loopback & mic capture",
     "https://github.com/bastibe/SoundCard", "BSD 3-Clause"),
    ("sounddevice (PortAudio)", "详情页试听播放", "preview playback in the detail page",
     "https://github.com/spatialaudio/python-sounddevice", "MIT"),
    ("PySoundFile (libsndfile)", "音频读写", "audio file I/O",
     "https://github.com/bastibe/python-soundfile", "BSD 3-Clause / LGPL"),
    ("onnxruntime", "Beat This! 小节检测的推理后端",
     "inference backend for Beat This! downbeat detection",
     "https://github.com/microsoft/onnxruntime", "MIT"),
    ("mutagen", "音乐元数据（歌名 / 曲风）", "music metadata (title / genre)",
     "https://github.com/quodlibet/mutagen", "GPL v2+"),
    ("cyndilib", "NDI 输出", "NDI output",
     "https://github.com/cyndilib/cyndilib", "Apache 2.0"),
    ("SpoutGL (Spout SDK)", "Spout 输出（发给 OBS / Resolume）",
     "Spout output (to OBS / Resolume)",
     "https://github.com/jlai/Python-SpoutGL", "BSD 3-Clause"),
    ("GLFW", "GPU 解码用的离屏 OpenGL 上下文",
     "offscreen OpenGL context for GPU decoding",
     "https://github.com/glfw/glfw", "MIT (zlib/libpng)"),
    ("Pillow", "缩略图处理", "thumbnail processing",
     "https://github.com/python-pillow/Pillow", "MIT-CMU"),
    ("cffi", "底层库绑定（SoundCard 依赖）", "C bindings (SoundCard dependency)",
     "https://github.com/python-cffi/cffi", "MIT"),
    ("PyInstaller", "打包为可执行程序", "packaging into an executable",
     "https://github.com/pyinstaller/pyinstaller", "GPL v2+ (packaging exception)"),
    ("NDI® SDK (Vizrt)", "NDI 收发运行库（非 GitHub，官方站点）",
     "NDI runtime (not on GitHub — official site)",
     "https://ndi.video/for-developers/ndi-sdk/", "NDI SDK License"),
]

# AI 模型（随软件分发，放在 assets/models）
MODELS = [
    ("Beat This! (CPJKU)", "小节头（downbeat）判定：给没有 VDJ 数据的曲目补小节相位",
     "downbeat detection for tracks without VDJ data",
     "https://github.com/CPJKU/beat_this", "MIT"),
]

# 来源与致谢：代码或思路的来源项目（MIT 署名要求，须保留）
CREDITS = [
    ("VJVision (ichiryu)", "音乐电池 ChargeBar 决策状态机、音频指纹峰值检测"
     "（本项目 charge_engine.py / fp.py 的来源）",
     "Music Battery (ChargeBar) decision engine & fingerprint peak detection "
     "(origin of charge_engine.py / fp.py)",
     "https://github.com/ichiryu0021/VJVision", "MIT"),
    ("KVJ / KDJ (kumoSleeping)", "变速与「能不能作准」的思路参考：白化频带相关打分、"
     "互不重叠的多次印证、按倍率档位检索。**仅参考思路，未取用任何代码**"
     "（对方为 GPL-3.0，本项目独立实现）",
     "design reference only for tempo handling and acceptance: whitened spectral-band "
     "scoring, corroboration across non-overlapping windows, rate-candidate search. "
     "**No code taken** (upstream is GPL-3.0; implemented independently here)",
     "https://github.com/kumoSleeping/KVJ", "GPL-3.0 (reference only)"),
    ("Genre Police AutoVJ (lbnandy)", "「演出前分析曲库 + 现场识别曲目 + Spout / NDI 输出」"
     "的整体思路参考",
     "overall reference: pre-show library analysis + live track ID + Spout / NDI output",
     "https://github.com/lbnandy/genre-police-autovj", "MIT"),
]

# 兼容旧引用（与 PROJECTS 同源的简表：名称, 许可）
COMPONENTS = [(p[0], p[4]) for p in PROJECTS]

