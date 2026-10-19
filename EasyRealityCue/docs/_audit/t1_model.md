# model 层审计报告

> 范围：`src/ercue/model/{cuesheet,ercue_io,timeline,units,features,precheck}.py`
> 方式：**只读**代码审计 + 真实运行验证（用项目 `venv/Scripts/python.exe` 加载 `src` 跑实验，未改动任何源码）。
> 参照物：`docs/架构方案.md`（§3.2 单位纪律 / §4-D,E）、`docs/码表功能设计_v0.2.md`、`docs/规格书_旧实现考古.md`。

## 小结

- 审计文件：**6** 个 / **2,213** 行；发现 **P0 0 / P1 3 / P2 3 / P3 4 = 10** 项。
- **最值得关注**：`timeline._alpha_for()` 只认"下一个 item 的起点"、**不认本 item 自己的 `end_sec`**，导致"片段显式设了时长"（面板设时长 / 拖右边缘都会写 `end_sec`）且与下一段之间有空隙时，**配置的淡出被整段丢弃、到点硬切**；这是"位置→画面"唯一真源里最直接的一类错误。
- 另有一条**能力声明与实际实现不一致**（`trans_glitch`/`trans_zoom` 已实现却声明"未支持"，软件会把它们写进文件再自己提示"将被忽略"），与 render 层 t3 报告的结论一致，此处从 `features.py`/`ercue_io` 的**声明侧**补上根因与证据。

> 全部结论均给出 `文件:行号` + 真实代码片段；凡实验结论均附实际运行输出。

---

## 发现清单

### [P1] 淡出（trans_out）被"下一段起点"抢走：带 `end_sec` 的片段在有空隙时不淡出、到点硬切

- **位置**：`src/ercue/model/timeline.py:197-210`（`_alpha_for`）+ `src/ercue/model/timeline.py:177-179`（`_pick_item`）
- **现象**：某层里有两个片段，前一段**设了时长**（`end_sec` 有值）且 `end_sec` 早于后一段的起点（层内存在空隙）时，前一段**不会淡出**——它一直以 `alpha=1.0` 满亮显示，直到 `end_sec` 那一刻**直接消失**。用户在面板里配的「出场=淡出 2 秒」完全不生效。
- **根因**：`_alpha_for()` 计算淡出起点 `fade_start` 时**优先取下一段的 `at_sec`，只有"没有下一段"时才回落到自己的 `end_sec`**：

  ```python
  # timeline.py:197-205
  fade_start = None
  if nxt is not None:
      fade_start = _units.to_float(nxt.at_sec)          # ← 有下一段就用它，忽略自己的 end_sec
  elif it.end_sec is not None:
      fade_start = _units.to_float(it.end_sec)
  tout = _units.to_float(it.trans_out_sec, 2.0)
  if fade_start is not None and tout > 1e-6 and it.trans_out != "cut":
      left = fade_start - pos
      if left <= tout:
  ```

  而 `_pick_item()` 又按**自己的 `end_sec`** 到点即失活（且不受淡出影响）：

  ```python
  # timeline.py:177-179
  if it.end_sec is not None and pos >= _units.to_float(it.end_sec):
      return None, i, True                              # ← 到 end_sec 直接失活，淡出窗口根本没机会播
  ```

  两者叠加：淡出窗口被算在"下一段起点之前"（太晚），而片段本人在 `end_sec`（更早）就被判失活 ⇒ 淡出窗口永远落在"已失活"的区间里。
- **证据**（实跑）：层内 `i1`(at=0, **end=10**, 出场 fade 2s) 与 `i2`(at=20)：

  ```
  pos=  8.50 active=True  item='i1'  alpha=1.000 ended=False
  pos=  9.99 active=True  item='i1'  alpha=1.000 ended=False   ← 8~10 的淡出窗口 alpha 始终 1.0
  pos= 10.00 active=False item=''    alpha=0.000 ended=True    ← 到 10s 直接硬切
  ```

  对照（**唯一区别是没有下一段**，即 `end_sec` 被回落到 `fade_start`）则正常淡出：

  ```
  pos=  8.50 active=True  alpha=0.750
  pos=  9.00 active=True  alpha=0.500
  pos=  9.99 active=True  alpha=0.005
  ```

