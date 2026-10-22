# 组 A 修复报告（解释器正确性 + 两端对齐）

> 阶段：修复。允许改代码。范围：`src/ercue/model/timeline.py`、`src/ercue/model/features.py`、`src/ercue/render/compositor.py`。
> 验证脚本：`tools/_fix_a_verify.py`（`tempfile.mkdtemp()` 造夹具，无头运行）。
> 解释器：`P:\ERAVJ\EasyRealityCue\venv\Scripts\python.exe`（Python 3.13.14 / PyAV 18.1.0 / PySide6 6.11.2）。

## 结论速览

| 项 | 状态 | 判据 |
|---|---|---|
| A1 出场淡出窗口 | ✅ 修复 | 8s→1.0 / 9s→0.5 / 10s→0；无下段回归不变；**反向验证：旧写法 9s/10s=1.0（断言必失败）** |
| A2 `src_out_sec` | ✅ 实现 | `src_out=4`⇒`play_sec` 钳在 4、`src_ended=True`；全程不越界；**反向验证：旧式 elapsed=5⇒5（越界，断言必失败）** |
| A3 能力表 | ✅ 修复 | `trans_glitch/trans_zoom` 入 `SUPPORTED_FEATURES`；导出→导入不再报"将被忽略" |
| A4 几何对齐 | ✅ 修复 | 默认 xform⇒cover(max)（4:3→16:9 得 160×120）；有变换⇒contain(min×us)；crop 以裁切尺寸为基准、宽高比保持 |
| A5 fade 曲线 | ✅ 修复 | fade@50%⇒α²≈0.25×base（= 演出端 `alpha*trans_p`）；cut 仍硬切 |

- `python -m compileall -q src/`：**零错误**
- `tools/selftest.py`：**7/7 全绿**
- `tests/` **全量**：`test_timeline` / `test_media` / `test_audio` / `test_ui_smoke` / `test_compositor` —— **全部通过**
- `tools/_fix_a_verify.py`：**A1~A5 全部通过**
- `tests/test_compositor.py`：3 例断言旧设计的用例已**按新口径更新期望值**（见文末「测试更新说明」）。

---

## A1 — 出场淡出窗口（timeline.py `_alpha_for`）

### 改法（代码）

```python
# src/ercue/model/timeline.py  _alpha_for 内
        # ★ A1：淡出窗口的右边界。
        #   本 item **有 end_sec**（面板「时长」/ 拖右边缘都会写它）⇒ 以本 item 的 `end_sec`
        #   为准；**只有开放结尾（end_sec is None）**才退回用下一个 item 的 `at_sec`。
        fade_start = None
        if it.end_sec is not None:
            fade_start = _units.to_float(it.end_sec)
        elif nxt is not None:
            fade_start = _units.to_float(nxt.at_sec)
```

（改前顺序相反：`if nxt is not None: … elif it.end_sec is not None: …`。）

### 判据结果

`i1(end=10, trans_out=fade 2s)` + `i2(at=20)`：

| 位置 | 期望 alpha | 实得 |
|---|---|---|
| 8s | 1.0 | 1.0 ✓ |
| 9s | 0.5 | 0.5 ✓ |
| 10s | 0.0 | 0.0 ✓（且该层在 `end_sec` 处失活） |

- **回归**：单 item、`end_sec=None`（无下段）⇒ 不淡出，alpha 恒 1.0（与改前一致）✓
- **反向验证**：`_alpha_old`（`nxt.at_sec` 优先）在 9s/10s 均得 **1.0** ⇒ 若把源码改回旧写法，本项 0.5/0.0 的断言**必然失败** ✓

### 采用语义与依据

- 淡出窗口右界 = 本 item 的 `end_sec`（有）；否则 = 下一段 `at_sec`。淡出在窗口内完成，正好到 `end_sec` 归零。
- `_pick_item`（`timeline.py:177-179`）在 `pos >= end_sec` 时让该层失活，与"到 `end_sec` 归零"一致、不冲突。

---

## A2 — `src_out_sec`（timeline.py + compositor.py）

### 旧实现逐行核对（先读后改，**以旧实现为准**）

- 旧 runtime `AutoVJ_码表备份_20260930/src/cuesheet_runtime.py:174`
  `ps.play_sec = ps.src_in_sec + ((dt_beats * bl) / spd if bl else 0.0)` —— **完全忽略 `src_out_sec`**。
