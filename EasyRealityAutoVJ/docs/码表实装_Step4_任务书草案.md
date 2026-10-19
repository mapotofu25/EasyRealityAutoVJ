# 码表实装 · **Step 4 任务书草案**（运行时挂进合成 = 热路径）

> 作者：工程师（寇豆码）· 2026-10-17 · **状态：草案，待架构师评审后定稿**
> 上游依据：`docs/码表实装_设计.md`（§4.2 / §4.4 / §⑤ / 附录 B / 附录 C）· 团队裁定「走 **A′**」· 实测报告 `tools/_cue_seek_probe.md`
> 前置已完成：**Step1（数据层）已交付并验收** —— `src/cuesheet.py` / `src/cuesheet_runtime.py` + `tools/twoside_parity_check.py`（`PARITY_OK`，含变异测试）

---

## 0. 本步要解决的一件事

把「`CueRuntime` 每帧算出来的 `cs_states`」变成**画面**，且满足两条硬底线：

1. **对既有普通图层逐像素零变化**（`_draw_layer` 加热路径形参、`_compose` 加分支，都必须可证明"没碰它们"）。
2. **码表接管期间解码器有界**（9-30 事故：12 核 / 750MB/s 的根因就是"激活了没人关"）。

**不做**：编排界面、设备位置源（§1.3）、码表编辑/另存、多码表叠一个图层。

**本步假设**：Step2（引擎数据面 `set_cue_sheet/clear_cue_sheet`）与 Step3（认歌+拍位）已落地。
若未落地，本步必须能用「**注入固定 `pos_beats`**」的方式独立自验（见 §10）。

---

## 1. 交付物与改动点（逐条给到 `文件:行号`）

> 「热路径」= 每帧执行；改动必须**可单测 + 可单独 revert**。

| # | 文件:行号 | 动作 | 热路径 | 风险 |
|---|---|---|---|---|
| 1 | `src/engine.py:372` `Layer.__init__` | 加 9 个字段：`is_cuesheet=False` · `cuesheet_id=""` · `cs_states=[]` · `cs_active=False` · `_cue_players={}` · `_cue_items={}` · `_cue_last_frame={}` · `_cue_sub_tmps={}` · `_cue_seek_state={}`；`to_dict()` 带 `is_cuesheet`/`cuesheet_id` | 否 | 低 |
| 2 | `src/engine.py:1226`（`_tick_locked` 里 `self._compose(snap)` **之前**） | 插 `self._cuesheet_tick(snap)` | 是（1 次/帧，轻） | 中 |
| 3 | `src/engine.py:3051-3055`（`for lay in reversed(...)` 内，`if not lay.visible` 之后、`if not lay.player` **之前**） | 插码表分支（§6.3） | **是** | **高** |
| 4 | `src/engine.py:3180` `_draw_layer(...)` | 签名末尾加 `crop=None`；内部归一 `src_rect` 并走 `drawImage(tgt, img, src_rect)` | **是** | **高**（附录 C 已给零变化方案） |
| 5 | `src/engine.py:2471` `_get_player` | **在函数开头加 cue 早返回分支**（`lay.is_cuesheet ⇒ lay._cue_players`，上限 `max(32, len(cue_media)+16)`，LRU 逐出前先 `set_active(False)` 再 `close()`） | 否 | 中 |
| 6 | `src/engine.py:2533` `_preload_clips` | cue 图层**不**走自动预加载（由 `_cuesheet_tick` 自己 warm） | 否 | 低 |
| 7 | `src/engine.py:2806` `_apply_color` / `2872` `_apply_postfx` | 加 `if self._cuesheet_on(): 跳过`（用户 2026-09-28 要求：码表图层不受调色/后处理影响） | **是**（只加 1 个 bool 判断） | 中 |
| 8 | `src/engine.py:3303` `close()` | `close` 之前 `self.clear_cue_sheet(silent=True)` | 否 | 中 |
| 9 | `src/media_manager.py` 三个播放器 | **只加两个只读方法**：`seek(sec) -> bool`（§3.1）+ `position() -> float|None`（§3.2）；`VideoPlayer`(~862) / `AvAlphaPlayer`(~1038+) / `DxvGpuPlayer`(~1627+)。私有成员可加（`_seek_req/_seek_t0/_pts`） | 否 | 中 |
| 10 | `src/media_manager.py` 编码普查（`~1322-1382`） | 复用**既有后台普查**写入 `_codec/_is_dxv`；**新增**可选的 `_cue_gop_frames` 探测（载入/预加载时一次，见 §4.3-③），**不进热路径** | 否 | 低 |
| 11 | `tools/cue_resource_probe.py`（新） | Step4 的资源/并发实测入口（附录 B.4） | — | 低 |

**移植蓝本**（`AutoVJ_码表备份_20260930/pre_strip_bak/_bak_engine_before_strip.py`，只挑片段、绝不整体覆盖）：
`_CueSubLayer`(284) · `set_cue_sheet`(2222) · `clear_cue_sheet`(2246) · `_cuesheet_on`(2282) · `_cuesheet_autoswitch`(2326) · `_cue_pos_from_fingerprint`(2379) · `_cue_beat_len`(2407) · `_cue_set_active`(2420) · `_cuesheet_tick`(2443) · `_cue_item`(2489) · `_cue_frame`(2511) · `_draw_cuesheet_layer`(2542) · `_compose` 分支(2607) · `_get_player` cue 分支(1643)。

**★ 移植时必须改的（备份是 9-29 版，不能照抄）**
1. `_cue_frame` **必须消费 `play_sec`/`src_in`/`src_ended`**（备份全文 `grep play_sec|src_in` = **0 命中**，即完全没实现入点与冻结）。
2. `_cue_frame` 的 `set_speed` 规则按 §5.3，**不是**照抄备份的"无脑 `set_speed(ps.speed)`"。
3. `_cuesheet_tick` 里 `_cue_beat_len` 返回 `None` 时无需再兜底（Step1 的 `CueRuntime.resolve_beat_len` 已退化 1 拍 = 1 秒）。
4. **备份播放器既没有 `seek` 也没有 `position`** ⇒ 备份的 `_cue_frame` 只能"从头播"，`src_in`/`src_out`/漂移校正**全都无从实现**。本步两个方法都要新加（§3），这是与备份最大的结构性差异。

---

## 2. Step1 已提供、本步直接消费的字段（`LayerPlayState`）

`src/cuesheet_runtime.py`（**零 Qt**，已自检 + 两端对照）：