- **影响**：`end_sec` 由属性面板「时长」写入（`props_panel.py:286 it.end_sec = (it.at_sec + ln) if ln > 0 else None`），拖右边缘也写入（`window.py:1019`）。因此**只要用户在层内留了空隙并给前一段设了时长，出场淡出就静默失效**；预览与最终编排都会"到点硬切"，而数据里却写着 `trans_out="fade"`。
- **建议**：**修**。`fade_start` 应取"本片段真正的结束点"：`end_sec` 非空时优先 `end_sec`，为空才用下一段 `at_sec`。即把 `timeline.py:198-201` 两个分支的优先级对调。改动小、语义明确，且是"解释器正确性"级别的问题，值得修。
- **置信度**：高（代码 + 实跑双向印证）

---

### [P1] 素材"出点"`src_out_sec` 全程无人使用：`clip_inout` 能力是半张空头支票

- **位置**：`src/ercue/model/timeline.py:264-265`（只算 `play_sec`，从不卡 `src_out`）、`src/ercue/model/cuesheet.py:164-182`（`normalized` / 模型字段）；下游 `render/compositor.py` 的 `LayerDrawState` **没有 `src_out` 字段**
- **现象**：用户在属性面板设了「素材入点/出点」（例如只要素材的 2~4 秒），导出后**出点毫无作用**——素材会一直往后播，越过出点继续放，直到片段自己的 `end_sec`（若没设则一直到曲终）。即"只取素材的一段"做不到，只有"入点"生效。
- **根因**：`src_out_sec` 被完整地存进模型、写进文件（`ercue_io.py:84`）、在面板里可编辑（`props_panel.py:289`），但**运行时解释器与合成器链路没有任何一处读它**。`CueRuntime.update()` 只产出 `src_in_sec` 与 `play_sec`：

  ```python
  # timeline.py:247-265（节选）
  st.src_in_sec = _units.to_float(it.src_in_sec)
  spd = max(1e-6, _units.to_float(it.speed, 1.0))
  elapsed = max(0.0, pos - _units.to_float(it.at_sec))
  st.play_sec = st.src_in_sec + elapsed * spd         # ← 没有对 src_out_sec 做任何钳制/判停
  ```

  `LayerDrawState`（`timeline.py:68-91`）里也没有 `src_out` 字段，合成器拿不到它。
- **证据**（实跑）：片段 `src_in_sec=2, src_out_sec=4`，`end_sec=None`：

  ```
  pos=0  play_sec=2.00   pos=2  play_sec=4.00   pos=4  play_sec=6.00   pos=6  play_sec=8.00   pos=10 play_sec=12.00
  ```

  `play_sec` 一路越过 `src_out=4` 继续增长，且片段始终 `active=True`。
- **影响**：`features.SUPPORTED_FEATURES` 里 `clip_inout` = "素材只取一段（入/出点）"（`features.py:18`），并被写入 `.ercue` 的 `requires_features`。但"出点"实际无实现 ⇒ **对演出端/接收方声明了一个并未兑现的能力**（正是"声明支持但没实现"的危险类型）。用户照面板设了出点，预览与现场都多放一截。
- **建议**：**修（优先于其它）**。两条路选一：① 让解释器在 `play_sec >= src_out_sec` 时把该 item 判为结束（与 `end_sec` 同级的失活条件）；② 至少在 `recompute_features` 里**不要把出点记成 `clip_inout`**，并在导出前检查里提示"出点当前不生效"。方案 ① 才能让能力表名副其实。
- **置信度**：高（全项目 grep：`src_out_sec` 仅出现在 cuesheet/ercue_io/features/props_panel，运行链路无引用）

---

### [P1] 能力协商误报：`trans_glitch` / `trans_zoom` 已实现却声明"未支持"，软件把自己写的能力判成"将被忽略"

