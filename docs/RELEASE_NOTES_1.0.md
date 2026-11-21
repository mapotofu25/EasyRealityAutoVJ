# EasyReality AutoVJ 1.0 正式版

把音乐变成画面的实时 VJ 引擎。它用音频指纹认出正在放的歌，按提前做好的码表，以及曲库预分析的节拍和能量段落，自动决定画面。素材切换、跟拍、跟情绪都由它完成，DJ 只管放歌。

## 这一版能做什么

- **跟着歌走**：读 BPM、八拍和能量段落，进 drop 当帧换素材池，变速也跟得住。
- **自动选素材**：按动态、前景/背景、铺满度打标，高动态给 drop，背景铺底，前景叠加。
- **稳定输出**：NDI/Spout 直出，OBS、Resolume 直接选；DXV 走 GPU，保留 Alpha，多屏独立。
- **现场可控**：KV、调色、多屏、黑场/冻结/下一素材、手动接管。
- **素材准备**：导入、打标、预览、码表、曲库分析一站完成。

## 安装包

| 文件 | 内容 |
| --- | --- |
| `EasyRealityAutoVJ_1.0.zip` | 演出主程序（免安装，解压即用） |
| `EasyRealityCue_1.0.zip` | 码表编辑器 |
| `EasyReality_Tools_1.0.zip` | ERLIB 曲库分析 + 音频发送端 + NDI 接收自检 + 音源诊断 |

解压覆盖即可，配置、素材库和码表数据都在 `%LOCALAPPDATA%\AutoVJ`，不会被清空。第一次用按“使用说明”四步走：选音源 → 扫曲库 → 导素材并打标 → 开始。

## 系统要求

- Windows 10/11 64 位
- 支持 NDI 或 Spout 的输出环境（OBS、Resolume 等）
- 4K / DXV 素材建议使用支持硬件解码的显卡

## 校验

```text
1a965393ee75ab44709679f129ea8bf512ace2ebf25574a1e0843dbcc75a9605  EasyRealityAutoVJ_1.0.zip
4daa5866da4987e521013b9e9b70bbae3f3348ff875859a38880dab26f89394b  EasyRealityCue_1.0.zip
2ca2d1021036f76ab851a522d37bfaec834b18e6a0bae1896746367e2dd47847  EasyReality_Tools_1.0.zip
```

## 隐私

发布包只包含程序、运行库、使用说明和第三方许可，不包含：

- 用户配置、日志、崩溃记录
- 素材库索引、音频指纹数据库、缩略图、预览缓存
- 码表工程或任何用户素材

## 注意

⚠ 本软件含快速闪烁画面，光敏性癫痫者请勿使用。演出前请务必充分测试。

反馈：QQ 1793170435
项目：https://github.com/mapotofu25/EasyRealityAutoVJ
