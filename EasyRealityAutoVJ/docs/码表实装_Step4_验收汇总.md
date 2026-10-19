# 码表实装 · **Step 4（运行时挂进合成）验收汇总**

> 范围：S0~S7 八段（7 个 commit）· 全部通过 · 本文只列**数字与判据**，不写主观描述。
> 环境：Windows 11 · Python 3.13.14 · PySide6 6.11.2 · OpenCV 4.10 · PyAV 18.1
> 复跑：见文末「附：复跑命令」；任一条不过 = 不许合入。

---

## 1. 门禁总表（7 段）

| 段 | commit | 门禁 | 实测数字 | 通过 |
|---|---|---|---|---|
| **S0** | 见下① | 政策单测 + 源码级变异 | **26 用例 / 26 通过**；**5 个变异 5 个被抓** | ✔ |
| **S1** | `a09456a` | 选型矩阵 13 项 / 普通图层零影响 / 播放器定位 | `cue=False` **6 组合逐值一致**（cv2/av/gpu 与改前规则全等）；`cue=True`+DXV **绝不返回 GPU**；日志 3 次创建 **1 条**、换文件仍 1 条；`make_player` **全项目仅 1 个调用点**（`engine.py:2545`，`_get_player` 缓存未命中 ⇒ 不在热路径）；**落点误差 0.000**（VideoPlayer 39/87/111ms；AvAlpha 长 GOP 347/623ms；DXV 35/39/40ms） | ✔ |
| **S2** | `d428b73` | 像素守卫 / crop 几何 / fps | **11 场景 17 帧 = 0 字节差异**；crop **14/14**（四角色 + **分界 960** + 上下行差 0）；fps 合计 **−0.13%**（min-of-8；最大单场景 **+0.95%**；同配置噪声底 8.4%）；新增代码解析上界 **0.173µs（crop=None）/1.86µs** 每次 | ✔ |
| **S3** | `68eede2` | 数据面自检 30 项 / AST 越界 | 三轮装载卸载：**创建 6 == 关闭 6**；`_cue_players` 引用点 **4 处**（`__init__`/`_cue_get_player`/`set_`/`clear_`），**热路径函数 0 处**；幂等 `rt_items` 仍 3；守卫 `PIXEL_OK` | ✔ |
| **S4** | `4993548` | 逐值一致 / 每帧开销 / 异常安全 | `cs_states` 与 Step1 runtime **20 字段 × 5 场景**（前进 161 帧 / 回退 41 / 大跳变 2 / 跨边界 16 / `src_ended` 后 21）**逐值一致（TOL=1e-6）**；空状态 **0.42µs**、3 活跃 **10.8µs** ⇒ 占一帧 **0.065%**；异常：不冒泡 / 本帧清空 / 日志限流 6→1 条 / **下帧恢复** | ✔ |
| **S5** | `a0a48f6` | 出画 / α² / crop / z 序 / fps | 真窗口截图 **2 张**（md5 不同：出画 vs 卸表）；**α² = 0 / 63 / 255**（期望 0/64/255）；crop 分界 **640**；z 序 中心绿 / 边缘蓝 / 隐藏后红；3 子层 **0.62ms** vs 等价普通 1.83ms；**普通图层合成跨版本 PNG md5 相同**（`ab3aba52…`，3366 字节） | ✔ |
| **S6** | `239f47a` | `src_in` / 冻结 / 漂移 / 次数 / 计速 / 防自激 | t=0.46s：`play_sec=3.462`、`position=3.300`（**不是 0.5**）；过出点 **30 帧 md5 同一值**、`play_sec` 恒 8.000、冻结起点≈播 5.0s；**控制误差 0.348s**（阈值 0.35）· 采样峰值 0.367s；seek **7.5 次/30s**（门槛 30）；`set_speed` 序列 **[1.5]**（无 1.0）；`src_ended` 期间 seek **0 次** | ✔ |
| **S7** | `426737d` | 30 次启停 / 资源 / close / 后处理跳过 | **30 轮：创建 90 == 关闭 90、存活 0、句柄 Δ0、线程 Δ0**；真实 3 路 1080p60：**活跃解码器 3（≤8）**、磁盘 **4.1 / 41.7 MB/s**、**RSS 146MB/路**（总 438）、**CPU 1.69 核/路**（总 5.06）；`close()` 后缓存 0 / 3-of-3 关 / `_rt`+`_sheet` 置空 / **关完可重新装表（states=3, active=True）**；接管时 `_apply_color`+`_apply_postfx` **画布 0 字节改动** | ✔ |