- **位置**：`src/ercue/model/features.py:14-27`（`SUPPORTED_FEATURES` / `KNOWN_FEATURES`）、`src/ercue/model/features.py:80-83`（推断）、`src/ercue/model/ercue_io.py:220-222`（协商）
- **现象**：用户给片段选了「故障(glitch)」或「缩放(zoom)」过渡并导出，**再用本软件（或演出端）打开这张码表**，会弹出："这张码表用到以下本版没有的能力，将被忽略：trans_glitch"。可实际上合成器**已经实现了** glitch/zoom 的空间变换（`render/compositor.py:173-176` 的 slide/zoom、`:200-217` 的 glitch 分带错位）。提示与事实相反。
- **根因**：`SUPPORTED_FEATURES` 只列 7 项、**不含** `trans_glitch`/`trans_zoom`；而 `recompute_features()` 会因用到 glitch/zoom 而**把它们写进 `requires_features`**：

  ```python
  # features.py:80-83
  if tin == "glitch" or tout == "glitch":
      feats.add("trans_glitch")
  if tin == "zoom" or tout == "zoom":
      feats.add("trans_zoom")
  ```

  导入时协商逻辑据此把这两项判为"本版没有"：

  ```python
  # ercue_io.py:220-222
  unknown = [f for f in (sheet.requires_features or []) if f not in SUPPORTED_FEATURES]
  if unknown:
      warn.append("这张码表用到以下本版没有的能力，将被忽略：%s" % "、".join(sorted(unknown)))
  ```

- **证据**（实跑）：片段 `trans_in="glitch"` → 导出推断 `requires_features=['beat_snap','clip_fade','trans_glitch']` → 再导入协商：

  ```
  WARN: 这张码表用到以下本版没有的能力，将被忽略：trans_glitch
  ```

  另注：`TRANSITIONS=('fade','cut','slide','zoom','glitch')`（`cuesheet.py:35`），其中 **`slide` 连一个能力 token 都没有**——它被 `recompute_features` 的 `tin not in ("cut",)` 规则笼统归入 `clip_fade`，能力表无法区分"淡入"与"滑动"。
- **影响**：跨程序契约（`requires_features`）**自相矛盾**：软件声明某能力被使用，又声明自己不支持它 → 用户看到无意义的"将被忽略"告警；真正换演出端时，也可能让接收方误以为该片段不可渲染而忽略 glitch/zoom。`slide` 的能力粒度缺失是同一问题的另一半。
- **建议**：**修**。把 `trans_glitch`/`trans_zoom`（以及 `slide` 对应 token）提升进 `SUPPORTED_FEATURES`，与 `render/compositor.py` 的实际实现对齐；若坚持不实现某一种，则应从 `TRANSITIONS` 里移除或从 `requires_features` 推断里剔除以免误导。**本条与 render 层 `docs/_audit/t3_media_audio_render.md`（引擎侧同一结论）指向同一矛盾**，此处仅补声明侧根因；由 team-lead 决定合并归口。
- **置信度**：高

---

### [P2] "曲终淡出"未实现：开放结尾（`end_sec=None`）的末片段永不淡出

- **位置**：`src/ercue/model/timeline.py:197-203`（`fade_start` 三支）
- **现象**：某层最后一段是"开放结尾"（面板「时长」留空 ⇒ `end_sec=None`）时，即便出场设为「淡出」，它也**从头到尾 alpha=1.0**，连曲终都不淡出。
- **根因**：`fade_start` 在"既无下一段、`end_sec` 也为 None"时保持 `None`，`a_out` 恒 `1.0`：

  ```python
  # timeline.py:197-203
  fade_start = None
  if nxt is not None:
      fade_start = _units.to_float(nxt.at_sec)
  elif it.end_sec is not None:
      fade_start = _units.to_float(it.end_sec)
  tout = _units.to_float(it.trans_out_sec, 2.0)
  if fade_start is not None and tout > 1e-6 and it.trans_out != "cut":
  ```

  解释器本身**不知道曲目长度**（未使用 `track.identity.duration_sec`），所以无法在曲终触发收尾。而设计文档 §7.3 step 5 明确写「**曲终 → 各层按各自 trans_out 淡出**」。
- **证据**（实跑）单条开放结尾片段（`trans_out="fade" 2s`），轨道时长 300s：

  ```
  pos=250 active=True alpha=1.000    pos=299 active=True alpha=1.000    pos=300 active=True alpha=1.000
  ```

- **影响**：设了出场淡出的开放结尾片段，在预览与最终编排中都不会淡出；与设计约定不符。
- **建议**：**修**（中等优先级）。让 `CueRuntime` 持曲目总长（`track.identity.duration_sec`），在 `pos` 逼近曲终的最后 `trans_out_sec` 内对"开放结尾"片段施加淡出。注意别与 `timeline_total_sec`（UI 侧）重复定义曲长。
- **置信度**：中（"应当淡出"来自设计文档 §7.3，非代码内契约）