| 字段 | 语义 | 本步怎么用 |
|---|---|---|
| `active` / `missing` / `ended` | 该子层此刻是否该画 / 缺素材 / 区间已结束 | 决定跳过（`missing`/`not active`） |
| `clip_id` / `clip_path` | 素材 | `_cue_item` + `_get_player` |
| `src_in_sec` / `play_sec` | 素材内入点 / **此刻应播到第几秒**（含 `×speed`、已钳到 `src_out`） | **seek 目标**（§5） |
| `src_out_sec` / `src_ended` | 出点 / 是否已到出点（`play_sec` 已钳住） | **持最后一帧**，不 seek |
| `alpha` / `trans` / `trans_p` | 淡入淡出（`trans_p = a_in` / `1-a_out` ⇒ 合成 `α²`） | 直接喂 `_draw_layer(prog=trans_p)` |
| `blend` / `opacity` | **已解析成有效值**（item 级覆盖本层） | 直接用，**不要再判继承** |
| `crop` / `xform` / `speed` | 裁切（归一化） / 变换（**恒为 dict，全默认 = cover**） / 倍率 | 传给 `_draw_layer(..., crop=)` 与 `_cue_item(xform)` |
| `z` | 绘制顺序（运行时已按 z 升序返回） | 顺序遍历即可 |

---

## 3. `seek(sec)` / `position()` 接口契约（**本步最容易翻车的地方**）

### 3.0 六条约束（**按风险排序 = 实施优先级**，团队裁定）

| 优先级 | 约束 | 违反后果 | 本文章节 |
|---|---|---|---|
| **P0** | **`seek` 必须在解码线程执行**，渲染线程只投递目标 `play_sec`，绝不等待 | 53~848ms 同步等待 ⇒ 每帧掉 3~50 帧，画面卡死 | §3.1-3 · §5.2 |
| **P1** | **触发条件 = 片段进入 + 漂移 > 阈值**（DXV 0.10 / H.264 0.35 / HEVC 1.00） | 漂移越滚越大 ⇒ 素材内位置与拍点脱节（卡点码表全废） | §4 · §5.4 |
| **P2** | **进入前预热 seek**（预加载窗口提前 1~1.5s） | 进入瞬间黑/卡（HEVC 可达 848ms） | §7 · §5.4 |
| **P3** | **`src_out` 冻结 = 持 `_cue_last_frame`，不 seek** | 多付一次 seek 且可能越过出点 | §5.2-① |
| **P4** | **任何 seek 失败/超时都降级为"继续用当前帧"，且不得重开容器** | 重开容器 ⇒ 回到第 0 帧（比不 seek 更糟）；抛异常 ⇒ 渲染线程挂 | §3.1-4 · §5.5 |
| **P5** | **绝不做"每帧 / 每 N 帧 seek 校正"**（每帧注入只允许出现在"新方案重新评审"里） | 长 GOP 素材上必然掉帧；且要求 `set_speed(1.0)`，两套计速打架（§5.3） | §5.3 · §5.4 |

> 另有一条**依赖前提**（不加就没有 P1）：播放器必须能**读出"当前帧的素材内时间"** ⇒ 见 §3.2 `position()`。
> 没有 `position()` ⇒ 漂移算不出来 ⇒ P1 只能退化成"仅入场 seek + `set_speed(speed)`"，**长片段 + `speed≠1` 时误差会滚雪球**（等于 A′ 只剩入场预热）。

### 3.1 `seek(sec)` 语义（8 条，逐条验收）

1. **`seek(sec)` = 把"下一次 `current()` 返回的帧"定位到 `sec` 处的帧**（不返回图像、不阻塞渲染线程）。
2. **必须"seek + 前向解到 `pts >= sec`"**。
   ⚠ 实测：PyAV 的 `container.seek()` **只落到前一个关键帧**，不补齐时落点误差 **P50 0.166~1.672s、max 2.4s**
   ⇒ 对卡点码表是致命的（"seek 成功但位置错"，画面停在上一段）。**这条是本契约的核心。**
   ⛔ **禁止在实现里直接用 `container.seek()` 当作"定位完成"**（那只是"定位开始"，必须继续 `decode()` 直到 `frame.pts >= sec`）。
3. **在解码线程内执行**（或由解码线程消费的请求队列），**绝不能**在渲染线程同步等待。
   一次 H.264 seek 实测 **53~76ms**（HEVC 192~848ms），远超 16.7ms 帧预算。
4. **失败降级 = 保持当前帧**：任何异常/超时（> `CUE_SEEK_TIMEOUT_SEC`=2s 未落地）⇒ 返回 `False`，
   **画面继续用 `current()` 的旧帧**，**不抛异常、不黑屏、不重开容器**
   （⛔ **禁止"取不到帧就重开容器"** —— 那正是 9-30 事故的形状；重开会回到第 0 帧，比不 seek 更糟）。
5. **只加新方法，不改既有语义**：`read/current/set_speed/set_active/warm_up/close` 一行不改（§8）。
6. **幂等 + 节流**：目标与 `position()` 相差 < 1 帧时可空转返回 `True`；同 player 两次 seek 间隔
   ≥ `CUE_SEEK_MIN_INTERVAL_SEC`（§4），**片段切换例外**（不同 item 立即 seek）。
7. **不改变播放状态**：`seek` 不启动/不停止解码线程，不改 `_active`；seeking 期间 `current()` 仍返回旧帧。
8. **不破坏 warm 状态**：seek 成功后该 player 视为已 warm（不要因 seek 触发 `warm_up()` 重解开容器）。

### 3.2 `position() -> float | None` 语义（**新增的第二个只读方法**）

> 为什么要它：A′ 的 P1（漂移校正）需要知道"**播放器现在实际播到第几秒**"。
> 现有三个播放器只有 `set_speed/set_active/warm_up/current`，**没有位置读数** ⇒ 漂移算不出来 ⇒ P1 无法实现。

1. **返回「当前实际显示的那一帧」的素材内 pts（秒）」**，且**与 `current()` 同源**（就是同一个 `self._img` 对应的 pts）。
2. ⛔ **不是**"解码器内部偷偷继续前进的 pts"（读内部计数器会让"持帧冻结"期间读数继续涨 ⇒ 触发自激，见 §3.3）。
3. **O(1)**：**只读已缓存的 pts**，**不做 decode、不阻塞、不锁等待**。
4. **取不到 / 不可信 ⇒ 返回 `None`**（例如首帧还没出来、GIF/图片、格式不报 pts）。
   调用方见到 `None` 就**跳过本次漂移判断**（不报错、不 seek、不重开）。
5. **只读扩展**：只被 cue 路径调用；普通图层一行不改（§8 同一白名单）。

### 3.3 防自激契约（**`position()` / `src_ended` / seek in-flight 三者必须一起实现**）

