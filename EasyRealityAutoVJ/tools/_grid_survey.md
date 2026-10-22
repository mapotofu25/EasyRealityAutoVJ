# 节拍网格 / 八拍乐句 / 变速同步 —— 外部方案调研

> 调研员：grid-researcher　日期：2026-09-27
> 目的：为「离线乐句网格 / 实时兜底 / 变速同步」三条目标找可借用的现成方案。
> 约束：纯 Python + numpy，不装新依赖；实时部分不能做整曲 STFT。
> **本文件只做调研，不改任何生产代码。**

---

## 0. 结论先行（TL;DR）

### 0.1 一个必须先讲清的概念错位

业界说的 **"phrase（乐句）" ≠ 我们说的 "8 拍"**。这是本次调研发现的最大坑：

| 说法 | 拍数 | 小节数 | 出处 |
|---|---|---|---|
| 1 bar（小节） | 4 拍 | 1 | 通用 4/4 |
| VirtualDJ "Force downbeat **1:4**"（bar） | 4 拍 | 1 | VDJ BPM Editor 手册 |
| VirtualDJ "Force downbeat **1:16**"（phrase） | **16 拍** | 4 | 同上 |
| rekordbox Phrase Analysis 的 phrase | 若干小节（典型 8 或 16 小节） | 8/16 | rekordbox Phrase Edit Guide |
| 我们项目的「八拍乐句」 | **8 拍** | 2 | 项目内部定义 |

也就是说：**VDJ 的 phrase 是 16 拍，rekordbox 的 phrase 是 8~16 小节（32~64 拍），我们的「八拍乐句」比所有人都细一档。**

→ 这件事其实是**好消息**：我们的目标其实更简单。见 §E.1。

### 0.2 三条目标的最优路线（一句话版）

| 目标 | 结论路线 | 依据 |
|---|---|---|
| **离线乐句网格** | 不用做「结构分析」。**8 拍相位 = 下拍相位 + 固定周期假设**：只要拿到干净的 downbeat 相位（每 4 拍），八拍头就是"每隔一个小节头"。我们现有 `src/beatgrid.py` 的 `phrase_phase()`（在 {0,4} 里二选一）思路已经是对的，缺的是**下拍相位本身的稳健性**。 | §C.2 概念拆解；§A rekordbox/VDJ 也是 bar 级 |
| **实时兜底** | 纯 numpy 可行：**低频起音包络 → 相位折叠定 BPM → 梳状/DP 平滑定拍位 → 相位直方图分桶定八拍相位**。业界纯 DSP 在线方案就是这个量级（aubio / IBT），够"兜底"。**不要**指望在线做下拍/乐句，那是学术难题（在线 downbeat F1 只有 ~0.46）。 | §B aubio/IBT；§C.3；§C.1 精度表 |
| **变速同步** | **网格表达改用「每拍时间戳数组 / BPM 分段折线」，而不是「BPM + 单锚点」**（跟 Mixxx Beat Map 一致）。变速时：用「已播音频时间 × 速率 r → 原曲时间 → 查表 → 再 [/r] 回演出时间轴」。r 的来源优先用**在线 BPM 估计比值**，其次用**指纹散点斜率**反推。 | §B Mixxx；§D.1 §D.3 |

### 0.3 值得借用 / 不要碰

| 方案 | 收益 | 投入 | 依赖风险 | 能否纯 numpy |
|---|---|---|---|---|
| **8拍=下拍相位+固定周期** 的思路 | 高（省掉整块结构分析） | 低 | 无 | ✅ 已经是我们的做法 |
| **Mixxx 的 Beat Map 数据模型**（可变 tempo = 每拍显式时间戳） | 高（变速天然支持） | 低 | 无（只是数据表达） | ✅ 纯数据结构 |
| **Beat This! 的 ONNX 模型**（离线，已含 downbeat） | 中高（离线一键出 beat+downbeat） | 中 | 中（需 onnxruntime——**我们已有**；但要自己写 mel 前处理） | ⚠️ 非纯 numpy，但 onnxruntime 已在依赖里 |
| **指纹散点斜率反推变速倍率** | 高（直接拿到 r） | 中 | 无 | ✅ 纯 numpy |
| **相位直方图分桶定 8 拍相位**（我们现有） | 中 | 已投入 | 无 | ✅ |
| madmom（DBN 下拍） | 高精度 | 高 | **高：需 scipy/cython；模型 CC BY-NC-SA 非商用** | ❌ |
| librosa / BeatNet / Essentia | — | 高 | **高：numba/scipy/torch/tensorflow；AGPL/GPL** | ❌ |
| aubio | 实时拍位 | 中 | **GPLv3 + 2019 起停更** | ❌ |
| 完整音乐结构分析（MSA / Segmentino / RefraiD） | 低（我们只需要 8 拍，不需要段落） | 高 | 高 | ⚠️ 不值得 |

---

## A. 商业 DJ 软件怎么做网格

### A.1 VirtualDJ

