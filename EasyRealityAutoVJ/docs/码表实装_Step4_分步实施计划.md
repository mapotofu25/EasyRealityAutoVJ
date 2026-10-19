# 码表实装 · **Step 4 分步实施计划**（分段交付，每段可独立回滚）

> 作者：工程师（寇豆码）· 2026-10-17 · **状态：待团队确认后排期**
> 配套：`docs/码表实装_Step4_任务书草案.md`（契约与门槛）· `tools/cue_should_seek_test.py`（已交付：25 用例 + 5 变异全抓）
> 总原则：**一次只交付一段、每段自带门禁与回滚点**；任一段过不了门槛 ⇒ **回滚该段**，不整体重写。

---

## 0. 为什么这样切（切分依据）

1. **先把"零风险但被依赖"的东西铺好**：政策模块（纯逻辑）、播放器两个只读方法、`_draw_layer(crop)`。
   这三样都能**独立证明"对现有行为零影响"**（无调用者 / 默认参数 / 0 字节像素差异），后面出问题时不至于怀疑它们。
2. **热路径改动（`_compose` 分支、`_cue_frame`）单独成段**，每段必须附 fps A/B 与像素 diff。
3. **解码器生命周期放最后一段但单独可回滚**（它是 9-30 事故的一票否决项，必须能在不影响画面的前提下单独回退）。

---

## 1. 分段总览

| 段 | 内容 | 热路径 | 风险 | 依赖 | 门禁（必须全绿） |
|---|---|---|---|---|---|
| **S0** | **新增 `src/cue_seek_policy.py`**（零 Qt 政策模块） | 否 | 低 | 无 | 单测 `--impl real` 25/25 + 变异 5/5 |
| **S1** | `media_manager` 加 `seek()` / `position()`（**零调用**） | 否 | 中 | S0（无） | fps/像素逐值一致 + `position()` 同源单测 |
| **S2** | `_draw_layer(..., crop=None)`（**零变化**） | **是** | **高** | 无 | 30 帧**0 字节差异** + crop 用例 diff + fps 一致 |
| **S3** | `Layer` 字段 + `_CueSubLayer` + `set/clear_cue_sheet` 数据面 | 否 | 中 | S0 | 无头装载/卸载 + 解码器计数归零 |
| **S4** | `_cuesheet_tick` 接线（每帧算 `cs_states`） | 是（轻） | 中 | S3 | 与 Step1 runtime 输出逐值一致 |
| **S5** | `_draw_cuesheet_layer` + `_compose` 分支（**出画**） | **是** | **高** | S2/S4 | 单层出画/z 序/α²/crop/缺素材 + fps A/B |
| **S6** | `_cue_frame` + seek/position 接线 + 防自激 + 计速 | **是** | **高** | S0/S1/S5 | 单测 `--impl real` + 入点/冻结/漂移 + seek 次数上界 |
| **S7** | 解码器生命周期 + 收尾（apply_color/postfx/close）+ 资源实测 | 是（轻） | **高** | S5/S6 | §9.1 全部门槛 + 30 次启停 + 退出无残留 |

> S1 虽在 S2 之前，但两者互不依赖；若想更快拿到"零变化"证据，可先做 S2（`crop` 默认参数对现有调用零影响，最容易证明）。

---

## 2. 逐段详情

### S0 · 新增 `src/cue_seek_policy.py`（零 Qt，纯逻辑）

**为什么先做它**：`_cue_should_seek` / 档位 / 落地判定 / 计速这些判断**全部是纯逻辑**。
放进引擎里就只能"跑起来试"，放进零 Qt 模块就能**用假播放器桩逐条断言 + 变异验证**。
否则 `tools/cue_should_seek_test.py` 只能测"我抄的一份参考实现"——**测副本没有意义**。

**改动**
- 新增 `src/cue_seek_policy.py`：`CueSeekPolicy`（`mode() / drift_limit() / land_tol() / settle() / should_seek() / on_seek() / desired_speed()`），常量按任务书 §4.1。
- **不 import PySide6、不 import engine、不碰 IO**（`python -m cue_seek_policy` 可自检）。
- 本段**不接进引擎**（引擎仍在用旧 `_cue_frame`）⇒ 对现网零影响。

