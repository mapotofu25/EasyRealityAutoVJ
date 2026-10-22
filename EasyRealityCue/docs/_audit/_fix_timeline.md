# 时间轴 5 条用户问题 · 修复报告

**范围**：只改 `P:\ERAVJ\EasyRealityCue\src\ercue\**` 与 `tests/**`（未触碰 `P:\ERAVJ\AutoVJ\`）。
**自测脚本**：`tools/_fix_timeline_verify.py`（离屏 Qt 实测：真造 180s 视频、真取帧、真跑鼠标拖放）。
**结论**：5 条全部修复；`compileall -q src/` 零错误；`tools/selftest.py` **7/7**；
`tests/` **全绿**（test_media 8/8、test_ui_smoke 全通过、test_timeline/test_compositor/test_audio 全通过）；
`_fix_timeline_verify.py` **6/6 通过（含 ①②③ 三条反向验证）**。

跑法：
```
P:\ERAVJ\EasyRealityCue\venv\Scripts\python.exe  tools/_fix_timeline_verify.py
QT_QPA_PLATFORM=offscreen  P:\ERAVJ\EasyRealityCue\venv\Scripts\python.exe  tests\test_ui_smoke.py
```

---

## 结论速览

| # | 用户原话 | 根因（一句话） | 修复文件 |
|---|---|---|---|
| ① | 三分钟的东西有时候被拉成更长时间 | 放入片段时 `end_sec` 留成 `None`，绘制把开放结尾**拉到下一片段/视口右端** | `ui/window.py` |
| ④ | 一首歌三分钟却在时间轴上被铺满 | **同 ①**：`end_sec=None` 且本层没有下一片段 ⇒ 画到**视口右端**（=铺满） | `ui/window.py` |
| ② | 预览开始后视频被拉长减速 | 预览用**关键帧快速路径**取帧，同一 GOP 内所有时刻都返回**同一张关键帧** | `media/frames.py` + `ui/preview.py` |
| ③ | 拖动应以拖动距离为准，而不是让素材开头对准鼠标 | 松手时落点用了**裸鼠标位置**（没加按下偏移），把片段**开头对齐到光标** | `ui/timeline_widget.py` |
| ⑤ | 把吸附功能去掉 | 吸附是**功能本身**（UI 控件 + 落点逻辑 + 导出检查） | `ui/window.py`+`ui/timeline_widget.py`+`model/precheck.py` |

★ **重点说明（关于"①②④ 是不是同一个根因"）**：
- **① 与 ④ 确实是同一个根因** —— `end_sec` 没写（`None`），而渲染把 `end_sec=None` 解释为
  "到本层下一个片段 / 曲终"。一个 3 分钟素材因此被拉到"下一段起点"或"视口右端"，
  前者表现为"**拉成更长时间**"，后者（本层没有下一段）表现为"**铺满整条时间轴**"。
  一处修复（放入时写入素材真实时长）同时解决 ①④。
- **② 是另一个独立根因**（预览取帧走错路径），只是**症状相似**（都被用户描述成"拉长/减速"）。
  素材在时间轴上的"长度"和预览里"画面是否前进"是两件事，必须分别修。

---

## ① / ④（同一根因）片段被拉长 / 铺满时间轴 —— 已修

### 现象
- 选一个 3 分钟的素材放到某层：**有时候**它的色块被拉得比 3 分钟长得多（当后面还有别的片段时）；
- 如果它是本层唯一/最后一个片段：色块**铺满整条时间轴**（无论缩放多少都铺满）。

### 根因（真实代码）
**A. 放入片段时从未写 `end_sec`** —— `src/ercue/ui/window.py`（修复前 `add_cue`）：
```python
it = CueItem(item_id=iid, clip_id=clip_id, at_sec=at,
             trans_in="fade", trans_in_sec=2.0,
             trans_out="fade", trans_out_sec=2.0,
             snap=self._snap_mode()).normalized()      # ← 没有 end_sec ⇒ 默认 None
```
**B. 渲染把 `end_sec=None` 拉到"下一片段 / 视口右端"** —— `src/ercue/ui/timeline_widget.py:228`：
```python
def _item_end_px_sec(self, ly, it):
    end = it.end_sec
    if end is None:
        nxt = [x.at_sec for x in ly.items if _units.to_float(x.at_sec) > _units.to_float(it.at_sec)]
        end = min(nxt) if nxt else (self.view0 + self.view_span)   # ← 没有下一段就画到视口右端
    return max(_units.to_float(end), _units.to_float(it.at_sec))
