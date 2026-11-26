# Easy Reality AutoVJ 1.3 正式版

**把音乐变成画面的实时 VJ 引擎。** 它用音频指纹认出正在放的歌，按提前做好的码表，以及曲库预分析的节拍和能量段落，自动决定画面。素材切换、跟拍、跟情绪都由它完成——DJ 只管放歌，你不用整场盯着电脑点下一首。

**EasyReality AutoVJ** ｜ real-time auto-VJ for live shows (Windows). It recognizes the playing track by audio fingerprint, then follows your cue sheet together with the pre-analyzed beat grid and energy sections to drive multi-layer clip switching, transitions and live effects. Output goes to OBS / Resolume via **NDI / Spout**, with DXV GPU decoding, alpha and per-screen multi-output.

关键词：AutoVJ · 自动 VJ · 现场 VJ · 音乐可视化 · 音频指纹认歌 · 码表 · 节拍网格 · 能量段落 · NDI · Spout · OBS · DXV · GPU 解码 · 多屏输出。

## 下载（三个包）

到 **[Releases](../../releases/latest)** 下载。三个包分工如下：

| 包 | 内容 | 跑在哪台机器 |
|---|---|---|
| `EasyRealityAutoVJ_1.3正式版.zip` | 演出主程序：认歌 → 按码表 / 预分析段落自动切素材 → 输出 | 演出机（B 机） |
| `EasyRealityCue_1.3正式版.zip` | 码表编辑器：逐首编「第几拍放哪个素材、什么转场」，导出 `.ercue` | 编排机 / DJ 机 |
| `EasyReality_Tools_1.3.zip` | ERLIB 曲库分析 + 音频发送端 + NDI 接收自检 + 音源诊断 | DJ 机 / 放歌机 / 任意排查机 |

工具集里：

- **ERLIB曲库分析**：扫曲库、导出 `.eravjlib`（封面 / 指纹 / 节拍网格 / 能量段落），给演出机认歌用。
- **音频发送端**：B 机没有歌文件时，把 A 机的系统声音实时送过去。
- **NDI接收自检**：确认本机能不能收到 NDI 画面，连上后存截图。
- **音源诊断**：查看系统声音 / 麦克风电平，判断到底有没有声音进来。

**怎么配合**：ERLIB 扫曲库 → EasyRealityCue 编码表（`.ercue`）→ 演出机导入码表和素材，靠指纹认出 DJ 正在放的歌，按码表 **精确到拍** 切素材；同时读取曲库里预分析的节拍网格和能量段落，进 drop 当帧换素材池。演出机不需要有歌文件。B 机没有歌时再用音频发送端送声音。

解压后双击 `EasyRealityAutoVJ.exe` 即可，无需安装 Python。配置、素材库和码表数据都在 `%LOCALAPPDATA%\AutoVJ`，升级覆盖不会清空。

## 四步上手

1. **选音源**：系统声音（WASAPI 回环）/ 麦克风 / 线路输入 / ASIO / 局域网音频。看电平表有没有动。
2. **扫曲库**：音乐曲库 → 导入音乐或曲库分析包 → 扫描。生成指纹、节拍网格和能量段落。
3. **导素材、打标**：素材库导入视频/图片 → 扫描打标（动态、频闪、铺满度、前景/背景）。图片和 GIF 不扫描，直接当静态素材用。
4. **开始**：点开始（Ctrl+Space）。黑场、冻结、下一素材、手动接管随时可用。

详细步骤见发行包里的 `使用说明.txt` / `使用说明_EN.txt`。

## 功能

### 认歌与跟随

- 音频指纹认出正在播放的曲目；演出机不需要有歌文件。
- 读取曲库预分析的 BPM、八拍网格和能量段落；进 drop 的那一帧换素材池，不用等能量慢慢爬。
- 歌中途变速也跟得住；没被识别到的曲目回退到实时能量分析。
- 音频输入：WASAPI 系统声音、麦克风 / 线路输入、ASIO（低延迟）、局域网音频。ASIO 被 VirtualDJ / DAW 占用时自动回退到系统声音并提示。

### 自动选素材

- 素材按动态强弱、前景/背景、铺满度打标；高动态留给 drop，背景铺底，前景叠加。
- 自动 / 常规切 / 快切 / 手动四种切换节奏；切换对齐小节线，无鼓点不切。
- 5 种过渡：硬切 / 淡入 / 滑动 / 缩放 / 故障。
- 强度、能量映射、防重复冷却可调。

### 图层与 KV

- 图层不限数量：添加 / 删除 / 排序 / 重命名 / 显示 / 不透明度 / 独立素材池。
- **KV 主视觉图层永远置顶**：没声音时带进待机画面，有声音时带出，任何操作都不会把它压到下面。
- 固定播放、快速模式、无声音时隐藏图层。

### 逐拍交替

- 独立开关（默认关），只作用于**前景层**，只在**能量段落 tier=high 或名字含 drop** 的段落里触发；没有段落表不触发。
- 段内默认 1 拍 A↔B，换段才换对子；drop 段内暂停该层常规切换。
- 多屏按屏独立参数；面板会显示作用范围，并警告该屏没有前景层。
- 快捷键 Ctrl+T 随时开关。

### 输出

- 独立输出窗口，OBS 窗口捕获友好；Ctrl+F 全屏。
- **NDI / Spout** 直出，OBS、Resolume 里直接选，不用采集卡；需要透明时开启 Alpha。
- 多屏输出：每屏独立显示、模式、分辨率和裁切。
- DXV 素材 GPU 解码，4K 不靠 CPU 硬扛；不支持时自动回退，不黑屏。

### 现场可控

