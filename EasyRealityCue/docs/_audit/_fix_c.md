# 组 C 修复报告 —— `audio/player.py` 打开失败永不复位（C1）

> 范围：`src/ercue/audio/player.py`（唯一改动的 src 文件）+ `tests/test_audio.py`（1 处断言随契约更新）
> 新增：`tools/_fix_c_verify.py`（离线 mock 验证脚本）、本报告。
> 判据：全部通过（4/4）；`compileall -q src/` 零错误；`tests/test_audio.py` 全绿。

## 小结

- **根因**：`_open_failed` 一旦置位**永不复位**（`:206` 初始化、`:405/:428` 置位、`:226` 消费）。
  打开设备失败一次后，`available` 恒 False，而 `play()` 又用 `available` 当闸门
  （旧 `:345`）⇒ **整个会话永久无声**，用户只能重启软件。
- **改法**：① 每次显式 `play()` **复位** `_open_failed` 并**重新尝试打开**；
  ② 连续失败按 **1→2→4s** 退避（首败后的第一次重试放行；冷却期内不再尝试，绝不每帧重试）；
  ③ 打开仍在**专用线程**（A6），`play()` 只**有界等待**其结果 ⇒ 打开失败**同步返回 False**；
  ④ `available` 改为**冷却期一过即自动恢复**，不再"永久不可用"。
- **判据**：首次失败→`play()=False` + error ✓；二次 `play()`（mock 成功）能起播 ✓；
  连续失败有退避（5 次调用仅真开 2 次，冷却 2.0s）✓；**反向验证**：去掉复位→第二次仍失败 ✓。

## 现状（修复前）

```python
# player.py:206（旧）—— 一处初始化，之后只有置位、没有复位
self._open_failed = False

# player.py:226（旧）—— available 直接吃这个永久标记
@property
def available(self) -> bool:
    return self._device_ok and not self._open_failed

# player.py:345（旧）—— play() 用 available 当闸门 ⇒ 失败后永久拒绝
if not self.available:
    self.error = self.error or "本机没有可用的音频输出设备"
    return False

# player.py:405 / :428（旧）—— 后台线程置位，且**没有任何复位点**
self._open_failed = True
```

## 改法（附代码）

### ① 新常量 + 退避阶梯函数（player.py 顶部）

```python
OPEN_RETRY_BASE_SEC = 1.0
OPEN_RETRY_MAX_SEC = 4.0
OPEN_WAIT_SEC = 0.4        # play() 等待"打开结果"的最长时间（有界等待）

def _open_backoff_sec(fail_count: int) -> float:
    """连续失败第 fail_count 次后应退避的秒数：1→2→4（封顶 OPEN_RETRY_MAX_SEC）。"""
    if fail_count <= 0:
        return 0.0
    return min(OPEN_RETRY_MAX_SEC, OPEN_RETRY_BASE_SEC * (2 ** (fail_count - 1)))
```

### ② `available` 改为"退避冷却期一过即恢复"（不再永久 False）

```python
@property
def available(self) -> bool:
    if not self._device_ok:
        return False
    if not self._open_failed:
        return True
    return time.monotonic() >= self._next_open_ts      # ★ 冷却期后自动恢复可重试
```

### ③ `play()`：复位 + 退避 + 有界等待结果

```python
now = time.monotonic()
with self._lock:
    fails = self._open_fail_count
    next_ts = self._next_open_ts
# ② 退避：连续失败 ≥2 次后，冷却期内不再尝试（绝不每帧重试）
if fails >= 2 and now < next_ts:
    self.error = "音频输出设备打开失败，%.1f 秒内不再重试" % (next_ts - now)
    return False
# ① 显式 play() 一律复位"永久失败"标记，本次真正尝试打开（绝不再永久拒绝）
with self._lock:
    self._open_failed = False
...
ev = threading.Event(); opened = threading.Event()
with self._lock:
    self._stop = ev; self._opened = opened; self._open_result = None
th = threading.Thread(target=self._run, args=(ev, opened), name="ercue-audio", daemon=True)
th.start()
opened.wait(OPEN_WAIT_SEC)          # ③ 有界等待：失败→False；成功→True；卡住→乐观 True
with self._lock:
    res = self._open_result
return res != "fail"
```

### ④ `_run()`：登记打开结果 + 会话守卫（防旧线程污染新会话）

```python
def _run(self, ev, opened=None):
    ...
    try:
        sink = self._sink_factory(self._sr, self._ch)
    except Exception as e:
        self._on_open_failed(e, "打开音频输出设备失败", ev); return
    self._on_open_ok(ev)
    if opened is not None:
        opened.set()                 # 打开成功 → play() 即刻返回 True（不必等满超时）
    ... 播放循环（原样）...
    finally:
        if opened is not None and not opened.is_set():
            opened.set()             # 异常/超时路径也保证唤醒 play()
        ... sink.close(); ev.set()
```