```
**推断依据**：A 提供 `end_sec=None`，B 把这个 `None` 解释成"开放结尾"。于是色块的宽度 =
"到下一段起点"（有后续片段时）或"视口右端"（没有后续片段时），**两者都不等于素材真实时长**。
这正是"有时候拉长"（有后续片段）与"铺满"（无后续片段）两种表现的来源。

### 改法
`src/ercue/ui/window.py` —— 放入片段时**写入素材真实时长**：
```python
# window.py:1021 add_cue
dur = self._clip_duration_sec(clip_id)              # 素材真实时长（秒）
end = (at + dur) if dur > 0.0 else None             # 量得到才写；量不到才保留"开放结尾"
it = CueItem(item_id=iid, clip_id=clip_id, at_sec=at, end_sec=end,
             trans_in="fade", trans_in_sec=2.0,
             trans_out="fade", trans_out_sec=2.0).normalized()
```
```python
# window.py:1054 新增：素材真实时长（MediaRef.duration_sec → 现场探测兜底并回填）
def _clip_duration_sec(self, clip_id) -> float:
    ref = s.sheet.media_by_id(clip_id)
    if ref is not None and float(ref.duration_sec or 0.0) > 0.0:
        return float(ref.duration_sec)
    p = s.media_paths.get(clip_id) or ""
    if p and os.path.isfile(p):
        d = self._audio_duration_sec(p)             # mutagen / PyAV 容器时长（音视频通用）
        if d > 0.0:
            if ref is not None: ref.duration_sec = float(d)     # 回填，避免反复探测
            return float(d)
    return 0.0
```
> 说明：`MediaRef.duration_sec` 在**新增素材时**由 `_probe_media_ref`（组 B 已接线 `probe()`）写入；
> 兜底用 `_audio_duration_sec()`（mutagen 优先、PyAV 容器时长次之），**音/视频都能量**，
> 覆盖用户说的"视频**音频**在时间轴上都有问题"。
> 图片没有时长（`probe` 对 image 返回 0）⇒ 仍为 `None`（静图当作"持续到下一 cue"是合理语义）。

### 判据结果
- `tools/_fix_timeline_verify.py`（真造 180s / 5fps 视频）：
  - 探测时长 = **180.00s**；`add_cue(at=10)` ⇒ `end_sec == 190.0s`（= at+180）✅
  - 视口分别设 **60 / 180 / 600s**，`_item_end_px_sec` **恒为 190s**（与缩放无关，**不铺满**）✅
  - 曲目总长 = **180.0s**（来自真实时长）✅
- **反向验证 ①**：
  - 把 `end_sec` 回退成 `None`（旧行为）⇒ `_item_end_px_sec == 600.0s`（**视口右端，被拉长**）⇒ 判据成立 ✅
  - 回退 `add_cue`（`_clip_duration_sec` 返回 0）⇒ `end_sec is None`（**修复前正是如此**）✅

---

## ② 预览播放"被拉长减速" —— 已修

### 现象
点 ▶ 开始播放后，**视频画面不前进**：停在一张画面上不动，偶尔跳一下，看起来像被拉长/慢放。

### 根因（真实代码）
预览取帧走了**关键帧快速路径** —— `src/ercue/ui/preview.py:116`（修复前）：
```python
return self.src.frame(path, play_sec)      # ← frame() 每次 seek 到不晚于 sec 的最近关键帧
```
`src/ercue/media/frames.py` 的 `_get_frame(exact=False)`：
```python
if not exact:
    fr = next(iter(src.container.decode(video=0)), None)   # ← 只取关键帧后第一帧
    return _frame_to_bgra(fr) if fr is not None else None
```
**推断依据**：现实素材（h264/DXV）关键帧**稀疏**（GOP 常 1~4s）。`frame()` 每次都
`seek(backward=True)` 回关键帧再取一帧 ⇒ **同一个 GOP 内所有时刻都返回同一张关键帧**。
播放头在动、位置在变，但取到的帧不变 ⇒ 画面"卡住"，只在跨过 GOP 边界时跳一下 = 用户看到的"拉长减速"。

### 改法
新增 **播放专用取帧** `frame_play()`（`src/ercue/media/frames.py:207`）：**顺序解码前进**，
只在"回退 / 大前跳"时才 seek；同位置重复取帧复用上一帧（音频停住时画面必须停住）。
预览改走它（`src/ercue/ui/preview.py:120`）：
```python
return self.src.frame_play(path, play_sec)
```
```python
# frames.py:207  frame_play —— 播放取帧：优先顺序解码前进，绝不每帧 seek
if (src.cur_frame is not None and src.cur_pts is not None
        and src.cur_pts - PLAY_REUSE_BACK_SEC <= target <= src.cur_pts + 1e-6):
    return src.cur_frame                                     # ① 同位置复用（不跳帧）
