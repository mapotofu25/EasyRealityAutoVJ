# -*- coding: utf-8 -*-
"""记录：NDI 输出没有音频的根因（三个 bug 叠加 + 一个我自己的失误）。"""
import io
import os

MEM = r"C:\Users\Administrator\WorkBuddy\自动VJ软件\.workbuddy\memory"
LOG = os.path.join(MEM, "2026-10-01.md")

TXT = """

## 十、★ NDI 输出「还是没有音频」——三个 bug 叠加，音频**从来没响过**

拿发行包里自带的 cyndilib 真跑一遍 sender，把 API 实测清楚：

| 实测 | 结果 |
|---|---|
| `AudioSendFrame.shape` | **(2, 4800)** ⇒ 布局是 **(声道数, 样本数) 平面(planar)** |
| `channel_stride` | 19200 = 4800×4 ⇒ 印证是平面、不是交错 |
| `write_data((2,1024))` | **成功** |
| `write_data((1024,2))` | 失败 `number of channels must match` |
| 老代码传的**一维交错** | 失败 `Buffer has wrong number of dimensions (expected 2, got 1)` |
| `hasattr(a, 'set_sample_rate')` | **False** —— 只有 `sample_rate` 属性能赋值 |

⇒ **三个 bug 叠在一起**，而每一个都被 `except Exception: pass` 吞掉、
**一条日志都没有**，所以看起来就是"开关是开的、源也在、就是没声音"：
1. `a.set_sample_rate(sr)` —— 这个方法根本不存在（AttributeError）；
2. `write_data` 需要 **(声道, 样本) 平面二维数组**，代码传的是一维交错；
3. 异常被静默吞掉（无法从日志发现）。

**还有一个更严重的隐患**（回环实测发现）：
- `open()` 之后**立刻** `send_audio()` ⇒ **段错误（native crash，Python 抓不住）**；
  等 0.3s 再发就正常。现场路径正好踩：视频线程刚建好 sender，音频线程 ~20ms 后就来了。
- **没有接收端连上时** `send_audio()` 也会段错误（`send_video` 则是抛 `Object is not writable`）。

### 修法（`src/ndi_out.py`）
1. `a.sample_rate = sr`（属性）；交错 → `np.ascontiguousarray(pcm.reshape(-1,2).T)` 平面；
2. **开门静默期** `_WARMUP_SECS = 0.3`：open 后这段时间内只丢帧，不碰 native；
3. **音频侧连接数门控**：`sender.get_num_connections(0)==0` 时直接丢块（防段错误），
   ⚠ 但**视频不能门控** —— NDI 是"接收端收到数据后连接才建立"，不发视频就永远连不上
   （这一点是我先加错了、被回环测试抓出来又改回来的）；
4. `send_video` 的写失败**不再 `enabled=False`**（老代码一次瞬时错误就永久关掉整个 NDI 输出）；
5. `feed_audio` 不再静默：首次成功发一行、首次因未连接丢块也发一行日志。

### 未能在此环境验证的部分
`tools/_test_ndi_loopback.py`（本进程内起 NDI 接收端连自己）在沙箱里**连不上**
（连接数恒 0、视频写 `Object is not writable`），是环境挡住了 NDI 组播，
不是代码问题（用户的生产环境里接收端能看到源）。⇒ 需要用户在真实环境验证。

### 教训
**"开关开着却没效果"优先怀疑 native API 用错 + 静默 except 的组合**：
这次三个错误全是"抛异常 → 被吞掉 → 无日志"，查了一个小时才靠**直接调 API**定位。
以后写 native 封装：**每个 except 至少记一次日志**，别让失败隐形。
"""

io.open(LOG, "a", encoding="utf-8").write(TXT)
print("已写入", LOG)
