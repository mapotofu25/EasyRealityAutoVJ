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

APP_VERSION = BUILD_VER                # 2026.09.23.07 / dev
APP_BUILD = BUILD_DATE or ""           # 2026-09-23
APP_SEQ = BUILD_SEQ                    # 7
APP_ZIP_NAME = ZIP_NAME                # EasyRealityAutoVJ_测试版092307.zip

# 作者与反馈渠道
AUTHOR = "麻婆豆腐"
CONTACT = "QQ 1793170435"
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
    ("能量算法", "Energy engine",
     "响度 / 低频冲击 / 起伏量多信号融合，扛得住系统音效与压缩",
     "Fuses loudness, low-frequency punch and fluctuation — robust against "
     "system audio effects and compression"),
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
    ("Genre Police AutoVJ (lbnandy)", "「演出前分析曲库 + 现场识别曲目 + Spout / NDI 输出」"
     "的整体思路参考",
     "overall reference: pre-show library analysis + live track ID + Spout / NDI output",
     "https://github.com/lbnandy/genre-police-autovj", "MIT"),
]

# 兼容旧引用（与 PROJECTS 同源的简表：名称, 许可）
COMPONENTS = [(p[0], p[4]) for p in PROJECTS]

