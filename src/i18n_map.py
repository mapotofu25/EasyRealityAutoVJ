# -*- coding: utf-8 -*-
"""UI 文案词表：中文原文 → 英文。

设计说明
--------
本项目历史上 UI 里大量硬编码中文文案（约 335 条），逐个改成 tr("key") 调用
风险高、易漏、且切回中文时容易丢原文。这里改用**以中文原文为 key**的映射：

    i18n.retranslate(root) 遍历控件树 → 取控件当前文本 → 查本表 → setText(译文)

好处：不侵入原代码；切回中文直接把原文写回即可；新增控件只要中文文案一致，
加一条词条就自动生效。

⚠ 本表**只允许放用户可见的界面文案**。
   切勿放入「数据型字符串」——例如 tags_def 的『低动态』会被写进配置并参与
   曲风匹配，翻译它会让匹配失效。标签类双语显示请走 tags_def 的双语表。
"""

# 长文案统一用 Q 前缀（引用时清晰），短词直接用
ZH2EN = {
    "已补齐 {} 个素材的缩略图": "Rebuilt thumbnails for {} clips",
    "已清理 {} 个缓存文件，正在后台重新生成缩略图。":
        "Cleared {} cache files. Thumbnails are being rebuilt in the background.",
    # ==================== 通用按钮 / 对话框 ====================
    "确定": "OK",
    "取消": "Cancel",
    "关闭": "Close",
    "保存": "Save",
    "重置": "Reset",
    "恢复默认": "Reset Defaults",
    "添加": "Add",
    "全选": "Select All",
    "清空": "Clear",
    "应用": "Apply",
    "移除": "Remove",
    "删除": "Delete",
    "重命名…": "Rename…",
    "浏览…": "Browse…",
    "选择文件夹": "Choose Folder",
    "已在运行": "Already running",
    "已恢复默认": "Reset to defaults",

    # ==================== 图层 ====================
    "图层": "Layer",
    "图层名称": "Layer Name",
    "图层类型": "Layer Type",
    "图层角色": "Layer Role",
    "新建图层": "New Layer",
    "重命名图层": "Rename Layer",
    "删除图层": "Delete Layer",
    "图层设置…": "Layer Settings…",
    "手动（自己拖素材）": "Manual (drag clips yourself)",
    "自动（按曲风匹配画面）": "Auto (match visuals by genre)",
    "Kv 主视觉图层（待机层：无声音时切入）":
        "Kv Main Visual (idle layer: fades in when silent)",
    "图层角色（自动匹配按此挑素材）":
        "Layer Role (auto matching picks clips by this)",
    "图层角色（自动匹配按此挑素材）：点击在前景/背景间切换":
        "Layer Role (auto matching uses this): click to toggle Foreground / Background",
    "前景": "Foreground",
    "背景": "Background",
    "前景（特效/覆盖上层）": "Foreground (effects / overlay)",
    "前景（特效/覆盖在上层）": "Foreground (effects / overlay on top)",
    "背景（铺满打底）": "Background (full-frame base)",
    "背景（铺满画面打底）": "Background (full-frame base)",
    "自动图层会随曲风实时替换画面，手动拖入的素材不生效":
        "Auto layers swap visuals to match the genre in real time; manually dragged clips are ignored",
    "Kv 主视觉图层：没有声音时把待机画面带进来、有声音时带出去。\n":
        "Kv Main Visual: brings the idle visual in when silent, takes it out when audio returns.\n",
    "Kv 主视觉图层：没有声音时把待机画面带进来、有声音时带出去。\n全局只能有一个，添加后自动置顶；参数在右侧设置区顶部单独配置。":
        "Kv Main Visual: brings the idle visual in when silent, takes it out when audio returns.\n"
        "Only one is allowed globally and it is pinned to the top when added; "
        "its settings live in the pinned section at the top of the right-hand settings area.",
    "在「{}」分类下新建标签：": "New tag under \"{}\": ",
    "只取左声道做分析。不省 CPU，作用是在左右声道反相时避免低频被平均抵消":
        "Analyse the left channel only. Does not save CPU - it prevents bass from being "
        "averaged away when the two channels are out of phase",
    "当前节奏: -": "Tempo: -",
    "仍然加入（复制模式下会生成 \"名字 (1)\" 的独立新条目）":
        "Add anyway (in copy mode it becomes a separate entry named \"name (1)\")",
    "图层（越靠上显示越靠上）": "Layers (higher = drawn on top)",
    "+ 图层": "+ Layer",
    "显示/隐藏（点亮=显示中）": "Show / Hide (lit = visible)",
    "Solo：只显示本图层": "Solo: show this layer only",
    "Solo（只显示本层）": "Solo (show this layer only)",
    "\n双击重命名": "\ndouble-click to rename",
    "混合模式": "Blend Mode",
    "播放速度": "Playback Speed",
    "保留素材自带透明通道（alpha）": "Keep clip alpha channel",
    "保留素材自带透明通道（点亮=透明区域透出下层；关闭=透明区域填黑）":
        "Keep clip alpha (on = transparent areas reveal lower layers; off = filled black)",
    "锁定画面（不随脉冲/漂移）": "Lock frame (no pulse / drift)",
    "锁定画面（logo/字幕用）：不随能量脉冲缩放、不随氛围漂移":
        "Lock frame (for logos / subtitles): no energy-pulse scaling, no drift",
    "固定播放本层当前素材": "Loop this layer's current clip",
    "无声音时隐藏本层": "Hide this layer when silent",
    "静音时暂停播放": "Pause playback when silent",
    "自动匹配模式（按曲风挑素材）": "Auto match mode (pick clips by genre)",
    "上移（更靠上）": "Move Up",
    "下移（更靠下）": "Move Down",
    "复制素材池到新图层": "Copy clip pool to a new layer",
    "重置本层设置": "Reset this layer's settings",
    "固定为本层素材": "Set as this layer's clip",
    "从本层移除": "Remove from this layer",
    "移到其他图层": "Move to another layer",

    # ==================== 两界面（页签条）====================
    "🎬 演出台": "🎬 Live",
    "🎵 曲库 · 曲风": "🎵 Music · Genres",
    "现场：预览 / 图层 / 效果 / 输出": "Live: preview / layers / effects / output",
    "演出前准备：音乐曲库（导入音乐 + 扫描分析曲风）/ 曲风映射":
        "Pre-show prep: music library (import + genre scan) / genre mapping",
    "双击缩略图 = 立即播放到当前图层；右键可「添加到图层」": "Double-click a thumbnail = play it on the current layer; right-click for \"Add to layer\"",
    "演出前的准备工作都在这里：导入音乐、扫描分析曲风（每首歌的曲风 + 节拍网格），以及编辑「曲风 → 画面标签」的绑定关系。\n素材库在「🎬 演出台」上，演出中随时能取素材、直接拖进图层。":
        "All the pre-show prep lives here: import music, scan & analyze genres (per-track genre + beat "
        "grid), and edit the \"genre → visual tag\" bindings.\nThe media library is on the \"🎬 Live\" "
        "page, so you can grab clips and drag them into layers during the show.",
    "🎵 音乐曲库…": "🎵 Music Library…",
    "曲风映射…": "Genre Mapping…",
    "曲库：已导入 {} 首；其中 {} 首已有节拍网格（八拍乐句对齐要用它）":
        "Library: {} track(s) imported; {} of them have a beat grid (used for 8-beat phrase alignment)",

    # ==================== 素材库 ====================
    "素材库": "Media Library",
    "标签: 全部": "Tag: All",
    "按标签筛选素材（可多选）": "Filter clips by tag (multi-select)",
    "角色": "Role",
    "全部": "All",
    "数量:": "Count:",
    "搜索文件名 / 标签": "Search filename / tag",
    "扫描打标": "Scan & Tag",
    "用本地视觉模型为素材自动打内容标签（后台、可续扫）":
        "Auto-tag clips with a local vision model (background, resumable)",
    "从素材库移除": "Remove from Library",
    "从素材库移除当前素材": "Remove current clip from library",
    "从所有图层移除": "Remove from all layers",
    "预览大小": "Preview Size",
    "查看占用…": "View usage…",
    "清理缩略图缓存": "Clear thumbnail cache",
    "缩略图缓存": "Thumbnail Cache",
    "导入文件夹…": "Import Folder…",
    "导入文件…": "Import Files…",
    "导入素材设置…": "Import Settings…",
    "修改导入方式（复制/移动/仅引用），即使之前勾选了「记住我的选择」":
        "Change import mode (copy / move / reference), even if “remember my choice” was checked",
    "设置素材库目录…": "Set library folder…",
    "素材库目录": "Library Folder",
    "导入素材": "Import Clips",
    "如何导入这些素材？": "How should these clips be imported?",
    "复制到素材库（原文件保留）": "Copy into library (keep originals)",
    "移动到素材库（复制后删除原文件）": "Move into library (delete originals after copying)",
    "仅引用（不复制，保留原路径）": "Reference only (no copy, keep original path)",
    "记住我的选择（以后不再询问）": "Remember my choice (don't ask again)",
    "发现重复素材": "Duplicate Clips Found",
    "替换旧素材（保留原标签/角色，用新文件替换）":
        "Replace old clip (keep its tags / role, swap in the new file)",
    "不添加（保持素材库不变）": "Don't add (keep library unchanged)",
    "导入失败": "Import Failed",
    "添加到图层": "Add to Layer",
    "播放到图层（立即切换）": "Play in layer (switch now)",
    "排除自动打标/匹配（logo 等固定素材）":
        "Exclude from auto-tag / auto-match (logos and other fixed clips)",
    "扫描打标此素材": "Scan & Tag This Clip",
    "编辑标签…": "Edit Tags…",
    "全量重扫打标（含已打标）": "Full re-scan (including already tagged)",
    "播放此素材": "Play this clip",
    "素材大小与位置…": "Clip Size & Position…",
    "从图层移除": "Remove from Layer",

    # ---- 标签编辑对话框 ----
    "自动:": "Auto:",
    "动态:": "Motion:",
    "额外:": "Extra:",
    "三选一（可全不选）；自动识别的会标蓝，改选后以手动为准":
        "Pick one (or none); auto-detected shown in blue, manual choice wins",
    "频闪（亮暗交替）素材；缓和段选材会避开它":
        "Flicker clip (bright / dark alternation); avoided when picking calm-section clips",
    "输入自定义标签，回车或点添加": "Type a custom tag, press Enter or click Add",
    "+ 新建词条": "+ New tag",
    "新建词条": "New Tag",
    "自动识别——取消勾选即排除，重扫不会恢复":
        "Auto-detected — uncheck to exclude; re-scanning won't restore it",
    "已勾选——点一下取消": "Checked — click to uncheck",
    "标签筛选（可多选）": "Tag Filter (multi-select)",
    "勾选要显示的标签（素材含任一选中标签即显示；不选=全部）：":
        "Check the tags to show (a clip appears if it has ANY checked tag; none checked = all):",

    # ==================== 音频 / 音源 ====================
    "🎛 音源": "🎛 Audio",
    "音源设置": "Audio Source Settings",
    "音源类型": "Source Type",
    "系统声音（WASAPI Loopback）": "System Sound (WASAPI Loopback)",
    "系统声音": "System Sound",
    "麦克风": "Microphone",
    "线路输入/声卡": "Line-In / Sound Card",
    "设备": "Device",
    "重新扫描": "Rescan",
    "采样率": "Sample Rate",
    "自动（跟随系统）": "Auto (follow system)",
    "自动=跟随设备原生采样率（避免重采样、能量更准）；手动可强制指定":
        "Auto = device native rate (no resampling, more accurate energy); manual overrides it",
    "单声道分析": "Mono analysis",
    "正在扫描…": "Scanning…",
    "（未找到设备）": "(no device found)",
    "没有扫描到可用设备：请检查连接后点「重新扫描」。":
        "No usable device found — check the connection, then click “Rescan”.",
    "本类型上次使用的设备「{}」已不可用（可能被拔出），请重新选择后点「应用」。":
        "The device used last time for this source type (“{}”) is unavailable "
        "(possibly unplugged). Pick another one and click “Apply”.",
    "未采集": "not capturing",
    "输入电平": "Input Level",

    # ==================== 输出 ====================
    "输出设置": "Output Settings",
    "可直接输入，例如 1600 900（宽 高，空格分隔）":
        "Type directly, e.g. 1600 900 (width height, space-separated)",
    "重置窗口大小": "Reset Window Size",
    "把输出窗口按当前分辨率设置重置（退出全屏/按比例缩放到屏幕内）":
        "Reset the output window to the current resolution (exit fullscreen / scale to fit the screen)",
    "输出显示器": "Output Display",
    "选择后输出窗口会全屏到对应显示器（适合双屏演出）":
        "The output window will go fullscreen on the selected display (for dual-screen shows)",
    "Spout 输出": "Spout Output",
    "把画面实时共享给本机其他程序（OBS 需装 Spout 插件后选本 Sender）":
        "Share frames to other local apps in real time (install the Spout plugin in OBS, then pick this Sender)",
    "Sender 名称（OBS 里显示的名字）": "Sender Name (shown in OBS)",
    "NDI 输出（音画同步）": "NDI Output (A/V synced)",
    "通过网络发送画面+声音（运行时已随软件自带，无需安装；接收端需装 NDI Runtime）":
        "Send video + audio over the network (runtime is bundled - nothing to install; the receiver needs NDI Runtime)",
    "渲染帧率": "Render FPS",
    "GPU 解码（实验）": "GPU Decoding (experimental)",
    "让显卡硬件解压 DXV 素材（帧内 DXT 压缩块直传显存），降低解码 CPU 占用：DXT5 素材约省一半以上、超宽素材约省七成；解压失败或显卡不支持时自动回退软解，不影响播放。默认关闭，建议先试播一轮再上台":
        "Let the GPU hardware-decompress DXV clips (in-frame DXT blocks go straight to VRAM) "
        "to cut decoding CPU: DXT5 clips save about half or more, ultra-wide clips about 70%. "
        "Falls back to software decoding automatically if it fails or the GPU is unsupported - "
        "playback is never affected. Off by default; test a full round of clips before going live",
    "GPU 解码": "GPU Decoding",
    "开启后用显卡硬件解压 DXV 素材，降低解码 CPU 占用；显卡不支持或素材解不了会自动回退软解，不会黑屏。只作用于普通图层，待机层不受影响。已在播的素材要等下次切换才换路径":
        "Decode DXV clips with GPU hardware decompression to cut decoding CPU. Falls back to "
        "software automatically if the GPU or a clip is unsupported - never a black screen. "
        "Applies to normal layers only; the Kv standby layer is unaffected. "
        "A clip already playing switches over on its next switch",
    "这台机器暂时用不了 GPU 解码，已自动回退软解：\n{}":
        "GPU decoding is not available on this machine, fell back to software decoding:\n{}",
    "引擎每秒合成多少次画面；素材多为 25 到 30fps，选 60 时约有一半是把同一帧重复合成 —— 机器吃紧（同时开 VDJ、直播、录制）就选 30，能省约一半渲染 CPU，画面仍跟得上素材":
        "How many frames per second the engine composites. Footage is usually 25-30 fps, so at 60 "
        "roughly half the composites repeat the same frame. If the machine is busy (VDJ + streaming "
        "+ recording), pick 30 - it cuts render CPU about in half and still keeps up with the footage",
    "NDI 不可用": "NDI unavailable",
    "NDI 运行时加载失败，无法启用 NDI 输出。":
        "The NDI runtime failed to load, so NDI output cannot be enabled.",
    "运行时是随软件一起提供的，不需要单独安装。常见原因是杀毒软件隔离了软件目录里的 Processing.NDI.Lib.x64.dll，请把它加入白名单；或者重新解压一次安装包再试。":
        "The runtime ships with the app - there is nothing to install. The usual cause is antivirus "
        "quarantining Processing.NDI.Lib.x64.dll in the app folder; please whitelist it, or unpack the "
        "release package again and retry.",
    "错误详情：": "Error details:",
    "NDI 源名称": "NDI Source Name",
    "NDI 传输音频（48 kHz 立体声）": "Send NDI audio (48 kHz stereo)",
    "把采集到的声音随 NDI 一起发出去（48 kHz 立体声）。默认关闭：接收端若本来就在监听同一路声音，会听到双声/回声；只在接收端需要单独取声音时才打开。":
        "Send the captured sound along with NDI (48 kHz stereo). Off by default: if the receiver is "
        "already monitoring the same sound, you will hear doubling/echo - only enable it when the "
        "receiver needs the audio on its own.",
    "Spout 只传画面，不含音频。需要音频请用 NDI 或虚拟声卡。":
        "Spout carries video only - no audio. Use NDI or a virtual audio device if you need sound.",
    "全局": "Global",
    "仅窗口": "Window only",
    "不保存": "Don't Save",
    "这些全局快捷键没能注册（可能被其它程序占用）：\n{0}\n\n"
    "可以换成带 Ctrl/Alt 的组合键，或把范围改回「仅窗口」。":
        "These global hotkeys could not be registered (another app may already own them):\n{0}\n\n"
        "Try a combination with Ctrl/Alt, or set the scope back to \"Window only\".",
    "快捷键有未应用的修改，要保存吗？": "These hotkey changes are not applied yet. Save them?",
    "把表格里的快捷键/范围/启用写进配置并立即生效":
        "Write the key / scope / enabled columns into the config and take effect immediately",

    # ==================== 切换节奏 ====================
    "素材切换节奏": "Clip Switch Tempo",
    "自动（推荐）": "Auto (recommended)",
    "常规切": "Normal",
    "快切": "Fast",
    "逐拍交替": "Beat Alternation",
    "灵敏度": "Sensitivity",
    "低": "Low",
    "中": "Medium",
    "高": "High",
    "极高": "Extreme",
    "画面振幅强度": "Beat Amplitude",
    "过渡方式": "Transition",
    "跟随模式": "Follow mode",
    "淡入淡出": "Fade",
    "硬切": "Cut",
    "滑动": "Slide",
    "缩放": "Zoom",
    "故障": "Glitch",
    "跟随模式：慢切/常规=淡入淡出，逐拍=硬切":
        "Follow mode: Normal = Fade, Beat = Cut",
    "能量映射": "Energy Mapping",
    "BPM Sync 视频速度": "BPM Sync Video Speed",
    "节拍网格（八拍乐句对齐）": "Beat Grid (8-beat phrase align)",
    "认出曲库里的歌时，用离线算好的八拍网格驱动拍位：素材切换踩在八拍乐句头，变速播放也能对齐。关掉则完全回到原来的实时拍钟。":
        "When a library track is recognized, drive the beat position from the offline 8-beat "
        "grid: clip switches land on 8-beat phrase heads, and it stays aligned even when the "
        "track is pitched. Off = fall back entirely to the original real-time beat clock.",
    "八拍相位翻转": "Flip 8-beat phase",
    "离线网格把「两个小节里哪一个是乐句头」判反时（听感上差 4 拍）点这里。"
    "只作用于当前正在放、且已被识别出来的那首歌，就地生效（不用重扫、不打断已对齐的拍位），"
    "并记进配置，下次放同一首仍然生效。点一下翻转，再点一下翻回来。"
    "怎么听：看预览里的节拍网格线，「◆」应该正好落在音乐换句的地方。":
        "Use this when the offline grid picked the wrong bar as the phrase head (you hear it as "
        "being 4 beats off). It applies to the track that is playing and currently recognized, "
        "takes effect immediately (no re-scan, does not disturb the aligned beat position) and is "
        "saved to the config so it sticks next time the same track plays. Click again to flip back. "
        "How to check: in the preview beat-grid strip, the \"◆\" should land exactly where the "
        "music changes phrase.",
    "没有正在识别的歌": "No track is being recognized",
    "相位 {}": "Phase {}",
    "交替间隔": "Alternation Interval",
    "1/4 拍": "1/4 beat",
    "1/2 拍": "1/2 beat",
    "1 拍": "1 beat",
    "2 拍": "2 beats",
    "4 拍": "4 beats",
    "8 拍": "8 beats",
    "16 拍": "16 beats",
    "一对素材里 A↔B 每隔多久交替一次（默认每 1 拍交替）":
        "How often A↔B alternate within a pair (default: every beat)",
    "每对小节数(换素材周期)": "Bars per Pair (clip cycle)",
    "能量校正": "Energy Correction",
    "伽马校正：>1 压低人声段能量、鼓点段基本不动。本机系统低频增强导致人声段虚高时往右拉":
        "Gamma: >1 lowers vocal-section energy while leaving kicks mostly unchanged. "
        "Turn right if your system's bass boost inflates vocal sections.",
    "实验性：现场系统低频增强导致能量虚高时用。默认 1.0 不校正。":
        "Experimental: use when on-site bass boost inflates energy. Default 1.0 = no correction.",

    # ==================== 颜色渲染 ====================
    "启用颜色渲染": "Enable Color Rendering",
    "给画面整体调色，降低对素材数量的依赖；会让同一批素材反复看也不腻":
        "Grades the whole frame to reduce reliance on clip quantity, so the same clips stay fresh",
    "自动模式：跟随音乐能量自动变色": "Auto mode: follow music energy",
    "关掉后只有点色卡才染色": "When off, color applies only when you click a swatch",
    "色卡（点一下临时锁定该色）": "Swatches (click to lock temporarily)",
    "Bypass：原片 / 调色": "Bypass: Original / Graded",
    "已旁路（显示原片）": "Bypassed (showing original)",
    "一键在「调色版」和「原片」之间闪切（热键也可）":
        "Toggle instantly between graded and original (hotkey works too)",
    "染色强度": "Color Strength",
    "整体浓淡：防止颜色过重掩盖素材细节":
        "Overall intensity: keep color from washing out clip detail",
    "调色方式": "Color Mode",
    "LUT 调色（保留素材色彩）": "LUT grade (keeps clip color)",
    "双色调（最像换了一个素材）": "Duotone (looks like a different clip)",
    "色相轮换时间": "Hue Rotation Time",
    "能量突变强制刷新颜色": "Force color refresh on energy burst",
    "频闪素材": "Flicker clips",
    "休眠（防闪+变色叠加）": "Sleep (avoid flicker + color stacking)",
    "不休眠（照常染色）": "Active (grade as usual)",
    "随机（每次换素材掷一次）": "Random (roll once per clip)",
    "恢复自动变色": "Back to Auto",
    "手动锁定色卡后，点这里立刻回到自动模式":
        "After locking a swatch, click here to return to auto mode",

    # ==================== 后处理 ====================
    "启用后处理": "Enable Post-FX",
    "模式": "Mode",
    "手动": "Manual",
    "全局强度": "Global Strength",
    "整体效果强度：自动挑出的效果都按这个强度为上限":
        "Overall strength: auto-picked effects cap at this value",
    "自动联动（随曲风/音乐触发）": "Auto link (genre / music triggered)",
    "关掉则效果只随能量变化，不跟曲风/事件":
        "When off, effects follow energy only (no genre / events)",
    "触发方式（常驻/曲风/事件）、音频驱动等详细设置":
        "Trigger mode (always / genre / event), audio drive and more",
    "手动模式独立生效：这里的强度就是它自己的数值，":
        "Manual mode is independent: this strength is used as-is, ",
    "触发模式": "Trigger Mode",
    "常驻": "Always",
    "曲风": "Genre",
    "事件": "Event",
    "曲风名，逗号分隔（如 Hardcore, Trance）":
        "Genre names, comma-separated (e.g. Hardcore, Trance)",
    "事件衰减时长": "Event Decay",
    "0.5 秒": "0.5 s",
    "1 秒": "1 s",
    "1.5 秒": "1.5 s",
    "2 秒": "2 s",
    "3 秒": "3 s",
    "4 秒": "4 s",
    "音频驱动（强度随频段能量连续缩放）":
        "Audio Drive (strength scales with band energy)",
    "无": "None",
    "低频": "Low",
    "中频": "Mid",
    "高频": "High",
    "总能量": "Total Energy",
    "强度：0 无效果，100 最强": "Strength: 0 = no effect, 100 = strongest",

    # ==================== 音乐曲库 ====================
    "🎵 音乐曲库": "🎵 Music Library",
    "音乐曲库": "Music Library",
    "导入音乐、扫描分析曲风（演出前准备）":
        "Import music and analyze genres (pre-show prep)",
    "曲风映射": "Genre Mapping",
    "编辑曲风→画面标签的绑定关系": "Edit genre → visual tag bindings",
    "导入音乐文件夹": "Import Music Folder",
    "导入音乐文件": "Import Music Files",
    "逐首建指纹 + 查曲风（后台、增量）":
        "Fingerprint + genre lookup per track (background, incremental)",
    "全量重扫": "Full Re-scan",
    "忽略已有结果，重新分析全部": "Ignore existing results and re-analyze everything",
    "删除全部指纹与自动识别的曲风（可选保留手动设置）":
        "Delete all fingerprints and auto-detected genres (optionally keep manual ones)",
    "纠正曲风…": "Correct Genre…",
    "重新分析扫描此曲目": "Re-analyze this track",
    "此曲目信息": "Track Info",
    "从曲库移除": "Remove from Library",
    "角色（单选）": "Role (single choice)",
    "小": "Small",
    "大": "Large",
    "纠正曲风": "Correct Genre",
    "勾选该曲的曲风（可多选；手动指定优先于自动识别）。点大类展开细分：":
        "Check genres for this track (multi-select; manual overrides auto). "
        "Expand a group to see sub-genres:",
    "曲风 → 画面标签映射": "Genre → Visual Tag Mapping",
    "选一个曲风，在右侧勾选它对应的画面标签。自动匹配时曲风先映射成这些标签，再和素材内容标签求交集挑素材。":
        "Pick a genre, then check its visual tags on the right. Auto matching maps the genre to "
        "these tags, then intersects them with clip tags to pick clips.",
    "删除曲风": "Delete Genre",
    "丢弃自定义映射，恢复软件内置的默认曲风→画面标签表":
        "Discard the custom mapping and restore the built-in genre → tag table",
    "曲风英文名（如 Frenchcore）：": "Genre name in English (e.g. Frenchcore):",
    "新建曲风": "New Genre",
    "清除": "Clear",
    "清除分析数据": "Clear Analysis Data",
    "扫描分析": "Scan & Analyze",
    "曲目信息": "Track Info",
    "重新分析": "Re-analyze",

    # ==================== 输出预览 / 画面设置 ====================
    "输出预览": "Output Preview",
    "画面设置": "Display Settings",
    "预览显示设置：缩放模式 / 刷新率":
        "Preview display settings: scale mode / refresh rate",
    "显示模式/倒计时/BPM/能量浮层":
        "Show mode / countdown / BPM / energy overlay",
    "下一个素材": "Next Clip",
    "角落预看即将切入的素材": "Corner preview of the clip coming up next",
    "预览画面设置": "Preview Settings",
    "预览分辨率": "Preview Resolution",
    "预览": "Preview",
    "显示预览画面": "Show preview",
    "预览显示设置：开关 / 缩放模式 / 刷新率": "Preview display: on/off, scale mode, refresh rate",
    "关掉主界面预览画面。实测省的 CPU 很少（约 0.05 个核，整机不到 1%）——\n它属于「微调」，解决不了卡顿；输出窗口 / Spout / NDI 完全不受影响":
        "Turn off the main-window preview. It saves very little CPU (about 0.05 cores, under 1% of a "
        "16-core machine) - a fine-tuning switch, not a fix for freezing; the output window, Spout and "
        "NDI are completely unaffected",
    "关掉后主界面不再渲染预览。实测省的 CPU 很少（约 0.05 个核）；\n输出窗口 / Spout / NDI 完全不受影响。它解决不了卡顿":
        "The main window stops rendering the preview. It saves very little CPU (about 0.05 cores); the "
        "output window, Spout and NDI are completely unaffected. It will not fix freezing",
    "预览已关闭\n（关掉它省的 CPU 很少，约 0.05 个核；可在「画面设置」里重新打开）":
        "Preview is off\n(it saves very little CPU, about 0.05 cores; turn it back on in \"Display settings\")",
    "「预览刷新率」和「预览分辨率」对 CPU 的影响都很小（60→30 大约省 0.05 个核）；\n关掉上面的「显示预览画面」也就省这么多 —— 它们都是微调，解决不了卡顿。\n真正吃 CPU 的是解码（用「GPU 解码」解决）和后处理特效。\n预览设置只影响主界面预览窗口，不改变输出画面。":
        "Preview refresh rate and resolution barely affect CPU (60->30 saves about 0.05 cores);\n"
        "switching off \"Show preview\" above saves about the same - these are fine-tuning, not a fix for freezing.\n"
        "The real CPU eaters are decoding (solved by GPU decoding) and post-processing effects.\n"
        "Preview settings only affect the main-window preview, never the output.",
    "原始（最清晰）": "Full (sharpest)",
    "1/2（省性能）": "1/2 (lighter)",
    "1/4（最省性能）": "1/4 (lightest)",
    "缩放模式": "Scale Mode",
    "等比（完整显示）": "Fit (whole frame)",
    "铺满（裁切填满）": "Fill (crop to fill)",
    "拉伸（变形填满）": "Stretch (fill, distorted)",
    "预览刷新率": "Preview FPS",
    "60 fps（最流畅）": "60 fps (smoothest)",
    "30 fps（省性能）": "30 fps (lighter)",
    "15 fps（最省）": "15 fps (lightest)",
    "预览分辨率/刷新率越低越省 CPU（预览是独立的实时渲染，不受输出影响）。\n":
        "Lower preview resolution / FPS reduces CPU load "
        "(the preview renders in real time, independent of the output).\n",

    # ==================== 顶部工具条 / 其它 ====================
    "切换亮色/暗色主题": "Toggle light / dark theme",
    "☀ 亮色": "☀ Light",
    "🌙 暗色": "🌙 Dark",
    "高级设置 ▸": "Advanced ▸",
    "高级设置 ▾": "Advanced ▾",
    "检测中…": "Detecting…",
    "（自动切换已停，画面实时）": " (auto switching stopped, preview live)",
    "  [自动]": "  [Auto]",

    # ==================== 占位符模板（供 i18n.Tf 使用）====================
    "清除分析数据": "Clear Analysis Data",
    "已有扫描在进行中，请稍候。": "A scan is already running, please wait.",
    "扫描正在进行中，请稍后再清除。": "A scan is running, please try again later.",
    "将删除全部已建立的指纹与自动识别的曲风。\n音乐文件本身和曲库列表不会被删除。":
        "This deletes all fingerprints and auto-detected genres.\n"
        "Music files and the library list are kept.",
    "保留手动设置过的曲风": "Keep manually set genres",
    "以下 {} 个素材已在素材库中：": "The following {} clip(s) are already in the library:",
    "确定从素材库移除「{}」吗？\n（图层中的该素材会一并移除，标签与摆位也会清除）":
        "Remove “{}” from the library?\n"
        "(It will also be removed from layers; tags and placement are cleared)",
    "确定从素材库移除 {} 个素材吗？\n（图层中的这些素材会一并移除，标签与摆位也会清除）":
        "Remove {} clip(s) from the library?\n"
        "(They will also be removed from layers; tags and placement are cleared)",
    "同时删除本地文件（仅限「素材库」文件夹内的文件）":
        "Also delete local files (only files inside the library folder)",
    "无法删除文件：\n": "Failed to delete file:\n",
    "已清理 {} 个缓存文件，下次显示时会重新生成。":
        "Cleared {} cache file(s); they will be regenerated when displayed next time.",
    "无法复制 {}：": "Failed to copy {}: ",
    "正在扫描素材库… {}/{}": "Scanning library… {}/{}",
    "当前能量：{}　": "Energy: {}  ",
    "锁定为「{}」（10 秒后自动回到自动变色）":
        "Locked to “{}” (back to auto in 10 s)",
    "效果设置 - ": "Effect Settings - ",
    "编辑标签 - ": "Edit Tags - ",
    "素材大小与位置 - ": "Clip Size & Position - ",
    "共 {} 首，已分析 {} 首": "{} track(s), {} analyzed",
    "不透明度 {}%": "Opacity {}%",
    "音源设置\n当前：": "Audio Source Settings\nCurrent: ",
    "当前采集设备: ": "Current device: ",

    # ==================== 顶部工具条 / 状态行 ====================
    "开始自动 VJ": "Start Auto VJ",
    "▶ 开始自动 VJ": "▶ Start Auto VJ",
    "黑场": "Blackout",
    "冻结": "Freeze",
    "暂停自动": "Pause Auto",
    "下一素材": "Next Clip",
    "下一素材 ▶▶": "Next Clip ▶▶",
    "语言": "Language",
    "快捷键": "Hotkeys",
    "关于": "About",
    "使用说明": "User Manual",
    "打开数据文件夹": "Open Data Folder",
    "就绪": "Ready",

    # ==================== 设置分区标题（CollapsibleSection）====================
    "Kv 主视觉图层": "Kv Main Visual",
    "视觉行为模式": "Switching Behavior",
    "效果": "Effects",
    "颜色渲染": "Color Rendering",
    "后处理": "Post-FX",
    "逐拍交替参数": "Beat Alternation",
    "实验性": "Experimental",
    # 2026-09-27 设置面板重组后的新分区标题（7 区 → 4 区，按用途归组）
    "演出行为": "Show Behavior",
    "画面效果": "Visual Effects",
    "性能": "Performance",
    # 「画面效果」里的子标签页（用户反馈：四组堆一起更乱 ⇒ 拆成子标签）
    "基础": "Basics",
    # 工具条「⋯」收纳菜单
    "⋯ 更多": "⋯ More",
    "更多：主题 / 语言 / 快捷键 / 关于": "More: theme / language / hotkeys / about",
    # ==================== 右键菜单（2026-09-27 P4：长条目拆短，说明挪进悬停提示）====================
    # ⚠ 短标题也必须进词表：菜单是用 i18n.retranslate(menu) 遍历替换的，缺了就露中文。
    "Solo": "Solo",
    "自动匹配模式": "Auto Match",
    "锁定画面": "Lock Picture",
    "保留透明通道": "Keep Alpha",
    "上移": "Move Up",
    "下移": "Move Down",
    "播放到图层": "Play to Layer",
    "排除自动打标/匹配": "Exclude from auto-tag/match",
    "全量重扫打标": "Full re-scan & tag",
    "只显示本层": "Show this layer only",
    "按曲风给本层自动挑素材": "Auto-pick clips for this layer by genre",
    "画面不随能量脉冲/漂移": "Picture ignores the energy pulse / drift",
    "保留素材自带的 alpha 通道（透明区域）":
        "Keep the clip's own alpha channel (transparent areas)",
    "更靠上（合成顺序）": "Further up (composite order)",
    "更靠下（合成顺序）": "Further down (composite order)",
    "立即切换，不等下一拍": "Switch immediately, do not wait for the next beat",
    "logo 等固定素材：不参与自动打标与匹配":
        "Fixed clips such as logos: not auto-tagged or auto-matched",
    "含已打标的一起重扫": "Re-scan everything, including already-tagged clips",

    # ==================== Kv 主视觉图层设置 ====================
    "静音判定阈值": "Silence Threshold",
    "电平低于该值视为静音；持续超过「静音触发延迟」后待机层切入":
        "Level below this counts as silence; the idle layer enters after it persists "
        "past “Silence Trigger Delay”",
    "自动检测底噪": "Auto-detect Noise Floor",
    "监听当前环境 3 秒，把阈值设为比底噪高 3dB（防场地设备噪音导致待机层乱切）":
        "Listens to the room for 3 s and sets the threshold 3 dB above the noise floor "
        "(prevents venue gear noise from making the idle layer flap)",
    "静音触发延迟": "Silence Trigger Delay",
    "声音消失后必须持续静音这么多秒，待机层才切入（防半拍停顿导致突兀闪现）":
        "Silence must last this long before the idle layer enters "
        "(avoids an abrupt flash during a half-beat pause)",
    "声音恢复切出延迟": "Audio-Return Exit Delay",
    "检测到声音恢复后，持续有声这么多秒才把待机层切出":
        "After audio returns, it must stay audible this long before the idle layer exits",
    "切入（没声音时把画面带进来）": "Enter (bring the idle visual in when silent)",
    "切入方式": "Enter Transition",
    "淡入（Fade）": "Fade",
    "硬切（Cut）": "Cut",
    "故障（Glitch）": "Glitch",
    "切入时长": "Enter Duration",
    "切出（有声音时把画面带出去）": "Exit (take the idle visual out when audio returns)",
    "切出方式": "Exit Transition",
    "淡出（Fade）": "Fade",
    "缩放（Zoom）": "Zoom",
    "切出时长": "Exit Duration",
    "素材切换速度": "Clip Rotation Speed",
    "Kv 显示期间每隔多少秒换下一个素材；0 = 不轮换（保持当前素材）":
        "While the idle layer shows, rotate to the next clip every N seconds; "
        "0 = no rotation (keep current clip)",
    "待机层：没声音时把画面带进来、有声音时带出去。完全独立，不参与自动切换，也不受其它任何设置影响。":
        "Idle layer: brings a visual in when silent, takes it out when audio returns. "
        "Fully independent — it does not take part in auto switching and is unaffected "
        "by any other setting.",

    # ==================== 颜色渲染色卡 ====================
    "原色": "Original",
    "赛博紫": "Cyber Purple",
    "故障红": "Glitch Red",
    "琥珀金": "Amber Gold",
    "电光粉": "Electric Pink",
    "深海蓝": "Deep Sea Blue",
    "霓虹青": "Neon Cyan",
    "霓虹绿": "Neon Green",
    "LUT：整体调色，细节保留最好（1.7ms/帧）\n双色调：灰度重映射成双色渐变，视觉差异最大，但浅色素材会变海报感":
        "LUT: overall grade with the best detail retention (1.7 ms/frame)\n"
        "Duotone: grayscale remapped to a two-color gradient — the biggest visual change, "
        "but bright clips turn poster-like",
    "高能量染色状态下，每隔多少秒平滑轮换一种颜色（1~60 秒）\n设得很小（1~3 秒）就是快速变色，注意别和频闪素材叠在一起":
        "While strongly colored, smoothly rotate to another color every N seconds (1–60 s)\n"
        "Very small values (1–3 s) mean rapid color cycling — avoid stacking it with flicker clips",
    "遇到带「频闪」标签的素材时，颜色渲染怎么办：\n休眠：颜色停掉，避免画面在闪、颜色同时在变\n不休眠：照常染色（想要颜色一致就选这个）\n随机：每次换到新素材随机决定休眠或染色，整段素材保持不变\n注：手动点色卡锁定的颜色永远优先，不受这一项影响":
        "What should color rendering do with clips tagged “Flicker”:\n"
        "Sleep: stop coloring, so the frame isn't flashing and shifting color at once\n"
        "Active: grade as usual (pick this if you want consistent color)\n"
        "Random: roll sleep/active once per new clip, then keep it for that clip\n"
        "Note: a color locked by clicking a swatch always wins, regardless of this setting",

    # ==================== 后处理：效果名 ====================
    "色差": "Chromatic Aberration",
    "辉光": "Bloom",
    "故障位移": "Glitch Displace",
    "曝光脉冲": "Exposure Pulse",
    "饱和度脉冲": "Saturation Pulse",
    "柔焦": "Soft Focus",
    "拖影": "Trails",
    "颗粒": "Grain",
    "像素化": "Pixelate",
    "径向频谱": "Radial Spectrum",
    "音频反应变形": "Audio Deform",
    "自动：根据曲风和能量自动挑选 2~3 个合适的效果并随高潮增强\n手动：自己逐个开效果、调强度":
        "Auto: picks 2–3 suitable effects by genre and energy, intensifying at peaks\n"
        "Manual: enable and tune each effect yourself",
    # ---- 效果保持（自动模式下多久重挑一组效果）----
    "效果保持": "Effect Hold",
    "实时（跟随能量）": "Live (follow energy)",
    "32 拍": "32 beats",
    "64 拍": "64 beats",
    "自动模式下，选好的一组效果保持多少拍之后再重新挑下一组。\n「实时」= 跟着能量即时变化（能量在门槛附近抖动时，效果会忽有忽无）；\n设成 8~64 拍则每组效果稳定停留一段时间，变化更有节奏感。":
        "In auto mode, how many beats a chosen set of effects stays before a new set is "
        "picked.\n“Live” = follows energy instantly (effects can flicker in and out when energy "
        "hovers near a threshold);\n8–64 beats keeps each set stable for a while, giving the "
        "changes a sense of rhythm.",
    "自动模式按当前曲风挑效果，强度跟着音乐能量连续变化。\n效果强度 = 能量 × 全局强度的 60%（全局强度只决定「多强」，不影响多久换一次效果）。":
        "Auto mode picks effects by the current genre; strength follows music energy "
        "continuously.\nEffect strength = energy × 60% of global strength (global strength "
        "sets how strong, not how often effects change).",
    "自动模式会根据当前曲风挑选效果，强度跟着音乐能量连续变化。\n效果强度 = 能量 × 全局强度的 60%（全局强度只作用于自动模式）。":
        "Auto mode picks effects by the current genre, with strength following music energy "
        "continuously.\nEffect strength = energy × 60% of global strength "
        "(global strength applies to auto mode only).",
    "手动模式独立生效：这里的强度就是它自己的数值，不受上面「全局强度」影响（全局强度只作用于自动模式）。":
        "Manual mode is independent: this strength is used as-is and is not affected by "
        "“Global Strength” above (that applies to auto mode only).",

    # ---- 各效果悬停说明 ----
    "红/蓝通道左右分离，边缘泛紫边（复古镜头感）":
        "Splits red/blue channels horizontally; edges get a purple fringe (retro lens look)",
    "亮部向外扩散柔光，暗场更梦幻":
        "Bright areas bleed into a soft glow; dark scenes feel dreamier",
    "随机横向撕裂 + 色块跳位（数字故障感）":
        "Random horizontal tearing and block displacement (digital glitch look)",
    "整幅画面的亮暗随音乐跳动": "Overall brightness pumps with the music",
    "颜色的浓淡随音乐跳动（高能量更艳）":
        "Saturation pumps with the music (more vivid at high energy)",
    "整体柔化：模糊 + 轻微发光（缓拍段更柔）":
        "Overall softening: blur plus a slight glow (gentler on calm sections)",
    "上一帧的残影叠上来，运动留下拖尾":
        "The previous frame lingers on top, leaving motion trails",
    "叠加噪点，胶片颗粒质感": "Adds noise for a film-grain texture",
    "马赛克方块（像素风）": "Mosaic blocks (pixel-art look)",
    "画面中心叠加 24 段环形频谱：低频橙红→高频青，随音乐跳动":
        "Overlays a 24-band radial spectrum at the center: low frequencies orange-red → "
        "high frequencies cyan, pulsing with the music",
    "画面切成 24 条横带，每条按对应频段强度左右错位（低频错得最多，撕裂/扭动感）":
        "Splits the frame into 24 horizontal bands, each shifted sideways by its band's "
        "energy (low frequencies shift most — a tearing / warping feel)",
    "红/蓝通道左右分离，边缘泛紫边（复古镜头感）\n\n强度：0 无效果，100 最强":
        "Splits red/blue channels horizontally; edges get a purple fringe (retro lens look)"
        "\n\nStrength: 0 = none, 100 = strongest",
    "亮部向外扩散柔光，暗场更梦幻\n\n强度：0 无效果，100 最强":
        "Bright areas bleed into a soft glow; dark scenes feel dreamier"
        "\n\nStrength: 0 = none, 100 = strongest",
    "随机横向撕裂 + 色块跳位（数字故障感）\n\n强度：0 无效果，100 最强":
        "Random horizontal tearing and block displacement (digital glitch look)"
        "\n\nStrength: 0 = none, 100 = strongest",
    "整幅画面的亮暗随音乐跳动\n\n强度：0 无效果，100 最强":
        "Overall brightness pumps with the music\n\nStrength: 0 = none, 100 = strongest",
    "颜色的浓淡随音乐跳动（高能量更艳）\n\n强度：0 无效果，100 最强":
        "Saturation pumps with the music (more vivid at high energy)"
        "\n\nStrength: 0 = none, 100 = strongest",
    "整体柔化：模糊 + 轻微发光（缓拍段更柔）\n\n强度：0 无效果，100 最强":
        "Overall softening: blur plus a slight glow (gentler on calm sections)"
        "\n\nStrength: 0 = none, 100 = strongest",
    "上一帧的残影叠上来，运动留下拖尾\n\n强度：0 无效果，100 最强":
        "The previous frame lingers on top, leaving motion trails"
        "\n\nStrength: 0 = none, 100 = strongest",
    "叠加噪点，胶片颗粒质感\n\n强度：0 无效果，100 最强":
        "Adds noise for a film-grain texture\n\nStrength: 0 = none, 100 = strongest",
    "马赛克方块（像素风）\n\n强度：0 无效果，100 最强":
        "Mosaic blocks (pixel-art look)\n\nStrength: 0 = none, 100 = strongest",
    "画面中心叠加 24 段环形频谱：低频橙红→高频青，随音乐跳动\n\n强度：0 无效果，100 最强":
        "Overlays a 24-band radial spectrum at the center: low frequencies orange-red → "
        "high frequencies cyan, pulsing with the music"
        "\n\nStrength: 0 = none, 100 = strongest",
    "画面切成 24 条横带，每条按对应频段强度左右错位（低频错得最多，撕裂/扭动感）\n\n强度：0 无效果，100 最强":
        "Splits the frame into 24 horizontal bands, each shifted sideways by its band's "
        "energy (low frequencies shift most — a tearing / warping feel)"
        "\n\nStrength: 0 = none, 100 = strongest",

    # ==================== 逐拍交替 / 输出 ====================
    "素材选取": "Clip Picking",
    "顺序": "Sequential",
    "随机": "Random",
    "末尾行为": "End Action",
    "循环": "Loop",
    "反向": "Reverse",
    "分辨率": "Resolution",
    "1080 x 1920 (竖屏)": "1080 x 1920 (Portrait)",
    "1080 x 1080 (方形)": "1080 x 1080 (Square)",
    "显示输出窗口": "Show Output Window",
    "先点「▶ 开始」后再使用输出设置":
        "Press “▶ Start” first, then use the output settings",
    "锁定 16:9": "Lock 16:9",
    "无边框": "Borderless",
    "窗口置顶": "Always on Top",

    # ==================== 素材库 / 混合模式 ====================
    "标签": "Tag",
    "图片": "Image",
    "视频": "Video",
    "导入文件夹": "Import Folder",
    "导入文件": "Import Files",
    "常规": "Normal",
    "减少": "Subtract",
    "屏幕": "Screen",
    "多层": "Multiply",
    "变亮": "Lighten",
    "变暗": "Darken",
    "添加": "Add",

    # ==================== 状态行 / HUD（动态模板，供 i18n.Tf 使用）====================
    "自动": "Auto",
    "当前节奏: {}（{}）": "Tempo: {} ({})",
    "自动换色中（{}，剩余 {} 秒后轮换）": "Auto color cycling ({} · next in {} s)",
    "当前能量：{}　{}": "Energy: {}  {}",
    "  |  还有 {} 拍切换": "  |  {} beats to next switch",
    "  |  距八拍头 {} 拍（{} 秒）": "  |  {} beats ({} s) to next 8-beat phrase",
    "正在扫描… {}": "Scanning… {}",
    "当前：": "Current: ",
    "窗口模式（可拖动）": "Window mode (draggable)",
    "（主）": " (primary)",
    "显示器 {} 全屏{}  {}x{}": "Display {} fullscreen{}  {}x{}",

    # ==================== 颜色渲染状态文案（colorfx 输出，UI 状态行显示）====================
    "能量": "Energy",
    "已旁路": "Bypassed",
    "待机（Kv）不染色": "Idle (Kv) — not colored",
    "已关闭": "Off",
    "手动锁定「{}」（{}s 后回自动）": "Manually locked to “{}” (auto in {} s)",
    "频闪素材，颜色休眠": "Flicker clip — color asleep",
    "手动模式：请点色卡选色": "Manual mode: click a swatch to choose a color",
    "能量过低，颜色渲染已休眠": "Energy too low — color rendering asleep",
    "辅助染色（中低能量）": "Assisted coloring (mid-low energy)",
    "强制染色（高能量）": "Full coloring (high energy)",
    "未找到「使用说明.txt」，请确认它和程序放在同一目录。":
        "“使用说明.txt” was not found. Please keep it in the same folder as the program.",
    "版本信息、第三方组件、反馈渠道":
        "Version info, third-party components, feedback channels",

    # ==================== 快捷键设置表（「设置」列标签）====================
    "开始/停止": "Start / Stop",
    "顺序/随机（逐拍交替）": "Sequential / Random (Beat Alt.)",
    "逐拍交替开关": "Toggle Beat Alternation",
    "强度+": "Intensity +",
    "强度-": "Intensity −",
    "手动切换": "Manual Switch",
    "锁定素材": "Lock Clip",
    "输出全屏": "Output Fullscreen",
    "显示/隐藏界面": "Show / Hide UI",
    "颜色 Bypass（原片/调色）": "Color Bypass (original / graded)",
    "紧急恢复": "Panic Reset",

    # ==================== 打标产生的附加标签（tagger 亮度标签）====================
    "明亮": "Bright",
    "昏暗": "Dim",
    "中等": "Medium",
    "总电平": "Total Level",
    "图层 {}": "Layer {}",

    # ==================== 素材大小与位置 / 预览设置 ====================
    "大小": "Size",
    "水平位置": "Horizontal",
    "垂直位置": "Vertical",
    "旋转": "Rotation",
    "100% = 完整显示（保持比例）；位置为相对画面中心的百分比。\n只作用于当前素材，其它素材不受影响。":
        "100% = fit (keeps aspect ratio); position is a percentage relative to frame center.\n"
        "Applies to this clip only — other clips are unaffected.",
    "预览分辨率/刷新率越低越省 CPU（预览是独立的实时渲染，不受输出影响）。\n预览显示设置只影响主界面预览窗口，不改变输出画面。":
        "Lower preview resolution / FPS reduces CPU load "
        "(the preview renders in real time, independent of the output).\n"
        "These settings affect the main-window preview only — the output frame is unchanged.",
    # ==================== 导入（后台批量） ====================
    "正在导入中，请稍候…": "Import in progress, please wait…",
    "正在扫描素材…": "Scanning clips…",
    "已发现 {} 个素材，正在导入…": "Found {} clips, importing…",
    "导入完成：新增 {} 个，跳过 {} 个（重复或无法识别）":
        "Import finished: {} added, {} skipped (duplicates or unsupported)",
    "　记得点「扫描打标」给新素材打标签":
        "  Remember to click “Scan & Tag” for the new clips",
    "导入出错：{}": "Import error: {}",
    "硬核系":
        "Hardcore family",
    "Hardstyle系":
        "Hardstyle family",
    "浩室系":
        "House family",
    "Trance系":
        "Trance family",
    "Techno系":
        "Techno family",
    "贝斯/回响系":
        "Bass / Dub family",
    "Drum & Bass系":
        "Drum & Bass family",
    "陷阱系":
        "Trap family",
    "车库/碎拍系":
        "Garage / Breakbeat family",
    "氛围/慢节奏":
        "Ambient / Downtempo",
    "电子其他":
        "Other Electronic",
    "嘻哈/R&B/灵魂":
        "Hip-Hop / R&B / Soul",
    "流行/摇滚":
        "Pop / Rock",
    "金属系":
        "Metal family",
    "爵士/古典/世界":
        "Jazz / Classical / World",
    "其他":
        "Other",
    "已选：":
        "Selected: ",
    "+ 新建曲风":
        "+ New Genre",
    "点击取消该标签":
        "Click to remove this tag",
    "（未选，下面勾选画面标签）":
        "(nothing selected — tick tags below)",
    "左侧按大类选一个曲风（每个大类末尾有「+ 新建曲风」），在右侧勾选它对应的画面标签。自动匹配时曲风先映射成这些标签，再和素材内容标签求交集挑素材。":
        "Pick a genre from the groups on the left (each group ends with “+ New Genre”), then tick its visual tags on the right. Auto matching maps the genre to these tags, then intersects them with clip tags to pick clips.",
    "删除当前曲风在映射表里的条目；自定义曲风同时从大类里移除":
        "Remove this genre from the mapping; a custom genre is also removed from its group",
    "在「{}」里新建曲风的英文名（如 Frenchcore）：":
        "New genre name in English for “{}” (e.g. Frenchcore):",
    "纠正曲风 - ": "Correct Genre - ",
    "在「{}」里新建一个曲风（会出现在纠正曲风里）":
        "Add a genre to “{}” (it will also show up in Fix Genre)",
    "退出全屏并按输出分辨率重置窗口大小": "Leave fullscreen and reset the window to the output resolution",
    "🗂 素材库": "🗂 Library",
    "全屏管理素材：搜索 / 筛选 / 导入 / 打标签（演出台那份原样不动）":
        "Manage clips full-screen: search / filter / import / tag (the Live-page copy stays untouched)",
    "这里是全屏版素材库，和「🎬 演出台」上那份是同一批素材（那份保持原样）。\n搜索 / 筛选 / 导入 / 右键打标签都在这里做；双击缩略图 = 播放到当前图层。\n⚠ 「拖进图层」只能在演出台做（跨页面拖拽系统不允许）。":
        "This is the full-screen copy of the media library (same clips as the 🎬 Live page, which stays untouched).\nSearch / filter / import / right-click to tag here; double-click a thumbnail to play it on the current layer.\n⚠ Dragging a clip into a layer only works on the Live page (cross-page drag is not supported by Qt).",
    "排序 / 筛选": "Sort / Filter",
    "排序": "Sort",
    "BPM（按节拍网格）": "BPM (from beat grid)",
    "时长": "Duration",
    "只看文件缺失": "Missing files only",
    "换盘 / 改名之后找不到文件的曲目（修复清单）":
        "Tracks that can no longer be found after moving drives / renaming (repair list)",
    "重置筛选": "Reset filters",
    "BPM / 时长来自曲库扫描时建的节拍网格；「无节拍网格」的曲目"
    "八拍乐句对齐用不了，是需要处理的清单。":
        "BPM / duration come from the beat grid built during the library scan; "
        "tracks without a grid cannot use 8-beat phrase alignment — that is your "
        "to-do list.",
    "未分析": "Not analysed",
    "无网格": "No grid",
    "文件缺失": "File missing",
    "共 {} 首，已分析 {} 首（当前显示 {} 首）":
        "{} tracks, {} analysed (showing {})",
    "筛选后 {} / {} 首": "Filtered: {} / {}",
    # ==================== ASIO 低延迟输入 ====================
    "ASIO（低延迟）": "ASIO (low latency)",
    "不可用：": "Unavailable: ",
    "绕过系统混音器直连声卡驱动，延迟 1~10ms。"
    "⚠ 同一驱动同时只允许一个程序使用 —— VirtualDJ / DAW 占着时会连不上，"
    "此时会自动回退到系统声音。":
        "Bypasses the system mixer and talks straight to the sound-card driver, "
        "latency 1-10 ms. "
        "⚠ A driver can only be used by one program at a time — if VirtualDJ or a DAW "
        "holds it, ASIO cannot connect and the app falls back to system sound automatically.",
    # ==================== 曲风映射编辑器 ====================
    "让这个曲风不再映射到任何画面标签（保存后生效；自定义曲风同时从大类里移除）":
        "Make this genre map to no visual tags (applies after saving; a custom genre "
        "is also removed from its family)",
}


