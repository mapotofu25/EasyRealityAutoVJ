# 第三方组件与致谢 / Third-Party Notices

本项目（**Easy Reality AutoVJ**）在开发中参考或改写了若干开源项目的代码与思路，
并使用第三方模型。按各自许可的要求，署名与许可说明如下。

This project references/derives from the following open-source projects and
uses third-party models. Attributions and license notes are listed below as
required by their respective licenses.

---

## 一、代码 / 思路来源（MIT —— **署名必须保留**）

### 1. VJVision — ichiryu
- 用途：音乐电池 ChargeBar 决策状态机、音频指纹峰值检测
  （本项目 `charge_engine.py` / `fp.py` 的来源）
- Usage: Music Battery (ChargeBar) decision engine & fingerprint peak detection
  (origin of `charge_engine.py` / `fp.py`)
- 出处 / Source: https://github.com/ichiryu0021/VJVision
- 许可 / License: **MIT**

### 2. Genre Police AutoVJ — lbnandy
- 用途：「演出前分析曲库 + 现场识别曲目 + Spout / NDI 输出」的整体思路参考
- Usage: overall reference — pre-show library analysis + live track ID + Spout / NDI output
- 出处 / Source: https://github.com/lbnandy/genre-police-autovj
- 许可 / License: **MIT**

### 3. KVJ / KDJ — kumoSleeping
- 用途：**变速识别与「这个结果能不能作准」的整体思路参考**，具体参考了三处：
  1. 认歌的「确认」不只看得分，还要看**最弱窗**（把查询窗分三段取最低）和**领先**
     （得分减别处次高分）——本项目的三档判据就是照作者公开的口径实现的；
  2. 变速场景用**白化频带相关**打分、用**互不重叠的多次印证**（而不是单点峰值）；
  3. BPM 显示拆成「**原曲 BPM × 变速倍率**」，而不是直接显示实时估算值。
- Usage: design reference for tempo handling and acceptance — whitened spectral-band
  scoring, corroboration across non-overlapping windows, score/weakest-window/lead
  verification, and showing BPM as "source BPM × rate" instead of a raw live estimate.
- 出处 / Source: https://github.com/kumoSleeping/KVJ
- 许可 / License: **GPL-3.0**（上游）
- ⚠ **仅参考思路，未取用任何代码** —— 上游是 GPL-3.0，本项目为避免许可传染，
  相关实现（`fp.py` / `bandfeat.py` / `phrase_bt.py` 的判据部分）全部独立编写。
  This project references the **ideas only**; **no source code was taken**
  (upstream is GPL-3.0, implemented independently here).

---

## 二、模型权重

### Beat This! — CPJKU
- 用途：小节头（downbeat）判定 —— 给没有 VDJ / rekordbox 数据的曲目补小节相位
- Usage: downbeat detection for tracks without DJ-software metadata
- 出处 / Source: https://github.com/CPJKU/beat_this
- 许可 / License: **MIT**（代码与已发布的模型权重同为 MIT）

> 发行包内 `assets/models/beat_this/` 是**唯一**随包分发的模型（约 10 MB）。
> ⚠ 模型文件本身**不进仓库**（体积大，见 `.gitignore` 的 `assets/models/`），
> 由 `tools/build_exe.py` 的 `--add-data assets/models` 打进发行包。
> 源码运行时缺这个模型不会崩 —— 小节相位会退回到纯信号侧的判定。

---

## 三、其他运行时依赖

以下组件以库的形式被调用，本仓库不分发其源码，各自遵循原始许可：

| 组件 | 许可 | 用途 |
|---|---|---|
| PySide6 / Qt | LGPL-3.0 | 界面 |
| OpenCV (`opencv-python`) | Apache-2.0 | 颜色 LUT / 图像处理 |
| **PyAV**（含 FFmpeg 运行库） | BSD-3-Clause；其内置 FFmpeg 为 LGPL / GPL（视构建而定） | 带 alpha 的视频解码 |
| FFmpeg（经 PyAV / OpenCV 调用） | LGPL / GPL（视构建而定） | 底层多媒体库 |
| onnxruntime | MIT | Beat This! 的推理后端 |
| numpy | BSD-3-Clause | 数值计算 |
| soundfile (libsndfile) | BSD-3-Clause / LGPL | 音频读写 |
| soundcard | BSD-3-Clause | WASAPI 回环采集 |
| **sounddevice (PortAudio)** | MIT | 详情页试听播放 |
| **mutagen** | **GPL-2.0-or-later** ⚠ | 音乐元数据 / 封面（见下方说明） |
| **SpoutGL (Spout SDK)** | BSD-3-Clause | Spout 输出 |
| **GLFW** | MIT (zlib/libpng) | GPU 解码的离屏 OpenGL 上下文 |
| **Pillow** | MIT-CMU | 缩略图处理 |
| **cffi** | MIT-0 | SoundCard 的底层绑定 |
| **PyInstaller** | GPL-2.0-or-later（含打包例外） | 打包为 exe |
| `cyndilib` | Apache-2.0 | NDI 输出 |
| **NDI® SDK / Runtime** | NDI 为 NewTek 的商标与技术，**其分发与商业使用另有专门条款**。<br>本项目不修改、不重新分发 NDI SDK 源码，仅依赖其运行时；<br>如需在商业场景使用 NDI 输出，请自行确认并遵守 NDI 官方许可。 |

> ### 关于 `mutagen`（GPL-2.0-or-later）与发行包的许可
>
> 本项目的**源代码**是 MIT（见 `LICENSE`）。但发行包里**静态打包了 `mutagen`**，
> 它采用 **GPL-2.0-or-later**。按 GPL 的通常解释：
>
> - **源代码**（本仓库）仍按 MIT 提供；
> - **打包后的发行包**（Releases 里的压缩包 / exe）因为包含 GPL 组件，
>   整体应视为 **GPL-2.0-or-later** 分发 —— 想再分发这个二进制包时请遵守 GPL；
> - 对应源码在本仓库公开可取，满足 GPL 的源码提供要求。
>
> 如果你的场景不能接受 GPL，可以自行从发行包中移除 `mutagen`（此时封面读取会失效，
> 其余功能不受影响），或只使用本仓库源码并自行替换成 MIT 许可的标签读取库。
>
> **Source code is MIT. Packaged releases bundle `mutagen` (GPL-2.0-or-later), so the
> binary distribution should be treated as GPL-2.0-or-later; the corresponding source
> is available in this repository.**

---

## 四、商标与名称

「**Easy Reality AutoVJ**」这一名称与标识**不随源代码的 MIT 授权一并授予**。
你可以在 MIT 许可下自由使用、修改、分发**代码**，但**不得使用该名称发布衍生版本**，
以免与官方版本混淆。（此条属商标范畴，不限制 MIT 授予的代码权利。）

The name and identity "**Easy Reality AutoVJ**" are **not** covered by the MIT
license granted for the source code. You may freely use, modify and redistribute
the **code** under MIT, but you may **not** use this name for derivative releases.

---

## 五、免责声明

本软件按「现状」提供，不附带任何明示或暗示的担保（详见 `LICENSE`）。
使用者需自行确保所使用的音频/视频素材、第三方模型与输出内容符合当地法律
及各自的许可要求。

This software is provided "as is", without warranty of any kind (see `LICENSE`).
Users are responsible for ensuring that the media assets, third-party models and
outputs they use comply with applicable laws and their respective licenses.
