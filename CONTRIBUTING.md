# 贡献指南 / Contributing

感谢你有兴趣为 **Easy Reality AutoVJ** 出力！
这是一个**面向现场演出**的 Windows 自动 VJ 软件，稳定性优先级高于一切 ——
改动前的第一问永远是：**「演出中它会不会把画面弄崩？」**

Thanks for helping! This is a **live-show** auto-VJ tool for Windows,
so stability beats features: always ask *"could this break a show?"* first.

---

## 一、怎么参与 / How to contribute

| 方式 | 说明 |
|---|---|
| **报 Bug** | 开 Issue，附上「版本号 + 复现步骤 + 日志」。**版本号**在程序「关于」里，形如 `2026.10.01.06`。 |
| **提需求** | 开 Issue 说清使用场景（什么样的演出、什么设备、希望软件做什么）。 |
| **提 PR** | 见下文流程。**欢迎**，尤其欢迎「性能」与「稳定性」方向的修复。 |
| **报兼容性** | 什么显卡 / 什么采样率 / 什么输入设备会出问题，对现场稳定性帮助极大。 |

**报 Bug 请务必附上**：版本号、日志文件（程序里「打开日志目录」按钮，
重点是 `startup_error.log` 与 `engine_error.log`）、以及出问题时的**操作路径**。

> 💡 描述现象时请尽量带上**因果线索**，例如
> 「以前不会，这次开了一个小时才慢慢变卡」「一切到某个素材就卡住」
> —— 这类信息往往一句话就能定位到根因。

---

## 二、从源码跑起来 / Run from source

需要 **Windows + Python 3.11+**（64 位）。GPU 解码与 NDI 输出都是**可选**的，
缺依赖时会自动降级，不影响启动。

```bat
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
venv\Scripts\python.exe src\main.py
```

打包与冒烟（发布流程，改了代码就请跑一遍）：

```bat
venv\Scripts\python.exe tools\build_exe.py      :: PyInstaller 单目录打包 → dist\EasyRealityAutoVJ\
venv\Scripts\python.exe tools\smoke_exe.py 40   :: 40 秒冒烟：进程存活 且 没有新的 startup_error.log
venv\Scripts\python.exe tools\make_zip.py       :: 生成发行压缩包
```

> ⚠ 冒烟测试默认会结束**所有**同名进程。如果你自己正在用这个软件，
> 请加 `--isolated`，它会用临时数据目录启动，只关掉自己起的那个实例。
>
> ⚠ **只改文档不必打包。** 打包会给版本号 +1，导致包名与已发布产物对不上。

---

## 三、改代码前请先跑这两个自检

项目吃过「同一个类里**同名方法写了两遍**、后一份静默覆盖前一份」和
「GUI 直接调用引擎方法但**忘记加锁**」的亏 —— 这两类问题**编译器不报错、测试也不报错**，
已经在现场炸过。所以：

```bat
venv\Scripts\python.exe tools\_scan_dupdef.py   :: 同名方法重复定义
venv\Scripts\python.exe tools\_scan_locks.py    :: 引擎方法的加锁情况
```

**任何 PR 都请在说明里贴上这两个脚本的输出。**（`tools/_*.py` 属开发期脚本，
默认不进仓库；这两个请随 PR 一起提交，或直接在 PR 里粘输出。）

---

## 四、几条硬约定 / House rules

这些是踩过坑换来的，请务必遵守：

1. **不要用控件文本判断逻辑分支。** 界面支持中英切换，`if button.text() == "播放"`
   这种写法切到英文就失效。请改用 `objectName`、`Qt.UserRole` 或索引。

2. **新增/修改界面文案，必须补 `src/i18n_map.py`。** 本项目 i18n 以**中文原文为 key**；
   补完请跑 `tools\i18n_coverage.py`，它扫不到残留中文才算过。
   动态拼接的文本要 `setProperty("_i18nDynamic", True)`。