**回归命令**
```
python -m compileall -q src/cue_seek_policy.py
python tools/cue_should_seek_test.py --impl real          # 25/25
python tools/cue_should_seek_test.py --impl real --mutations   # 5/5 被抓
```
**通过判据**：上述两条命令退出码均为 0；且 `grep -c PySide6 src/cue_seek_policy.py` = 0。
**回滚点**：删除该文件（无任何引用 ⇒ 零影响）；单测自动回落到参考实现。
**额外收益**：把"档位/阈值/防自激"从引擎热路径里**物理隔离**，Review 时不需要读 3300 行 engine.py。

---

### S1 · `media_manager` 只加 `seek()` / `position()`（**零调用**）

**改动（只加不改）**
- `VideoPlayer`：`seek(sec) -> bool`（投递请求，解码线程消费，**含前向解到 `pts >= sec`**）、`position() -> float|None`（读缓存 pts）。
- `AvAlphaPlayer` / `DxvGpuPlayer`：同签名同语义（PyAV 路径**必须**前向补齐，否则只到关键帧）。
- 私有成员：`_seek_req` / `_seek_deadline` / `_pts` / `_pts_lock`。
- ⛔ 不动 `read/current/set_speed/set_active/warm_up/close`、不动 pacing、不动缓存/线程。
- 本段 **engine 一行不改、零调用** ⇒ 行为必须与今天**逐值一致**。

**回归命令**
```
python -m compileall -q src/media_manager.py src/engine.py
grep -n "\.seek(" src/engine.py        # 期望：0 命中（证明"零调用"）
python tools/_bench_player.py          # 现有播放器基准（改动前后各跑一次）
python "P:/ERAVJ/brand/ui_audit/_fps_ab.py"   # 与 baseline 对比
```
**通过判据**
- `grep "\.seek(" src/engine.py` = 0；
- 基准 `video_fps` / `compose p50/p95` 与 `brand/ui_audit/_baseline_fps.json` **逐值一致（<1%）**；
- 新增的 `position()` 自检：随机 100 帧 `position()` == `current()` 那一帧的 pts（误差 = 0），首帧前返回 `None`。
**S1 实测（已跑，`VideoPlayer` 与 `AvAlphaPlayer` 两个通用路径）**
| 播放器 | 素材 | seek 目标 | 落点误差 | 落地耗时 |
|---|---|---|---|---|
| VideoPlayer(cv2) | 短 H.264（3.6s，GOP=109） | 0.5 / 2.0 / 3.0 | **0.000 / 0.000 / 0.000** | 39 / 87 / 111 ms |
| AvAlphaPlayer(PyAV) | 同片（**长 GOP**） | 1.5 / 3.0 | **0.000 / 0.000** | **347 / 623 ms** ← 前向补齐生效（不补会停在 0.0） |
| AvAlphaPlayer(PyAV) | DXV（0.9s，GOP=1） | 0.2 / 0.5 / 0.8 | **0.000** | 35 / 39 / 40 ms |
- 首帧前 `position()` 一律 `None`；非法参数（负数/字符串/NaN）一律 `False`；
- 播放正常前进（0.45s 后 +0.433），说明 seek 没有破坏 pacing。

**⚠ S1 待裁定**：`DxvGpuPlayer`（GPU 实验路径，只接管 DXV）的帧生成器 `_gpu_frames()/_soft_gen()`
不暴露 pts，**无法在不改生成器的前提下实现 `seek/position`**。两个选项：
  · **(a) cue 场景禁用 GPU 解码**（DXV 走 `AvAlphaPlayer`，已实测 39ms/0 误差）——改动最小、行为可测；
  · (b) 给 `DxvGpuPlayer` 的生成器加 pts 输出 + 重启式 seek —— 侵入既有实验路径，风险高。
  **我建议 (a)**（码表本就要求"可定位"，GPU 路径的收益抵不过风险）；等裁定后再动。

**回滚点**：删这两个方法（无调用者 ⇒ 零影响）。

---

### S2 · `_draw_layer(..., crop=None)`（**零变化**）

**改动**
- `src/engine.py:3180` 签名末尾加 `crop=None`；内部 `src_rect` 归一（取整/钳位/越界收缩）；
  `iw, ih = (src_rect.w, src_rect.h) if src_rect else (原图宽高)`；
  `src_rect is None` ⇒ **走今天一字不差的 `drawImage(tgt, image)`**；否则 `drawImage(tgt, image, src_rect)`；
  glitch 分支改成"在 `src_rect` 内按行切"（`src_rect=None` 时与原行为逐像素一致）。**绝不 `image.copy()`**。

