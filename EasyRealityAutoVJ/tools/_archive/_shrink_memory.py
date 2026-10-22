# -*- coding: utf-8 -*-
"""把 MEMORY.md 的三节压短（体积已超注入上限，必须精简）。"""
import io
import os

P = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")),
                 "WorkBuddy", "自动VJ软件", ".workbuddy", "memory", "MEMORY.md")
P = os.path.abspath(P)

NEW_BEAT = """## 节拍网格 / 八拍相位（`src/beatgrid.py`）
- 统一约定：**八拍头 = `anchor` + 8n×`beat_len`**，消费方只记这一条。`GRID_VER` 变更触发全库重算。
- ★ **做识别算法前先问「实时还是离线」**：需整首歌信息才能判准的（如 8 拍乐句相位）**必须建库时离线算一次并入库**，演出中只**查表**（引擎有 `recognized_song_id`/`recognized_offset`：每 0.5s 用最近 4.5s 指纹匹配声部）。实时特征只做**兜底**，绝不做整曲 STFT。
- ★ **离线网格「只存相位、不存时间戳」**：只存"每 8 拍里第几拍是乐句头"。**变速不改变相位** ⇒ 免疫变速。
- 数据源优先 **VirtualDJ 库**（`W:\\VirtualDJ\\database.xml`）；字段语义/三级匹配/本机 3 个库的坑见 REFERENCE §五。无 VDJ 才走本地 Beat This。
- ⚠ **别把锚点折叠到 `[0, period)`**（会丢「锚点=小节头」，八拍头整体平移）。
- ⚠★★ **八拍相位才是难点，BPM 本就算得准**：现有「冷启动」离线方法判小节相位 **9/9 首全错** ⇒ 无 VDJ 数据时不可信。0/4 判反靠手动「八拍相位翻转」修（人工兜底，**不能当主方案**）。
- ★★ **本地小节识别 = `src/phrase_bt.py`（Beat This! / CPJKU，代码与权重均 MIT 含商用）**：自转
  `small0_fixed.onnx`（9.9 MB，`onnxruntime` 已在包里 ⇒ 零新依赖、不装 torch），**小节相位 21/21 判对**。
  **默认窗口 60s（1.91 秒/首），拍数不足自动延到 180s**；前处理与模型来源见 REFERENCE §五。
  - **定锚点三铁律**：① 不能用 BT 全局 BPM 展开小节序号（0.2~0.8% 误差会累积）；② 不能拿 downbeat
    直线拟合（会多检/漏检）；③ 正解 = **bar 抗离群 LS（`_fit_bar` 3 轮）+ 相位中位数折叠**。
  - ⚠ **BT 只到小节级（4 拍），8 拍乐句头仍要自己判**（低频重音/换和弦）。
- ★★ **引擎拍钟跟不上变速**：`beat_interval` 每块被 `60/bpm_smooth` 重设 + 锁定后只接受 ±8 BPM ⇒ **>±5~6% 冻结**；要跟变速须**另立"网格驱动"路径**（`engine._beat_pos()`），不是调参。
- ★★ **指纹认歌的变速容忍度**：**开 keylock ⇒ ±10% 仍能认出**；**关 keylock ⇒ 彻底失效**。⚠ 变速下 `recognized_offset` 应为 `off + 4.5*r`。
- ⚠ `phrase_scores()` **必须传全局拍号 `ks`**（否则整体平移）。
"""

NEW_CUE = """## 码表 / 时间码（`.ercue`）
- 包 = zip（`cuesheet.json` + 可选 `media/`）；`format_version`/`min_app_version`/`requires_features`
  三件套（**只提示不阻断**）。模块：`cuesheet.py`（模型+IO）、`cuesheet_runtime.py`（解释器）、
  `position_source.py`（probe/start 分离，VdjSource 纯 Python OSC）、`cue_editor.py`（编排界面）。
- **码表图层 = 容器**：`Layer.is_cuesheet` + 子层替身 `_CueSubLayer`（`clips` 用真实 `MediaItem`）。
  ⚠ `VideoPlayer._active` 默认 False，必须 `_cue_set_active()` 每帧激活。
  - 图层头**青绿** + **码表徽标 `btnCue`**（未绑定=橙虚线警示；已绑定=码表名，点一下进编排界面）；
    容器**隐藏 α/锁/混合**（对容器无意义）。
  - ★★ **不受画面效果影响**（同 Kv，用户明确要求）：`engine._cuesheet_on()` ⇒ `_apply_postfx` 整条跳过、
    `_apply_color` 把 `kv_on` 顶到 1.0。
- ★ **编排界面是 PR 式**：上 = 左标签页（①曲目 ②码表包·素材 ③片段属性）+ 右**节目监视器** `CuePreview`
  （`latest_frame()`、8fps、可见才刷）+ 模拟驱动滑杆；下 = **时间轴全宽**。
  **曲目 = 绑定音乐**（`self._ti` + `_track()`，不再 `tracks[0]`）；「导入音频…」用 mutagen 读标签/时长，
  BPM 取 `cfg["music_meta"][path]["grid"]["bpm"]`。标尺自适应 `CueTimeline._ruler_plan(pxb)`
  （数字间距 ≥30px 才画、竖线 ≥2px）。
- 内嵌素材**解在包旁边 `<包名>.media/`**（不可写才退回 app_base_dir）。**用户要求：东西不要放 C 盘**。
- `probe()` 必须绑 `0.0.0.0`；示例码表必须**自包含**（`embed=True`）。
- 设计文档：项目根 `_码表功能设计.md`。待办：`src_in/src_out`（播放器无 `seek()`）、VDJ 实测。
"""

