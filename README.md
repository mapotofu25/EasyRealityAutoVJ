# Easy Reality AutoVJ

**面向现场演出的 Windows 自动 VJ 软件** —— 实时分析正在播放的音乐，自动完成多图层素材切换、过渡与实时特效，并通过 **Spout / NDI / 窗口捕获**输出给 OBS 等导播软件。

> **Realtime auto-VJ for live shows (Windows).** It analyses the music as it plays and
> drives multi-layer clip switching, transitions and live effects, then outputs via
> Spout / NDI / window capture.

---

## 下载 / Download

到 **[Releases](../../releases/latest)** 页面下载最新发行包
（GitHub 上的附件名会把中文去掉，形如 `EasyRealityAutoVJ_092615.zip`，就是它），
解压后双击 `EasyRealityAutoVJ.exe` 即可，**无需安装 Python**。

⚠️ 发行包内包含第三方模型（详见 [NOTICE.md](NOTICE.md)），其中 **Discogs-EffNet 为
CC BY-NC-SA 4.0，仅限非商业使用**。个人演出 / 学习使用完全没问题。

---

## 功能

### 界面
主区分**三页**，共用同一套引擎与同一批素材：

| 页面 | 用途 |
|---|---|
| **🎬 演出台** | 演出时用。图层、素材库、播放控制都在这一页 —— **拖素材进图层只在这里能拖**（跨页面拖拽系统不允许）。 |
| **🗂 素材库** | 演出台那份的**全屏副本**：同一批素材、共用搜索/筛选状态，专门给「导入 + 打标签 + 整理」用。**演出台那一栏原样不动**，演出中照样能取素材。双击缩略图 = 播放到当前图层。 |
| **曲库·曲风** | 演出前的准备工作：导入音乐、扫描分析曲风（每首歌的曲风 + 节拍网格）、编辑「曲风 → 画面标签」的绑定。左侧带**排序 / 筛选**。 |

**曲库的排序 / 筛选**（左侧栏）：
- **排序**：原顺序 / 曲名 / 艺人 / BPM 从低到高 / BPM 从高到低 / 时长从短到长 / 时长从长到短
- **筛选**：BPM 区间（全部 / 无节拍网格 / <100 / 100–128 / 128–150 / 150–180 / ≥180）、
  时长（全部 / 时长未知 / <3 分钟 / 3–5 / 5–7 / ≥7 分钟）、**只看文件缺失**、重置筛选
- BPM / 时长来自曲库扫描时建的**节拍网格**；「无节拍网格」与「文件缺失」都是**待处理清单**
  （前者用不了八拍乐句对齐，后者是换盘/改名后找不到文件的曲目）。

### 音频输入
- **WASAPI Loopback（系统声音，默认）**、麦克风、线路输入 / 声卡设备
- **ASIO（低延迟）** —— 绕过系统混音器直连声卡驱动，实测本机 48000 Hz / 缓冲 528 帧
  ≈ **11 ms** 输入延迟。⚠ ASIO 驱动**同时只允许一个程序使用**：VirtualDJ / DAW 占着时
  会连不上，此时软件会**自动回退到系统声音**并提示（演出不会因此没声音）。
  ⚠ ASIO 的采样率是驱动全局的，本软件一律沿用驱动当前值、不去改它。
- 设备选择、电平表、静音提示

### 实时分析
- **BPM**（众数投票 + 锁定；界面显示值驱动拍钟的取值解耦）
- 拍位与小节（匀速节拍钟 + 强拍锁相），并识别小节的第 1 拍
- 低 / 中 / 高频能量、低频冲击密度、Drop 检测
- 能量综合（响度 / 低频冲击 / 起伏量多信号融合），驱动自动切换与实时特效

### 素材与图层
- 文件 / 文件夹导入、自动缩略图、文件夹即标签、标签筛选、播放高亮、自动循环
- 图层不限数量：添加 / 删除 / 排序 / 重命名 / 显示 / 不透明度 / 独立素材池
- 固定播放、快速模式（单图层）、无声音时隐藏图层
- **Kv 主视觉图层**：没声音时把待机画面带进来、有声音时带出去（视觉独立）

### 自动 VJ
- **3 种行为模式**（按能量自动选择，也可手动强制固定）：**常规切** / **快切** / **逐拍交替**
- 切换间隔固定：常规切 **16 拍**（4 个 4/4 小节）、快切 **8 拍**（2 个小节）；切换时机对齐小节线
- 无鼓点时不切换；能量映射、防重复冷却
- 5 种过渡效果（硬切 / 淡入 / 滑动 / 缩放 / 故障）
- BPM Sync 视频速度、**颜色渲染**（一键调色，LUT 实现）

### 逐拍交替
- A B A B 每拍交替、顺序 / 随机选取、随机池防重复
- 触发条件（始终 / 高能 / Drop）、每对小节数可调、可限定图层