```python
def _is_current_session(self, ev) -> bool:
    return ev is None or self._stop is ev       # 与解码侧 _load_token 同思路

def _on_open_ok(self, ev=None) -> None:
    with self._lock:
        if not self._is_current_session(ev):
            return
        self._open_fail_count = 0; self._next_open_ts = 0.0
        self._open_failed = False; self._open_result = "ok"

def _on_open_failed(self, exc, prefix="打开音频输出设备失败", ev=None) -> None:
    now = time.monotonic()
    with self._lock:
        if not self._is_current_session(ev):
            return
        self._open_fail_count += 1
        self._next_open_ts = now + _open_backoff_sec(self._open_fail_count)
        self._open_failed = True; self._open_result = "fail"
        self.error = "%s：%s: %s" % (prefix, type(exc).__name__, exc)
```

> `_open_failed` 的**含义未改**（仍是"上次打开失败"，供 `available` 判断），
> 只是新增了 `_open_fail_count` / `_next_open_ts` 承担退避语义 —— 遵循"有歧义就新增字段，
> 不改旧字段含义"的约束。

## 退避参数

| 参数 | 值 | 说明 |
|---|---|---|
| `OPEN_RETRY_BASE_SEC` | 1.0 s | 退避基数 |
| `OPEN_RETRY_MAX_SEC` | 4.0 s | 退避封顶 |
| 阶梯 | 1 → 2 → 4 → 4 … | 连续失败第 N 次后 = `min(4, 2^(N-1))`；**成功即清零** |
| `OPEN_WAIT_SEC` | 0.4 s | `play()` 有界等待"打开结果"的上限（成功/失败通常几十 ms 就返回） |
| 首败后第一次重试 | **放行** | 让条件恢复（设备插回 / 占用解除）的用户一次 ▶ 即恢复；从**第 2 次**连续失败起才进冷却 |

## 判据结果（`tools/_fix_c_verify.py` 实跑输出）

```
退避参数：base=1.0s  max=4.0s  阶梯=[1.0, 2.0, 4.0, 4.0]  等待打开=0.40s
  ✓ 判据1：首次打开失败 → play()=False、error='打开音频输出设备失败：RuntimeError: 模拟打开声卡失败'、available=False
  ✓ 判据2：首败后第二次 play() 成功起播（重试次数=2，available 恢复 True）
  ✓ 判据3：连续失败进入退避（5 次调用仅尝开 2 次；剩余冷却 2.00s；阶梯 1→2→4s）
  ✓ 判据4（反向）：去掉复位 → 第二次 play() 仍失败（False）；恢复后 → 成功
汇总：全部通过 ✓
```

- **判据1**：mock 首次 `open` 抛异常 ⇒ `play()` **返回 False** 且 `error` 非空。
- **判据2**：mock 第二次成功 ⇒ `play()` **返回 True 且真的再次调用 `sink_factory`**（尝试次数=2），
  `available` 恢复 True ⇒ 证明"复位/重试"生效，不再是永久拒绝。
- **判据3**：mock 恒失败，连调 `play()` 5 次 ⇒ **只真正尝试打开 2 次**（首败放行 1 次重试，此后进冷却），
  退避窗口仍有效 ⇒ 证明"有退避、不每帧重试"。
- **判据4（反向）**：把复位/退避恢复去掉（`_next_open_ts` 设为永不结束）⇒ **第二次 `play()` 仍失败**、
  且根本不再尝试打开 ⇒ 重现修复前的"永久无声"，反证复位是关键。

## 对既有测试的影响

- `tests/test_audio.py::test_device_open_failure` 原本断言 `p.play(0.0) is True`
  （旧契约：打开在后台线程，先返回 True 再异步静音）。**新契约要求打开失败同步返回 False**，
  故把该断言改为 `is False`，并补一条"冷却期过后 `available` 自动恢复"的断言。
  其余 8 个用例**未改**，实跑全绿：

```
--- 位置语义=已交声卡帧号 ---  ✓（不喂不前进、喂了才前进、可按起点定位）
--- stop() 不卡 ≤0.8s ---     ✓（卡死声卡下 0.7508s 返回）
--- 无设备不崩 ---            ✓（available=False，play()=False）
--- 设备打开失败降级 ---      ✓（play()→False、available→False(可恢复)、不崩）
汇总：全部通过 ✓
```

## 影响面 / 未改动

- `play()` 现在**有界阻塞**调用方最多 `OPEN_WAIT_SEC=0.4s`：仅在"打开卡住"这一极端情形才等满，
  成功/失败路径都在几十 ms 内返回；打开本身仍在专用线程（**A6 未被破坏**：sounddevice 调用依旧只在播放线程）。
- 未新增对其它模块的依赖，未改动 decoder / UI / 数据层。
- **未修**（保持记录，非本次范围）：`device_probe` 只在构造时探测一次 ⇒ 若"启动时无声卡、之后热插"，
  `available` 仍为 False（`play()` 直接返回 False）。这属另一条链，如需可后续在 `play()` 内按需重探。