# ============================================================================
# 素材标签（Tag）双语：直接复用 tags_def 里已有的中英对照，批量注入词表。
#
# 关键约束：标签的**中文原值是数据**（写进 config 的 clip_tags、参与曲风匹配），
# 绝不能因为切语言而改变。这里只把「显示名」映射出来 —— UI 显示标签时走本词表，
# 数据层始终用中文原值，所以切换语言不会影响匹配结果。
# ============================================================================
def _inject_tag_labels():
    try:
        from tags_def import (ALL_TAGS, TAG_CATEGORIES,
                              DYNAMIC_LOW, DYNAMIC_MID, DYNAMIC_HIGH, DYNAMIC_FLICKER)
    except Exception:
        return
    for zh, en, _cat in ALL_TAGS:
        ZH2EN.setdefault(zh, en.title())
    for cat, _items in TAG_CATEGORIES:
        ZH2EN.setdefault(cat, CATEGORY_EN.get(cat, cat))
    for zh, en in ((DYNAMIC_LOW, "Low Motion"), (DYNAMIC_MID, "Mid Motion"),
                   (DYNAMIC_HIGH, "High Motion"), (DYNAMIC_FLICKER, "Flicker")):
        ZH2EN.setdefault(zh, en)


CATEGORY_EN = {
    "颜色": "Color",
    "元素特效": "Effects",
    "场景": "Scene",
    "人物生物": "People & Creatures",
    "风格": "Style",
}

_inject_tag_labels()
