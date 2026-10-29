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

# ★ 2026-10-22：RC2 → RC3。识别防误报（白名单+峰值门槛+id 自愈）、ASIO 开放、码表 GIF 修复。
#   打包版显示 2026.10.19.xx-RC1；源码运行显示 dev-RC1。
APP_CHANNEL = "RC3"
APP_VERSION = ((BUILD_VER + "-RC3") if BUILD_VER and BUILD_VER != "dev" else "dev-RC3")
APP_BUILD = BUILD_DATE or ""           # 2026-09-23
APP_SEQ = BUILD_SEQ                    # 7
APP_ZIP_NAME = ZIP_NAME                # EasyRealityAutoVJ_测试版092307.zip

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
    ("听歌识曲 + 曲风匹配", "Song ID + genre matching",
     "识别正在播放的歌，按曲风自动挑选匹配的画面",
     "Recognizes the playing track and picks matching visuals by genre"),
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
    ("变速认歌（DJ 打碟 / 机台推速）", "Song ID under DJ speed changes",
     "DJ 机台 beatmatch 会保留音高、只改速度，原来的指纹票会被摊到几十个位置上、"
     "单点票掉到十几票而锁不上。补了三层：① 票不够时自动用 9 秒长窗再认一次"
     "（票数超线性于窗长）；② 变速与「实时 BPM ↔ 库内 BPM 比值」互为印证，"
     "并允许撞顶（220 上限钳位）造成的 2 倍关系；"
     "③ 新建 12 音级 chroma 特征库（94 首），锁定后用音级能量相关复核倍率与位置 —— "
     "它对变速天然不敏感（4.5 秒窗实测同曲 1.08x 得 0.86、异曲只有 0.10~0.16），"
     "给出的倍率比速度估计准一个量级。"
     "实测 1.20× 从「认歌 36/36 但倍率停在 1.000、位置每秒漂」变为"
     "倍率 1.143、位置误差 0.20 秒内、全程无码表闪出",
     "When a DJ beatmatches (pitch kept, speed changed) the landmark votes get spread "
     "over dozens of positions and the per-position count collapses below the lock "
     "threshold. Three layers were added: (1) a 9-second window retry when the short "
     "window cannot lock (vote count grows faster than window length); (2) the speed is "
     "cross-checked with the live-vs-library BPM ratio, including the 2x relation created "
     "by the BPM detector's 220 clamp; (3) a 12-bin chroma feature library (94 tracks) "
     "re-verifies speed and position after lock -- chroma is inherently immune to speed "
     "changes (4.5 s window: same track at 1.08x scores 0.86 while other tracks score "
     "0.10-0.16), and its speed estimate is an order of magnitude better than the "
     "temporal estimator. Measured: 1.20x went from '36/36 recognised but speed stuck at "
     "1.000 with position drifting every second' to speed 1.143 with position error under "
     "0.20 s and no cue dropouts")
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
    ("onnxruntime", "本地 AI 曲风识别", "on-device AI genre detection",
     "https://github.com/microsoft/onnxruntime", "MIT"),
    ("mutagen", "音乐元数据（歌名 / 曲风）", "music metadata (title / genre)",
     "https://github.com/quodlibet/mutagen", "GPL v2+"),
    ("cyndilib", "NDI 输出", "NDI output",
     "https://github.com/cyndilib/cyndilib", "Apache 2.0"),
    ("SpoutGL (Spout SDK)", "Spout 输出（发给 OBS / Resolume）",
     "Spout output (to OBS / Resolume)",
     "https://github.com/jlai/Python-SpoutGL", "BSD 2-Clause"),
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
    ("Chinese-CLIP (OFA-Sys)", "素材内容打标：识别画面里有什么",
     "clip content tagging (what is in the picture)",
     "https://github.com/OFA-Sys/Chinese-CLIP", "MIT"),
    ("Discogs-EffNet (Essentia / MTG)", "曲风识别：400 种 Discogs 风格",
     "genre detection: 400 Discogs styles",
     "https://github.com/MTG/essentia", "CC BY-NC-SA 4.0 (non-commercial only)"),
]

# 来源与致谢：代码或思路的来源项目（MIT 署名要求，须保留）
CREDITS = [
    ("VJVision (ichiryu)", "音乐电池 ChargeBar 决策状态机、音频指纹峰值检测"
     "（本项目 charge_engine.py / fp.py 的来源）",
     "Music Battery (ChargeBar) decision engine & fingerprint peak detection "
     "(origin of charge_engine.py / fp.py)",
     "https://github.com/ichiryu0021/VJVision", "MIT"),
    ("Genre Police Visualizer (lbnandy)", "细分曲风关键词表、按曲风切换视觉的思路"
     "（本项目 genre_keywords.py 的来源）",
     "fine-grained genre keyword table & genre-driven visual switching "
     "(origin of genre_keywords.py)",
     "https://github.com/lbnandy/genre-police-visualizer", "MIT"),
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

