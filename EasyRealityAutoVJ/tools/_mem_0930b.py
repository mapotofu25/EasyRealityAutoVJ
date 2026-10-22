# -*- coding: utf-8 -*-
"""把"待办清单 14 项"的完成情况写进今日日志（文件方式，避免 shell 吃反引号）。"""
import io

P = r"C:\Users\Administrator\WorkBuddy\自动VJ软件\.workbuddy\memory\2026-09-30.md"

ADD = """

## 五、审计报告「待办清单」14 项全部处理完毕

### 高（5 项）
1. **fp._find_peaks**：每个候选峰的 41×41 邻域 `.copy()` 换成"切片视图 + 计数"
   （`count_nonzero(nb >= val) > 1` 等价于"去掉中心后 any(nb >= val)"）⇒ 零拷贝。
2. **fp._hashes**：`f1 // FREQ_QUANT` 提出内层循环、`b"%d|%d|%d" % (...)` 省掉 encode、
   常量提局部 —— ⚠ **刻意不改哈希算法本身**：换哈希 = 换指纹格式，用户库 1421 首得整库重扫。
   为此专门写了 `tools/_verify_perf_fp.py`，拿 P 盘备份里的**改动前** fp.py 当基准，
   三段合成音频的指纹**逐项完全一致**（161/490/0 条）⇒ 才敢说"不用重扫"。
3. **audio_engine 每块重算**：`rfftfreq` / 频带掩码 / `geomspace` 边界 / `sr // BLOCK` /
   kick 低频掩码 全部进 `_Analyzer._fft_plan()`（按采样率缓存，换设备自动重算）。
4. **删掉旧的"每 6 帧刷高亮"通路**（`_update_preview` 里）+ 随之变成死变量的 `_hl_tick`。
5. **`_loading_ui`**：全库没有任何一处置 True 的恒假守卫，删掉；并注明"要抑制就 blockSignals"。

### 中（6 项）
- 输出面板「重置窗口大小」tooltip 与实际行为对齐 + 改走 `T()`（原来英文界面还显示中文）；
  新文案由 `i18n_coverage.py` 抓到缺译，已补进 ZH2EN。
- `panels.result_data` 的长历史注释压成两行。
- **删掉 `i18n.missing_keys()`**：实现是 `sorted(k for k in ZH2EN if not k)`（恒为空、误导），
  且无任何调用方；真正的孤儿词条检查由 `tools/i18n_coverage.py` 负责。
- `tagger.analyze_media`：`locals().get("has_alpha", False)` ⇒ 显式 `has_alpha = False`。
- `beatgrid` 的 grid API：**审计说"已无调用方"，实测是误报** —— `tools/_grid_test.py`
  与 `tools/_qa_grid_e2e.py` 在用 `grid_anchor_after` / `grid_beat_index` ⇒ **保留**，
  只把顶部那句指向 `store_grid` 的注释改成"实际由曲库扫描直接写"。
- 同上：审计说"`phrase_bt` 无 ONNX 时会 import torch（几百 MB）"也是**误报** ——
  该模块是纯 numpy + onnxruntime，全文没有 torch。**没改**。
- 启动路径核实：`配置 0.00s / PyAV 0.00s / 启动画面 1.05s / 单实例锁 0.1s / 主窗口 2.1s / 合计 3.4s`，
  alpha/codec 探测本来就在后台线程（`_probe_alpha_async` / `_probe_codec_async`）⇒ 无同步重活。

### 低（3 项）
- `tools/` 归档 **153 个**历史一次性脚本 → `tools/_archive/`（**移动不删**）；原地留 51 个
  （21 个 git 跟踪的构建/selftest + 30 个当前工作集）。
- `dist_new/` 下 13 个旧产物只留最新两个 ⇒ **释放 8.8 GB**。
- 删掉失效的 `更新到新版.bat`（指向改名前的路径，还会打"完成！"的假成功）。

### ★★ 我自己造的一个回归（必须记住）
把 FFT 段的局部变量 `freqs` 改名成 `_freqs` 之后，**同一个函数体下面第 444 行**
`bass_mask = (freqs >= 20) & (freqs <= 200)` 还在用旧名 ⇒ `NameError` **每块都抛**
⇒ 采集线程把它捕获成 `st.error = "分析块错误（已跳过）: ..."`
⇒ **整机能量/BPM 全废**（现场状态栏红字、BPM 显示 `-`、能量 0.00）。
**为什么没被测试抓到**：当时只跑了"指纹恒等"和"频带平均"两个单测，**没跑 `_Analyzer.feed`**。
⇒ 新铁律：**改热路径里的局部变量名，必须 grep 整个函数体；改完必须真跑一遍热路径**
（已加 `tools/_audioerr.py`：直接喂 1024 帧给 `_Analyzer.feed`，异常完整打出）。

### ★★ 播放高亮：从"能亮"到"看得见"
`refresh_playing` 改按**路径**比对之后，用 `tools/_hl_diag.py` 验证过它确实在工作
（图层 2/3 各 2 格亮着 = 正在播的 + 过渡中的上一个）。但用户仍说"没修好"——
原因很实际：缩略图的 `iconSize = (CELL_W-8) × (CELL_H-10)` 几乎铺满格子，
**金黄底只在四周露出约 4px**，一屏深色素材里根本注意不到。
⇒ 新增 `_PlayBorderDelegate`：在缩略图**之上**再画一道 3px 金边（用 `PLAY_ROLE` 数据角色
传递"在播"标记，不依赖背景刷子）。真窗口实测：每格 104×58 整块变金，肉眼一眼可见。

### 取证三件套（以后遇到"用户说没修好"直接用）
- `tools/_hl_diag.py` —— 离屏起真 MainWindow + **真启动引擎**，打印每层 `cur`/clips/命中情况；
  ⚠ 不启动引擎就全是 `cur=-1`，会得出"没修好"的错误结论（我第一次就踩了）。
- `tools/_hl_grab.py` —— 抓 `ClipRow` 图并数金黄像素。
- `tools/_hl_live.py` —— 真窗口取证：抢前台 → 点页签 → 点开始 → **最小化输出窗口**
  （它一开就盖住图层区，不关掉根本看不到）→ 截图统计。
"""

io.open(P, "a", encoding="utf-8").write(ADD)
print("日志已追加，现", len(io.open(P, encoding="utf-8").read()), "字符")