| 项 | 事实 | 来源 |
|---|---|---|
| CBG | Computer Beat Grid，4/4 拍的小节指示；网格线是纯数学：`t = anchor + n × beatLen` | [VDJ6 User Guide PDF](http://www.virtualdj.com/documents/Manuals/VirtualDJ6-UserGuide.pdf) |
| Rigid BPM | 单一固定 BPM，锚点左侧网格**锁死不可调**，只有锚点右侧可调 | [BPM Editor 手册](https://www.virtualdj.com/manuals/virtualdj/editors/bpmeditor.html) |
| Fluid BPM | 曲中有多处变速时选它；VDJ **自动**加多个 beatgrid anchor，**每个 anchor 带自己的 BPM**；`fluidAnalysis=auto` 时自动切到 Fluid | 同上 |
| 半速/倍速 | `/2`、`x2` 按钮；"BPM 是真实值的一半或两倍时用" | 同上 |
| Downbeat | 可前后移 1 拍；右键锚点可 **Force downbeat 1:4（bar）** 或 **Force downbeat 1:16（phrase=16 拍/4 小节）** | 同上 |
| AltBpm | 手册**未解释**该字段。我们的 `tools/vdj_grid.py` 观察它"通常是 1.5×Bpm（即 2/3 BPM 的半速候选）" —— **属项目观察，非官方文档** | 项目注释 |
| 自动段落 | 分析时自动写 Remix Points：`Break 1` / `Break 2` / `End Break 1` 等；POI Editor 可改 | [DJTechTools 2018 评测](https://djtechtools.com/2018/11/12/review-virtualdj-2018-outmatches-serato-dj-pro-traktor-pro-3-in-innovation)、[VDJ 论坛](https://de.virtualdj.com/forums/244124/General_Discussion/Remix_Points%3A__automatically_analyzed_by_VirtualDJ_.html) |
| POI 类型 | beatgrid anchor / hotcue / saved loop / **remix point（break）** / automix point / load point / action | [POI Editor 手册](https://it.virtualdj.com/manuals/virtualdj/editors/poieditor.html) |
| 变速 | Fluid 模式用多锚点表达；Rigid 不表达 | BPM Editor 手册 |

**结论**：VDJ 自动分析到 **break（段落）级**；"phrase" 概念只存在于手动 `Force downbeat 1:16`（=16 拍），**没有自动 8 拍乐句**。变速用「多锚点 + 每锚点 BPM」表达。

### A.2 Serato DJ

| 项 | 事实 | 来源 |
|---|---|---|
| Beatgrid marker 种类 | **Tempo marker（红，可多处，最多 128 个）** / Bar marker（带小节号） / Beat marker | [Serato Sample: Beatgrids](https://support.serato.com/hc/en-us/articles/360002025835) |
| 多锚点 | 用 ≥2 个 Tempo marker 可**只拉伸/压缩某一段**网格 | 同上 |
| Beat Warp Marker | 变速处手动放新红色 marker，"网格从该 marker 起被 warp，跟住变速"；可用任意多个 | Serato DJ / DJ Lite 用户手册（[DE](https://serato.com/downloads/files/135621/Serato%20DJ%20Software%20Manual%20German.pdf)） |
| Lucid Beatgrids（DJ 5.0 Beta） | 全新分析引擎："improved downbeat detection" + **首次支持 automatic tempo tracking**；**Flexible Beatgrid Analysis 为默认**，把 tempo 变化整体映射到全曲、marker 放在真实拍位；可把 BPM 变化分辨率设为 **bar 或 beat**（默认 bar）；有 `Set Grid Start` / `Set Beat`（单拍微调） | [The Drop: Lucid Beatgrids](https://the-drop.serato.com/announcements/introducing-lucid-beatgrids-in-serato-dj-5-0) |
| Beat Jump | 按设定长度（1/32…64 拍）前后跳，量化到网格 | Serato 手册 |

**结论**：Serato 的**动态网格最成熟**（多 Tempo marker + Warp + 可选 beat 级分辨率），但**没有任何乐句/8 拍分析**。

### A.3 Traktor

| 项 | 事实 | 来源 |
|---|---|---|
| Elastic Beat Grid | 网格被 **Grid Marker 分段**，每段（tempo zone）**内部等间隔**，段可各自设 BPM；段与段之间用 marker 对齐 | [NI Advanced Usage Tutorials](https://www.native-instruments.com/ni-tech-manuals/traktor-pro-manual/en/advanced-usage-tutorials) |
| Auto Grid | 自动在检测到的第一个底鼓上放 Grid Marker | [NI 支持文档](https://support.native-instruments.com/support/solutions/articles/69000879457-how-to-set-beatgrids-in-traktor) |
| Tempo Reset / Reset BPM | `Reset GRID`=重跑自动分析并清掉手动 marker；`Reset BPM`=把当前 tempo zone 的 BPM 还原成自动分析值，**不动 marker 位置** | [Traktor Pro 4 手册](https://www.modesdemploi.fr/native-instruments/traktor-pro-4/mode-d-emploi?p=38) |
| 变速跟随 | 每段恒定 → 变速靠"多放 marker"（live 录音建议**每 16 拍甚至每 4 拍**放一个） | NI Advanced Usage Tutorials |
| Lock | 锁定 BPM/marker 防止被重分析覆盖 | NI 支持文档 |

**结论**：Elastic grid = **分段恒定 tempo** 的多锚点模型；无乐句/8 拍分析。

### A.4 rekordbox / Engine DJ

| 项 | 事实 | 来源 |
|---|---|---|
| rekordbox Phrase Analysis | 5.1.0+；7 种类型：**INTRO / UP(build-up) / DOWN(breakdown) / CHORUS / BRIDGE / VERSE / OUTRO**；另检测 **Fill in**（乐句末尾的即兴变化，**最多 4 拍**，出现在 Intro/Up/Chorus 末尾） | [rekordbox Phrase Edit Operation Guide（官方 PDF）](https://cdn.rekordbox.com/files/20241203210634/rekordbox7.0.5_Phrase_Edit_operation_guide_EN.pdf) |
| 粒度 | phrase = **若干小节**的段落（不是固定拍数）；deck 头有 `Bar.Beat` 计数器；可设 **Bars per Phrase**（把计数器设成 8，则每 32 拍 phrase 号 +1） | 官方 Guide（类型）+ [EDM Ghost Production](https://edm-ghost-production.com/dj-knowledge-base/bars-beats-and-time-signatures)（Bars per Phrase，**第三方**） |
| 可编辑 | 拖动改边界 / 切分(CUT) / 改类型 / 清除，全部可手动 | 官方 Guide |
| Engine DJ | Grid Edit：Tap、Metronome、Reset、**Beat Grid Lock**、BPM ±、`/2 X2`、**Insert Anchor（"warping of songs with fluctuating tempos"）**、Downbeat Left/Right、Delete Anchor、Grid Left/Right | [Engine DJ User Guide v5.0.0（官方 PDF）](https://cdn.inmusicbrands.com/Software/ENDJ5/Engine%20DJ%20-%20User%20Guide%20-%20v5.0.0.pdf) |
| Engine anchor 限制 | 只能在**最后一个 anchor 之后**插入新 anchor；放 anchor 会把"从上一个 anchor 起到全曲"的网格重新对齐 | [Engine DJ 社区](https://community.enginedj.com/t/question-about-mixing-track-using-loops/43215/11) |

**结论**：**rekordbox 是唯一把"段落级 phrase 分析"做成消费级功能的**，但粒度是**小节组（8~16 小节）**，不是 8 拍；且它是"段落标签"，不是"每 8 拍一个边界"。Engine DJ 只有多锚点 warp，无 phrase。

### A.5 Mixed In Key / Algoriddim djay

| 产品 | 事实 | 来源 |
|---|---|---|
| Mixed In Key 10 | 只做 **调性 / BPM / Energy(1-10) / 最多 8 个自动 cue point**（可导出 rekordbox/Serato/Traktor）。**没有乐句或 8 拍分析** | [mixedinkey.com/mik10](https://mixedinkey.com/mik10/) |
| djay（旧） | 网格 = **BPM + 第一个 downbeat 两个量** | [djay iPad 手册](https://download.algoriddim.com/manual/djay-ios-ipad-manual-pressqual.pdf) |
| djay Pro 5.0 **Fluid Beatgrid** | "instantly analyzing and predicting the structure of any song…follows **every tempo fluctuation and interruption**" | [DJTechTools: djay Pro 5.0](https://djtechtools.com/2023/12/06/algoriddims-djay-pro-5-0-is-here-hyper-fast-stem-separation-flexible-beat-grids/) |
| djay 手册 | **Straight vs Dynamic** 两种网格，软件**自动判断**；**Anchor Points**：变速处放锚点，anchor 之间自动重算网格；可在 `Settings > Library` 关闭变速检测 | [Algoriddim Beatgrids 手册](https://help.algoriddim.com/user-manual/djay-android/dj-tools/beatgrids-bpm-sync/beatgrids)、[Beatgrid editor 手册](https://help.algoriddim.com/user-manual/djay-pro-windows/dj-tools/beatgrids-bpm-sync/beatgrid-editor) |

**结论**：MIK 完全不做乐句；djay 的 Fluid Beatgrid 是"**自动变速网格**"的标杆，但同样**没有乐句/8 拍**。

### A.6 商业软件小结

| 软件 | 网格模型 | 变速表达 | 有没有 8 拍/乐句级 | 粒度 |
|---|---|---|---|---|
| VirtualDJ | BPM+锚点（Rigid）/ 多锚点+每锚点BPM（Fluid） | Fluid 多锚点 | ❌（手动 Force downbeat 1:16=16拍） | 段落(break) |
| Serato | 多 Tempo marker + Warp | 多 Tempo marker，可分 beat | ❌ | — |
| Traktor | 分段 tempo zone（Grid Marker） | 多 Grid Marker | ❌ | — |
| rekordbox | BPM+网格锚点 | 固定（+用户改） | ✅ **段落级 phrase** | 8~16 小节 |
| Engine DJ | BPM + 多 anchor | 多 anchor warp | ❌ | — |
| djay | Straight / **Dynamic（Fluid）** | Anchor Points | ❌ | — |
| MIK | — | — | ❌ | — |

**→ 没有任何一家做「8 拍（2 小节）级」的自动网格。** 我们想要的东西在业界不存在现成对手；但它的底层零件（BPM + downbeat 相位）业界全都做。

---

## B. 开源项目 / 工具 / 库

统一对比表（先看结论再看细节）：

| 工具 | 语言/依赖 | 许可 | 实时/在线 | downbeat | 安装代价（对我们） |
|---|---|---|---|---|---|
| **madmom** | Py+Cython，numpy/scipy/cython（**不需 torch**） | 代码 BSD；**模型 CC BY-NC-SA 4.0（非商用）** | 有 `online` 模式（forward 算法） | ✅ 强项 | ❌ 要 scipy/cython |
| **librosa** | Py，numba/scipy/sklearn/soxr… | ISC | ❌ 非因果 DP | ❌ | ❌ 依赖太重 |
| **aubio** | C + py 绑定 | **GPLv3** | ✅ 原生 causal | ❌ | ❌ GPL + 2019 停更 |
| **Essentia** | C++ + py | **AGPLv3** | 有 streaming 模式 | ❌ **算法列表里没有** | ❌ AGPL + 重 |
| **BeatNet** | Py，librosa+madmom+**torch**+pyaudio | — | ✅ online/realtime/streaming | ✅ | ❌ 依赖爆炸 |
| **Beat This!** | Py+**torch**；**另有 ONNX 版** | **MIT** | ❌ 离线全曲 | ✅ | ⚠️ 见下（**我们已有 onnxruntime**） |
| **Vamp QM Bar&Beat** | C++ 插件 | GPL | 有实时实现参考 | ✅ 到 **bar 级** | ❌ 非 Python |
| **Vamp Segmentino** | C++ 插件 | AGPL | ❌ | 结构分段 | ❌ 非 Python |
| **Mixxx** | C++/Qt（源码参考） | GPL | — | ❌（手动） | 只借**思路/数据模型** |

细节：

### B.1 madmom

- **不需要 torch**：依赖是 numpy / scipy / cython。**但需要 scipy + Cython 编译** → 直接违反"不装新依赖"。
  - 判据：[PyPI madmom 0.16.1](https://pypi.com.cn/project/madmom)（Prerequisites: numpy, scipy, cython）、[CPJKU/madmom README](https://github.com/CPJKU/madmom/blob/5ab1d50a183adc785faa9ece1130bd9033ebd4ba/README.rst)
- **许可陷阱（重要）**：源码 BSD，但**模型/数据文件是 CC BY-NC-SA 4.0（非商用）**；官方明确写"commercial product 需联系 Gerhard Widmer"。
  - 判据：README.rst License 段（原文）。
- 核心处理器（[madmom.features.beats 文档](https://madmom.readthedocs.io/en/v0.16/modules/features/beats.html)）：
  - `DBNBeatTrackingProcessor(min_bpm=55, max_bpm=215, num_tempi, transition_lambda=100, observation_lambda=16, online=False)` —— RNN + DBN(HMM 近似)；`online=True` 时把 Viterbi 换成 **forward 算法**（因果）。
  - `RNNDownBeatProcessor` + `DBNDownBeatTrackingProcessor(beats_per_bar=[3,4], …)` —— 联合 beat+downbeat 跟踪。
- **注意 real-time 的坑**：网络是 **BLSTM（双向）**，严格因果实时推理不成立；官方 `online` 只解决 DBN 那一层。所以 madmom 更适合**离线**。
- 精度（一手）：Böck/Krebs ISMIR 2016 —— Ballroom **beat F1 0.938 / downbeat 0.863**；RWC Popular 0.943/0.861；GTZAN（unseen）0.856/**0.640**。
  - 判据：[ISMIR 2016 论文 PDF](https://archives.ismir.net/ismir2016/paper/000186.pdf)
- `MultiModelSelectionProcessor` / CRFBeatDetectionProcessor 也存在（多模型选择），但同样吃整段激活。

### B.2 librosa

- `beat_track`：Ellis 动态规划（DP），**默认估计单一全局 tempo，只容忍小幅波动**；支持动态 tempo（`bpm=tempo_dynamic` 数组 / `librosa.feature.tempo(aggregate=None, std_bpm=…)`）。
  - 判据：[librosa 动态 tempo 教程](https://librosa.org/doc/dev/auto_tutorials/03-advanced/plot_dynamic_beat.html)、[beat_track API](https://librosa.org/doc/0.11.0/generated/librosa.beat.beat_track.html)
- 许可 ISC（宽松）。依赖 numpy/scipy/numba/scikit-learn/soxr… → **太重**，放弃。
- **没有 downbeat，没有 phrase**（`librosa.segment` 只是通用工具，结构分析要自己写）。

### B.3 aubio

- 定位：**专为流式/实时/嵌入式**（causal，逐块处理），是经典 DSP（非神经网络）。
- 能力：onset（多方法）、pitch、**tempo/beat（`aubio_tempo`，实时 BPM + 拍位）**、MFCC。
- 许可 **GPLv3**（copyleft，商用有法律风险）；**最后版本 0.4.9 / 2019-02**，之后停更（维护模式）。
- **没有 downbeat。**
- 判据：[aubio.org](https://aubio.org/?desc=1)（License: GNU/GPL）、[github](https://github.com/aubio/aubio)

### B.4 Essentia

- 节拍算法：`RhythmExtractor2013`（`degara` 快 / `multifeature` 准）、`BeatTrackerDegara`、`BeatTrackerMultiFeature`、`BpmHistogramDescriptors`、`NoveltyCurve`、`Onsets`。
- **`RhythmExtractor2013` 依赖整曲统计 → 官方明说 "not suited for real-time detection"**。
  - 判据：[Essentia 节拍教程](https://essentia.upf.edu/tutorial_rhythm_beatdetection.html)
- **没有 downbeat / phrase 算法**：官方算法总览的 Rhythm 分类里只有上面这些 + SBic（分段）+ RhythmTransform。
  - 判据：[algorithms_overview.rst](https://github.com/MTG/essentia/blob/42e6cd02ae29c9031ae8b5852e959c925c3b7aba/doc/sphinxdoc/algorithms_overview.rst)
- 许可 **AGPLv3**（copyleft；亦提供商业授权）。
- 有 streaming 模式（dataflow），但对我们而言"AGPL + C++ 库"性价比很低。

### B.5 BeatNet（ISMIR 2021）

- CRNN + **粒子滤波（PF）**；四种模式：streaming / realtime / online / offline。
- **依赖：librosa + madmom + torch + pyaudio** → 直接排除。
- 精度（一手，GTZAN）：**在线** beat 75.44 / downbeat 46.49 F1；**离线(+DBN)** beat 80.64 / downbeat 54.07。
- 判据：[github.com/mjhydri/BeatNet](https://github.com/mjhydri/BeatNet)、[arXiv 2108.03576](https://ar5iv.arxiv.org/html/2108.03576)

### B.6 Beat This!（CPJKU，ISMIR 2024）—— **本次最值得深挖的一个**

- 论文：Foscarin / Schlüter / Widmer，*"Beat This! Accurate Beat Tracking Without DBN Postprocessing"*，**去掉 DBN 后处理**（因此不假设固定 meter/tempo，能处理变速/变拍）。[JKU 论文页](https://research.jku.at/en/publications/beat-this-accurate-beat-tracking-without-dbn-postprocessing/)
- 模型规模：主模型 final0 **~78 MB**；小模型 small0 **~8.1 MB（2.1M 参数）**。
- **关键：有 ONNX 转换版，MIT 许可**——`final0.onnx ~80MB` / `small0.onnx ~10MB`，输入 **128-bin mel 频谱 (batch, time, 128)**，输出 `beat` / `downbeat` logits（sigmoid + 峰值挑选）。
  - 判据：[HuggingFace: ashudesai/songbird-models](https://huggingface.co/ashudesai/songbird-models)（明确写 "License MIT — same as the original CPJKU/beat_this"，且已验证 onnxruntime 与 PyTorch 数值一致 < 0.00002）
- **对我们意味着什么**：项目**已有 onnxruntime**（且 GPU 解码那边也有 ONNX 经验，见 `tools/onnx_cost.py`）。所以"You can't add deps"这条约束下，**ONNX 版 Beat This 是唯一"不需要新依赖就能拿到神经网络级 beat+downbeat"的选项**。
- **但有两个硬限制**：① 它是**离线全曲**模型（要完整 mel 频谱，非因果）→ 只能用于**离线建库**，不能做实时兜底；② 输入是 mel 频谱，**需要自己实现 mel 滤波器组**（`src/beatgrid.py` 已有 STFT 基础，但要把 dB/幅度谱乘 mel 矩阵；这块约几十行 numpy，可做）。
- 另外：论文自述"在连续性指标上更差（performs worse on continuity metrics）"——对"每 8 拍切一次"这种要**稳定相位**的应用，需要额外的相位稳定性处理。

### B.7 Vamp 插件（Sonic Annotator / Sonic Visualiser）

| 插件 | 能力 | 粒度 | 方法 | 许可 | 来源 |
|---|---|---|---|---|---|
| **QM Bar and Beat Tracker** | 输出**小节线 + 被计数的拍位**（小节第 1 拍=1） | **到 bar 级（4 拍）** | Complex Domain onset → 6s 帧自相关 → 感知加权 comb filterbank → Viterbi 找周期路径 → **Ellis DP** 找拍位 → 第二遍做 beat-synchronous 分析 | GPL | [QM Vamp 文档](https://vamp-plugins.org/plugin-doc/qm-vamp-plugins.html) |
| QM Tempo and Beat Tracker | 拍位 + tempo（会随变化输出） | beat | 同上族 | GPL | 同上 |
| **Segmentino** | **结构分段**（section 级） | 段落 | beat 量化 chroma + beat-shingle 相似度 + 穷举段长比较 + 贪婪 family 选择 | AGPL | [c4dm/segmentino](https://github.com/c4dm/segmentino) |

- 实时可行性：QM 文档点名有**实时实现参考**（Stark et al. 2009），说明这套算法可做成流式。
- Segmentino 的**自我限制**（重要，避免踩坑）："为 classic rock 设计，假设 —— 和声重复与结构段重合、**稳定拍**、段长在一定范围、**对应段拍数相同**"。→ 电子舞曲里"段落=重复"这一前提**不成立**（drop/breakdown 是能量变化，不是和声重复）。**这就是为什么我不建议走 MSA 路线。**
- 各工具的 downbeat 支持情况的一份第三方汇总表（Table 2）也可参考：[arXiv 2111.03895](https://arxiv.org/pdf/2111.03895v2)（Devaney, Oxford Handbook 章节）。

### B.8 Mixxx（**最贴近我们现状，重点看**）

Mixxx 是开源桌面 DJ 软件，思路最贴近我们（也是"本地文件 + 实时演出"）。

**它的 beat analyzer 有两个插件**（[Mixxx 开发者指南 Analysers](https://github.com/mixxxdj/mixxx/wiki/Developer-Guide-Analysers)、[analyzerbeats.cpp](https://github.com/clickfreak/mixxx/blob/master/src/analyzer/analyzerbeats.cpp)）：

| 插件 | 产出 | 备选 | 特点 |
|---|---|---|---|
| **AnalyzerQueenMaryBeats**（默认） | **每拍位置数组** → beat map（可变 tempo） | 封装 QM beat tracker（Vamp SDK），onset + 概率 beat tracking | 准，但慢（约 **2~3× 实时**） |
| AnalyzerSoundTouchBeats | 单一平均 BPM | SoundTouch BPM detector | 快、省内存，但**无法建模变速** |

**它的网格数据模型（我们最该借的）**：

| 模型 | 内部表示 | 查询 | 用于 |
|---|---|---|---|
| **Constant Beat Grid** | `bpm + firstBeatPosition` | O(1) | 恒定 tempo |
| **Beat Map** | **每拍显式 frame position 的 sorted QVector** | 二分查找 | **变速 / live 录音 / 非机械节拍** |

- 关键 API：`nextBeat / previousBeat / nthBeat / getBeatDistance / getBpmInRange`；修改是 immutable 的 `translated()` / `scaled(newBpm)`。
- 更新的数据模型（[Beat and Bar Edit Workflow  wiki](https://www.mixxx.org/wiki/doku.php/Beat%20and%20Bar%20Edit%20Workflow)）：
  - `beatLength = 60 × sampleRate × (4 / timeSigDenom) / bpm`
  - 用 **`timeSignatureMarkers` + `bpmMarkers`（按 downBeatIndex 索引）** 表达"多 BPM 段 + 拍号变化"
  - 每拍有类型 **DOWNBEAT / BEAT**
- **Mixxx 没有自动 downbeat / 没有 phrase**：downbeat 是用户手动设的（Beat & Bar Edit）；analyzer 只出 beat。Mixxx 还能**导入 rekordbox 的 beat 数据**（源码里有 `mixxx::rekordboxconstants`）。

**→ 结论**：Mixxx 证明了「**可变 tempo 网格 = 每拍时间戳数组 / BPM 分段折线**」这条路在真实 DJ 软件里是可行且必要的；而"自动 downbeat/乐句"连 Mixxx 都没做。**我们要借的是它的数据模型，不是它的算法。**

---

## C. 学术方案

### C.1 下拍 / 小节跟踪（downbeat / bar tracking）

| 工作 | 特征/模型 | 精度量级 | 需要 NN？ | 来源 |
|---|---|---|---|---|
| **Böck/Krebs ISMIR 2016**（madmom） | RNN(BLSTM) 联合 beat+downbeat → DBN | Ballroom **beat 0.938 / downbeat 0.863**；RWC-Pop 0.943/0.861；**GTZAN 0.856/0.640** | ✅ | [ISMIR2016 PDF](https://archives.ismir.net/ismir2016/paper/000186.pdf) |
| **Krebs/Böck ISMIR 2015** | 高效 state space（联合 tempo+meter）的 DBN/HMM | 同上族 | ✅（配 RNN） | [madmom 文档引用](https://madmom.readthedocs.io/en/v0.16/modules/features/beats.html) |
| **Durand et al.（MIREX 2014-16）** | DNN + Viterbi | Beatles downbeat ~0.842 | ✅ | [arXiv 1906.03870 综述](https://ar5iv.arxiv.org/html/1906.03870) |
| Krebs 等 bar pointer model / HMM（KSH1） | bar pointer + HMM | 非西方数据集 0.4~0.85 | ❌ 可不用 NN | 同上 |
| **Beat This! ISMIR 2024** | Transformer，无 DBN | 论文自述 F1 超 SOTA（连续性指标较差） | ✅ | [JKU](https://research.jku.at/en/publications/beat-this-accurate-beat-tracking-without-dbn-postprocessing/) |
| **Whiteley et al.** | 多特征 HMM | **未取得一手数值**（本次未检索到原文） | ❌ | — |

**三条硬事实**：
1. **downbeat 精度系统性低于 beat**（Ballroom 0.863 vs 0.938；GTZAN 0.640 vs 0.856）。
2. **downbeat 一旦错，整条网格会整体平移**（业界公认难点）："**Downbeat tracking is still very error-prone and even when the resulting beat positions possibly align with actual beats, the entire beat grid could be shifted due to erroneous downbeat detection.**"（[audioXpress: Beat Tracking Explained](https://audioxpress.com/article/audio-processing-beat-tracking-explained)）
3. → 这正好解释了我们为什么要在 {0,4} 之间二选一，以及为什么"八拍相位对比度"经常"证据不足"。

### C.2 「乐句 / 8 拍结构」到底有没有专门方法？

**结论：没有专门做"8 拍乐句边界"的论文/特征。** 现有"乐句/结构"工作全是**段落级（section，8~16 小节量级）**：

| 方法 | 核心特征 | 用途 | 精度 | 来源 |
|---|---|---|---|---|
| **Foote novelty (2000)** | 自相似矩阵(SSM)对角线卷核 → novelty curve → 阈值找边界 | 段落边界 | 经典基线 | [arXiv 2311.18604](https://arxiv.org/html/2311.18604v1/) |
| **SSM 四准则**（homogeneity / novelty / repetition / regularity） | 帧相似度方阵 | 分段 | — | 同上 |
| **Goto RefraiD (2006)** | chroma 时滞矩阵 + 扩展相似度（含 12 半音移调）+ 三条启发式（长度范围、长重复段、半长子段） | **chorus 检测** | **100 首中 80 首正确** | [IEEE/ACM abstract](https://dl.acm.org/doi/abs/10.1109/TSA.2005.863204) |
| Segmentino（Mauch） | beat 量化 chroma + beat-shingle 相似度 + 贪婪 family | 结构分段 | — | [c4dm/segmentino](https://github.com/c4dm/segmentino) |
| Serrà "Structural Features" / McFee-Ellis 谱聚类 | 重复/齐次编码 | 分段 | 当前最佳无监督族 | [arXiv 2311.18604](https://arxiv.org/html/2311.18604v1/) |

**关键推论（对项目直接有用）**：
> 我们要的「8 拍乐句」在音乐上等价于「**每 4 拍一个小节，每 2 个小节一个组**」。
> 因此它**完全由 downbeat 相位决定**（知道小节头在哪，八拍头就是"隔一个小节取一个"）。
> **完全不需要** SSM / novelty / chorus detection / MSA 这套重武器。
> —— 我们 `src/beatgrid.py` 里的 `phrase_phase()`（在 {0,4} 二选一）**就是这个思路，方向是对的**。
> 真正需要提升的不是"结构分析能力"，而是**下拍相位的稳健性**与**相位证据不足时的降级策略**。

（注：如果哪天要做"drop/breakdown 切场景"，那就需要 MSA；但那是另一个需求，且电子舞曲里"段落=和声重复"不成立，RefraiD/Segmentino 会失灵。）

### C.3 在线 / 因果（online / causal）拍位与下拍跟踪

| 方案 | 类型 | 因果？ | 需 NN？ | 精度量级 | 来源 |
|---|---|---|---|---|---|
| **aubio** | onset 自相关 + comb filter（经典 DSP） | ✅ | ❌ | GTZAN beat F1 **57.09**；Ballroom 56.73 | [arXiv 2108.03576 表 2](https://ar5iv.arxiv.org/html/2108.03576) |
| **IBT**（INESC Porto, Oliveira 2012） | 多 agent + 并行 tempo/beat 假设 + state recovery | ✅ causal / 也可非因果 | ❌ | GTZAN 68.99；Ballroom 70.79 | [IEEE TASLP abstract](https://dl.acm.org/doi/abs/10.1109/TASL.2012.2210878) |
| **Böck FF**（forward 算法） | RNN + DBN forward | ✅ | ✅ | GTZAN 74.18 | 同上 |
| **DLB (Don't Look Back)** | causal RNN | ✅ | ✅ | GTZAN 73.77 | 同上 |
| **BeatNet** | CRNN + **粒子滤波(PF)** | ✅ | ✅ | GTZAN beat 75.44 / **downbeat 46.49** | 同上 |
| QM 实时实现 | Viterbi→在线化（Stark 2009） | 部分 | ❌ | — | [QM Vamp 文档](https://vamp-plugins.org/plugin-doc/qm-vamp-plugins.html) |
| librosa.beat_track | 全局 DP | ❌ | ❌ | — | [librosa](https://librosa.org/doc/0.11.0/generated/librosa.beat.beat_track.html) |

**结论**：
- **在线 beat 是可行的、纯信号处理也不需要 NN**（aubio / IBT / 我们的相位折叠，beat F1 约 57~70）。对"兜底"够用。
- **在线 downbeat 是难题**：无 NN 的方案在文献里基本没有可靠结果；上 NN 的 BeatNet 也只有 **F1 0.46**。
- **可行的在线法（低成本）**：`低频/全带起音包络 → 相位折叠定 BPM → 梳状滤波或 DP 平滑定拍位 → 对拍位特征做 mod-8 相位直方图选八拍头`。这就是我们现有 `beatgrid.py` 的在线化版本，**纯 numpy 完全可做**，且 CPU 开销很小（只需一个滑动窗 ODF）。
- 学术上还有 **particle filter / HMM 状态空间法**，但既然 `phrase_phase` 的直方图分桶已经能表达同样的东西，没必要引入 PF 的复杂度。

---

## D. 变速（tempo change）的处理

### D.1 各家怎么在网格里表达"速度变化"

| 软件 | 表达方式 | 来源 |
|---|---|---|
| VirtualDJ | Fluid 模式：**多个锚点，每锚点带 BPM** | [BPM Editor](https://www.virtualdj.com/manuals/virtualdj/editors/bpmeditor.html) |
| Serato | **多个 Tempo marker**（≤128），可局部拉伸；Beat Warp marker | [Serato Sample Beatgrids](https://support.serato.com/hc/en-us/articles/360002025835) |
| Traktor | **Grid Marker 分段的 tempo zones**，段内恒定 | [NI Tutorials](https://www.native-instruments.com/ni-tech-manuals/traktor-pro-manual/en/advanced-usage-tutorials) |
| Engine DJ / djay | **多 anchor**，anchor 间自动重算 | [Engine 手册](https://cdn.inmusicbrands.com/Software/ENDJ5/Engine%20DJ%20-%20User%20Guide%20-%20v5.0.0.pdf) / [Algoriddim](https://help.algoriddim.com/user-manual/djay-android/dj-tools/beatgrids-bpm-sync/beatgrids) |
| **Mixxx** | **Beat Map = 每拍显式帧位置数组**；或 `bpmMarkers` 分段 | [Mixxx wiki](https://github.com/mixxxdj/mixxx/wiki/Developer-Guide-Analysers) |

**两类表达**：① **BPM + 多锚点**（VDJ Fluid / Traktor / Serato / djay / Engine）② **每拍时间戳数组**（Mixxx）。
对我们的场景（DJ 推杆 = **全局均匀缩放**），②更直接、更少假设。

**成熟做法参考（人工修网格时的坑，值得记）**：变速点必须**落在旧网格的 downbeat 上**，否则 Serato/Engine 会自动挪位、导致变速段前整段失步；社区用 "filler / auxiliary beat marker" 解决。
判据：[Lexicon DJ 论坛讨论](https://discuss.lexicondj.com/t/my-findings-on-working-with-multigrid-markers-in-lexicon-serato-and-enginedj/1180)

**对我们最实用的变速同步公式**（DJ 推杆是全局均匀缩放倍率 r，keylock 时只改时间轴）：

```
# 存的是原曲时间轴（r=1）
原曲时间  t_src = t_live × r            # t_live = 演出时间轴上"这歌已播了多久"
拍长      beat_len_src = 60 / bpm_src
八拍头    t_src_next = anchor_src + ceil((t_src - anchor_src) / (8·beat_len_src)) · 8·beat_len_src
回到演出  t_live_next = t_src_next / r
```
只要 **r 估计准确**，网格自动跟着变——**不需要重建网格**。r 的估计手段见 §D.3。

### D.2 时间拉伸后"网格重映射"的成熟做法

- **均匀拉伸**：`beat_len' = beat_len / r`，`anchor' = anchor / r` —— 这是 djay/Traktor 内部对推杆的等价操作（软件把播放速率乘到网格时间轴上）。
- **非均匀（曲内变速）**：用多锚点/每拍时间戳分段——见 §D.1。
- **"先做变速点、再对齐"的工程细节**：见上面 Lexicon 讨论（必须落在 downbeat、用 filler marker）。
- **我们已有的实践**：`grid_anchor_after()` / `grid_beat_index()` 已经是"anchor + 8n·beat_len"的查表式；变速只需把 `beat_len`、`anchor` 按 r 缩放即可，改动量极小。

### D.3 音频指纹对 tempo/pitch 变化的容忍度（**关键问题**）

先看一手文献的量化结论：

| 方案 | 抗速度变化能力 | 备注 | 来源 |
|---|---|---|---|
| Philips / Haitsma-Kalker（经典谱带能量指纹） | 原始算法**不抗变速**；改良版（log 频带 + 自相关移不变）达到 **±6%** | 被引用一手文献确认 | [Linear speed-change resilient audio fingerprinting（ResearchGate 摘要）](https://www.researchgate.net/publication/228690796_Linear_speed-change_resilient_audio_fingerprinting) |
| **Dupraz & Richard（ICASSP 2010）** | **抗 ≤5% 变速** | 频率型指纹 + 速度变化鲁棒单元 | [ICASSP10 PDF](https://perso.telecom-paristech.fr/grichard/Publications/Icassp10_Dupraz.pdf) |
| **Panako（Six & Leman, ISMIR 2014）** | **抗 ±10% 时间/音高变化**，**并能返回时间拉伸量 + 音高偏移量** | Constant-Q 关键点 + 多匹配；3 万首库实测 | [UGhent biblio](https://biblio.ugent.be/publication/5754913) |
| Shazam 式（峰值配对 landmark） | 对**大幅**变速（TikTok 加速 10~30%）**会失败**（峰位置 + 时间差全变） | 面向"同一份录音"，不面向"同一首歌的变速版" | [inspiredbybeatz（非学术）](https://www.inspiredbybeatz.com/en/song-detection-limit-sped-up-remix-ai) |
| 匹配阶段弹性对齐 | 有资料称索引/匹配可"allow flexible time alignment … such as tempo changes" | 非权威，仅供参考 | [Milvus FAQ](https://milvus.io/ai-quick-reference/how-do-services-like-shazam-perform-audio-matching-and-search) |

**→ 结论：5% 变速正处在"临界带"**：
- 对**经典谱带能量型**指纹：5% 通常**还在容忍范围内**（±5% ~ ±6%）。
- 对**峰值配对（landmark）型**指纹（**我们的 `src/fp.py` 就是这种**）：**风险明显更高**，因为
  - keylock **开**：只有时间差被 5% 缩放（频率峰不动）→ 短时间差的 peak-pair 大多还能落进同一个量化桶，**可能可活**；
  - keylock **关**：峰的整体频率位置也被平移（如 1kHz 处 ±5% = ±50Hz），而我们 `FREQ_QUANT=4`（≈43 Hz/桶）→ **freq1/freq2 会跨桶，hash 直接变**，**基本必挂**。

**我们 `src/fp.py` 的现状与风险点**（读码得出的判断，非外部文献）：
- `DT_QUANT=4`（时间差按 4 帧≈**186 ms** 一桶）、`FREQ_QUANT=4`（频率按 4 个 bin≈**43 Hz** 一桶）。
- 短时间差的对（fan-out 只有 8、多数是相邻峰）对 5% 变速较鲁棒；**长时间差的对会被 5% 放大后跨桶**。
- → **必须实测**，不能拍脑袋。建议测法见 §E.3。

**可借用的一个关键思路（推荐）**：不要在变速下"硬匹配点"，而是**用指纹散点图的斜率反推 r**：
- 常数倍率 r 的时间拉伸，会把 Shazam 式的对齐散点（横轴=查询时间，纵轴=库内时间）从"一条水平带"变成"**一条斜线**"，**斜率 = r**。
- Panako 就是这么做的（它额外返回拉伸量与音高偏移量）。→ 我们能**从指纹本身直接读出 r**，喂给 §D.1 的网格缩放公式。
- 好处：即使 r 把 vote 峰值"抹开"导致匹配票数下降，只要还能定位到候选歌，**斜率仍能给出 r**。

### D.4 半速歧义（我们踩过的坑）

- 业界做法：VDJ / Traktor / djay 都提供 **`/2`、`x2` 手动按钮** + 明确的"BPM 可能是真实值的一半/两倍"提示语，**交给用户判断**（[VDJ](https://www.virtualdj.com/manuals/virtualdj/editors/bpmeditor.html)、[djay](https://help.algoriddim.com/user-manual/djay-pro-windows/dj-tools/beatgrids-bpm-sync/beatgrid-editor)）。
- 算法侧：madmom DBN 用很宽的 BPM 范围（**55~215**）规避；BeatNet 用 meter 联合建模。
- 我们已有的 [115,185] 门控 + `beat_vs_half` 低频强度比（`tools/beat_analyze.py`）是**合理的工程特化**——业界没人用这个先验，因为 DJ 软件面向全品类；而我们是"DJ 演出场景"，先验收窄是合理的。**建议保留，并作为 BPM 候选打分的一项**。

---

## E. 结论与建议

### E.1 值得借鉴（按「收益 ÷ 投入 ÷ 依赖风险」降序）

1. **「8 拍乐句 = downbeat 相位 + 固定周期」的概念简化**（收益最高、零投入）
   - 明确：**不做 MSA / 结构分析**。八拍头 = 小节头隔一个取一个（`anchor + 8n·beat_len`，n 为整数）。
   - 依据：§C.2；且业界"phrase"最细也就是 bar 级（VDJ 1:16、rekordbox 段落级）。
   - **能纯 numpy**：✅（我们已经在做）
   - 待补：下拍相位的**稳健性**（证据不足时的降级）、**4 拍 vs 8 拍的相位二义性**处理。

2. **Mixxx 的 Beat Map 数据模型**（收益高、投入低）
   - 网格从「BPM + 单锚点」升级为「**BPM 分段折线 / 每拍时间戳数组**」，让曲内变速天然可表达。
   - 依据：§B.8（Mixxx 是唯一开源桌面 DJ 软件，其 Beat Map 正是为此设计）
   - **能纯 numpy**：✅（只是数据结构 + 二分查找）
   - 落地：`src/beatgrid.py` 的 grid dict 增加可选字段 `bpm_segments: [(t, bpm), …]` 或 `beat_times: [t0, t1, …]`，`grid_anchor_after()` 优先用分段；旧数据（单 BPM）自动降级为单段。**向后兼容、零风险**。

3. **Beat This! 的 ONNX 小模型，用于离线建库**（收益中高、投入中、依赖风险低）
   - **我们已有 onnxruntime**，`small0.onnx` 仅 ~10 MB、MIT 许可，能出 beat + downbeat。
   - 依据：§B.6（HF 有官方 ONNX，MIT，数值已验证）
   - **能纯 numpy**：⚠️ 需要自己写 **mel 滤波器组**（几十行 numpy，可做）；推理走 onnxruntime（已在依赖里）。
   - 用途：**离线**对曲库做 downbeat（比自算的更靠谱），实时段**不要用**（非因果）。
   - 风险：模型是"通才"训练（含古典、变拍），在电子舞曲上需**实测**；论文自承连续性指标较差，需加相位稳定后处理。

4. **指纹散点斜率反推变速倍率 r**（收益高、投入中、零依赖）
   - 依据：§D.3（Panako 的成熟做法）
   - **能纯 numpy**：✅
   - 落地：`FingerprintDB.match()` 现在取 vote 直方图的**单点最大值**；改成对匹配对做 **(q_off, db_off) 线性拟合**，斜率给 r、截距给 offset。既能容忍变速，又**顺手拿到 r**。

5. **相位折叠 + 相位直方图分桶的在线拍位/八拍相位**（收益中、投入已沉没）
   - 依据：§C.3（aubio/IBT 同量级）
   - **能纯 numpy**：✅（已有 `scan_bpm` / `grid_conc` / `phase_of` / `phrase_phase`）
   - 待补：从"整曲分析"改造成**滑动窗增量更新**（在线兜底），这是目前唯一缺的一块。

### E.2 看着好但**不要碰**

| 方案 | 不碰的理由 |
|---|---|
| **madmom** | 需 scipy + Cython（违反"不装新依赖"）；**模型是 CC BY-NC-SA（非商用）**，商用要授权；BLSTM 本身非因果，实时不成立。 |
| **librosa** | 依赖 numba/scipy/sklearn/soxr… 太重；且没有 downbeat。 |
| **BeatNet** | 依赖 librosa + madmom + **torch** + pyaudio，爆炸。 |
| **aubio** | **GPLv3**（商用法律风险）+ **2019 起停更** + 有 C 编译。 |
| **Essentia** | **AGPLv3** + C++ 库 + 官方明说 RhythmExtractor **不适合实时** + **没有 downbeat 算法**。 |
| **完整音乐结构分析**（Segmentino / RefraiD / SSM-novelty） | 我们要的是 8 拍，不是段落；且这些方法假设"段落=和声重复"，在电子舞曲上不成立；AGPL/GPL + 非 Python。 |
| **在线 downbeat 神经网络** | 学术尚未解决（BeatNet online downbeat F1 仅 0.46），投入产出比极差。 |

### E.3 三条目标的技术路线建议

#### 目标 1：离线乐句网格（建库）

| 步骤 | 做法 | 依据 |
|---|---|---|
| 数据源优先 | **VDJ database.xml 优先**（已有 `load_vdj`）：BPM + Phase(锚点) 零成本、可信度最高。注意 `Scan@Bpm` 是**每拍秒数**（真 BPM = 60/它）、`Phase` 是锚点。 | 项目 `src/beatgrid.py` / `tools/vdj_grid.py`；§A.1 |
| 自算兜底 | 现有 `analyze_signal` 的 BPM 扫描 + `phase_of` + `phrase_phase`（{0,4} 二选一）。**保留 [115,185] 半速门控**。 | §D.4 |
| **可选的精度升级** | 试 **Beat This! ONNX `small0`**（onnxruntime 已就绪）算 downbeat，与自算结果交叉验证；只在高置信时采用。 | §B.6 |
| 网格表达升级 | 存 `bpm` + `anchor` + **`phrase`**（现有）+ 可选 `bpm_segments`（吸收 Mixxx Beat Map 思路）。 | §B.8 |
| 验收 | 与 VDJ 网格逐拍比对（已有 `tools/_vdj_compare.py`），统计八拍头对齐误差；对 `contrast` 低的曲子标"低置信"，演出时降级到"只用 4 拍网格"。 | §C.1（downbeat 错会整体平移） |

#### 目标 2：实时兜底（曲库外音乐）

| 步骤 | 做法 | 依据 |
|---|---|---|
| CPU 预算 | **只维护一个滑动窗 ODF**（如 4~8 s 窗、hop ≈ 20~50 ms），O(窗长) 而非 O(整曲)。 | §B.4（Essentia 明说整曲统计不适合实时） |
| 拍位 | 相位折叠定 BPM（已有 `grid_conc`/`scan_bpm` 的增量版）+ 梳状滤波/DP 平滑拍位。**纯 numpy**。 | §C.3（aubio/IBT 量级，beat F1 57~70 够兜底） |
| 八拍相位 | 对拍位特征（低频能量 / 谱通量 / 低频谱质心跳变）做 **mod-8 相位直方图**选取头（= 现有 `phrase_scores`）。 | §C.2、现有实现 |
| 明确不做 | 不做在线 downbeat / 不做结构分析；用"4 拍网格 + 8 拍相位"近似即可。 | §C.3 |
| 兜底策略 | 相位置信度低时，**退化为纯 4 拍切换**（比错误的 8 拍切换更安全）。 | §C.1 |

#### 目标 3：变速同步

| 步骤 | 做法 | 依据 |
|---|---|---|
| r 从哪来（优先级 1） | **在线 BPM 估计 ÷ 库内 BPM**（兜底路径本来就在估 BPM，顺手得到 r）。 | §D.1 |
| r 从哪来（优先级 2） | **指纹散点斜率**（改造 `FingerprintDB.match()` 做线性拟合）。 | §D.3、§E.1-4 |
| r 从哪来（优先级 3） | 若软件能拿到 DJ 软件的推杆值（DJ 软件 now-playing/参数），直接用——最稳。 | — |
| 网格映射 | `beat_len' = beat_len / r`；`anchor' = anchor / r`（局部段用录得的 `t_src` 反推）。 | §D.1 公式 |
| 指纹可行性 | **先实测 5%**：同一首歌，0% / +5% / -5% tempo（keylock 开/关各一组）跑 `FingerprintDB.match()`，统计票数与是否命中。**这是当前最大的未知，建议列为一个独立小实验**。 | §D.3 |
| 若实测比 5% 差 | 参考 Panako 思路：① 先降采样/对数频率轴增强鲁棒；② 用"斜率拟合"替代"点匹配"；③ 提高 `DT_QUANT` 桶宽（用召回换精度）。 | §D.3 |

### E.4 明确的"查不到 / 未证实"

- Alle **Whiteley** 的下拍工作：本次**未检索到一手原文数据**（只说"存在这类工作"，不给数值）。
- **VDJ `AltBpm` 的确切定义**：官方手册**未解释**；我们代码里"=1.5×Bpm"是**项目自己的观察**。
- **rekordbox Phrase 的精确粒度**：官方 Phrase Edit Guide **只给出 7 种类型，没写拍/小节数**；"Bars per Phrase=8→32 拍"来自**第三方**站，未在官方文档核实。
- **Mixed In Key 是否做乐句**：官方页面**只有 key/BPM/energy/cue**，**未见任何乐句分析**（结论：没有）。
- **aubio 维护状态**"2019 起停更"来自**第三方知识库**，非官方公告（但最后 release 0.4.9 / 2019-02 与官方发布记录一致）。

---

## 附：主要来源清单

**商业软件**
- VirtualDJ BPM Editor 手册 https://www.virtualdj.com/manuals/virtualdj/editors/bpmeditor.html
- VirtualDJ POI Editor 手册 https://it.virtualdj.com/manuals/virtualdj/editors/poieditor.html
- VirtualDJ6 User Guide PDF http://www.virtualdj.com/documents/Manuals/VirtualDJ6-UserGuide.pdf
- Serato Sample Beatgrids https://support.serato.com/hc/en-us/articles/360002025835
- Serato Lucid Beatgrids (5.0) https://the-drop.serato.com/announcements/introducing-lucid-beatgrids-in-serato-dj-5-0
- Traktor 网格调整教程 https://www.native-instruments.com/ni-tech-manuals/traktor-pro-manual/en/advanced-usage-tutorials
- Traktor 官方设置 Beatgrid https://support.native-instruments.com/support/solutions/articles/69000879457-how-to-set-beatgrids-in-traktor
- rekordbox Phrase Edit Guide PDF https://cdn.rekordbox.com/files/20241203210634/rekordbox7.0.5_Phrase_Edit_operation_guide_EN.pdf
- Engine DJ User Guide v5.0.0 PDF https://cdn.inmusicbrands.com/Software/ENDJ5/Engine%20DJ%20-%20User%20Guide%20-%20v5.0.0.pdf
- Algoriddim Beatgrids https://help.algoriddim.com/user-manual/djay-android/dj-tools/beatgrids-bpm-sync/beatgrids
- Algoriddim Beatgrid editor https://help.algoriddim.com/user-manual/djay-pro-windows/dj-tools/beatgrids-bpm-sync/beatgrid-editor
- Mixed In Key 10 https://mixedinkey.com/mik10/
- djay Pro 5.0 Fluid Beatgrid 报道 https://djtechtools.com/2023/12/06/algoriddims-djay-pro-5-0-is-here-hyper-fast-stem-separation-flexible-beat-grids/
- VDJ 自动 break 点（论坛）https://de.virtualdj.com/forums/244124/General_Discussion/Remix_Points%3A__automatically_analyzed_by_VirtualDJ_.html

**开源**
- madmom 文档 https://madmom.readthedocs.io/en/v0.16/modules/features/beats.html
- madmom README（许可）https://github.com/CPJKU/madmom/blob/5ab1d50a183adc785faa9ece1130bd9033ebd4ba/README.rst
- librosa beat_track https://librosa.org/doc/0.11.0/generated/librosa.beat.beat_track.html
- librosa 动态 tempo 教程 https://librosa.org/doc/dev/auto_tutorials/03-advanced/plot_dynamic_beat.html
- aubio https://aubio.org/?desc=1
- Essentia 节拍教程 https://essentia.upf.edu/tutorial_rhythm_beatdetection.html
- Essentia 算法总览 https://github.com/MTG/essentia/blob/42e6cd02ae29c9031ae8b5852e959c925c3b7aba/doc/sphinxdoc/algorithms_overview.rst
- BeatNet https://github.com/mjhydri/BeatNet
- Beat This! ONNX https://huggingface.co/ashudesai/songbird-models
- Beat This! 论文 https://research.jku.at/en/publications/beat-this-accurate-beat-tracking-without-dbn-postprocessing/
- QM Vamp Bar and Beat Tracker https://vamp-plugins.org/plugin-doc/qm-vamp-plugins.html
- Segmentino https://github.com/c4dm/segmentino
- Mixxx 开发者指南 Analysers https://github.com/mixxxdj/mixxx/wiki/Developer-Guide-Analysers
- Mixxx Beat and Bar Edit Workflow https://www.mixxx.org/wiki/doku.php/Beat%20and%20Bar%20Edit%20Workflow
- Mixxx analyzerbeats.cpp https://github.com/clickfreak/mixxx/blob/master/src/analyzer/analyzerbeats.cpp

**学术**
- Böck/Krebs ISMIR 2016 https://archives.ismir.net/ismir2016/paper/000186.pdf
- Krebs/Böck ISMIR 2015（经 madmom 引用）
- Downbeat 深度学习综述 arXiv 1906.03870 https://ar5iv.arxiv.org/html/1906.03870
- BeatNet arXiv 2108.03576 https://ar5iv.arxiv.org/html/2108.03576
- IBT IEEE TASLP 2012 https://dl.acm.org/doi/abs/10.1109/TASL.2012.2210878
- Goto RefraiD IEEE TASLP 2006 https://dl.acm.org/doi/abs/10.1109/TSA.2005.863204
- SSM/novelty/structure 综述 arXiv 2311.18604 https://arxiv.org/html/2311.18604v1/
- Vamp/工具能力对照表 arXiv 2111.03895 https://arxiv.org/pdf/2111.03895v2
- 指纹变速鲁棒性（Philips ±6%）https://www.researchgate.net/publication/228690796_Linear_speed-change_resilient_audio_fingerprinting
- Dupraz & Richard ICASSP 2010（≤5%）https://perso.telecom-paristech.fr/grichard/Publications/Icassp10_Dupraz.pdf
- Panako ISMIR 2014（±10% + 返回拉伸量）https://biblio.ugent.be/publication/5754913
- audioXpress: Beat Tracking Explained（downbeat 易错）https://audioxpress.com/article/audio-processing-beat-tracking-explained