- 旧引擎 `…/engine.py:2511-2540 _cue_frame`：只取 `player.current()`；素材放到末尾时**保持最后一帧**、
  **不因素材放完而失活**；层是否显示仅由 item 的 `end_beats / 下一段 / 淡入淡出` 决定。
- ⇒ **采用语义：`play_sec` 钳到 `src_out_sec`（到出点冻结最后一帧），直到本 item 结束/被下一段替换。**
  这与"素材物理放完后停住"视觉等价，并尊重用户设定的出点。（旧实现是"忽略出点、继续播到素材末尾"，
  本项是**按任务要求补上出点语义**；两端都不在"素材放完"时失活，这点一致。）

### 改法（代码）

```python
# src/ercue/model/timeline.py  LayerDrawState 新增字段
    src_out_sec: Optional[float] = None   # ★ A2：素材出点（None = 到素材末尾）
    src_ended: bool = False               # ★ A2：素材已放到出点（play_sec 已钳在出点，此帧起冻结）
```
```python
# src/ercue/model/timeline.py  update() 内
                elapsed = max(0.0, pos - _units.to_float(it.at_sec))
                raw_play = st.src_in_sec + elapsed * spd
                st.src_out_sec = (None if it.src_out_sec is None
                                  else _units.to_float(it.src_out_sec))
                if st.src_out_sec is not None and raw_play >= st.src_out_sec:
                    st.play_sec = st.src_out_sec
                    st.src_ended = True
                else:
                    st.play_sec = raw_play
                    st.src_ended = False
```
```python
# src/ercue/render/compositor.py  compose() 内（防御性消费）
            so = getattr(st, "src_out_sec", None)
            if so is not None:
                try:
                    play_sec = min(float(play_sec), float(so))
                except (TypeError, ValueError):
                    pass
```

### 判据结果

- `src_in=0, src_out=4, speed=1, at=0, end=10`：9s 时 `play_sec=4.0`、`src_ended=True`、`src_out_sec=4.0` ✓
  （不改写：全程 0~10s 的 `play_sec` 均 **≤ 4**）
- 变速：`speed=2, src_out=4` ⇒ `elapsed=1` 即到出点（`play_sec=4`）✓
- **反向验证**：旧式（不钳位）`elapsed=5 ⇒ play_sec=5 > 4` ⇒ 若去掉钳位，`play_sec ≤ 4` 断言**必然失败** ✓
- `compositor` 侧：`src_out_sec` 被消费（越过出点的 `play_sec` 会被钳回），保证即使调用方不走 timeline 也不越界。

---

## A3 — 能力表（features.py）

### 改法（代码）

```python
# src/ercue/model/features.py
SUPPORTED_FEATURES = {
    "layer_multi", "clip_fade", "clip_inout", "clip_speed",
    "clip_crop", "video_embed", "beat_snap",
    "trans_glitch",   # ★ 已实现（compositor glitch；演出端 engine.py:2845）
    "trans_zoom",     # ★ 已实现（compositor zoom；演出端 engine.py:2841）
}
KNOWN_FEATURES = set(SUPPORTED_FEATURES) | {"multi_layer_cue", "remote_source"}
```
（`selftest` 断言同步：`len==9`、成员集合、`trans_glitch/trans_zoom ∈ SUPPORTED`、`multi_layer_cue/remote_source ∈ KNOWN \ SUPPORTED`。）

### 判据结果

- `trans_glitch/trans_zoom ∈ SUPPORTED_FEATURES` ✓；`KNOWN_FEATURES ⊇ SUPPORTED_FEATURES` ✓
- 含 glitch/zoom 的码表：`recompute_features()` ⇒ `requires_features` 含 `trans_glitch`/`trans_zoom` ✓
- **导出 → 重新导入 → `capability_report` 不再出现"将被忽略"** ✓

### 依据

- 本软件 `compositor.py` 已实现 glitch/zoom；演出端引擎 `engine.py:2841`（zoom）/`:2845`（glitch）也能渲染。
  旧代码把它们判成"本版没有，将被忽略"属**自相矛盾**。两端能力表据此一致。
- `multi_layer_cue` / `remote_source` 确未实现，保留在 `KNOWN_FEATURES`。

---

## A4 — 画面几何对齐演出端（compositor.py）

### 演出端判据（逐条对照 `engine.py`）

