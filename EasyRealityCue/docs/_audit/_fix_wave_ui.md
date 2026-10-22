# 真实彩色波形接入时间轴 · 接线报告

**范围**：只改 `src/ercue/ui/timeline_widget.py`（主）+ `src/ercue/ui/window.py`（喂路径 / 停线程）
（**未改** `src/ercue/audio/waveform.py` 数据层；**未触碰** `<项目根>\AutoVJ\`）。
**自测脚本**：`tools/_fix_wave_ui_verify.py`（离屏 Qt 实测：真造音频、真跑后台线程、真取像素）。
**结论**：把 `ui/timeline_widget.py` 里那条**假波形**换成**真数据**；
`_fix_wave_ui_verify.py` **6/6 通过（含 ②③ 两条反向验证）**；`compileall -q src/` 零错误；
`tools/selftest.py` **7/7**；`tests/` **全绿**（test_media 8/8 含 `test_frame_play`、test_ui_smoke 全通过、
test_timeline / test_compositor / test_audio 全通过）。

跑法：
```
<项目根>\EasyRealityCue\venv\Scripts\python.exe  tools/_fix_wave_ui_verify.py
<项目根>\EasyRealityCue\venv\Scripts\python.exe  tools/selftest.py
QT_QPA_PLATFORM=offscreen  <项目根>\EasyRealityCue\venv\Scripts\python.exe  tests\test_ui_smoke.py
```

---

## 一、接线方式

### 1) 数据来源：零 Qt 的数据层（**只调用、不修改**）
`src/ercue/audio/waveform.py` 提供：
```python
data = wf.load_or_compute(audio_path, columns=None, progress=cb)   # WaveformData | None（绝不抛）
# data.columns / data.amp[i] / data.r[i],data.g[i],data.b[i] / data.color_at(i) / data.duration_sec
# 列密度 COLUMNS_PER_SEC=200（约 5ms/列）；低频→R、中频→G、高频→B；缓存命中≈0.009s、3 分钟首次≈0.8s
```
本层**只做"算一次 → 按视口重采样着画"**，不自己实现任何 DSP。

### 2) 入口：把"当前曲目的音频路径"告诉时间轴
`ui/window.py` 的 `_sync_timeline_data()`（每次刷新都走）末尾新增一行：
```python
# ★ 真实彩色波形：把当前曲目的音频文件喂给时间轴（换轨即重算；无音频不触发计算）
self.timeline.set_audio_path(self._audio_path())
```
`_audio_path()` 读 `session.track_audio[当前曲目]`；**没导入音频的曲目返回空串** ⇒ 纯图片素材
（以及视频素材但没绑音频）**根本不会触发计算**。

### 3) `CueTimeline.set_audio_path()`（`ui/timeline_widget.py`）—— 四条分支
```python
def set_audio_path(self, audio_path):
    ap = str(audio_path or "")
    if ap == self._wav_path:            # ① 路径没变 ⇒ 直接返回（不重算、不打断在算的）
        return
    self._wav_path = ap
    self._wav = None
    self._wav_progress = 0.0
    if self._wav_worker is not None:    # ② 换轨（含换到"没音频"的轨）：顶掉在途的旧请求
        self._wav_worker.request("")
    if not ap or not os.path.isfile(ap):    # ③ 无音频 / 文件不存在 ⇒ 退化，绝不触发计算
        self._wav_state = "none"; self.update(); return
    cached = self._load_wave_cache(ap)      # ④ 命中缓存 ⇒ 同步读入（毫秒级，无线程）
    if cached is not None:
        self._wav = cached; self._wav_state = "ready"; self.update(); return
    self._wav_state = "computing"           # ⑤ 未命中（数秒）⇒ 后台线程
    self._ensure_wave_worker().request(ap)
    self.update()