① **S0 只以文件形式存在**：`src/cue_seek_policy.py`（首次入库在 `239f47a`）；其门禁为
`tools/cue_should_seek_test.py --impl real`（26 用例）与 `--mutations`（5 变异）。
> 注：S0 当时先以"参考实现"跑通，S0 落地后改为 `--impl real` **测真代码**；变异也从"标志位"升级为
> **源码级变异**（读模块 → 单行改坏 → 临时副本导入 → 跑全量），否则 `--impl real --mutations` 会是假绿。

### 1.1 汇总口径（避免误读）

| 项 | 说明 |
|---|---|
| **fps 门禁**比的是「机制开销」而非「内容成本」 | S5 里码表 3 子层 0.62ms **低于**等价普通图层 1.83ms（`_CueSubLayer` 跳过 `player.current()×2` / `prev_player` / composition-mode 来回切换）；码表**多出来的画面**本身要付一次全屏 blit，那是内容成本 |
| **S7 的 RSS/CPU**是「3 路并发绝对值」，不是 §B.2 的单路量级 | 按每路折算后 146MB / 1.69 核均达标；绝对值 438MB / 5.06 核是 3 路 1080p60 **软解本身**的成本，与码表机制无关（S4/S5 已证机制开销 ≈0.065% / ≈0） |
| **S6 漂移**是「阈值 + 往返延迟」 | 阈值 0.35 是判定线，触发那一帧必然刚跨过（控制误差 0.348），落地再要 ~80ms ⇒ 采样峰值 0.367。团队裁定**不降阈值**（降阈值 ⇒ 更频繁 seek ⇒ 更多抖动与 IO） |
| **DXV/HEVC 的 seek 次数**只有 H.264 实测 | 7.5 次/30s 是 H.264 实测；DXV/HEVC 档位由 `cue_should_seek_test` 的频率上界用例覆盖（DXV ≤30、HEVC ≤2 /30s），**未做真机** ⇒ 见 §3 缺口 |

---

## 2. 照抄备份会错的 5 个地方

> 备份 = `AutoVJ_码表备份_20260930/pre_strip_bak/*`（9-29 版）。本次移植**只挑片段**，
> 但不是照抄：下面 5 处照抄就是错的，其中 3 处是**备份自身的缺陷**。

| # | 位置 | 备份的做法 | 错在哪（实测证据） | 本设计的做法 |
|---|---|---|---|---|
| 1 | `_draw_layer` 的 **glitch** 分支 | **不应用 crop**（对 cover/旋转裁，对 glitch 不裁，自相矛盾） | 带 crop 的片段一旦用 glitch，裁切**静默失效**（画面露出裁切窗口外的内容） | 在 `src_rect` 内按比例切；`src_rect=None` 时与原式**逐像素一致**（守卫 glitch 场景 3 帧 0 字节差异） |
| 2 | **`_cue_frame`** | 只 `current()`，**完全没消费** `play_sec` / `src_in` / `src_ended` | 备份全文 `grep play_sec\|src_in` = **0 命中** ⇒ 入点失效（永远从素材 0 秒播）、出点不冻结、变速不生效 | 消费三者；seek 全走 `CueSeekPolicy`；到出点持 `_cue_last_frame`（实测冻结 30 帧 md5 同值） |
| 3 | **`_CueSubLayer` 的 `trans_active`** | 构造后**没有置 `trans_active=True`** | `_draw_layer` 在 `if tr=="beat_cut" or prog>=1.0 or (not is_kv and not lay.trans_active)` 处**提前返回** ⇒ tr/prog 全被忽略 ⇒ 有效 α 从 **α² 退化成 α**（实测像素 **127**，应为 **64**）⇒ **备份的码表过渡根本没生效** | `stub.trans_active = (prog < 1.0)`；实测 α² = 0/63/255 |
| 4 | `cue_seek_policy.CueSeekPolicy.__init__` 的 `now` 默认值（**本轮新增**） | 初版写成 `lambda: 0.0`（本意给单测用） | 引擎不传时钟时 `now - last_req ≡ 0 < MIN_INTERVAL` ⇒ **节流永远成立 ⇒ 整段只 seek 一次**；实测 6s 内漂移涨到 **0.48** 却不再校正（功能"看起来在跑"，实际已放弃校正） | 默认 `time.monotonic`；单测仍显式传假时钟（26 用例不受影响） |
| 5 | `settle()` 的落地判定（**本轮新增**） | 只判 `\|pos − target\| ≤ 2 帧` | seek 之后 `position()` 会**越过**目标（cv2 的 `POS_MSEC` 还多报 1~2 帧）⇒ **永远判不出落地** ⇒ 2s 后误判超时 ⇒ `degraded` ⇒ **整段再也不校正**（实测只 seek 了 2 次） | `on_seek` 记 `from_pos`，`settle` 里"向目标方向越过"也算落地（向后 seek 对称） |

> 第 4、5 条是**单测全绿、真机才暴露**的两类 bug（典型"功能看起来在跑"）：
> 前者是"给测试用的默认值泄漏到生产路径"，后者是"判定条件漏了方向"。