3. **主线程绝不做耗时活儿。** 解码、扫描、探测一律放后台线程；
   跨线程传帧只传「轻量信号 + 一份最新帧」，**不要把整幅图塞进信号参数**
   （曾经 30 秒涨到 3 GB）。

4. **等锁一律带超时。** 任何可能被驱动/解码卡住的调用，都不能持有别人需要的锁；
   等不到锁要**记日志并降级继续跑**，而不是永久等待 ——
   否则用户连「停止」「关闭」都按不动，只能强杀进程。

5. **改函数签名，必须 grep 全部调用点。** Python 不会报错，只会静默改变语义。

6. **修根因，不要用「关掉这个功能」绕过去。** 性能问题先问「哪条路径有 bug」。

7. **改了能量/节奏算法，必须 grep 所有消费点核对阈值**（档位判定散落在多处）。

8. **批量正则改代码之后，必须做一次编译验证**再提交。

9. **不要在仓库里留临时文件。** 测试夹具请用 `tempfile.mkdtemp()`（**不要**传 `dir=项目根`）
   落到系统临时目录；诊断截图、探针脚本、日志一律不要提交（`.gitignore` 已兜了一层）。

10. **不要把模型权重、素材、发行包提交进仓库。** 仓库只放「源码 + 面向用户的文档 + 构建脚本」。

---

## 五、PR 流程 / Pull request workflow

1. **Fork** 本仓库，从 `main` 切一个描述性分支（如 `fix/two-frames-on-transition`）。
2. 一次 PR **只做一件事**，改动面越小越容易合并。
3. 提交前自查：
   - [ ] 能正常启动（`src\main.py` 或打包后跑冒烟）
   - [ ] 跑了 `_scan_dupdef.py` / `_scan_locks.py`，输出干净
   - [ ] 改了文案 → 跑了 `i18n_coverage.py`
   - [ ] 改了热路径（音频分析、解码、渲染）→ 说明里写清**怎么验证的**
4. 提交信息请说清「**修了什么 / 为什么**」，不要只写「update」。
5. PR 描述里请写明：**改了哪些文件、怎么复现、怎么验证、有什么副作用**。
6. 涉及大改（界面重构、新增依赖、改引擎结构）**请先开 Issue 讨论**，别直接上 PR。

> 依赖方面：**新增第三方库请先说明理由**。本项目打包体积与「现场零安装」体验都很重要，
> 能不加依赖就不加；涉及许可证不兼容的库（尤其 GPL / 非商业）**一律不接受**。

---

## 六、许可证与署名 / License & attribution

- 本项目**源代码**采用 **MIT**（见 [`LICENSE`](LICENSE)）。
- **提交 PR 即表示你同意：你的贡献同样以 MIT 授权给本项目**
  （即你确认你有权这样做，且同意将该贡献置于 MIT 之下）。
- **原作者署名必须保留**：请勿移除或改写 `LICENSE` 中的版权行、
  `NOTICE.md` 中的第三方署名、以及「关于」界面里的致谢信息。
- 第三方组件与模型的许可（含 **Discogs-EffNet 的非商业限制**）见 **[`NOTICE.md`](NOTICE.md)**，
  **请务必阅读**。
- **名称「Easy Reality AutoVJ」不随代码授权。** 你可以在 MIT 下自由使用、修改、分发**代码**，
  但**不得用这个名称发布衍生版本**，以免与官方版本混淆。想发布自己的分支，请换个名字。
- 注意：**打包发行版内含 CC BY-NC-SA 4.0 的模型，因此发行包仅限非商业用途。**
  提 PR 时请勿引入需要商业授权的新组件。

---

## 七、交流 / Contact

- Issue / PR：首选，公开可追溯。
- QQ 群 **ERAVJ测试**（543643838）：面向使用者的答疑与反馈。

---

<div align="center">

**Easy Reality AutoVJ** · MIT（代码）· 非商业（含模型的发行版）

</div>