> 不做这一节，会做出一个"**抖动 seek 循环**"：`position()` 越追不上 `play_sec` ⇒ 越判定超阈值 ⇒ 越发 seek ⇒ 越落不了地。

| 场景 | 规则 | 不这么做的后果 |
|---|---|---|
| **`ps.src_ended == True`**（已到出点、画面持帧） | **跳过漂移校正，绝不 seek**；`position()` 允许返回 `None` | 持帧期间解码器 pts 仍在涨 ⇒ 漂移持续变大 ⇒ **每帧都判超阈值 ⇒ 每帧 seek**，还会把冻结帧 seek 掉 |
| **seek in-flight**（已发出、未落地） | **加 `in_flight` 标记，落地后清**；**in-flight 期间不做漂移判断** | 长 GOP 素材落地要 664~848ms，期间又发 seek ⇒ **自激** |
| `position() is None` | 跳过漂移校正（不是错误） | 拿 `None` 参与比较 ⇒ 抛异常或被当成 0 ⇒ 永久 seek |

**落地判定**（把 in-flight 清掉的唯一条件，二选一即清）：
1. `abs(position() − 请求目标) ≤ 1 帧` ⇒ **落地成功**，清 in-flight；
2. 距发请求 > `CUE_SEEK_TIMEOUT_SEC` ⇒ **落地失败**，清 in-flight + 标 `degraded`（本片段内不再 seek，§3.1-4）。

> **一句话总结（原样写进实现注释）**：
> **P1（漂移校正）只在「正常前向播放 + `src_ended == False` + seek 非 in-flight + `position() is not None`」时生效。**

### 3.4 建议签名（两个方法 × 三个播放器）

```python
def seek(self, sec: float) -> bool:
    """请求「下一次 current() 定位到 sec（含前向解到 pts >= sec）」。解码线程内执行。

    · 只受理请求，不阻塞调用线程；返回 False = 不受理/已降级（画面保持旧帧）。
    · 绝不重开容器；不改 _active / speed / _warmed 语义。
    """

def position(self) -> float | None:
    """当前**实际显示帧**的素材内时间（秒），与 current() 同源；O(1) 只读。

    · 首帧未就绪 / 格式不报 pts / 冻结持帧中不可信 ⇒ 返回 None。
    """
```
`GifPlayer` / `ImagePlayer` **不需要** `seek`（GIF 按轮循环、图片无时间轴）；调用方按 `getattr(p, "seek", None)` 容错。

---

## 4. 常量 / 格式分级 / **档位来源**（`禁止运行时猜`）

### 4.1 常量

```python
# engine.py 顶部（新常量区）
CUE_SPEED_PACER           = "player"  # 计速主路径 = 播放器计速（见 §5.3，唯一实现路径）
CUE_SEEK_DRIFT_SEC        = 0.35      # 普通档（H.264）：漂移超过它才 seek
CUE_SEEK_DRIFT_SEC_DXV    = 0.35      # ★ 裁定：DXV **也不逐秒**（逐秒 seek 会让时间不连续，DXV 是主力素材，抖动会被放大）
                                      #   先与普通档一致；若现场实测仍有明显漂移，再单独评估调小
CUE_SEEK_DRIFT_SEC_CONSV  = 1.00      # 保守档（HEVC/长 GOP）：尽量不 seek
CUE_SEEK_MIN_INTERVAL_SEC = 1.0       # 同一 player 两次 seek 的最小间隔（防连续 seek）
CUE_SEEK_TIMEOUT_SEC      = 2.0       # 未落地超时 ⇒ 判失败 + 降级（§3.3 落地判定）
CUE_PREWARM_LEAD_SEC      = 1.5       # 预加载窗口提前量（进入前先 seek 到 src_in）
# —— 档位来源：GOP 探测（只在载入/预加载时做一次，绝不进热路径）——
CUE_GOP_PROBE_SEC         = 3.0       # 采样时长（秒）
CUE_GOP_PROBE_MAX_FRAMES  = 300       # 采样帧数上限
CUE_GOP_PROBE_TIMEOUT_SEC = 8.0       # 探测超时即放弃，落回 codec 粗分
CUE_GOP_CONSERVATIVE      = 60        # 平均 GOP > 60 帧（≈2s@30fps）⇒ 归保守档
```

配置项（`cfg["cue"]`，Step7 落地；本步先按缺省读）：

| 键 | 取值 | 语义 |
|---|---|---|
| `seek_profile` | `"auto"`（缺省）/ `"conservative"` / `"off"` | `conservative` ⇒ **所有素材**按保守档（只入场+预热）；`off` ⇒ 完全不 seek（等价 B，现场应急开关） |

### 4.2 分级表（依据实测；**阈值不许凭感觉调**）

| 档位 | 归属判据（**确定性、O(1)、只读缓存**） | 实测 seek P95 | 策略 | 漂移阈值 | 计时基准 |
|---|---|---|---|---|---|
| **free**（DXV） | `item._is_dxv` 或 `item._codec == "dxv"` 或 `item._cue_gop_frames <= 2` | **1.85ms**（GOP=1） | 进入 + 漂移校正（**≈1Hz 量级**） | `0.35`（裁定：不逐秒） | 播放器计速 |
| **normal**（H.264） | 其余 `kind == "video"`（**含"探测不到"的一切情况**） | 53ms（cv2）/ 76ms（av，GOP≈30） | 进入 + 漂移校正 | `0.35` | 播放器计速 |
| **conservative**（长 GOP / HEVC） | `item._codec == "hevc"` 或 `item._cue_gop_frames > 60` 或 `seek_profile=="conservative"` | 192ms（cv2）/ 848ms（av，GOP≈143） | **只进入 + 预热**（不做运行中校正） | `1.00`（仅作入场容差） | 播放器计速 |

> 判据来源：`tools/_cue_seek_probe.md`（15Hz/2Hz 两轮，真实用户素材）。
> **GOP 决定成本**：DXV GOP=1 ⇒ 1.85ms；短 H.264 整段 1 个关键帧 ⇒ 664ms。

### 4.3 **档位从哪来**（三道闸门，全部 O(1) 读缓存；⛔ 禁止运行时猜）

