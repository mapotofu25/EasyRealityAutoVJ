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

### 2. Genre Police Visualizer — lbnandy
- 用途：细分曲风关键词表、按曲风切换视觉的思路（本项目 `genre_keywords.py` 的来源）
- Usage: fine-grained genre keyword table & genre-driven visual switching
  (origin of `genre_keywords.py`)
- 出处 / Source: https://github.com/lbnandy/genre-police-visualizer
- 许可 / License: **MIT**

### 3. Genre Police AutoVJ — lbnandy
- 用途：「演出前分析曲库 + 现场识别曲目 + Spout / NDI 输出」的整体思路参考
- Usage: overall reference — pre-show library analysis + live track ID + Spout / NDI output
- 出处 / Source: https://github.com/lbnandy/genre-police-autovj
- 许可 / License: **MIT**

---

## 二、模型权重（⚠️ 其中 Discogs-EffNet **仅限非商业**）

### Chinese-CLIP — OFA-Sys
- 用途：素材内容打标（识别画面里有什么）
- Usage: clip content tagging
- 出处 / Source: https://github.com/OFA-Sys/Chinese-CLIP
- 许可 / License: **MIT** ✅

### Discogs-EffNet — Essentia / MTG
- 用途：曲风识别（400 种 Discogs 风格）
- Usage: genre detection (400 Discogs styles)
- 出处 / Source: https://github.com/MTG/essentia
- 许可 / License: **CC BY-NC-SA 4.0（NonCommercial）** ⚠️

> ### ⚠️ 重要：非商业限制 / Important: Non-Commercial Restriction
>
> `assets/models/discogs/` 下的 **Discogs-EffNet** 采用 **CC BY-NC-SA 4.0**，
> **明确禁止商业使用**。因此：
>
> - 本项目的**源代码**按 **MIT** 授权（见 `LICENSE`）；
> - 但**任何包含该模型的打包发行版**（包括本仓库 Releases 中提供的压缩包）
>   **仅可用于非商业用途**。
> - 如需商业使用，请自行从发行版中移除该模型，或自行取得该模型的商业授权。
>
> The **Discogs-EffNet** model under `assets/models/discogs/` is licensed
> **CC BY-NC-SA 4.0**, which **forbids commercial use**. Hence:
>
> - the **source code** of this project is licensed under **MIT** (see `LICENSE`);
> - but **any packaged release including that model** (including the archives
>   offered in this repository's Releases) **may be used for non-commercial
>   purposes only**.
> - For commercial use, remove the model from the distribution or obtain a
>   separate commercial license for it.

---

## 三、其他运行时依赖

以下组件以库的形式被调用，本仓库不分发其源码，各自遵循原始许可：

| 组件 | 许可 |
|---|---|
| PySide6 / Qt | LGPL-3.0 |
| OpenCV (`opencv-python`) | Apache-2.0 |
| FFmpeg（经 PyAV / OpenCV 调用） | LGPL / GPL（视构建而定） |
| onnxruntime | MIT |
| numpy | BSD-3-Clause |
| soundfile (libsndfile) | BSD-3-Clause / LGPL |
| soundcard | BSD-3-Clause |
| `cyndilib` | 见其仓库许可 |
| **NDI® SDK / Runtime** | NDI 为 NewTek 的商标与技术，**其分发与商业使用另有专门条款**。<br>本项目不修改、不重新分发 NDI SDK 源码，仅依赖其运行时；<br>如需在商业场景使用 NDI 输出，请自行确认并遵守 NDI 官方许可。 |

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