NEW_WORK = """## 工作方式
- ★ **打包不用问，直接打**（2026-09-26 授权）；**卡死/无响应实例直接 `taskkill`**。**只改文档时不要 build**。
- ★★ **修根因，不要用「关掉功能」规避问题**（用户明确表态）。性能问题先查"哪条路径有 bug"。
- ★★ **不要主动写更新文案**（`更新内容*.txt`）。索要时**必须「公式化清单体」**：
  `【修复】/【新增】/【优化】/【改进】+ 一句话`，**不用第二人称、不写叙述、不解释原理**，限制单列末尾。
- 大改 UI 前先确认全部需求；改动偏大先列方案。**批量正则改代码必须编译验证**。
  **改函数签名必须 grep 全部调用点**；**「探测能力」与「真正建立」必须是两个函数**。
- 测试脚本：不调带模态弹窗的函数；合成状态前 `audio.stop()`；离屏测试清 `_drag_pause_until`。
- ★ **涉及用户状态的结论必须复查**（曾把旧快照当实时状态误报 bug）。
- 沙箱：`reg.exe`/`wmic.exe` 黑名单；bash 无 `head/tail/grep`；**批量删除钩子每回合限 50 个文件**。
- ⚠ **`Config` 没有 `.get()`**（只有 `__getitem__`）——`cfg.get(k)` 会**静默失败**（异常被吞），必须 `cfg[k]`。
- ⚠ **多行中文正文一律用 Write 工具落文件**：塞进 `python -c "…"` 里时，正文的**反引号 / `$()` 会被 shell 先执行**（曾把整段日志写坏）；**多行补丁写成文件再执行**（heredoc 会吃行尾）。
"""


NEW_GH = """## GitHub 开源 / 发布（要点，细节见 REFERENCE 第四、五节）
- 仓库 `https://github.com/mapotofu25/EasyRealityAutoVJ`（公开）。代码 MIT；`NOTICE.md` 标第三方与模型
  许可。只放「源码 + 面向用户文档 + 构建脚本」。
- ❌ **不上传**：`venv/ dist*/ build*/ *.spec *.exe`、发行 zip、`assets/models`、`assets/*.png` 截图、
  `更新内容*.txt`、**内部工程笔记**、`tools/` 一次性探针、`_zips_old/`、`config.json`、`.workbuddy/`、
  `_ui_shot/`。全进 `.gitignore`；一律 `git rm --cached`（保留本地文件）。
- ⚠⚠ **推送必须绕开凭据助手**（否则永久挂住）：`git -c credential.helper= -c http.postBuffer=524288000
  push -f origin main`（URL 带 token）；`git ls-remote` 加 `GIT_TERMINAL_PROMPT=0`。
- 发 Release 用 `tools/_make_release.py`（draft → 流式传附件 → 校验/改 ASCII 名 → PATCH 发布）；
  ⚠ **附件名不允许非 ASCII**；大文件用 `http.client` 流式；含中文 `.bat` 显式 `utf-8` + `chcp 65001`。
- 打包：`build_exe.py` → `make_zip.py` → **必须 `smoke_exe.py 40`**（存活且无新 `startup_error.log`）；
  判据 = **BUILD_VER 变了 + 日志有 `Build complete`**。⚠ 清 `build/` 用 **`os.rename`**：删除会被额度钩子拦
  且**连坐整条 `&&` 链**；旧 dist 同样改名成 `_prev_…`（**别 rmtree**）。
- ★★ **`dist/` 被占住（WinError 32）**：`dist` 改名/删除都失败而**子文件能改名** ⇒ **目录句柄**被占。
  `build_exe.py`/`make_zip.py`/`smoke_exe.py` 都认 **`AUTOVJ_DIST`** ⇒ **`AUTOVJ_DIST=dist_new`** 另开目录照跑。
- ★★ **cyndilib 瘦身 = 「写包时跳过」**（`make_zip._is_cyndilib_junk`）：**不再删 dist 里的文件** ——
  逐文件 `os.remove` 会被 safe-delete 钩子拦并**终止整个进程**（打包直接死）。跳 68 个≈80MB，包不变大。
"""


def replace_section(text, header, new):
    i = text.find("\n" + header)
    if i < 0:
        raise SystemExit("找不到小节: " + header)
    j = text.find("\n## ", i + 1)
    if j < 0:
        j = len(text)
    return text[:i + 1] + new + text[j + 1:]


t = io.open(P, encoding="utf-8").read()
before = len(t.encode("utf-8"))
t = replace_section(t, "## 节拍网格 / 八拍相位（`src/beatgrid.py`）", NEW_BEAT)
t = replace_section(t, "## 码表 / 时间码（`.ercue`）", NEW_CUE)
t = replace_section(t, "## 工作方式", NEW_WORK)
t = replace_section(
    t, "## GitHub 开源 / 发布（要点，细节见 REFERENCE 第四、五节）", NEW_GH)
io.open(P, "w", encoding="utf-8", newline="").write(t)
after = len(t.encode("utf-8"))
print("MEMORY.md  %d -> %d 字节" % (before, after))