---

### [P2] `bpm = Infinity` 会让**导出**抛 `ZeroDivisionError`

- **位置**：`src/ercue/model/units.py:42-57`（`bpm_is_usable` / `beat_len` / `sec_to_beats`）
- **现象**：极少数脏数据 / 手改文件把 `identity.bpm` 写成 `Infinity`（Python `json.loads` 默认接受 `Infinity` 字面量）时，**导入不报错**，但**导出**会崩在 `sec_to_beats`：`60.0/inf = 0.0`，随后 `x / 0.0` 抛 `ZeroDivisionError`。
- **根因**：

  ```python
  # units.py:42-57
  def bpm_is_usable(bpm) -> bool:
      return to_float(bpm) > _MIN_BPM          # inf > 1e-6 → True（被判"可用"）

  def beat_len(bpm) -> float:
      b = to_float(bpm)
      if b > _MIN_BPM:
          return 60.0 / b                      # 60.0 / inf = 0.0
      return DEGENERATE_BEAT_LEN

  def sec_to_beats(sec, bpm) -> float:
      return to_float(sec) / beat_len(bpm)     # x / 0.0 → ZeroDivisionError
  ```

  `beats_to_sec`（乘）不崩，所以导入路径安全，只有导出（`ercue_io._item_to_file` → `sec_to_beats`）会炸。
- **证据**（实跑）：`sec_to_beats(10, float('inf'))` → `ZeroDivisionError: float division by zero`。
- **影响**：低概率但一旦命中就是导出处崩溃（有 try/except 包裹会记一条"导出失败"，数据无法出包）。
- **建议**：**修**（一行）。`bpm_is_usable` 增加有限性判定（`math.isfinite`），或 `beat_len` 对非有限值回落退化态。属于健壮性加固。
- **置信度**：中（崩溃已实证；`bpm=Infinity` 的实际出现频率未知）

---

### [P2] 内嵌素材（`video_embed`）导入时从不释放：`extract_media` 定义了但全项目零调用

- **位置**：`src/ercue/model/ercue_io.py:275-294`（`extract_media`）、`src/ercue/model/ercue_io.py:250-272`（`load` 只看同目录侧车 `<pkg>.media`）
- **现象**：导出时勾了「内嵌素材」（`save(embed=True)`，素材写进 zip 的 `media/`），**换台机器导入该 `.ercue` 后，所有素材仍显示"缺失"**——因为 `load()` 只认包**旁边**的 `<pkg>.media` 目录，从不把 zip 里的 `media/` 解出来。
- **根因**：`save()` 会把素材写进 zip：

  ```python
  # ercue_io.py:324-329
  with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
      if embed:
          for m in t["media"]:
              p = m.pop("_src", None)
              if p and os.path.isfile(p):
                  z.write(p, "%s/%s" % (MEDIA_DIR, m["name"]))
  ```

  但 `load()` 指向的是**磁盘侧车目录**，而非包内 `media/`：

  ```python
  # ercue_io.py:245（zip 分支返回值）
  return raw, path + ".media"
  # ercue_io.py:266-267（load 里用它当媒体目录）
  md = media_dir if media_dir is not None else (
      embed_dir if embed_dir and os.path.isdir(embed_dir) else None)
  ```

  而真正能解出包内素材的 `extract_media()`（`ercue_io.py:275`）**在整个 `src/` 中没有任何调用点**（grep 仅命中定义与 `__all__`）。
- **影响**：`video_embed` 能力被声明为"支持"，但"内嵌 → 分发 → 打开"这条链路是断的；用户会得到"我明明内嵌了素材，对方却全丢"的体验。
- **建议**：**修**。在 `load()`（或 `import_sheet`）里：若包内存在 `media/` 且侧车目录不存在，则调用 `extract_media()` 解到 `<pkg>.media` 后再重挂。**此条与 ui 层 `docs/_audit/t2_ui.md` 的同一现象呼应**（他们从"UI 没调用"角度记录）；根因在 IO 层，故归此。
- **置信度**：高（代码 + grep 为零调用）

---

### [P3] `CueItem.normalized()` 不校验 `src_in_sec` / `src_out_sec`：负入点会原样传给取帧