**回归命令**
```
python tools/cue_pixel_diff_guard.py --scene norcrop --frames 30   # 新增（附录 C.3）
python tools/cue_pixel_diff_guard.py --scene withcrop --frames 30
python "P:/ERAVJ/brand/ui_audit/_fps_ab.py"
python tools/twoside_parity_check.py --pkg <真实包>               # Step1 门禁不许退化
```
**通过判据**
- **无 crop 场景：30 帧逐字节 0 差异**（改造前抓一份基准帧，改造后逐帧 `QImage` byte 比对）；
- crop 场景：与 Cue 端同参数渲染同帧 diff 在容差内、**不拉伸**（以裁切尺寸为基准）；
- fps / compose 与 baseline 一致（<1%）。
**★ S2 前置门禁（架构师复核补充，缺一不可）**

**(A) 确定性：不冻结时钟，30 帧 diff 永远 ≠ 0**
- `patch` **`engine.time.perf_counter`**（只 patch `engine` 模块里的引用，不动全局 `time`）为固定值或自增计数器；
  `_compose` 的 `prog`（3080-3085）与 `_draw_layer` 的 `now`（3200）都读它；
- **glitch 分支用 `rng = random.Random(int(now * 1000))`（3263-3264）** —— 而 glitch 恰恰是 S2 要重写的分支，
  不冻结就两难：要么把它排除在 diff 外（等于没验重写分支），要么永远红；
- 同时固定 `snap["bass"]`（pulse）、`silent`、`cfg["auto"]["intensity"]`（3041）；
- **基准帧必须"改造前用同一套冻结参数生成并存档"**，禁止用现场截图当基准。

**(B) 场景矩阵（覆盖 `_draw_layer` 全部分支）**
`fade(prev+cur)` · `slide` · `zoom` · **`glitch`（重写分支，必须逐字节）** · `cut` ·
**`rot≠0` 旋转（3229-3234）** · **`use_alpha=False` 扁平化（3189-3198）** · **`subtract` 图层（同走 `_draw_layer`）** ·
cover vs contain（xform 默认/非默认） · `pulse≠1`。
`solo / hide_silent` 属 `_compose`（3056/3062）⇒ 由 **S5 的 norcrop 0-diff 复跑**覆盖。

**(C) 漏掉的绘制分支：旋转分支也必须切源矩形**
- 旋转分支（3229-3234）同样是 `painter.drawImage(_tgt, image)` ⇒ 补 crop 后必须改成
  `src_rect is None ? drawImage(_tgt, image) : drawImage(_tgt, image, src_rect)`；
- glitch 分支（3266-3274）由"按整幅切行"改成"**在 `src_rect` 内按比例切**"，`src_rect is None` 时与原式逐像素一致。

**回滚点**：单独 revert 这一次提交（默认 `crop=None` ⇒ 所有既有调用点行为不变）。
**风险提示**：这是唯一"改既有热路径函数"的段，**必须配像素证据**，缺证据不合入。

---

### S3 · `Layer` 字段 + `_CueSubLayer` + `set/clear_cue_sheet`（数据面）

**改动**
- `Layer.__init__`（`engine.py:372`）加 9 字段（`is_cuesheet/cuesheet_id/cs_states/cs_active/_cue_players/_cue_items/_cue_last_frame/_cue_sub_tmps/_cue_seek_state`），`to_dict()` 带 `is_cuesheet/cuesheet_id`。
- **照搬备份 `_CueSubLayer` 的完整 `__slots__`（284-317）**，不要写"看起来够用"的缩写版。
  `_draw_layer` 实际读取：`opacity`(3182) · `is_kv`(3183,getattr) · `lock_motion`(3186/3242) ·
  `use_alpha`(3189) · `_alpha_key/_alpha_img`(3191/3197) · `prev_player`(3199) ·
  `clips/cur`(3203) · `clips/prev_idx`(3204) · `trans_active`(3204/3245)。
  ⚠ 缩写版**恰好**因为 `getattr` 有默认值而不崩 —— **别依赖这个巧合**（S5 一改语义就会静默走错分支）。
  ⚠ `speed` 在 `_draw_layer` 里**根本不读**（计速在播放器 `set_speed`）⇒ **stub.speed 无作用，别误以为它能变速**。
  ⚠ `op = base_op × ps.opacity × ps.alpha`：两个 opacity 是**不同层**（容器层 vs 码表 item 有效值），**不双计**。
