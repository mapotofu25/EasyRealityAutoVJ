# -*- coding: utf-8 -*-
"""修回归 + 顺手把 kick 用的低频掩码也纳入预算表。

★ 回归原因（2026-09-30，我改的）：把 FFT 段的 `freqs` 改名成 `_freqs` 之后，
  下面第 444 行 `bass_mask = (freqs >= 20) & (freqs <= 200)` 还在用旧名 ⇒
  **`NameError: name 'freqs' is not defined` 每块都抛** ⇒ 采集线程把它捕获成
  `st.error = "分析块错误（已跳过）: ..."` ⇒ 整机**能量/BPM 全废**（现场状态栏会红字提示）。
  ⇒ 教训：改热路径里的局部变量名，必须 grep 整个函数体；改完**必须真跑一遍**
  （这次我只跑了指纹与频带单测，没跑 `_Analyzer.feed`，所以漏了）。
"""
import io
import os
import sys

ROOT = r"P:\AutoVJ"
P = os.path.join(ROOT, "src", "audio_engine.py")
PAT = []


def rep(old, new, cnt=1):
    PAT.append((old, new, cnt))


# 1) 名字改回来（最小修复：让 444 行的 `freqs` 有定义）+ 顺带解包 rg_kick
rep("        _freqs, rg_bass, rg_mid, rg_high, edges, bpm_period = self._fft_plan()",
    "        freqs, rg_bass, rg_mid, rg_high, edges, bpm_period, rg_kick = self._fft_plan()")

# 2) kick 的低频掩码也进预算表（原来每块建一次布尔数组再做 fancy index）
rep("""        # ---- 低频(kick)通量：踢鼓节奏比全频稳定（全频易被旋律乐句周期带偏）----
        bass_mask = (freqs >= 20) & (freqs <= 200)
        bass_spec = spec[bass_mask]""",
    """        # ---- 低频(kick)通量：踢鼓节奏比全频稳定（全频易被旋律乐句周期带偏）----
        # ★ 掩码由 `_fft_plan()` 预算（原来每块都要建一次布尔数组 + fancy index）
        bass_spec = spec[rg_kick[0]:rg_kick[1]]""")

# 3) 预算表里加 rg_kick（20~200Hz，含右端：原式是 <=200，所以 hi 用 searchsorted'right'）
rep("""        # 现场指纹识别的**窗口长度**""", """        # 现场指纹识别的**窗口长度**""") if False else None
rep("""        plan = (freqs, _rg(20, 160), _rg(160, 2000), _rg(2000, 12000),
                edges, max(1, int(self.sr // BLOCK)))""",
    """        # ⚠ kick 频带原来是 `(freqs >= 20) & (freqs <= 200)` —— **右端是闭区间**，
        #   所以这里用 searchsorted "right" 才不会漏掉正好落在 200Hz 的那个 bin。
        rg_kick = (int(np.searchsorted(freqs, 20, "left")),
                   int(np.searchsorted(freqs, 200, "right")))
        plan = (freqs, _rg(20, 160), _rg(160, 2000), _rg(2000, 12000),
                edges, max(1, int(self.sr // BLOCK)), rg_kick)""")

# 5) 返回值的文档同步
rep('''        采样率在一次采集里是固定的 ⇒ 只算一次，按 sr 缓存（换设备/换采样率自动重算）。''',
    '''        返回 (freqs, 低/中/高频区间, 24 段边界, BPM 判定周期, kick 频带区间)。

        采样率在一次采集里是固定的 ⇒ 只算一次，按 sr 缓存（换设备/换采样率自动重算）。''')


def main():
    s = io.open(P, encoding="utf-8").read()
    for i, (old, new, cnt) in enumerate(PAT, 1):
        n = s.count(old)
        if n != cnt:
            print("✗ 第 %d 处命中 %d 次（应为 %d）：%s" % (i, n, cnt, old.strip()[:70]))
            return 1
    for old, new, cnt in PAT:
        s = s.replace(old, new)
    io.open(P, "w", encoding="utf-8", newline="\n").write(s)
    print("✓ audio_engine.py 已修")
    import py_compile
    py_compile.compile(P, doraise=True)
    print("语法 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
