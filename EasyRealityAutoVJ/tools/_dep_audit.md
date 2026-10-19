# 全量悬空依赖审计（2026-10-01）

工程师 software-engineer-2。目标：确认**会被打进包**的 PE 文件里，没有任何一个 import 了
**被 make_zip 跳过（不进包）** 的模块 —— 否则运行时该依赖解析不到，就是一条悬空依赖。

复现：`AUTOVJ_DIST=dist_new venv\Scripts\python -u tools\_dep_audit.py`
（脚本复用 `tools/make_zip.py` 的**真实判据** `_is_cyndilib_junk` / `_is_qt_unused`，与打包口径完全一致；
底层用 `tools/_pe_imports.py` 解析 PE 导入表。）

## 结果

| 指标 | 值 |
|---|---|
| dist 文件总数 | 1118 |
| 被跳过（不进包） | 88 |
| 会进包 | 1030 |
| 会进包里 PE(.dll/.pyd/.exe) | **230** |
| 被跳过模块名（按文件名匹配） | 87 |
| **悬空依赖条数** | **0** |
| PE 解析失败 | 0 |

结论：**扫描 230 个会进包的 PE 文件，0 条悬空依赖。**

## qtuiotouchplugin.dll 的来龙去脉

上一轮已知：包里保留的 `plugins/generic/qtuiotouchplugin.dll` 的 PE 导入表**硬依赖 `Qt6Network.dll`**
（我们跳过的那份）→ 一条悬空依赖。处置：**把 `qtuiotouchplugin.dll` 本身也纳入跳过**。

- 它是 TUIO 多点触控设备输入插件（触摸桌/交互墙用），桌面鼠标场景永不加载；真实平台插件是
  `platforms/qwindows.dll`，它只依赖 Qt6Gui/Qt6Core（不含 QtNetwork / OpenGL）。
- 与其为这个永不加载的可选插件多带整条 Networking 链（Qt6Network + qnetworklistmanager + tls/* ≈ 2.7 MB），
  不如连它一起跳过。
- 加入跳过后再审计：**悬空依赖 0 条**。

## 被跳过清单（合计 88 个文件 / 100.8 MB）

- cyndilib：**68 个 / 77.4 MB**（DirectShow/UWP 两份 NDI 库 + .cpp/.pyx/.pxd/.lib 源码）
- PySide6 未用 Qt：**20 个 / 23.4 MB**（含本轮新增的 `plugins/generic/qtuiotouchplugin.dll`，101,176 B）

## 实测（dist 临时改名，未重建）

把上列 Qt 文件 + 两份 NDI 库在 dist 与 venv 临时改名 `.bak` 后：
- `AUTOVJ_DIST=dist_new tools/smoke_exe.py 40 --isolated` → **✅ 冒烟通过**（进程存活>40s，无 startup_error.log）
- `QT_QPA_PLATFORM=offscreen tools/_uigeom.py --size 1600 900` → 三页几何与基线**逐行完全一致**
  （舞台/素材库/曲库 均 1592x802 @ 窗口 1600x900）

验证完已全部还原（无 .bak 残留）。

## 悬空依赖审计之外的口径说明
- 只有 **文件的 PE 导入表** 会被 DLL 加载器按名字解析；纯 Python import（PYZ 内）不受影响。
- `.exe` 本体与 `python3*.dll`、`shiboken6`、`cv2`、`av`、`onnxruntime` 等 230 个 PE 全部扫描，均无导入被跳过模块。
