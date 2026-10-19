# 包体积精简取证（2026-10-01）

工程师 software-engineer-2 的取证记录。两处精简都遵循「**先取证、再用现有 dist 临时改名验证、最后落到打包环节**」。

复现脚本（都在 `tools/`，`_` 前缀已 gitignore）：
- `tools/_pe_imports.py`      —— 零依赖 PE 导入表解析器（证明运行时到底加载哪份 DLL）
- `tools/_rename_ndi.py`      —— 临时改名 / 还原 cyndilib 的两份多余 NDI 运行库
- `tools/_rename_qt.py`       —— 临时改名 / 还原未使用的 Qt 模块
- `tools/_zip_skip_check.py`  —— 用 make_zip 的真实判据统计会跳过哪些文件

---

## ① cyndilib 两份多余的 NDI 运行库（约 63 MB）

`_internal/cyndilib/wrapper/bin/` 同时带了三份 NDI 运行库：

| 文件 | 大小 | 结论 |
|---|---|---|
| Processing.NDI.Lib.DirectShow.x64.dll | 40 MB | 用不到 |
| **Processing.NDI.Lib.x64.dll** | **27 MB** | **运行时真正加载的（保留）** |
| Processing.NDI.Lib.UWP.x64.dll | 23 MB | 用不到 |

### 取证 1：PE 导入表（`_pe_imports.py`）

cyndilib 的每个 `.pyd` 都静态 import `Processing.NDI.Lib.x64.dll`，**没有任何一个** import
DirectShow / UWP 变体。例（18 个 .pyd 中 7 个直接引用）：

```
cyndilib/sender.cp313-win_amd64.pyd      NDI 相关=['Processing.NDI.Lib.x64.dll']
cyndilib/finder.cp313-win_amd64.pyd      NDI 相关=['Processing.NDI.Lib.x64.dll']
cyndilib/receiver.cp313-win_amd64.pyd    NDI 相关=['Processing.NDI.Lib.x64.dll']
...（其余 .pyd：NDI 相关=（无），走运行期 DynamicLoad，同样只认 x64）
```

Cython 生成源码里的链接配置同样只写 x64：
`sender.cpp` → `"libraries": ["Processing.NDI.Lib.x64"]`。
`src/` 里也 grep 不到任何 directshow/uwp 字样。

### 取证 2：临时改名 + 实测（用现有 dist，不重建）

把 **dist_new 与 venv** 两处 `wrapper/bin/` 里的两份可疑 DLL 临时改名 `.bak` 后：

- NDI 音视频回环（`tools/_test_ndi_audio2.py loop2`，PATCH=0）：
  `[tx] exit=0   [rx] exit=0   收到音频帧 296（非静音 296）`，与未改名前的基线（322 帧）同级 →
  **画面 + 音频都正常**。
- exe 冒烟（`AUTOVJ_DIST=dist_new tools/smoke_exe.py 40 --isolated`）：
  `进程存活超过 40 秒 ✓ / 无 startup_error.log / ✅ 冒烟通过`。

验证完已把 `.bak` 全部还原（`_rename_ndi.py status` 显示 orig=True）。

### 落地

`tools/make_zip.py` 的 `_is_cyndilib_junk()` **早已**按文件名（`directshow` / `uwp`）在**写包时跳过**
（不删 dist，避开 safe-delete 钩子）。实测该判据在当前 dist 上跳过 **68 个 cyndilib 文件 / 77.4 MB**
（含这两份 DLL）。最新产出的 zip 里 `Processing.NDI.Lib.*` 只剩 x64 一份，可佐证。

**结论：① 无需改代码，机制已就绪且经实测确认。**

---

## ② PySide6 未使用的 Qt 模块（约 23 MB）

### 取证 1：源码到底用了哪些 Qt 模块

`grep -rhoE "(from|import)\s+PySide6(\.[A-Za-z0-9_]+)*" src/` ⇒ 只出现三个：
**`QtCore`(16) / `QtGui`(19) / `QtWidgets`(11)**。再用
`grep -rnE "QOpenGL|QtSvg|QtNetwork|QtMultimedia|QtQuick|QtQml|QtPdf|QtVirtualKeyboard|\.svg"` 扫 src/
⇒ 只有 `glctx.py` 注释里提到一次 `QOpenGLContext`（并未真用），**无 SVG / 无网络 / 无 QML**。

### 取证 2：PE 依赖图（谁是「根」）

Qt6 Widgets 家族的静态依赖（`_pe_imports.py`）：

```
Qt6Core.dll    -> 仅系统 DLL
Qt6Gui.dll     -> Qt6Core
Qt6Widgets.dll -> Qt6Gui, Qt6Core
```

即 Core/Gui/Widgets **不依赖** Quick/Qml/Network/OpenGL/Pdf/Svg/VirtualKeyboard。
真正把这些拉进来的是 **Qt 插件**（PyInstaller 沿插件 DLL 的 PE 导入表自动跟随）：

| 插件（根） | 它拉进来的 DLL |
|---|---|
| **platforminputcontexts/qtvirtualkeyboardplugin.dll** | Qt6VirtualKeyboard → **Qt6Qml / Qt6Quick** → Qt6QmlMeta / Qt6QmlModels / Qt6QmlWorkerScript / **Qt6Network / Qt6OpenGL** |
| imageformats/qpdf.dll | Qt6Pdf → Qt6Network |
| imageformats/qsvg.dll / iconengines/qsvgicon.dll | Qt6Svg |
| networkinformation/qnetworklistmanager.dll / tls/* | Qt6Network |

（`platforminputcontexts` 里只有「Qt 虚拟键盘」插件；Windows 的原生 IME 由 qwindows 平台插件内置，
删掉这个插件不影响中文输入法。）

### 取证 3：临时改名 + 实测（用现有 dist，不重建）

把上表 19 个文件（10 个 Qt6*.dll + `QtNetwork.pyd` + 8 个插件）在 **dist 与 venv** 临时改名 `.bak`：

- 界面渲染（`tools/_uigeom.py --size 1600 900`，三页真实布局 + 全窗口截图）：
  几何日志与未改名基线**逐行完全一致**；截图逐像素基本一致（154113 vs 154120 B）；
  曲库列表 `67 项、带图标 67`；正常输出 `截图已存 … 1600x900`。
- exe 冒烟（`AUTOVJ_DIST=dist_new tools/smoke_exe.py 40 --isolated`）：`✅ 冒烟通过`。

验证完已全部还原（`_rename_qt.py status` 全绿）。合计 **23.3 MB**。

### 落地（两处配合）

- `tools/build_exe.py`：加 `--exclude-module PySide6.QtQuick/QtQml/QtPdf/QtSvg/QtNetwork/QtOpenGL/QtOpenGLWidgets`
  —— 拦住 Python 绑定（如 `QtNetwork.pyd`）与对应 hook。
  ⚠ `--exclude-module` **拦不住 DLL 本体**（它们是插件的二进制依赖，被 PyInstaller 自动跟随）。
- `tools/make_zip.py`：新增 `_is_qt_unused()`，在**写包时跳过**这 19 个文件（与 cyndilib 同一套可靠机制）。

实测判据：Qt 未用 **19 个文件 / 23.3 MB**。

### 不动项
`opengl32sw.dll`（19.7 MB）：Qt 的软件 OpenGL 兜底，删了会让没有正常 GL 驱动的机器起不来 —— **保留**。

---

## 合计预计可省
cyndilib 两份 NDI 库 **63 MB** + PySide6 未用 Qt 模块 **23.3 MB** ≈ **86 MB**（对应解压体积）。