```python
# engine.py:2767-2776   _t(item)
us  = img_scale (默认 1.0); ux = img_x; uy = img_y; rot = img_rot
custom = (abs(us-1.0)>1e-3 or abs(ux)>1e-3 or abs(uy)>1e-3 or abs(rot)>1e-3)
# engine.py:2799-2802  paint_cover
if custom: s = min(W/iw, H/ih) * scale * us   # contain
else:      s = max(W/iw, H/ih) * scale        # cover（默认）
# engine.py:2786-2796  crop：先算裁切像素矩形，再 `iw, ih = cw, ch`（以裁切尺寸为适配基准）
```

### 改法（代码，`compositor.py`）

```python
def _is_custom(xf_scale, xf_x, xf_y, xf_rot) -> bool:
    """★ A4：判据逐字照抄演出端 engine.py:2774。"""
    return (abs(xf_scale - 1.0) > 1e-3 or abs(xf_x) > 1e-3
            or abs(xf_y) > 1e-3 or abs(xf_rot) > 1e-3)

def _crop_src_rect(crop, fw, fh):
    """归一化 crop → 源图像素矩形（照抄 engine.py:2786-2798 的取整/钳位）。"""
    ...  # round + clamp，见源码

def _target_src(st, fw, fh, w, h):
    us, ux, uy, rot = _xform_values(st)
    custom = _is_custom(us, ux, uy, rot)
    src = _crop_src_rect(getattr(st, "crop", None), fw, fh) or QRectF(0, 0, fw, fh)
    iw, ih = src.width(), src.height()          # ★ 以裁切后尺寸为适配基准
    if custom:
        k = min(w / iw, h / ih) * us            # contain + 用户缩放
    else:
        k = max(w / iw, h / ih)                 # cover（铺满 + 裁切）
    tw, th = iw * k, ih * k
    cx = w / 2.0 + ux / 100.0 * w
    cy = h / 2.0 + uy / 100.0 * h
    ...  # slide/zoom 空间变换不变；tgt = QRectF(cx - tw/2 + dx, cy - th/2, tw, th)
    return tgt, src
```

### 两端判据对照表

| 输入 | 演出端 | 改前 compositor | 改后 compositor |
|---|---|---|---|
| 默认 xform（scale=1,x=0,y=0,rot=0） | cover `max()` | contain `min()` ❌ | cover `max()` ✅ |
| 任一变换（scale/x/y/rot ≠ 默认） | contain `min()*us` | contain `min()*us` | contain `min()*us` ✅ |
| crop | 以**裁切尺寸**为基准再适配 | 用**整幅尺寸**算框、把裁切区拉伸进去 ❌ | 以裁切尺寸为基准 ✅ |

> 说明：旧注释把演出端的 `max()`（等比 cover）误判为"直接铺满 ⇒ 拉变形"；`max()` 是**等比 cover，不变形**。
> 这一误判导致 compositor 原先永远走 contain，非 16:9 素材的预览构图与现场不一致。

### 判据结果

- 4:3 素材（640×480）→ 16:9 画布（160×90），默认 xform ⇒ 目标框 **160×120**（= cover，高度超出画布裁切）✓
- `scale=1.2` ⇒ 目标宽 **144**（= `min(0.25,0.1875)*1.2*640`，contain）✓，且与 cover 结果不同 ✓
- `x=5`（未改 scale）也应判 contain ✓
- crop `[0,0,0.5,1.0]` @100×100，画布 64×64 ⇒ 源矩形 50×100、目标宽高比 = 源宽高比 = **2.000**（不变形）✓

---

## A5 — fade 曲线两端一致（compositor.py）

### 演出端精确公式（`engine.py:2830-2840`）

```python
# engine.py:2550  op = base_op * ps.opacity * ps.alpha      （ps.alpha 已含淡入量）
# engine.py:2832-2835  fade：
        if tr == "fade":
            if prev is not None:
                paint_cover(p, prev, prev_item, pulse, dx, dy, alpha)
            paint_cover(p, img, cur_item, pulse, dx, dy, alpha * prog, crop)   # ← alpha*prog
```
`prog = ps.trans_p`（`:2559`，cut 时强制 1.0）⇒ fade 最终 = `base × opacity × ps.alpha × trans_p`，
淡入时 `ps.alpha = trans_p = a_in` ⇒ **α²**。slide/zoom/glitch 分支**不**额外乘 `prog`。

### 改法（代码，`compositor.py` compose 内）

```python
            op = _clamp01(st.opacity) * _clamp01(st.alpha)
            if op <= 0.0:
                continue
            # ★ A5：fade 过渡与演出端对齐 —— 额外乘一次 trans_p（slide/zoom/glitch/cut 不乘）
            if (getattr(st, "trans", "fade") or "fade") == "fade":
                op *= _clamp01(getattr(st, "trans_p", 1.0))
                if op <= 0.0:
                    continue
```

