# Easy Reality · RC1 源代码

本仓库包含 Easy Reality 系列软件的源码（RC1 版本）：

| 目录 | 软件 | 说明 |
|---|---|---|
| `EasyRealityAutoVJ/` | ERAVJ 演出端 | Python + PySide6 + moderngl；实时视频合成、码表播放、NDI/Spout 输出、局域网音频接收 |
| `EasyRealityCue/` | ERCUE 编排端 | Python + PySide6 + PyAV；码表编排、预览、导出 `.ercue` |

## 五个可交付软件与源码对应关系

| 软件 | 源码位置 |
|---|---|
| EasyRealityAutoVJ（演出端） | `EasyRealityAutoVJ/src/` |
| EasyRealityCue（编排端） | `EasyRealityCue/src/ercue/` |
| ERLIB 曲库分析 | `EasyRealityAutoVJ/src/erlib/` + `EasyRealityAutoVJ/erlib_main.pyw` |
| ERSND 音频发送端 | `EasyRealityAutoVJ/src/lan_audio*.py` + `EasyRealityAutoVJ/lan_audio_sender.pyw` |
| NDI 接收自检 | `EasyRealityAutoVJ/tools/_archive/ndi_probe.py` |

## 依赖

- Python 3.13
- PySide6（Essentials 足够跑编排端）、numpy、PyAV **18.1.0（钉死）**、soundcard、sounddevice、moderngl、cyndilib

## 说明

- 仓库**只含源码**：不含 `venv/`、`dist/`、`build/`、日志、数据库、测试素材与任何二进制产物。
- `assets/` 内的图标与文档中的截图未随源码发布。