- 移植 `set_cue_sheet`(2222) / `clear_cue_sheet`(2246) / `cuesheet_status()`，接 Step1 的 `cuesheet.load` + `CueRuntime`。
- `_get_player`（2471）**开头**加 cue 早返回分支（§6.5），`_cue_players` 上限 `max(32, len+16)`，逐出三件套 `set_active(False) → close() → 删引用`。

**回归命令**
```
python tools/twoside_parity_check.py --pkg <真实包>     # 仍 PARITY_OK
python -c "…无头装载/卸载脚本…"                         # 见下判据
grep -n "\.seek(" src/engine.py                        # 仍 0 命中
```
**通过判据**
- 无头装载真实包 ⇒ `cuesheet_status()` 的 `title/tracks/bpm/res/warnings` 与 `cuesheet.load` 一致；
- `clear_cue_sheet(silent=True)` 后：`cs_states==[]`、`_cue_players=={}`、`_cue_items=={}`、`_cue_last_frame=={}`、`_cue_seek_state=={}`，且**解码器计数归零**（`close()` 被调用）；
- 普通图层播放未受影响（fps 一致，`_get_player` 早返回分支未命中）。
**回滚点**：独立提交，revert 后回到"无 cue 图层"的现状。

---

### S4 · `_cuesheet_tick` 接线（每帧算 `cs_states`）

**改动**
- `_tick_locked`（1128）里 `self._compose(snap)`（1226）**之前**插 `self._cuesheet_tick(snap)`。
- 移植 `_cuesheet_tick`(2443) / `_cue_set_active`(2420) / `_cue_item`(2489)；位置与认歌**用 Step3 的产出**（本段只消费 `pos_beats`）。
- `_preload_clips`（2533）对 cue 图层跳过自动预加载。

**回归命令**
```
python tools/cue_should_seek_test.py --impl real
python tools/_after_strip_smoke.py        # 现有无头冒烟
```
**通过判据**
- 喂**固定 `pos_beats` 序列**（0 → 80 拍，步长 0.25），`lay.cs_states` 的 `(item_id, play_sec, alpha, blend, opacity, src_ended)` 与**直接调 Step1 `CueRuntime.update()` 的结果逐值一致**（TOL=1e-6）；
- 图层可见性关闭时 `cs_states == []`；
- 普通图层 fps 无退化（多了 1 次/帧的轻量调用，目标 <1%）。
**回滚点**：删那一行 `self._cuesheet_tick(snap)` 即回到现状。

---

### S5 · `_draw_cuesheet_layer` + `_compose` 分支（**出画**）

**改动**
- 移植 `_draw_cuesheet_layer`(2542)（op = `base_op × ps.opacity × ps.alpha`，`prog = ps.trans_p`，`crop=ps.crop`，subtract 走 `_cue_sub_tmps`）。
- `_compose`（3027）在循环 `3051` 里、`if not lay.visible`（3052）**之后**、`if not lay.player`（3054）**之前**插 cue 分支。三个细节：
  ① **`hide_silent` 用 `getattr(lay, "hide_silent", False)` 防御**；`is_kv` 对 cue 恒 False（用 getattr 无害）；
  ② **cue 分支必须 `continue`** —— 绝不能落到 3064 的 `lay.player.current()`（码表容器层没有单一 player）；
  ③ `blackout` 在循环之后（3123）生效，cue 分支在其前 ⇒ **黑场仍覆盖，不用改**。

**回归命令**
```
python tools/cue_pixel_diff_guard.py --scene norcrop --frames 30    # 普通图层仍 0 差异
python "P:/ERAVJ/brand/ui_audit/_fps_ab.py"                          # 码表关 vs 开
python tools/cue_should_seek_test.py
```
**通过判据**
- 单层单片段出画；两子层 z 序正确；
- **α²**：`opacity=1 / trans_in=fade / L=2 拍` ⇒ 有效 α `0 / 0.25 / 1.0`；
- crop 生效且**不拉伸**；缺素材只跳该层（其余层照常）；
- **solo / 可见性 / hide_silent 与普通图层一致**（码表层不得"盖在最上"）；
- fps 退化 **<1%**、`compose p95 ≤ 1.01×baseline`。
**回滚点**：删 `_compose` 分支 + `_draw_cuesheet_layer`（`crop` 形参留着无害）。