```text
① 配置覆盖（最高优先）：cfg["cue"]["seek_profile"]
     "off"           → 完全不 seek（B 模式应急）        ← 现场排障用
     "conservative"  → 强制保守档（只入场+预热）
     "auto"(缺省)    → 继续 ②
② codec 粗分（item._codec / item._is_dxv，由 media_manager 后台普查填，见 media_manager.py:1322-1382）
     dxv   → free
     hevc  → conservative
     其它/未知 → normal        ← **未知一律 normal（= H.264 档），绝不猜成 DXV**
③ GOP 实测（**可选增强**，只在“载入/预加载”时做一次）：
     在解码线程里试读 CUE_GOP_PROBE_SEC 秒（≤ CUE_GOP_PROBE_MAX_FRAMES 帧），
     统计 key_frame 间隔 → 平均 GOP 写入 item._cue_gop_frames（**缓存，永久复用**）
     · 探测超时 CUE_GOP_PROBE_TIMEOUT_SEC / 失败 → 直接沿用 ②
     · 平均 GOP > CUE_GOP_CONSERVATIVE(60) 帧 → 升为 conservative
```

**硬纪律（评审逐条对照）**
- ⛔ **禁止**在任何每帧路径、每次 seek、每次 `_cue_frame` 里**重新探测**（探测要 decode+`av.open`，几十~两百 ms）。
- ⛔ **禁止**在渲染线程 `av.open` / `decode`；GOP 探测属"载入期后台活"，与 `_probe_alpha_async` 同一类。
- ⛔ **禁止**把"探测不到"当成 free/DXV；**未知一律 normal**。
- 探测结果**只做 `MediaItem` 实例缓存**（裁定 Q4：先不做持久化，避免"缓存陈旧/失效"问题）。
  若将来实测发现**同一素材反复探测成本偏高**（例如切歌频繁且素材很大），再考虑持久化到 `cfg["music_meta"][path]["gop"]`。

```python
def _cue_seek_mode(self, item):
    """档位：返回 "free" / "normal" / "conservative"（见 §4.2/§4.3，全部 O(1) 只读）。"""
    prof = str(self.cfg.get("cue", {}).get("seek_profile", "auto") or "auto")
    if prof == "off":
        return "off"
    if prof == "conservative":
        return "conservative"
    gop = getattr(item, "_cue_gop_frames", None)
    if getattr(item, "_is_dxv", False) or str(getattr(item, "_codec", "")) == "dxv" \
            or (gop is not None and gop <= 2):
        return "free"
    if str(getattr(item, "_codec", "")) == "hevc" or (gop is not None
                                                      and gop > CUE_GOP_CONSERVATIVE):
        return "conservative"
    return "normal"          # ← 未知也走这里（H.264 档）
```


---

## 5. `_cue_frame` 改造点（本步的语义核心）

### 5.1 现状（备份 `2511`）与必须补的三件事

备份实现只有三行有效逻辑：`player.set_active(True)` → `player.set_speed(ps.speed)` → `player.current()`。
**它完全没消费 `ps.play_sec` / `ps.src_in_sec` / `ps.src_ended`** ⇒ 入点失效、进度漂、出点不冻结。

### 5.2 改造后（伪代码，逐行落地）

```python
def _cue_frame(self, lay, ps):
    item = self._cue_item(lay, ps.clip_path, ps.xform)     # 照旧：按路径缓存 MediaItem
    player = self._get_player(lay, item)                   # 照旧（cue 用 lay._cue_players）
    if player is None:
        return None
    player.set_active(True)                                # ⚠ 必须（不激活 ⇒ current() 恒 None）

    # ① 出点冻结：不 seek，直接用上一帧（P3；同时避免 §3.3 的自激）
    if ps.src_ended:
        return lay._cue_last_frame.get(ps.lyr_id) or player.current()

    st = lay._cue_seek_state.get(ps.lyr_id)
    if st is None:
        st = {"item_id": "", "last_req": 0.0, "inflight": False,
              "target": 0.0, "degraded": False}
        lay._cue_seek_state[ps.lyr_id] = st

    # ② 结算上一次 seek：落地判定（§3.3）—— 落地/超时才清 in-flight
    if st["inflight"]:
        pos_now = player.position()
        if pos_now is not None and abs(pos_now - st["target"]) <= self._cue_land_tol(player):
            st["inflight"] = False                      # 落地成功
        elif (time.perf_counter() - st["last_req"]) > CUE_SEEK_TIMEOUT_SEC:
            st["inflight"] = False                      # 落地失败
            st["degraded"] = True
            self._cue_health("码表 seek 超时，本片段降级为自由播放：%s" % ps.clip_path)

    # ③ 定位（P1 + 防自激）：要不要发新 seek
    if self._cue_should_seek(ps, item, st, player):     # §5.4
        st["item_id"] = ps.item_id
        st["target"]  = ps.play_sec
        st["last_req"] = time.perf_counter()
        ok = player.seek(ps.play_sec)                   # 只投递，不等待
        if ok:
            st["inflight"] = True
        else:                                           # 不受理 ⇒ 本片段不再 seek（P4）
            st["degraded"] = True
            self._cue_health("码表 seek 失败，降级为自由播放：%s" % ps.clip_path)

    # ④ 计速（§5.3 主路径 = 播放器计速）：**恒定 ps.speed，永不切 1.0**
    if abs(float(getattr(player, "speed", 1.0)) - float(ps.speed or 1.0)) > 1e-6:
        player.set_speed(float(ps.speed or 1.0))

    if not getattr(player, "_cue_warmed", False):
        player._cue_warmed = True
        player.warm_up()                                # 照旧（不 warm 要等 70~690ms）

    img = player.current()
    if img is None or img.isNull():
        img = lay._cue_last_frame.get(ps.lyr_id)         # 兜底上一帧（seek 进行中不黑）
    elif ps.active:
        lay._cue_last_frame[ps.lyr_id] = img             # 只存引用，不拷贝
    return img
```

```python
def _cue_land_tol(self, player):
    """「seek 落地」的容许误差：2 帧（默认 30fps ⇒ ≈66ms）。

    ⚠ 这只是"判定 seek 是否落地"的容差，**不是**漂移阈值（漂移阈值见 §4.2，按档位给）。
      落地容差给 2 帧是为了容忍 seek 的 pts 取整 + 变帧率素材。
    """
    fps = float(getattr(player, "fps", 0.0) or 0.0)
    return 2.0 / (fps if fps > 1.0 else 30.0)
```

### 5.3 **计速规则**（架构师原文 + 本任务书选定的主路径）

> 架构师原话（**原样保留**）：
> **若每帧/每 N 帧注入 seek**（`play_sec` 已含 speed）⇒ **必须 `set_speed(1.0)`**（否则双重计速）
> **若只入场 seek + 靠播放器自然前进**（推荐路径）⇒ **必须 `set_speed(speed)`**（否则两次校正之间画面按 1× 播、变速肉眼不对）
> **一句话：两者只能有一个在"计速"。**