forward = (src.cur_pts is not None and 1e-6 < (target - src.cur_pts) <= PLAY_FWD_MAX_SEC)
if not forward:
    if not self._seek(src, target):                          # ② 回退/大前跳才 seek
        return self._decode_first(src)
return self._decode_until(src, target)                       # ③ 顺序解到 pts ≥ target
```
> 实测依据：PyAV 的 `container.decode()` **在多次调用之间会继续前进**（探针实测：
> 一次 seek 后连续 6 次取帧 pts = 6144→6656→7168→7680→8192→8704），故"顺序前进"安全。
> 任何 seek 都会清空顺序位置（`_seek` 里重置 `cur_pts/cur_frame`），
> 与缩略图 / 入出点预览（`frame()`/`frame_exact()`）混用时**不会误用陈旧位置**。

### 判据结果
- `_fix_timeline_verify.py`（大 GOP h264，5fps）：旧 `frame()` 只出 **1** 张不同帧；
  `frame_play()` 出 **21** 张连续帧 ✅
- "正常速度（对比墙钟）"：1.0s 与 2.0s 取到**不同帧**；2.0→4.0s 内 **10/10** 个 0.2s 段画面前进 ✅
- 同位置重复取帧**返回同一帧**（不跳帧）✅
- **反向验证 ②**：旧路径 `frame()` 帧序号**跨度 = 0**（停在关键帧＝画面卡住）⇒ 判据成立 ✅
- 新增长期回归哨兵 `tests/test_media.py::test_frame_play`：
  **`frame()` 跨度=0（停在关键帧），`frame_play()` 跨度=46（连续前进 ≈ 目标×fps）** ✅

---

## ③ 拖动"以拖动距离为准"（而非开头对准鼠标） —— 已修

### 现象
拖动素材时，松手后**素材开头跳到鼠标松开的位置**，而不是"整块跟着手平移了多远"。

### 根因（真实代码）
`src/ercue/ui/timeline_widget.py` 修复前 `mouseReleaseEvent`：
```python
it = d["item"]
new_x = self._last_mx if self._last_mx is not None else (self._x(it.at_sec) + d.get("dx", 0.0))
nb = self._snap(self._sec(new_x))          # ← 落点 = 裸鼠标位置（没加 dx）⇒ 开头对齐到光标
self.moved.emit(d["lyr_id"], it.item_id, float(nb))
```
**推断依据**：按下时记录了 `dx = _x(片段起点) - 鼠标按下x`（见 `mousePressEvent`）。
拖动**预览**（`mouseMoveEvent`）用的是 `new_x = 鼠标x + dx`（= 以距离为准），
但**松手落点**却用了裸 `_last_mx`（= 开头对齐到光标）—— **预览与结果不一致**，
也正是用户感受到的"让素材开头对准鼠标位置"。

### 改法
`src/ercue/ui/timeline_widget.py:637` —— 松手落点与拖动预览**统一**为"鼠标 + 按下偏移"：
```python
# ★ 拖动语义 = 以拖动距离为准：落点 = 松手鼠标 x + 按下时记录的偏移 dx
mx = self._last_mx if self._last_mx is not None else self._x(it.at_sec)
new_x = mx + d.get("dx", 0.0)
nb = max(0.0, self._sec(new_x))
```

### 判据结果
- `_fix_timeline_verify.py`（在片段**中部**按下、拖 15s）：`at 10.0 → 24.89`（**平移 14.89s**），
  未跳到光标（光标 39.9s）✅
- 回归哨兵 `tests/test_ui_smoke.py::test_drag_drop_and_timeline_mouse`：中部按下拖 12s ⇒
  平移 ≈12s，且断言 **`|at - 光标秒| > 4`**（开头不跳光标）、**时长保持不变** ✅
- **反向验证 ③**：旧逻辑落点 = 光标秒（39.9s）≠ 平移后起点（25.0s）⇒ 修复确有区别 ✅

---

## ④ 见 ①（同一根因）

---

## ⑤ 移除吸附功能 —— 已修（**移除功能本身，不是隐藏**）

### 改法（end-to-end 移除）
| 位置 | 移除内容 |
|---|---|
| `ui/window.py` | 常量 `SNAP_ITEMS`；顶栏「吸附」下拉框 `cb_snap`（构建 + 接线）；`_snap_mode()`；`_update_snap_enabled()`；`_on_snap_changed()`；`_sync_view()` 里的 `set_snap_mode` 调用；`add_cue()` 的 `snap` 参数与落点吸附 |
| `ui/timeline_widget.py` | `snap_mode` 状态；`set_snap_mode()`；`_snap()`；落点/拖动/拉边里的吸附（改用**真实坐标** `self._sec(...)`） |
| `model/precheck.py` | 「**未吸附**」整类检查：`UnsnappedItem`、`_is_unsnapped()`、`SNAP_ALIGN_TOL_SEC`、`Report.unsnapped` 及其 `summary_lines/counts/clean` 分支 |
| `ui/dialogs.py` | `report_text()` 文案里的「未吸附」 |
| `model/cuesheet.py` | `CueItem.snap` 默认值 `"bar"` → `"off"`（新片段不再声称"对齐小节"） |

**保留**（仅为**旧文件格式兼容**，非功能）：`CueItem.snap` 字段、`SNAP_MODES`、
`ercue_io` 文件键 `snap`（被 selftest 钉死）、`units.snap_grid()`（纯换算工具 + 自带自检）、
`features` 里 `beat_snap` 能力词（旧文件可能含；"只增不删"）。
`precheck` 不再读 `snap`，故旧文件里 `snap="bar"` 也**不会再产生"未吸附"告警**。

### 判据结果
- `_fix_timeline_verify.py`：`CueTimeline` 无 `set_snap_mode/_snap/snap_mode`；
  主窗口无 `cb_snap/_snap_mode/_on_snap_changed`；`CueItem().normalized().snap == "off"`；
  `Report` 无 `unsnapped` 且 `counts()` 不含该键 ✅
- `tests/test_ui_smoke.py`：新增 `assert not hasattr(tl, "set_snap_mode"/"_snap"/"snap_mode")`、
  `assert not hasattr(w, "cb_snap")`；导出前检查 / 报告文本用例改为断言**无**"未吸附" ✅
- `python -m ercue.model.precheck` 自检通过（`缺1 超长2（吸附检查已移除）`）✅

---

## 全量验证结果

| 项目 | 结果 |
|---|---|
| `compileall -q src/` | 零错误 ✅ |
| `tools/selftest.py` | **7/7 全绿** ✅ |
| `tools/_fix_timeline_verify.py` | **6/6 全绿**（含 ①②③ 反向验证）✅ |
| `tests/test_media.py` | **8/8**（新增 `test_frame_play`）✅ |
| `tests/test_ui_smoke.py` | **全部通过**（含拖动/拉边/导出前检查/报告文本）✅ |
| `tests/test_timeline.py` / `test_compositor.py` / `test_audio.py` | **全部通过** ✅ |

### 因本修复而**必须要改**的既有测试（都是"旧断言依赖了 bug 行为"）
- `test_ui_smoke.py::test_end_to_end`：合成取 30.0s→**30.5s**。因为片段现在带真实时长，
  30.0s 时 it2 刚入点（淡入起 `alpha=0`）本就该是黑的；30.5s（淡入中段）才是有内容的一帧。
  （旧测试能过，是因为它依赖了 it1 的 `end_sec=None` 被"铺满"到 30s。）
- `test_ui_smoke.py::test_timeline_geometry`：删掉 `_snap/set_snap_mode` 断言，改为断言**已移除**。
- `test_ui_smoke.py::test_drag_drop_and_timeline_mouse`：拖动断言改为"**平移≈拖动距离**"。
- `test_ui_smoke.py::test_move_preserves_duration`（D）：`end_sec` 现在会被写入，故**显式**置 `None`
  再测"开放结尾移动后仍为 None"。
- `test_ui_smoke.py::test_pre_export_check` / `test_report_text`：去掉"未吸附"这一类。
- `test_media.py`：新增 `biggop` 夹具（`libx264 g=250` 强制单大 GOP）与 `test_frame_play`。

### 未改动的可疑点（留档，供后续）
- **静图（图片素材）** 没有时长 ⇒ `end_sec` 仍为 `None`（会被画到下一 cue / 视口右端）。
  这是"静图当作持续背景"的合理语义，故**未**擅自给图片造时长。
- `units.snap_grid()` 现在 UI 侧已无调用方（仅 selftest）；因属公开工具且被自检引用，保留。