---

### S6 · `_cue_frame` + seek/position 接线 + 防自激 + 计速

**改动**
- 按任务书 §5.2 重写 `_cue_frame`（消费 `play_sec/src_in/src_ended`；`settle → should_seek → seek → set_speed` 四步；`_cue_health` 每片段最多 1 条）。
- 全部判定走 **S0 的 `CueSeekPolicy`**（不在引擎里重写一遍）。
- 预热：`_cuesheet_tick` 的预加载窗口内对"下一个 item"先 `seek(src_in)`。

**回归命令**
```
python tools/cue_should_seek_test.py --impl real --mutations
python tools/cue_seek_probe.py --file <DXV> --file <长H264> --file <长HEVC> \
       --every 4 --seconds 10 --n 20 --cold 3 --out tools/_cue_seek_probe.md
python tools/cue_resource_probe.py --seconds 30        # 新增（本段必须有）
```
**通过判据**
- 单测 25/25 + 变异 5/5；
- 实机：`src_in` 首帧 `pts ≈ src_in_sec`（≤1 帧）；`|position()−play_sec| ≤ 1 帧`（DXV/H.264）/ ≤1.0s（HEVC）；
- `src_out` 到点后 **30 帧非空**且 `play_sec` 不再增长；
- **seek 次数在 §9.2 上界内**（30s：DXV ≤30 / H.264 ≤30 / HEVC ≤2），且 `set_speed` 序列里**没有 1.0**；
- 降级路径可见（状态栏 1 条 + 健康日志 1 条）。
**回滚点**：`_cue_frame` 回退到"只 `current()`"（等价"无入点/无冻结"），画面仍能出但会漂 —— **只作为紧急回退**。

---

### S7 · 解码器生命周期 + 收尾 + 资源实测

**改动**
- `_cue_set_active` 每帧遍历 **`lay.cache` + `lay._cue_players` 两个缓存**；LRU 逐出三件套；切歌/卸表/退出全关。
- `_apply_color`(2806) / `_apply_postfx`(2872) 加 `if self._cuesheet_on(): 跳过`。
- `engine.close`(3303) 前 `clear_cue_sheet(silent=True)`。
- 新增 `tools/cue_resource_probe.py`（附录 B.4）。

**回归命令**
```
python tools/cue_resource_probe.py --seconds 30 --csv cue_on.csv
python tools/cue_resource_probe.py --stress-restart 30        # 连续启停 30 次
python tools/twoside_parity_check.py --pkg <真实包>
```
**通过判据（附录 B.2/B.3 全绿）**
- 活跃解码器 **≤ 8**；缓存条目 ≤ `max(32, len+16)`；RSS 增量 ≤ +150MB@1080p；`num_handles` ≤ +2/min；
- 磁盘稳态均值 **≤120MB/s**、10s 峰 **≤250MB/s**、30min 斜率 ≈0；
- 连续启停 30 次后计数/handles 回基线（±2）；退出无残留线程/句柄；
- `clear_cue_sheet` 后解码器计数归零。
**回滚点**：独立提交（生命周期是纯"关东西"的改动，回退不会影响画面逻辑）。

---

## 3. 门禁矩阵（每段必跑的组合）

| 门禁 | 命令 | 何时必跑 | 不过怎么办 |
|---|---|---|---|
| **G1 数据层** | `python tools/twoside_parity_check.py --pkg <真实包>` | S2/S3/S5/S6/S7 | `PARITY_OK` 是硬门禁 ⇒ 停手上报（说明有人动了 Step1 语义） |
| **G2 政策单测** | `python tools/cue_should_seek_test.py --impl real --mutations` | S0/S4/S6 | 用例或变异漏检 ⇒ 修政策模块，**不许改测试期望** |
| **G3 像素** | `python tools/cue_pixel_diff_guard.py --scene norcrop --frames 30` | S2/S5 | 有差异 ⇒ 回滚该段（这是"误伤普通图层"的唯一证据） |
| **G4 帧率** | `python "P:/ERAVJ/brand/ui_audit/_fps_ab.py"` | S1/S2/S4/S5/S6 | 退化 ≥1% ⇒ 回滚该段 |
| **G5 资源** | `python tools/cue_resource_probe.py` | S6/S7 | 任一红线 ⇒ 回滚 S7，回 S6 查泄漏 |