**本任务书选定：主路径 = 「播放器计速」（= 架构师推荐路径），`set_speed(ps.speed)` 恒定。**

| 项 | 主路径（唯一实现） |
|---|---|
| 谁在计速 | **播放器**（`set_speed(ps.speed)`）；`play_sec` **不作时间源**，只作 seek **目标** |
| seek 频率 | 进入片段 1 次 + 运行中"漂移 > 阈值"时（P5：**绝不每帧/每 N 帧**） |
| 为什么这样不双重计速 | `play_sec` 只用于"重新对齐到绝对位置"；对齐后仍由播放器按 `speed` 自己前进 ⇒ 两者**同向同速**，不会叠加 |
| `set_speed(1.0)` 何时出现 | **本设计不出现**。只有当将来改成"每帧注入 seek 由 `play_sec` 主导"时才需要 —— 那是**新方案，必须重新评审 + 重过 §9 全部门槛** |
| **为什么不做 1.0 主路径**（写给未来的自己/别人） | **因为一旦用 `set_speed(1.0)`，就必须靠"每 N 帧注入 seek"来推进时间**（否则画面按 1× 播、变速不对）—— 而每帧注入正是 **P5 明令禁止**的形状：长 GOP 素材上每次注入都要 53~848ms，必然掉帧。**所以 1.0 不是一个可选开关，它等价于换方案。** |
| 切换时机 | 见下表（只有"换片段"与"speed 变化"两个时机，**不在 seek 前后切换**） |

**`set_speed` 切换时机（全表，别在两套之间摇摆）**

| 时机 | 动作 | 说明 |
|---|---|---|
| 进入新片段（`st["item_id"] != ps.item_id`） | `set_speed(ps.speed)`（变了才调）+ **立即 seek 到 `ps.play_sec`（= `src_in_sec`）** | 入点对齐，一次到位 |
| 运行中（同一片段） | **只 seek，不动 speed** | 漂移校正不改计速方式 |
| seek in-flight / 超时降级 | **不动 speed** | 降级只是"不再校正"，计速仍由播放器按 `speed` |
| `ps.src_ended`（持帧） | **不动 speed、不 seek** | 冻结帧，与计速无关 |
| `ps.speed` 变化（码表编辑后重载） | 重建 runtime ⇒ 重新进入片段路径 | 不支持运行中改 speed |
| `ps.speed == 1.0` | 两种路径等价 | 无歧义 |

> ⛔ **禁止**：`set_speed(1.0)` + 每帧 seek 的混搭（P5）；也禁止"seek 前切 1.0、seek 后切回 speed"这种摇摆实现 —— **本设计不需要切**。

### 5.4 `_cue_should_seek(ps, item, st, player)` 判定顺序（照此实现，别自己加分支）

```text
1.  ps.missing / not ps.active                       → False  没素材不 seek
2.  ps.src_ended                                     → False  ★防自激（持帧期间不校正）
3.  st["degraded"]                                   → False  已降级（失败/超时），本片段内不再 seek
4.  st["inflight"]                                   → False  ★防自激（上一次没落地不再发）
5.  片段刚切换（st["item_id"] != ps.item_id）          → True   立即 seek 到 ps.play_sec（节流不适用）
6.  mode == "off"                                    → False  cfg 强制关闭 seek（B 模式）
7.  mode == "conservative" 且不是"刚切换"              → False  保守档只入场，不做运行中校正
8.  player.position() is None                        → False  读数不可信（§3.2-4），跳过校正
9.  距上次请求 < CUE_SEEK_MIN_INTERVAL_SEC            → False  节流
10. |player.position() − ps.play_sec| > 阈值           → True   阈值按 §4.2 档位表
11. 否则                                             → False
```

> ⚠ 第 10 条必须用 **`position()`（实际显示帧 pts）**，**不是**「上一次请求的目标值」（`st["target"]`）。
> 用请求值算漂移 = 自己跟自己比，永远不触发 —— 这是最容易写错、且"看起来能跑"的 bug。


### 5.5 落日志用**现有** API（不要新造）

引擎没有全局 `logging`；排障日志是 `_LayerHealthLog`（`engine.py:88`，滚动保留）。
`_health_tick`（`~2385`）里的用法就是范式：

```python
def _cue_health(self, msg):
    """码表降级/缺素材/seek 失败：写进图层健康日志（滚动保留，便于事后排障）。

    ⚠ 每片段最多 1 条（`st["degraded"]` 只置一次）⇒ 不会每帧刷屏、不会把日志文件撑爆。
    """
    try:
        log = self._health_log
        if log is None:
            log = self._health_log = _LayerHealthLog()
        log.write("[%s] %s" % (time.strftime("%H:%M:%S"), msg))
    except Exception:                       # noqa: BLE001
        pass
```

用户可见的部分**不靠日志**（设计 §3.4：exe 里用户看不到 print/日志）⇒ 走对话框 + 状态栏，见 §9.3。

---

## 6. 挂进合成（`_CueSubLayer` / `_draw_cuesheet_layer` / `_compose` / `_draw_layer(crop)`）

### 6.1 `_CueSubLayer`（照搬备份 284-317）
`_draw_layer` 需要的是「单层替身」（读 `opacity/clips/prev_player/use_alpha/_alpha_key/_alpha_img/_sub_tmp/lock_motion/is_kv/speed/…`）。
**直接照搬，不要改成 duck-typing 的极简类**（会缺字段导致静默黑屏）。

### 6.2 `_draw_cuesheet_layer`（移植 2542 + 三处改动）
```
base_op = clamp(lay.opacity)
for ps in lay.cs_states:                        # 已按 z 升序
    if not ps.active or ps.missing or not ps.clip_path: continue
    op = base_op * ps.opacity * ps.alpha        # ← 有效值，不再乘 lay.blend 相关
    if op <= 0.002: continue                    # 与普通图层同门槛
    img = self._cue_frame(lay, ps)
    if img is None or img.isNull(): continue
    item = self._cue_item(lay, ps.clip_path, ps.xform)
    stub = _CueSubLayer(ps.lyr_id, op, item, name=ps.lyr_id)
    tr   = ps.trans if ps.trans in ("fade","cut","slide","zoom","glitch") else "fade"
    prog = 1.0 if tr == "cut" else clamp(ps.trans_p)
    blend = ps.blend if (ps.blend in BLEND_QT or ps.blend == "subtract") else "normal"
    # subtract：画到 lay._cue_sub_tmps[lyr_id] 再统一相减（照旧）
    self._draw_layer(painter, stub, img, W, H, prog, tr, 1.0, snap, 0.0, 0.0, crop=ps.crop)
```
★ **α² 的来源就是 `ps.alpha × prog(trans_p)`**（Step1 与 Cue 端逐点校核：0/0.25/1.0）—— **不要在这里再乘一次 alpha，也不要改 `prog` 的定义**。