### 判据结果（过渡进行到 50%，即 `alpha=0.5, trans_p=0.5`）

| | 值 |
|---|---|
| 本软件 compositor | `(49, 15, 15)`（≈ 0.25×base，量化误差≤1） |
| 演出端公式 `alpha*trans_p` | `(50, 15, 15)` |
| 旧线性（已弃） | `(100, 30, 30)` |

- **两端一致**（α²）✓；且明显区别于旧线性值 ✓
- `cut`（`trans_p=0`）仍硬切为原色 `(200,60,60)` ✓（cut 不参与该支）
- `slide/zoom/glitch` 不额外乘 `trans_p`，与演出端一致（未改动它们的空间变换）。

---

## 测试更新说明（`tests/test_compositor.py`，授权后更新）

本次 A4/A5 是团队裁定（用户已确认方向）的**行为变更**，3 例旧断言锁定的正是被推翻的旧设计
（"测试锁定了错误设计"）。按新版口径更新期望值，**未改回实现**。每处均留了带依据行号的注释。

### ① `test_contain_no_distortion` → 重命名 `test_geometry_matches_engine`

| | 旧值 | 新值 | 依据 |
|---|---|---|---|
| 函数名 | `test_contain_no_distortion` | `test_geometry_matches_engine` | 语义已改为"对齐演出端" |
| 默认 xform 断言 | 4:3 素材 contain（高吃满、左右黑边、宽高比 4:3） | **cover**：bbox 铺满整幅画布（320×180）、宽高比 16:9 | `engine.py:2774`（custom 判据）/`:2799-2802`（`max()` cover） |
| 断言值 | `bh==H` & `aspect==4/3` | `bw==W and bh==H`（铺满裁切，test 内：`assert bw == W and bh == H`） | 同上 |
| **新增**含变换用例 | （无） | `scale=0.8` ⇒ **contain**：bbox 192×144、宽高比 4:3、居中 | `engine.py:2800`（`min()*us`）；`k=min(320/400,180/300)*0.8=0.48` |

### ② `test_transitions_fade_and_cut`

| | 旧值 | 新值 | 依据 |
|---|---|---|---|
| fade 中点 `v5` | `≈ 半色` = `(100,50,25)` | `≈ α²` = `(50,25,13)`（`0.25×base`，`tol=2`） | `engine.py:2832-2835`（`alpha * prog`；alpha 已含淡入量）⇒ `0.5×0.5` |
| 断言原文 | `assert near(v5, tuple(c//2 for c in base))` | `assert near(v5, tuple(round(c*0.25) ...), tol=2)` | 同上 |
| cut | 硬切=原色（回归不动） | 不变 | `engine.py:2559`（cut 强制 `prog=1.0`） |
| slide/zoom/glitch@p=1 | 收敛原色 | 不变（**不**额外乘 trans_p） | `engine.py:2836-2860`（这三支不乘 prog） |

### ③ `test_timeline_to_compositor_integration`

| | 旧值 | 新值 | 依据 |
|---|---|---|---|
| 淡入中点 `mid` | `(100,50,25)`（×0.5） | `(50,25,13)`（×0.25，`tol=2`；实测 `(49,25,12)`） | `engine.py:2835` ⇒ `alpha(0.5)×trans_p(0.5)` |

### 附带（澄清注释，非断言变更）
- `test_alpha_multiplied_once`（D2）：补注"本用例 `trans=cut`，只验证**基础项**不被多乘；
  fade 另按 α²"——避免与 A5 的 α² 混淆。
- 模块 docstring 同步：`D1 contain` → "几何对齐演出端"；`fade 线性` → "fade α²"。

### 复核
- 改后 `tests/` **全量**：`test_timeline` / `test_media` / `test_audio` / `test_ui_smoke` / `test_compositor` —— **全部通过**（exit 0）。
- `tools/selftest.py` **7/7**；`tools/_fix_a_verify.py` **A1~A5 全过**；`compileall -q src/` 零错误。
- 未发现"某例失败其实源于实现真错"的情况——3 例失败均可由 A4/A5 的既定设计变更精确解释。

## 其它（非本次范围）
- A2 选择"到出点冻结"而非"失活"：若 team-lead 希望出点到达后**直接结束 item**（而非冻结），
  只需在 timeline 里把 `src_ended` 时的 `active` 也置 False；语义仅一行之差，按需切换。