- 黑场、冻结、暂停自动、下一素材、锁定素材、强度调节、手动接管。
- 颜色渲染：调色板 / 强度 / 色相 / 能量映射，Ctrl+Shift+C 一键对比原片。
- 后处理：辉光、颗粒、色差、故障位移、拖影、径向频谱、曝光脉冲等。
- 多屏输出、KV、调色、逐拍交替互不干扰。

### 素材准备

- 文件 / 文件夹导入，复制 / 移动 / 仅引用三种方式；自动缩略图、标签筛选、播放高亮。
- 扫描打标：超宽素材只提醒不转码；需要时转 DXV（无 Alpha）或 ProRes 4444（有 Alpha）。
- 悬浮预览：视频悬停显示 10 帧胶片；缓存按源文件 mtime + size 失效。
- 码表页：导入 `.ercue` 包、绑定图层、查看片段；删除时保留工程文件。
- 音乐曲库：BPM / 节拍网格 / 能量段落、波形与分段编辑、缺失文件清单。

### 易用性

- 中英文界面、亮/暗主题、快捷键面板（含作用域设置）。
- “关于”页可打开数据文件夹；日志可一键打包导出，方便反馈。

## 从源码运行

需要 **Python 3.11+**（Windows）：

```bat
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
venv\Scripts\python.exe src\main.py
```

**模型文件**（体积较大）不在仓库内。首次运行前把模型放到：

```text
assets/models/beat_this/       # Beat This! 小节检测（MIT，约 10 MB，small0_fixed.onnx）
```

或直接下载 Releases 里的发行包，里面已包含。

## 打包 / 构建

```bat
venv\Scripts\python.exe tools\build_exe.py      :: PyInstaller 单目录打包 → dist\EasyRealityAutoVJ\
venv\Scripts\python.exe tools\smoke_exe.py 40   :: 40 秒冒烟测试
venv\Scripts\python.exe tools\make_zip.py       :: 生成发行压缩包
```

## 目录结构

```text
├─ src/                 源码
│   ├─ main.py          入口
│   ├─ ui_main.py       主界面（演出台 / 素材库 / 音乐曲库 / 码表）
│   ├─ engine.py        渲染引擎（多图层合成 / Spout / NDI 输出）
│   ├─ audio_engine.py  音频采集与实时分析（BPM / 拍位 / 能量 / 指纹认歌）
│   ├─ beatgrid.py      节拍网格：BPM / 锚点 / 八拍乐句相位
│   ├─ phrase_bt.py     小节识别（Beat This!）
│   ├─ tagger.py        素材打标（动态 / 频闪 / 铺满度 / 角色）
│   ├─ preview_hover.py 悬浮胶片预览
│   ├─ proxy_plan.py    转码计划（DXV / ProRes / 超宽检测）
│   ├─ scan_core.py     素材扫描核心
│   ├─ media_manager.py 素材解码（含 GPU 解码）
│   ├─ fp.py            音频指纹识别
│   ├─ glctx.py         OpenGL 上下文（GPU 解码）
│   ├─ panels.py        设置面板与子面板
│   ├─ output_window.py 独立输出窗口
│   ├─ ndi_out.py       NDI 输出
│   ├─ lan_audio*.py    局域网音频（自研 UDP 协议）
│   ├─ wasapi_capture.py WASAPI 回环采集
│   ├─ imported_lib.py  导入的曲库包在演出机上的落地存储
│   └─ erlib/           曲库分析包（`.eravjlib` 格式层 + 扫描核心）
├─ tools/               构建脚本与自检工具
├─ assets/              UI 资源；模型放 assets/models（不入库）
├─ EasyRealityCue/      码表编辑器源码
├─ docs/                发布说明与架构文档
├─ NOTICE.md            第三方组件与许可说明
└─ LICENSE              MIT
```

## 隐私

发布包只包含程序、运行库、使用说明和第三方许可，**不包含**：

- 用户配置、日志、崩溃记录；
- 素材库索引、指纹数据库、缩略图、预览缓存；
- 码表工程或任何用户素材。

## 许可 / License

- **源代码**：**MIT**（见 [LICENSE](LICENSE)），可自由使用、修改、分发。
- **打包发行版**：唯一随包的模型是 `Beat This!`（**MIT**），可商业使用。
  ⚠ 发行包内静态打包了 **`mutagen`（GPL-2.0-or-later）**；按 GPL 的通常解释，
  **二进制发行包整体应按 GPL-2.0-or-later 分发**（源码仍为 MIT，本仓库公开可取）。
  详见 [NOTICE.md](NOTICE.md)。
- **名称与标识**：「Easy Reality AutoVJ」不随代码授权，请勿用于发布衍生版本。

The source code is licensed under **MIT**. Packaged releases bundle the `Beat This!` model
(**MIT**) and statically bundle **`mutagen` (GPL-2.0-or-later)**; following the usual
reading of the GPL, the binary release as a whole should be treated as
**GPL-2.0-or-later** (the matching source is this repository).

## 致谢

- **[VJVision](https://github.com/ichiryu0021/VJVision)**（ichiryu，MIT）——音乐电池 ChargeBar 决策状态机、音频指纹峰值检测。
- **[KVJ / KDJ](https://github.com/kumoSleeping/KVJ)**（kumoSleeping，GPL-3.0）——变速识别与认歌判据；仅参考思路，未取用代码。
- **[Genre Police AutoVJ](https://github.com/lbnandy/genre-police-autovj)**（lbnandy，MIT）——演出前分析曲库 + 现场识别 + Spout / NDI 输出的整体思路。

小节检测使用 **Beat This!** 模型（MIT）。完整署名与许可见 [NOTICE.md](NOTICE.md)。

反馈 / 交流：QQ 群 **ERAVJ测试**（543643838）
项目：https://github.com/mapotofu25/EasyRealityAutoVJ