### 6.3 `_compose` 插入点（**位置错了就黑屏/漏画**）

在 `src/engine.py:3051` 的 `for lay in reversed(self.layers):` 循环里，
**必须插在 `if not lay.visible: continue`（3052-3053）之后、`if not lay.player: continue`（3054-3055）之前**：

```python
if getattr(lay, "is_cuesheet", False):
    if solo_on and not getattr(lay, "solo", False):
        continue
    if lay.hide_silent and silent:
        continue                      # 与普通图层一致（§⑤ 边界）
    self._draw_cuesheet_layer(p, lay, W, H, snap, subtract_jobs)
    continue                          # ★ 码表图层是容器，没有单一 player，必须在此拦下
```

`solo` / `visible` / `hide_silent` **必须与普通图层表现一致**（旧实现踩过"码表层盖在最上不受 solo 管"）。

### 6.4 `_draw_layer` 补 `crop`（按附录 C，**零变化保证**）

- 签名末尾追加 `crop=None`（现有全部调用点不传 ⇒ 行为不变）。
- `src_rect` 由 `crop` 归一：`cw/ch` 取整、`cx/cy` 钳到 `[0, iw-1]/[0, ih-1]`、越界收缩。
- `iw, ih = (src_rect.width(), src_rect.height()) if src_rect else (原图宽高)` ⇒ **crop 后以裁切尺寸为 cover/contain 基准**（对齐 Cue `compositor._target_src`）。
- 绘制分支：`src_rect is None` 走**原调用** `painter.drawImage(tgt, image)`；否则 `painter.drawImage(tgt, image, src_rect)`。**绝不 `image.copy()`**。
- glitch 分支：把"按行切整幅"改成"在 `src_rect` 内按比例切"（`src_rect is None` 时与原行为逐像素一致）。

### 6.5 `_get_player` 为什么必须"**早返回**"而不是"在里面加个 if"

当前 `_get_player`（2471）有三处**只对普通图层成立**的前提，cue 图层直接走会出错：

1. LRU 上限写死 **8**，且只认 `lay.clips[lay.cur]` / `lay.player` / `lay.prev_player` 三个"在用"标记；
   码表容器层没有单一 `player`，`lay.clips` 也是空的 ⇒ 会把正在用的 cue 解码器当空闲逐出（黑屏）。
2. 它带 `clip._alpha_recheck` 的"丢解码器重开"逻辑（对 cue 无害，但没必要每帧走）。
3. 逐出用 `v.close()`，**没有先 `set_active(False)`**；cue 侧必须先失活再关（§7，9-30 事故根因）。

⇒ cue 分支写在**函数最前面**、命中即 `return`，**完全不进入**既有 LRU 代码：

```python
def _get_player(self, lay, clip):
    if getattr(lay, "is_cuesheet", False):          # ★ cue 早返回（§6.5）
        return self._cue_get_player(lay, clip)      # 独立缓存 lay._cue_players，上限 max(32, len+16)
    ...既有逻辑一行不改...
```

`_cue_get_player` 的逐出顺序固定为：**`set_active(False)` → `close()` → 从 dict 删引用**（三件套缺一不可）。

---

## 7. 解码器生命周期（附录 B.3，**本步唯一的一票否决项**）

| 时机 | 动作 | 失败后果 |
|---|---|---|
| 创建 | 预加载窗口内 `warm_up()` 建解码器；或首次 `current()` 懒建 | 进入时 70~690ms 黑/卡 |
| 激活 | "当前片段 + 过渡中片段 + 预加载的下一个" ⇒ `set_active(True)` | `current()` 恒 None ⇒ 黑屏 |
| **失活（每帧）** | `_cue_set_active(lay, active_paths)` **必须遍历 `lay.cache` 与 `lay._cue_players` 两个缓存** | **9-30 事故**（12 核 / 750MB/s） |
| 回收 | `_cue_players` 超 `max(32, len(cue_media)+16)` ⇒ **先 `set_active(False)` 再 `close()`**（LRU） | 句柄/内存泄漏 |
| 切歌 | 重建 runtime，旧 `_cue_players` 全关 | 旧解码器持续解码 |
| 卸表/退出 | `clear_cue_sheet(silent=True)` ⇒ 全关 + 清 `cs_states/_cue_items/_cue_last_frame/_cue_seek_state` | 退出卡住/残留线程 |

**验收口径（附录 B.3 修正）**：真正防事故的是「**活跃解码器 ≤ 8 + 每帧失活**」，不是缓存条目数。

---

## 8. `media_manager` 改动边界（**只加不改**）

**允许（有界扩展：只加两个只读方法）**
- `seek(sec) -> bool`（§3.1）：只受理请求、在解码线程执行、失败返回 False。
- `position() -> float | None`（§3.2）：**O(1) 只读已缓存 pts**，与 `current()` **同源**；取不到返回 None。
- 内部私有成员可加（`_seek_req` / `_seek_t0` / `_pts` / `_pts_lock` 等），**不得**改变既有字段含义。

**禁止（评审逐条对照）**
- 不改 `read()/current()/set_speed()/set_active()/warm_up()/close()` 的**签名与语义**；
- 不改解码循环的 pacing（`interval/self.speed`）；
- 不改缓存/线程/容器打开逻辑；
- 不在非 cue 路径调用 `seek`（普通图层/预览一律不调 ⇒ `seek` 存在但零调用时**行为与今天完全一致**）。

**回归证据要求**：`seek` 加入后，普通图层播放的 fps / 帧内容必须与改动前逐值一致（§10-②）。

---

## 9. 验收门槛（把本次实测当"不许退化"的基线）

### 9.1 性能/资源（**任一项不过 = 不合入**）