---

## 4. 交付节奏与复核

1. 每段一个提交（commit message 前缀 `cue-step4/Sx:`），**附带该段的门禁输出**（贴数字，不贴"已通过"）。
2. S2 / S5 / S6 三段**必须有 QA 独立复核**（`software-qa-engineer`）：S2 看像素证据、S5 看 α²/z 序/crop、S6 看 seek 次数与降级路径。
3. 全段完成后交一份 `PERF_REPORT.md`（附录 B.4 交付物）+ `docs/使用说明` 增补（含 §13 Q1 的"长 GOP 素材可能晚 ≤1s"口径）。

---

## 5. 停止条件（遇到就停手报 team-lead，不许"自己圆过去"）

| 现象 | 说明 | 处置 |
|---|---|---|
| `seek` 实测 P95 超出任务书基线 2 倍 | 例如长 H.264 > 200ms | 停手，重跑 `cue_seek_probe.py` 复测；确认不是基线漂移后上报 |
| 活跃解码器 > 8 或有持续增长 | 9-30 事故形状 | 立刻回滚 S7，带 `cue_resource_probe` CSV 上报 |
| 无 crop 场景出现**任何**像素差异 | 说明 `_draw_layer` 改坏了 | 回滚 S2 |
| `PARITY_OK` 变红 | 有人动了 Step1 语义 | 停手，之前先确认不是自己改的 |
| 需要"每帧 seek"才能看起来对 | 说明档位/阈值或 `position()` 实现有问题 | 停手，回到任务书 §3.3/§4 复核；**不许用 P5 禁止的形状绕过** |
| **6. 普通图层 fps 退化 ≥1% 或 compose p95 >1.01×** | 热路径被拖慢 | **停手上报**（G4 从"回滚该段"升为"停手"，不自行调参掩盖） |
| **7. `src_ended` 期间出现任何 seek，或 `set_speed` 序列里出现 1.0** | 计速主路径被改 / 防自激失效 | 停手；前者查 §3.3，后者查 §5.3（1.0 等价换方案） |
| **8. 非 cue 图层从 `_cue_players` 取到 player（路由错）** | `_get_player` 早返回写错 | 停手；判据：断言"存在非 cue 图层时 `lay._cue_players` 恒空" |
| **9. seek 次数超 §9.2 上界，或 `abs(position()-play_sec)` 每帧符号翻转（振荡）** | 阈值/节流/落地判定有环 | 停手；振荡 = 自激前兆，先查 in-flight 与节流是否真生效 |

---

**★ 架构师补记的 3 条风险（不阻塞 S1/S2，但 S6/S7 必须处理）**

| # | 风险 | 处置 |
|---|---|---|
| **R9** | **高倍速 × 4K 解码吞吐**：`set_speed(3.0)` 要 3× 解码，4K 可能撑不住 ⇒ drift 增大 ⇒ 频繁 seek | **S6 实测 `speed∈{0.5,1,3} × DXV/H.264 × 4K`**；超 §9.2 上界 ⇒ 高速档改用更保守阈值 |
| **R10** | `position()` 与 `current()` 可能差 **1 帧**（不同源或读写时序） | `land_tol` 必须 **≥1 帧且 ≤2 帧**（`land_tol(fps)=2/fps` 已满足）⇒ 防边界抖动 |
| **R11** | **`src_ended` 与换片段**：若不随 item 变更重置，新片段的漂移校正会被上一条的 `src_ended` 误关 | 换片段时（`st["item_id"] != ps.item_id`）**必须重置 seek 状态**（`new_state()`），`src_ended` 由新 `ps` 决定 |

---

## 6. 与 Step3 的接口（前置依赖提醒）

本计划的 S4/S6 需要 **Step3 给出的 `pos_beats`**（指纹秒 → 拍）。若 Step3 未完成：
- S4/S6 用**注入的固定 `pos_beats` 序列**做自验（`pos = k*0.25`，0→80 拍），先证明"解释器 → 画面"这一段正确；
- 位置源接好后只替换"`pos_beats` 从哪来"，**不改 S4/S6 的代码**。