- **位置**：`src/ercue/model/cuesheet.py:164-182`
- **现象**：脏文件里 `src_in_sec=-99` 能通过 `normalized()` 存活，运行时时 `play_sec` 变负，直接交给素材取帧。
- **根因**：`normalized()` 钳了 `at_sec/speed/trans_*/crop/end_sec`，**唯独漏了素材入/出点**：

  ```python
  # cuesheet.py:164-182（节选）
  self.speed = min(3.0, max(0.1, _f(self.speed, 1.0)))
  self.at_sec = max(0.0, _f(self.at_sec))
  self.trans_in_sec = max(0.0, _f(self.trans_in_sec, 2.0))
  self.trans_out_sec = max(0.0, _f(self.trans_out_sec, 2.0))
  if self.end_sec is not None:
      self.end_sec = max(self.at_sec, _f(self.end_sec))
  if self.crop is not None and len(self.crop) != 4:
      self.crop = None
  # ← 没有 self.src_in_sec = max(0.0, ...) ；也没有 src_out_sec 的合法性处理
  ```
- **证据**（实跑）：`src_in_sec=-99` 经 `normalized()` 后仍为 `-99.0`；`update(5.0)` 得 `play_sec = -94.0`。
- **影响**：低概率（需脏数据/负数输入），但会把非法值传播到取帧层。
- **建议**：**修**（一行）。补 `src_in_sec = max(0.0, ...)`；`src_out_sec` 若非 None 则保证 `> src_in_sec`，否则置 None。
- **置信度**：高（行为已实证；触发概率低）

---

### [P3] `min_app_version` 用**字符串**比较：非零填充版本号会判错

- **位置**：`src/ercue/model/ercue_io.py:217-219`
- **现象**：`sheet.min_app_version > app_version` 是纯字符串比较。当前版本号形如 `2026.10.01.09`（零填充），恰好成立；但只要一端出现非零填充（如 `2026.9.1`），比较即失效（字符串比较里 `"2026.9.1" > "2026.10.01.09"` 为真）。
- **根因**：

  ```python
  # ercue_io.py:217-219
  if app_version and sheet.min_app_version and sheet.min_app_version > app_version:
      warn.append("这张码表要求 %s 以上版本（当前 %s），部分画面可能不对。"
                  % (sheet.min_app_version, app_version))
  ```

  另注：源码态 `APP_VERSION` 回退为 `"0.0.0-dev"`（`window.py:66`），此时任何非空 `min_app_version` 都会被判成"更高"而误报警。
- **影响**：延迟性健壮性问题；当前版本格式下不触发。
- **建议**：**暂可不修**，但建议换成按段解析的版本比较，或显式约定"版本号必须零填充"并加断言。
- **置信度**：中（逻辑缺陷确定；当前格式下不表现）

---

### [P3] `CueRuntime.total_sec()` 对开放结尾片段返回 `at_sec`（≈死代码 / 语义误导）

- **位置**：`src/ercue/model/timeline.py:152-159`
- **现象**：对"单条开放结尾片段（`at_sec=0, end_sec=None`）"，`total_sec()` 返回 `0.0`；对"末尾开放片段"也只返回其 `at_sec`，而非真实编排终点。
- **根因**：

  ```python
  # timeline.py:155-159
  for plan in self._plans:
      for it in plan.items:
          end = it.end_sec if it.end_sec is not None else it.at_sec   # 开放结尾 → 用 at_sec
          mx = max(mx, _units.to_float(end))
  return mx
  ```
- **影响**：`total_sec()` 在 `src/` 中**仅被 `timeline.selftest` 使用**（UI 用的是 `window.timeline_total_sec`，见 `window.py:1088`），因此对用户无直接可见影响；但一个"编排总长"方法返回 0 会误导后续维护者。
- **建议**：**暂不修**（先确认是否要保留），若保留则与 UI 的曲长口径统一，或标注"仅统计显式 `end_sec`"。
- **置信度**：高（行为已实证；影响面见上）

---

### [P3] 淡出期的 `trans_p` 语义使 `slide` 出场变成"从右滑入"