| 维度 | 门槛 | 本次实测基线（`tools/_cue_seek_probe.md`） |
|---|---|---|
| DXV `seek` P95 | **≤ 5ms** | 1.85ms（GOP=1） |
| 长 H.264 `seek` P95 | **≤ 100ms** | 53.2ms（cv2）/ 75.8ms（av，GOP≈30） |
| HEVC `seek` | **只允许入场/预热**，单次 ≤ 1.0s | 192ms（cv2）/ 848ms（av） |
| 连续 seek 磁盘均值 | **≤ 120MB/s** | 15Hz 最坏 34.6 / 2Hz 最坏 14.1 |
| 连续 seek 磁盘峰值（10s） | **≤ 250MB/s** | 15Hz 最坏 36.5 / 2Hz 最坏 18.6 |
| **活跃解码器** | **≤ 8**（附录 B.3） | 待 Step4 用 `cue_resource_probe.py` 实测 |
| 缓存条目 | ≤ `max(32, len(cue_media)+16)` | 同上 |
| `video_fps` 退化 | **< 1%**（码表关 vs 开，3×30s 取中位） | 待测 |
| `compose p95` | ≤ 1.01 × baseline | 待测 |
| 进程 RSS 增量 | ≤ +150MB @1080p / ≤ +350MB @4K | 待测 |
| `num_handles()` 增长 | 预热后 ≤ +2/min | 待测 |
| CPU 增量 | 均值 ≤ +10%（单核）、峰值 ≤ +25% | 实测参考：单素材单后端最高 4.66 核（15Hz 极限） |

### 9.2 功能/正确性

| 项 | 门槛 |
|---|---|
| `src_in` 生效 | 片段第一帧的 `pts` ≈ `src_in_sec`（≤ 1 帧误差，DXV/H.264） |
| `play_sec` 跟踪 | 任意时刻 `abs(实际帧 pts − ps.play_sec) ≤ 1 帧`（DXV/H.264）；HEVC ≤ 1.0s |
| `src_out` 冻结 | 到点后连续 **30 帧非空**（不黑、不越过、不回卷到 0） |
| fade α² | 采样 `0 / 0.25 / 1.0`（与 Step1 parity 项⑤一致） |
| crop | 与 Cue 端同参数渲染同帧，diff 在容差内；**不拉伸**（以裁切尺寸为基准） |
| 无 crop 回归 | 普通图层 30 帧**逐字节 0 差异**（附录 C.3） |
| solo / 可见性 / hide_silent | 与普通图层行为一致（码表层不得"盖在最上"） |
| **`position()` 正确性** | 返回的 pts **等于 `current()` 那一帧的 pts**（同源，逐帧抽样 100 次误差 = 0）；首帧未就绪返回 `None`（不得返回 0） |
| **计速唯一性**（§5.3） | `speed != 1` 的片段：`position()` 的推进速率 ≈ `ps.speed`（允许 ±5%）；**且 `set_speed` 恒为 `ps.speed`**（日志里不出现 1.0 抖动） |
| **防自激 · `src_ended`** | `src_ended` 后 **seek 调用次数 = 0**（跑 10s 冻结期，计数为 0） |
| **防自激 · in-flight** | in-flight 期间**不得发出第二次 seek**（同一 player 的 seek 调用序列里，两次请求间隔 ≥ `CUE_SEEK_MIN_INTERVAL_SEC`，除非是换片段） |
| **seek 频率上界** | 单片段 30s 内 seek 次数：DXV ≤ 30、H.264 ≤ 30（`MIN_INTERVAL=1.0` 兜住）、HEVC ≤ 2（入场+预热）。**DXV 不逐秒**（裁定 Q2） |
| **降级后不再 seek** | 一次失败/超时后，该片段剩余时间 seek 次数 = 0 |
| 连续启停 30 次 | 活跃/缓存计数、句柄数回到基线（±2） |
| 退出 | 无残留线程/句柄；`clear_cue_sheet(silent=True)` 生效 |
| **Step1 门禁** | `tools/twoside_parity_check.py --pkg <真实包>` 仍 `PARITY_OK` |

### 9.3 降级可见性（§⑤ 不静默）
"seek 失败/降级""缺素材""未认到歌""位置丢失" 四类必须各有一条用户可见原因（对话框/状态栏/`_LayerHealthLog`）。

---

## 10. 测试与自检清单（Step4 交付时逐条打勾）

- [ ] **① 单测（无 GUI 部分）**：`_cue_should_seek` 的 7 条判定分支各 1 例（纯函数，可离线跑）。
- [ ] **② 无 crop 场景像素 diff**：普通图层 + 过渡（slide/zoom/glitch），改造前后各渲 30 帧，**0 差异像素**。
- [ ] **③ crop 场景**：`crop=[0.1,0.1,0.8,0.8]`，与 Cue 端同参渲染，diff 在容差内。
- [ ] **④ fade α² 逐帧**：`opacity=1 / trans_in=fade / L=2 拍`，采样 `0/0.25/1.0`。
- [ ] **⑤ `src_out` 冻结**：到点后 30 帧非空 + `play_sec` 不再增长。
- [ ] **⑥ 入点**：`src_in=3.0` 的片段，首帧 `pts` ≈ 3.0。
- [ ] **⑦ 解码器上界**：连跑 30s，`len(lay._cue_players)` ≤ 上限且**活跃 ≤ 8**、无持续增长。
- [ ] **⑧ fps A/B**：码表关 vs 开，`compose p50/p95/max` + `video_fps`，退化 < 1%。
- [ ] **⑨ 资源画像**：`files=tools/cue_resource_probe.py` 30s 稳态 + 连续启停 30 次，输出 CSV + 报告。
- [ ] **⑩ 回归**：`tools/twoside_parity_check.py` → `PARITY_OK`；普通图层播放逐值不变。
- [ ] **⑪ `position()` 同源验证**：随机 100 帧，`position()` 与 `current()` 帧的 pts 逐帧相等；首帧前返回 `None`。
- [ ] **⑫ 防自激三连**：① `src_ended` 冻结 10s，seek 计数 = 0；② 人为把 seek 拖慢（长 GOP 素材），确认 in-flight 期间不发第二次；③ `position()` 强返 `None`，确认不 seek、不抛。
- [ ] **⑬ 计速唯一性**：`speed=2` 的片段跑 10s，比较 `position()` 增量与 `play_sec` 增量（两者应同速）；日志确认未出现 `set_speed(1.0)`。
- [ ] **⑭ 档位来源**：对 DXV/H.264/HEVC 各一个素材，确认 `_cue_seek_mode()` 返回 free/normal/conservative；删掉 `_codec` 缓存后回落到 **normal**（不得变 free）；`seek_profile="off"` 时 seek 计数 = 0。
- [ ] **⑮ 边界（§⑤ 全表）**：包损坏/版本高/缺素材/切歌/暂停/跳转/回退/重叠/连续启停/退出 逐条走一遍。

---

## 10.5 与备份实现的差异（**照抄 + 修对**，不是照抄）