### 手动干预
- 黑场、冻结、暂停自动、**下一素材**、锁定素材、强度调节

### 输出
- 独立输出窗口（标题 `Easy Reality Output`，OBS 窗口捕获友好）
- **Spout**、**NDI** 输出
- 多分辨率：720p / 1080p / 竖屏 / 方形，宽高比锁定，记忆窗口尺寸

### 性能
- **GPU 解码**：DXV / DXT 压缩纹理直传 GPU 硬件解压，降低解码 CPU 占用（仅对 DXV 编码的素材生效）
- GPU 超时自动降级 + 自动恢复（画面不中断）
- 渲染帧率可调（60 → 30 约省一半**渲染**开销）；预览窗口可关

### 易用性
- 中英文界面切换、快捷键面板（含冲突检测）
- 素材导入方式可选：复制 / 移动 / 仅引用

---

## 从源码运行

需要 **Python 3.11+**（Windows）：

```bat
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
venv\Scripts\python.exe src\main.py
```

也可以直接双击 **`运行AutoVJ.bat`**（用内置 venv 跑源码，改动即时生效）。

**模型文件**（体积较大）不在仓库内。首次运行前请把模型放到：

```
assets/models/chinese_clip/    # Chinese-CLIP（MIT）
assets/models/discogs/         # Discogs-EffNet（CC BY-NC-SA 4.0）
```

或直接下载 [Releases](../../releases) 里的发行包，里面已包含。

---

## 打包 / 构建

```bat
venv\Scripts\python.exe tools\build_exe.py      :: PyInstaller 单目录打包 → dist\EasyRealityAutoVJ\
venv\Scripts\python.exe tools\smoke_exe.py 40   :: 40 秒冒烟测试（进程存活且无 startup_error.log）
venv\Scripts\python.exe tools\make_zip.py       :: 生成发行压缩包
```

> 若开发机已在运行本程序，冒烟测试请加 `--isolated`：默认模式会结束所有同名进程。

---

## 开发中 / Roadmap

- [ ] **节拍网格的实时应用**：目前已在**曲库扫描时离线**算出每首歌的 BPM、锚点与
      八拍乐句相位并入库（数据源可自动读取 VirtualDJ 已分析的结果，见 `src/beatgrid.py`）；
      **「演出中查表驱动切换」还在开发**，尚未接入引擎。
- [ ] 曲库外音乐（未被识别到的曲目）的实时八拍相位估计。
- [ ] GitHub Actions 自动发布。

---

## 目录结构

```
├─ src/                 源码
│   ├─ main.py          入口
│   ├─ ui_main.py       主界面（三页：演出台 / 素材库 / 曲库·曲风）
│   ├─ engine.py        渲染引擎（多图层合成 / Spout / NDI 输出）
│   ├─ audio_engine.py  音频采集与实时分析（BPM / 拍位 / 能量 / 指纹认歌）
│   ├─ beatgrid.py      节拍网格：BPM / 锚点 / 八拍乐句相位（离线计算）
│   ├─ phrase_bt.py     小节识别（Beat This!，离线；只判到 4 拍小节）
│   ├─ media_manager.py 素材解码（含 GPU 解码）
│   ├─ fp.py            音频指纹识别
│   ├─ glctx.py         OpenGL 上下文（GPU 解码）
│   ├─ panels.py        设置面板与子面板
│   ├─ output_window.py 独立输出窗口
│   └─ ndi_out.py       NDI 输出
├─ tools/               构建脚本与自检工具
├─ assets/              UI 资源；模型放 assets/models（不入库）
├─ CONTRIBUTING.md      贡献指南（**提 PR 前请先读**）
├─ NOTICE.md            第三方组件与许可说明（**请务必阅读**）
└─ LICENSE              MIT
```

---

## 许可 / License

- **源代码**：**MIT**（见 [LICENSE](LICENSE)）—— 可自由使用、修改、分发。
- **打包发行版**：内含 `Discogs-EffNet`（**CC BY-NC-SA 4.0**），因此
  **发行包仅限非商业用途**。
- **名称与标识**：「Easy Reality AutoVJ」不随代码授权，请勿用于发布衍生版本。
- 第三方署名与完整说明见 **[NOTICE.md](NOTICE.md)**。

The **source code** is licensed under **MIT**. However, packaged releases bundle
`Discogs-EffNet` (**CC BY-NC-SA 4.0**), so **binary releases are non-commercial only**.
See [NOTICE.md](NOTICE.md) for full third-party attributions.

---

## 致谢

本项目参考或改写了若干优秀的开源项目（VJVision、Genre Police Visualizer、
Genre Police AutoVJ），并使用了 Chinese-CLIP、Discogs-EffNet 等模型。
完整署名与许可见 [NOTICE.md](NOTICE.md)。

反馈 / 交流：QQ 群 **ERAVJ测试**（543643838）