---

## 3. 已知缺口（都是"未做的功能"，不是缺陷）

| # | 缺口 | 现状 | 归属 |
|---|---|---|---|
| 1 | **位置源未接** | `_cue_pos_beats` 只能由 `set_cue_position()` 注入（S4 留的接口）；**默认 `None` ⇒ 不接管**（安全降级） | Step3（指纹认歌 → 拍位） |
| 2 | **UI 入口未做** | 无导入菜单 / 无轻量对话框 / 无图层徽标；只能代码 `set_cue_sheet()` | Step5 / Step6 |
| 3 | **配置与 i18n 未做** | `cfg["cue"]`（`seek_profile`、最近文件、默认素材目录）与文案键都还没落 | Step7 |
| 4 | **使用说明未补** | 缺"码表章节"与"长 GOP 素材可能晚对齐 ≤1s"的口径（Q1 裁定原文已写在任务书 §13） | Step8 |
| 5 | **DXV/HEVC 无真机 seek 数据** | 本机只有 1 个 DXV 样片（0.9s，来自 `.ercue` 内嵌），无独立 `.dxv`；HEVC 有 954s 长片但未跑 e2e | Step9 |
| 6 | **`_cue_gop_frames` 探测未接** | 档位目前只用 codec 粗分（`_is_dxv` / `_codec`），GOP 实测那一步未实现（不影响正确性：未知一律按 normal 档） | Step4 二期 或 Step7 |

---

## 4. 留给 Step9 的验收项（本次**未做**，别当成已验）

| # | 项目 | 判据 | 为什么这次没做 |
|---|---|---|---|
| 1 | **30 分钟 soak** | 磁盘读**斜率 ≈ 0**（每分钟采样 → 线性拟合）；RSS 斜率 < +2MB/min；句柄 ≤ +2/min | 单次跑 30min 与"段内快速验收"节奏冲突，留给 Step9 集中跑 |
| 2 | **多素材并发（4 层不同文件）** | 活跃解码器 ≤ 8；磁盘均值 ≤120MB/s、10s 峰 ≤250MB/s；CPU 每路 ≤2 核 | 本次只测 3 路（同一素材的 3 个硬链接）；4 层不同编码/分辨率未测 |
| 3 | **真实演出时长（≥1h）** | 无泄漏（RSS/句柄/线程）、无卡顿（`stallwatch`）、码表位置不漂 | 需要长时间占用机器，留给 Step9 |
| 4 | **DXV / HEVC 真机 seek** | DXV ≤30 次/30s（阈值 0.35）、HEVC ≤2 次/30s（只入场） | 见 §3-5 |
| 5 | **4K 素材** | 单路 RSS ≤ +350MB；`speed∈{0.5,1,3}` 下漂移仍 ≤0.35 | 本次样本是 1080p60；4K 解码吞吐未测（R9 风险） |
| 6 | **UI 交互后回归** | 导入 → 绑定 → 播放 → 卸下 → 再导入：解码器计数回基线 | 依赖 Step5/Step6 的 UI |
| 7 | **两端一致性的包级回归** | `tools/twoside_parity_check.py --pkg <每张真实包>` 全 `PARITY_OK` | 已对 4 组包（含全字段包）验证，**未覆盖用户全部历史包** |

---

## 附：复跑命令（全绿即本表可复现）

```bat
:: 数据层（两端数值一致）
python tools\twoside_parity_check.py --pkg "<真实包.ercue>"

:: S0 政策（26 用例 + 5 源码级变异）
python tools\cue_should_seek_test.py --impl real
python tools\cue_should_seek_test.py --impl real --mutations

:: S1 选型矩阵
python tools\cue_factory_cue_test.py

:: S2 像素守卫（0 字节差异）+ crop 几何 + fps
python tools\cue_pixel_diff_guard.py
python tools\cue_pixel_diff_guard.py --verify-crop
python tools\cue_pixel_diff_guard.py --bench 400

:: S3 数据面 30 项
python tools\cue_sheet_mount_test.py

:: S4 tick 一致性 / 开销 / 异常安全
python tools\cue_tick_test.py

:: S5 出画（真窗口截图 + α²/crop/z 序）
python tools\cue_draw_test.py

:: S6 真机（真视频 + 真窗口）
python tools\cue_seek_e2e_test.py

:: S7 生命周期 + 资源
python tools\cue_resource_probe.py --cycles 30 --seconds 8
```

> `tools/_cue_pixbaseline/`（14MB 基准帧）**不入库**：换机器先跑
> `python tools\cue_pixel_diff_guard.py --record` 生成（工具顶部有说明）。
> `tools/_cueshots/` 的 4 张截图已入库，可直接看"出画 / 卸表 / 入点 / 出点冻结"。
