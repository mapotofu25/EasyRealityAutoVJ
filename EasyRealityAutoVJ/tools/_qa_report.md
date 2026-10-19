# QA 回归验证报告：卡帧看门狗修复（task #2）

- 验证人：Edward（QA）
- 对象：`src/engine.py` 的 `_heal_stalled` 熔断重写 + `_get_player` 健康校验
- 环境：`P:/AutoVJ/venv/Scripts/python.exe -u`，`QT_QPA_PLATFORM=offscreen`，未启动 GUI，未改 `src/`
- 素材：真实库 `Y:\EasyRealityAutoVJ\素材库\`（识别到 158 个真实视频；用真实 `.mov` 解码）
- config：`P:\AutoVJ\config.json`（`config.CONFIG_FILE = P:\AutoVJ\config.json`，source 运行）

## 总判决：**PASS**（核心冻结修复已实测有效）
- V1/V2/V3/V5 **全部 PASS**；V4 4 项中 3 项 PASS，1 项低危偏差（见下，非冻结回归）。
- Routing：**NoOne**（冻结修复可放行）；V4(d) 作为低危 Known Issue 记录，可选跟进。

---

## 基线（既有测试 + 静态三件套）——全部 PASS
- `tools/_test_freeze2.py` → `结果：全部通过 ✓`（VideoPlayer/AvAlphaPlayer/GpuDxvPlayer 自愈 7 场景）
- `tools/_test_spin_fix.py` → `结果：全部通过 ✓`（坏容器退避放弃，CPU≪墙钟）
- `tools/_test_freeze_heal.py` → `结果：全部通过 ✓`（`_open` 失败自愈 / 退避收工）
- `tools/_test_heal_rate.py` → `结果：全部通过 ✓`（假播放器 + 可控时钟）
- `tools/_scan_dupdef.py` → `共 0 处重复定义`
- `tools/_scan_locks.py` → 仅 1 条**既有**提示（`latest_frame` 无锁，GUI 每帧读取，预期无锁），非新增
- `tools/_scan_removed_defs.py` → `全部文件：没有丢失任何定义 ✓`；engine.py 仅新增 3 个熔断常量

---

## V1（最重要）正常播放绝不被误判 —— **PASS**
真实解码器 + 真实素材 + 真实 `_heal_stalled`/`_get_player`，跑真实时间。
命令：`QT_QPA_PLATFORM=offscreen venv/Scripts/python.exe -u tools/_qa_v1_real_playback.py`

| 场景 | 播放器 | 时长/ticks | 累计产帧 | 触发重建 |
|---|---|---|---|---|
| A `1 (3).mov` | VideoPlayer(OpenCV) | 40.0s / 2342 | 1187 (~30fps) | **0** |
| B `3D质感 (2).mov`(强制alpha) | AvAlphaPlayer(PyAV) | 30.0s / 1724 | 886 (~30fps) | **0** |
| C 4 素材每 6s 切换 | VideoPlayer | 32.0s / 1832 | 2925 | **0** |

原始输出：`✓ V1-A ... 触发重建 == 0   triggers=0` 等，`结果：V1 全部通过 ✓`
结论：**正常播放的素材触发次数 = 0**（含"刚切进来"的时序），且有真实产帧佐证（非"从不出帧"钻空子）。

## V2 长跑不复发风暴 —— **PASS**
命令：`venv/Scripts/python.exe -u tools/_qa_v2_storm.py`（可控时钟模拟 10 分钟 = 36000 ticks）
- (a) 20 图层健康场景 10 分钟：总重建 **0** 次。
- (b) 单素材"出过帧后卡死"：总重建 **3** 次（旧行为 ≈ 36000 次）。
- (c) 20 图层同时卡死：总重建 **60** 次（= 20×3 封顶；旧行为 ≈ 720000 次）。
结论：重建次数**有界**，与 tick 数无关。

## V3 判据修正正确 —— **PASS**
命令：`venv/Scripts/python.exe -u tools/_qa_v3_criteria.py`
- (1) 真实播放器指向**不存在文件**：跑 10s，`_frames==0`，`_ended=True`（解码线程自救收工），
  看门狗重建 **0** 次（根因②消除）。
- (2a) 出过帧后卡住、重建后健康：恰好重建 **1** 次，延迟 0.00s（≤5s），60s 内不反复。
- (2b) 出过帧后卡住、重建后仍卡：120s 内重建 **3** 次，相邻间隔 `['5.05','5.05']s`（≥冷却 5s，封顶 3）。

## V4 边界与不变式 —— **3/4 PASS + 1 低危偏差**
命令：`venv/Scripts/python.exe -u tools/_qa_v4_boundaries.py`
- (a) `running=False` → 重建 0 次、播放器未关 ✓
- (b) 到顶(3)后不再重建；`放弃重建`日志**只写 1 条** ✓
- (c) 换素材后 `_heal_path/_heal_last_t/_heal_count/_heal_gave_up` 确实复位，新素材立即被救 ✓
- (d) 健康播放器 → 重建 0 次、`health()`=0 ✓；但 **`stalled()` 被每 tick 调用** ✗
  - 失败断言原文：`✗ 热路径未调用 stalled()   stalled_calls=600`
  - 最小复现：`venv/Scripts/python.exe -u tools/_qa_repro_stalled_calls.py` →
    `tick 数=600  stalled() 调用次数=600（期望 0）  health()=0（符合）`
  - 根因：冷却闸门用 `lay._heal_last_t`，只有**重建时**才更新；健康素材从不重建 ⇒ 恒为 0 ⇒
    闸门永不拦截 ⇒ 每 tick 调一次 `stalled()`。`stalled()` 是 O(1)（无 I/O、无重建），
    **不复发卡死**（V1 已证 0 次重建），属**规格偏差/微优化缺失，低危**。

## V5 不破坏已有修复 —— **PASS**
命令：`venv/Scripts/python.exe -u tools/_qa_v5_regression.py`
- `_get_player` 命中 `_ended`/`_dead` 僵尸 ⇒ 丢弃重开（close 1 次、返回新播放器、缓存更新）✓；健康缓存命中则复用 ✓
- 三个 `_loop` 自愈仍在：3 个 `_loop`、`_REOPEN_MAX` 引用 9 处、`ever` 10 处、退避常量、`_STALL_SECS` 均在 ✓

---

## 限流参数评估（5s 冷却 / 每素材 3 次 / 全局 2s）
- V1 实测：**正常场景（真实解码 30~40s、多素材切换）触发 0 次** ⇒ 参数在正常运营中**永不触发**，
  故"是否过紧"对正常播放**无影响**。判定：**不激进，参数合适**。
- 真卡帧时最坏恢复时延 ≈ 检测 `_STALL_SECS`(1.5s) + 冷却(5s) ≈ 6.5s 才首次重建，属可接受
  （远优于"永久冻帧"）；多图层同时卡死由全局 2s + 每素材 3 次封顶共同兜住（V2(c) 实测有界）。
- 结论：**保持现值**。无需为"救得更快"放宽（放宽会牺牲熔断）。

## 交付物（均在 `tools/`，未改 `src/`）
`_qa_v1_real_playback.py`、`_qa_v2_storm.py`、`_qa_v3_criteria.py`、`_qa_v4_boundaries.py`、
`_qa_v5_regression.py`、`_qa_repro_stalled_calls.py`（+环境探针 `_qa_probe_*.py`）