```

### 4) 绘制：替换掉旧的伪波形
**删除**（旧 `_paint_waveform`）：`math.sin(sec*2.1)*0.6 + math.sin(sec*7.3)*0.3 + math.sin(sec*0.7)*0.1`
（"示意波形带（M3 不做真实波形）"）。

**新 `_paint_waveform` 要点**：
- **列 → 像素 = 线性插值（缩放只重采样、不重算）**：`wave_sample(x)` 把像素 x 经 `self._sec(x)`
  换成秒，再 `/ (duration_sec/columns)` 换成**浮点列号**，对相邻两列 `i0/i1` **线性插值** `amp / r / g / b`。
  数据层 200 列/秒的**原始列数被完整保留**（不预先降采样到像素数）。
- **只画可见列**：逐像素遍历 `x ∈ [GUTTER, width)`；视口外的列（如音频比视口短）`wave_sample` 返回
  `None` 自动跳过 ⇒ **长歌也只需遍历当前宽度那么多次**，缩放/平移丝滑。
- **高度对称**：以轨道**中线** `cy = r.center().y()` 为中心，`h = max(1, amp · (row高度-6)/2)` 上下对称。
- **每列上色**：`QColor.fromRgbF(r·k, g·k, b·k)`（**不交换通道**：低频红 / 中频绿 / 高频蓝）；
  `k = 0.55 + 0.45·amp` 是**等比压暗**（保色相、弱段也看得见）。
- **保留原有布局**：不改 `_row_rect` / `GUTTER` / `RULER_H` / 轨道底色与「音频」标签，只换数据源与上色。

**退化绘制 `_paint_wave_progress`**：计算中画一条淡中线 + 底部细进度条 + 「波形计算中 NN%」；
一旦 `None`（无 / 失败）则留空 —— **绝不假装有波形、绝不崩**。

---

## 二、线程模型（算在后台，绝不卡 GUI）

`WaveformWorker(QThread)`（`ui/timeline_widget.py`，仿 `ui/panels.py` 的 `ThumbWorker`）：

| 要素 | 做法 |
|---|---|
| 启动 | `_ensure_wave_worker()` **惰性**建线程（用到才建），与控件/窗口同生命周期 |
| 回主线程 | `done = Signal(str, object)` / `progressed = Signal(str, float)`；跨线程自动 **queued connection**，槽在主线程跑 |
| 计算 | `run()` 里 `wf.load_or_compute(job, progress=…)`（数据层自带 0.0→1.0 进度回调） |
| **取消** | **单槽 latest-wins**（不是队列）：`request(p)` 覆盖待办并 `_seq+=1`；在途计算返回后 `_is_current(seq)` 为假 ⇒ **结果丢弃、不回写** |
| 换轨 | `set_audio_path()` 变化时先 `request("")` 顶掉旧的；旧结果因路径/序号不符被丢弃 |
| 关窗 | `stop_waveform()`：`stop()` 置停止位 + 清待办 + `wait(1500)`；`window.shutdown()` 调用（**不阻塞退出**） |
| 缓存命中 | 主线程**同步**读（`_load_wave_cache`，毫秒级）—— 不需线程 |

> 为什么"缓存命中可以同步、未命中必须后台"：数据层实测**3 分钟首次 ≈ 0.8s、缓存命中 ≈ 0.009s**；
> 0.8s 放主线程会让首帧卡顿，故未命中一律丢线程（判据②已实测：`set_audio_path` 立即返回）。

---

## 三、判据结果（`tools/_fix_wave_ui_verify.py`，可复跑）

```
======================================================================
  [PASS] ① 画真数据（低红/高蓝 + 像素）
  [PASS] ② 不卡 UI（后台线程 + 响应 + 逆向）
  [PASS] ③ 换轨取消不串写（+逆向）
  [PASS] ④ 降级不崩（无/不存在/失败）
  [PASS] 接线：主窗口喂音频路径
  [PASS] ⑤ 静态闸门（compileall + selftest）