- **位置**：`src/ercue/model/timeline.py:206-210`
- **现象**：`trans_out="slide"` 淡出时，合成器用 `trans_p` 做空间变换（`compositor.py:173-175`：`dx=(1-tp)*w` 右入）。淡出期 `trans_p = 1 - a_out` 由 0 升到 1，于是片段**一边淡出、一边从右侧滑入**，而非滑出。
- **根因**：

  ```python
  # timeline.py:205-210
  if left <= tout:
      a_out = max(0.0, left / tout)
      if a_out < a_in:                  # 淡出优先（正在收尾）
          trans = it.trans_out
          trans_p = 1.0 - a_out          # ← 出场进度，但合成器把它当"入场进度"用
  ```
- **影响**：只影响 `slide`/`zoom`/`glitch` 出场的空间方向观感（淡出曲线本身正确）。属观感问题。
- **建议**：**暂可不修**；若要修，需合成器区分"入场/出场"两套空间变换，属 render 层协同改动。
- **置信度**：中

---

## 检查过但判定"没问题"的点

- **`units.py` 往返一致性 / 退化态**：`sec→beats→sec` 在 120/155.03/128/99.9 BPM 多点往返误差 `< 1e-9`；`bpm<=0`、`None`、非法串、`<1e-6` 一律退化为"1 拍=1 秒"且 `bpm_is_usable` 可显式判知——符合 §3.2。`NaN` 经 `to_float`/比较天然落入退化态（实测 `nan > 1e-6` 为 False）。
- **时间轴 z 序（D3）**：`update()` 末尾 `out.sort(key=lambda s: s.z)` + 断言；`Composition.sorted_layers()` 按 z 升序。UI 新增层 `z = max+10`（`window.py:898`）、时间轴倒序显示（`timeline_widget.py:202`）——**z 方向一致，不会上下颠倒**。
- **回滞（E1）与大跳变（E2）**：`_pick_item` 上界用 `pos + hyst` 抑制边界抖动；倒退超回滞 / 前跳超 `jump` 置 `jumped=True` 并跳过过渡。读码与 `tests/test_timeline.py`（±0.01s 抖动 50 次、前跳/倒退）一致，逻辑无发现。
- **缺素材只跳过不断链（E3）**：`update()` 对每层恒返回 `LayerDrawState`，仅置 `missing=True`；合成器按 `missing` 跳过——链不会断，符合要求。
- **`play_sec` 变速方向（D4，×speed）**：与演出端"speed=倍速"一致，`tests/test_timeline.py:257` 已有防回归断言——**判定为正确**。
- **`ercue_io` 版本协商四条文案与顺序**：格式版本 → `min_app_version` → 未知能力 → 缺素材，文案与规格书 §3.2 原文一致；`load` 对非法 JSON / 找不到文件抛 `ValueError`，脏数据经各 `from_state_dict` 容错不崩。
- **`state_dict`（秒）与 `file_dict`（拍）分离**：`state_vs_file_selftest` 已钉死"状态快照不含拍字段、文件形态含拍字段"；`at_beats` 为主坐标、导入不信 `at_sec`——符合 §3.2。
- **`Identity.meta_key` 与四级认歌**：`nz` 正则、时长取整到 2 秒、`match_track` 四级降级（含多命中取 |时长差| 最小 / 无时长加 `?`）逐字符合规格书 §2.2/§4，selftest 覆盖到位。
- **`precheck.check_sheet` 判据完整性**：缺素材（可强制导出）/ 超长（超曲目·超素材，且 `total<=0` 时跳过以免误报）/ 未吸附（off 与 off-grid 区分）三类均覆盖，容差 `1e-3` 合理。**仅两处小瑕疵**（不单列）：① 未用于任何 item 的 media 若文件丢失仍报"缺素材"并阻止导出（`precheck.py:168-171` 遍历的是全部 `sheet.media`，未用 `all_used_clip_ids()`）；② `snap != off` 的对齐判定基于"从 0 起算"的网格，未计入音频 anchor（`§4-C4`），若无 anchor 概念则该项对不齐属预期。
- **`Composition.sorted_layers` / `TrackEntry.layers` 别名**：`track.layers` 与 `track.composition.layers` 语义一致，无反向依赖，无循环 import（`features` 纯 duck-typing、不 import 本包）。
- **零 Qt 纪律**：`tools/selftest.py:39` 的 AST 扫描 + `tests/test_timeline.py:74` 的 AST 断言均确认 `model/` 无 Qt import。