| 位置 | 备份（9-29 版） | 本设计 | 为什么必须改 |
|---|---|---|---|
| `_cue_frame` | 只 `current()`，**完全没消费** `play_sec/src_in/src_ended` | 消费三者；seek + 漂移校正 | 备份全文 `grep play_sec\|src_in` = **0 命中** ⇒ 入点/出点冻结/变速全都没实现 |
| `_draw_layer` 的 **glitch** 分支 | **不应用 crop**（对 cover/旋转裁、对 glitch 不裁，自相矛盾） | **在 `src_rect` 内按比例切**；`src_rect=None` 时与原式逐像素一致 | 带 crop 的片段一用 glitch 就"裁切失效"；备份写法无法自圆其说（已提交 `d428b73`） |
| 播放器 | **既无 `seek` 也无 `position`** | 两个只读方法（PyAV 路径含**前向补齐**） | 没有它们，`src_in`/漂移校正/`src_out` 冻结全做不到（已实现并实测：落点误差 0.000） |
| `set_speed` | 无脑 `set_speed(ps.speed)`（与 `play_sec` 并存 ⇒ 双计速风险） | 主路径 = 播放器计速，`set_speed(ps.speed)` 恒定；`play_sec` **只作 seek 目标** | 见 §5.3：`1.0` 只属于"每帧注入"方案，本设计明令禁止 |
| DXV 的 GPU 解码 | GPU 路径优先 | **`cue=True` 时禁用 GPU**（走 `AvAlphaPlayer`） | `GpuDxvPlayer` 的生成器不暴露 pts ⇒ 无法 `position()`；软解实测 39ms/0 误差 |

---

## 11. 未测项与风险

| 项 | 说明 | 处置 |
|---|---|---|
| **多图层并发 seek** | 4 层同时 seek 会叠加（最坏 4×36.5 ≈ **146MB/s**，仍 < 250 峰值门槛，但**未实测**） | Step4 用 `tools/cue_resource_probe.py` 在真实播放上下文实测 |
| DXV 样片仅 0.9s | 本机盘上**无独立 `.dxv`**（唯一来源是 `.ercue` 包内嵌 `media/12.mov`）；磁盘量不可外推 | DXV 成本由 GOP=1 决定、与时长无关 ⇒ 延迟结论成立；长 DXV 到现场再抽测 |
| OS 页缓存未清 | 报告的冷/热都偏乐观（磁盘余量 6.8× ⇒ 翻不了结论） | 保留说明，不额外处理 |
| **seek 造成时间不连续** | 每次 seek 后画面会"跳"到目标（不是平滑快进） | **已裁定：所有档位阈值 ≥0.35s，不做逐秒 seek**（DXV 是主力素材，抖动会被放大）；HEVC 只入场 + 预热 |
| **R1 crop 动热路径** | 改错会影响**所有普通图层** | 按附录 C 实现 + §10-② 零差异证据 |
| **R2 解码器泄漏** | 9-30 事故复现风险 | §7 两缓存遍历 + 上限 + 退出全关 + §10-⑦ |
| R3 拍长/相位两端不一致 | Step1 已用 parity 钉死（`PARITY_OK`） | Step4 不再改 `beat_len`/anchor |
| `_is_dxv` / `_codec` 探测缺失 | 档位判据依赖它（§4.3-②）；探测失败会误判档位 | **未知一律 normal 档**（0.35s / 允许校正），**绝不猜成 free/DXV** |
| **R7 `position()` 与 `current()` 不同源** | 若读"解码器内部挺进的 pts"，持帧/seek 期间读数继续涨 ⇒ **抖动 seek 自激**（画面反复跳、CPU 飙） | §3.2-1/2 写死"与 `current()` 同源、O(1) 读缓存、不可信返 None"；§10-⑪⑫ 逐条验证 |
| **R8 GOP 探测误入热路径** | 一次探测要 `av.open`+decode（几十~两百 ms） | §4.3 硬纪律：只在载入/预加载**后台**做一次并缓存；⛔ 每帧/每次 seek 重探 = 不合入 |

---

## 12. 提交纪律与回滚点

1. **每个改动点单独提交**，可独立 revert；建议顺序：
   ① `_draw_layer(crop)`（先做，因为它可独立验证零变化）→ ② `Layer` 字段 + `_CueSubLayer`
   → ③ `media_manager.seek`（此时**零调用**，先证明不改变任何行为）→ ④ `_compose` 分支 + `_draw_cuesheet_layer`
   → ⑤ `_cue_frame` + seek 策略 + 分级常量 → ⑥ `_cuesheet_tick` + 解码器生命周期 → ⑦ 收尾（`close`/`apply_color`/`apply_postfx`）。
2. **`media_manager.seek` 先落地但不调用**（提交 ③），跑一轮 fps/像素回归，证明"加方法本身零影响"。
3. **热路径改动（②④⑤）必须各自附一份 fps A/B 与像素 diff**，缺一不合入。
4. 任一步骤过不了 §9 门槛 ⇒ **回滚该提交**，不大面积重写。

---

## 13. 已裁定事项（团队 2026-10-17 裁定，本节供实现与说明书直接引用）

| # | 事项 | 裁定 | 落点 |
|---|---|---|---|
| Q1 | 保守档容差 `CUE_SEEK_DRIFT_SEC_CONSV = 1.0s` | **接受**。演出更怕抖、不怕静态偏一点 | §4.2 保守档；**说明书口径见下** |
| Q2 | DXV 是否走 0.10s（≈10Hz） | **不逐秒，DXV 也用 0.35s**。逐秒 seek 会让时间不连续，而 DXV 是主力素材、抖动会被放大；先 0.35s，现场实测仍有明显漂移再单独评估 | §4.1 `CUE_SEEK_DRIFT_SEC_DXV = 0.35`；§9.2 频率上界 DXV ≤ 30/30s |
| Q3 | seek 失败/降级的用户提示 | **要，但每片段最多 1 条**（不静默、不刷屏）。用 §5.5 的 `_LayerHealthLog`，**不新造 API**；状态栏同类文案同频次 | §5.5 · §9.3 |
| Q4 | GOP 探测结果是否持久化 | **先只做 `MediaItem` 实例缓存**（避免"缓存陈旧/失效"）；若将来实测发现同一素材反复探测成本偏高，再考虑写 `cfg["music_meta"][path]["gop"]` | §4.3 |

**Q1 的说明书口径（Step8 写进 `使用说明`，原文可用）**
> 长 GOP 素材（如部分 HEVC/长关键帧间隔的视频）上，码表画面可能比拍点**晚对齐最多约 1 秒**。
> 这是解码器的定位特性（该格式每次定位要 0.2~0.8 秒，频繁定位会掉帧），**不是 bug**；
> DXV 素材不受影响（每帧独立，定位 ≈2ms）。

**Q3 的文案（每片段最多 1 条，状态栏 + 健康日志同一条）**
> "本片段定位失败，已回退自由播放（画面可能略微偏离拍点）"