======================================================================
汇总：6/6 通过  ⇒  全绿 ✓
```

| 判据 | 实测 |
|---|---|
| ① 真数据 | 合成 100Hz / 6kHz：低频取样 `=(1.00,0.00,0.00)` **R 主导**、像素**红 5754 / 蓝 0**；高频取样 `=(0.00,0.00,1.00)` **B 主导**、像素**红 0 / 蓝 5754**；换音频后像素分布改变 ⇒ 确非固定伪波形 |
| ② 不卡 UI | 模拟"算 3 秒"：`set_audio_path` 返回 **0.001s**；计算**在非主线程**（线程 id ≠ 主线程）；期间 0.6s 内 QTimer 跳 **19 次**；**逆向**：主线程直接跑 **3.01s** |
| ③ 取消不串写 | 先慢 A(0.8s) 立刻再请求快 B(0.05s)：最终=**B(amp=0.80)**，期间应用过的值**只有 0.80（无 A）**；再等 0.6s 仍显示 B；**逆向**：单独请求 A ⇒ 应用 A(amp=0.20)（证明判据有效） |
| ④ 退化不崩 | 不存在的文件 / 空路径 ⇒ 状态 `none`、**未起线程**、渲染不崩；计算返回 `None`（失败）⇒ 平滑退化 `none`、不崩 |
| 接线 | `MainWindow._sync_timeline_data → set_audio_path(low.wav)`，**缓存命中同步读入**（`_wav_worker is None`） |
| ⑤ 静态 | `compileall -q src/` 零错误；`tools/selftest.py` **7/7** |

**反向验证（②③）** 见上表：②证明"省掉的正是那 3 秒主线程计算"；③证明"单独请求 A 会被应用"，
从而"先 A 后 B 时 A 从未被应用"确由**换轨取消**造成，不是判据失效。

### 视觉留档
`_shots/m6_waveform.png`（合成 60s 歌：前 30s 安静低频 / 后 30s 副歌中高频）：
前段一条**红**色（低频）窄带，后段一条**高得多**的**青绿**（中频 900Hz + 高频 6kHz）带 —— 一眼看出歌曲结构。

---

## 四、全量验证表

| 闸门 | 命令 | 结果 |
|---|---|---|
| 编译 | `python -m compileall -q src/` | 零错误 |
| 模型自检 | `python tools/selftest.py` | **7/7** |
| 素材层 | `python tests/test_media.py` | **8/8**（含 `test_frame_play`：`frame()`跨度 0 / `frame_play()`跨度 46） |
| 时间轴/合成/音频 | `test_timeline.py` / `test_compositor.py` / `test_audio.py` | **全部通过** |
| UI 冒烟 | `QT_QPA_PLATFORM=offscreen python tests/test_ui_smoke.py` | **全部通过**（含截图、拖放、撤销、多码表、缺失素材、最近文件…） |
| 接线验证 | `python tools/_fix_wave_ui_verify.py` | **6/6（含 ②③ 反向验证）** |

---

## 五、降级与生命周期一览

| 情形 | 行为 |
|---|---|
| 无音频路径 / 纯图片素材 | `set_audio_path("")` ⇒ 状态 `none`，**不触发计算**、**不建线程** |
| 音频文件不存在 | 同上（`os.path.isfile` 拦截） |
| 计算失败（`None`） | `done(path, None)` ⇒ 平滑退化 `none`，渲染留空，不崩 |
| 计算中 | 画淡中线 + 进度条/百分比；期间 **GUI 不卡**（后台线程） |
| 换轨 | 取消在途计算（latest-wins）；旧结果**永不回写**新轨 |
| 关窗口 | `window.shutdown() → timeline.stop_waveform()`：`stop()` + `wait(1500)`，**不阻塞退出** |
| 缩放 / 平移 | **只重采样**（`wave_sample` 像素↔列插值），**不重算**、不起线程 |

---

## 六、留档 / 说明

- **未改数据层** `src/ercue/audio/waveform.py`（按要求：数据层已验收，缺陷只上报不自修）。
  数据层本层需求**全部满足**，无需上报缺项。
- **唯一一处轻度耦合**：缓存命中时同步读，用了数据层的私有只读接口 `_load_cache(cache_path_for(...))`
  —— 目的是"**只读缓存、绝不顺带重算**"。已用 `getattr(wf, "_load_cache", None)` 守护：
  若该接口未来缺席，**自动退化为走后台线程**，绝不阻塞主线程（`_load_wave_cache()`）。
- 数据层缓存在 `<app_base>/cache/waveform/*.npz`；**用户真实目录**在本项目里就是
  `%LOCALAPPDATA%\EasyRealityCue\cache\waveform`（数据层既定约定）；自测脚本已把 `wf.cache_dir`
  改写到临时目录，**不污染用户目录**。
- 旧文档里"`timeline_widget.py` 只画不改模型"的约束仍成立：本控件只**读**音频数据、只**画**，
  不写任何模型 / 不碰文件（除数据层自己的缓存）。
