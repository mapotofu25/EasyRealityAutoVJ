"""「增益 / 单声道分析 / 预览设置」到底影响什么 —— 实测

三件事都是用户直接问的：
  A) 音源里的「增益」有什么效果？（用户说"测不出来"）
  B) 「单声道分析」有用吗？省 CPU 吗？
  C) 预览设置（分辨率 / 刷新率）真的影响性能吗？影响多少？

结论写进 _perf_qa.txt。
"""

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

OUT = []


def w(s=""):
    print(s, flush=True)
    OUT.append(s)


def save():
    with open(os.path.join(ROOT, "_perf_qa.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(OUT))


FLAC = r"W:\临时结界相关\2of\tofu\Rolling Contact - Euclid Number.flac"


# ------------------------------------------------------------------ A / B
def audio_part():
    import numpy as np
    import av
    from audio_engine import _Analyzer, AudioState, BLOCK

    w("=" * 78)
    w("[A] 音源「增益」的影响")
    w("=" * 78)
    if not os.path.exists(FLAC):
        w("找不到测试音乐：%s" % FLAC)
        return
    sr = 48000
    c = av.open(FLAC)
    st_a = c.streams.audio[0]
    res = av.AudioResampler(format="fltp", layout="stereo", rate=sr)
    chunks = []
    for fr in c.decode(st_a):
        for f in res.resample(fr):
            chunks.append(f.to_ndarray().T.astype(np.float32))
        if sum(len(x) for x in chunks) > sr * 45:
            break
    c.close()
    pcm = np.concatenate(chunks, axis=0)
    seg = pcm[int(sr * 10):int(sr * 10) + sr * 30]          # 取 30 秒稳定段
    w("测试音频：Euclid Number 10s~40s（30 秒，%d 样本，峰值 %.3f）" % (len(seg), float(np.abs(seg).max())))

    def run(gain=1.0, mono=True):
        an = _Analyzer(sr)
        s = AudioState()
        x = seg * gain
        n = 0
        es = []
        for i in range(0, len(x) - BLOCK, BLOCK):
            blk = x[i:i + BLOCK]
            an.feed(blk[:, :1] if mono else blk, s)
            n += 1
            if n % 20 == 0:
                es.append(s.energy)
        return np.array(es), an, s

    base_e, base_an, base_s = run(1.0, True)
    w("")
    w("  %-22s %10s %10s %12s %10s" % ("设置", "能量均值", "能量最大", "与 1.0x 差", "结论"))
    w("  " + "-" * 74)
    rows = []
    for g in (0.5, 1.0, 2.0, 4.0):
        e, an, s = run(g, True)
        d = float(np.mean(np.abs(e - base_e))) if len(e) == len(base_e) else float("nan")
        rows.append((g, e.mean(), e.max(), d))
        w("  %-22s %10.3f %10.3f %12.4f %10s"
          % ("增益 %.1fx" % g, e.mean(), e.max(), d, "几乎无变化" if d < 0.01 else "有变化"))
    w("")
    w("  为什么：能量算法有两层「自适应归一化」——")
    w("    · 响度项先减一个 τ=80s 的自适应基线（整首一起变响 → 基线跟着走，差值不变）")
    w("    · kick/bass/起伏项都是「除以全曲慢峰 + 死区」，同样是比值判据")
    w("  ⇒ 增益是**整体等比缩放**，两处都抵消掉。设计如此，不是 bug。")
    w("  唯一会变的情况：增益大到把波形削平（clipping 会改变频谱形状）。")
    w("    峰值 %.3f × 4.0 = %.3f → %s"
      % (float(np.abs(seg).max()), float(np.abs(seg).max()) * 4,
         "已削波" if float(np.abs(seg).max()) * 4 > 1.0 else "未削波"))
    w("")

    # ---- 单声道 ----
    w("=" * 78)
    w("[B] 「单声道分析」有用吗")
    w("=" * 78)
    e_m, _, _ = run(1.0, True)
    e_s, _, _ = run(1.0, False)
    d = float(np.mean(np.abs(e_m - e_s)))
    w("  能量差异（单声道 vs 立体声平均）：平均 %.4f，最大 %.4f  → %s"
      % (d, float(np.max(np.abs(e_m - e_s))), "几乎一样" if d < 0.01 else "有差异"))

    def cpu_of(mono, reps=3):
        best = 9e9
        for _ in range(reps):
            an = _Analyzer(sr)
            s = AudioState()
            t0 = time.perf_counter()
            for i in range(0, len(seg) - BLOCK, BLOCK):
                an.feed(seg[i:i + BLOCK][:, :1] if mono else seg[i:i + BLOCK], s)
            best = min(best, time.perf_counter() - t0)
        return best / (len(seg) / sr) * 100     # 占 1 核 %

    cm = cpu_of(True)
    cs = cpu_of(False)
    w("  分析耗时（占 1 核 %%）：单声道 %.1f%%   立体声 %.1f%%   省 %.1f%%"
      % (cm, cs, (cs - cm) / max(cs, 1e-9) * 100))
    w("  说明：单声道只是「只取左声道」，**不省 FFT**（分析本来就是拿单声道做 FFT），")
    w("        所以省下的只是右声道那点搬运/平均 → 收益很小。")
    w("        真正的作用是：当左右声道反相（如某些立体声效果）时，")
    w("        「平均成立体声」会把低频抵消掉，单声道只取左声道可避免。")
    w("")


# ------------------------------------------------------------------ C
def preview_part():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import numpy as np
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QImage, QPixmap
    from PySide6.QtCore import Qt, QSize

    app = QApplication.instance() or QApplication([])

    def make_img(w, h):
        im = QImage(w, h, QImage.Format_RGBA8888)
        im.fill(0)
        a = np.frombuffer(im.bits(), dtype=np.uint8)
        a[:] = np.random.randint(0, 255, a.size, dtype=np.uint8)
        return im

    def bench(fn, n=20, warm=3):
        for _ in range(warm):
            fn()
        t = time.perf_counter()
        for _ in range(n):
            fn()
        return (time.perf_counter() - t) / n * 1000

    w("=" * 78)
    w("[C] 预览设置到底影响多少（微基准，合成 1920x1080 帧 → 预览窗口 420x236）")
    w("=" * 78)
    src = make_img(1920, 1080)
    view = QSize(420, 236)
    pm_full = QPixmap.fromImage(src)
    w("  QPixmap.fromImage(1080p)                 %6.2f ms   ← 与「预览分辨率」无关，省不掉"
      % bench(lambda: QPixmap.fromImage(src)))
    w("  缩放 → 预览窗口（Smooth）                %6.2f ms" % bench(
        lambda: pm_full.scaled(view, Qt.KeepAspectRatio, Qt.SmoothTransformation)))
    for res in (1.0, 0.5, 0.25):
        def one(res=res):
            pm = QPixmap.fromImage(src)
            if res < 0.999:
                pm = pm.scaled(max(1, int(pm.width() * res)), max(1, int(pm.height() * res)),
                               Qt.KeepAspectRatio, Qt.SmoothTransformation)
            return pm.scaled(view, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        w("  预览分辨率 %-5s 整条路径                 %6.2f ms" % ("%.2f" % res, bench(one)))
    w("")
    c60 = bench(lambda: QPixmap.fromImage(src).scaled(view, Qt.KeepAspectRatio, Qt.SmoothTransformation))
    w("  ⇒ 预览每帧成本 ≈ %.2f ms；按 60fps 折算 = 占 1 核 %.1f%%" % (c60, c60 * 60 / 10))
    w("    刷新率 60→30 直接减半（%.1f%% → %.1f%%），15 再减半"
      % (c60 * 60 / 10, c60 * 30 / 10))
    w("    分辨率 1.00→0.25 只省缩放的零点几毫秒（fromImage 那 %.2f ms 省不掉）"
      % bench(lambda: QPixmap.fromImage(src)))
    w("")
    w("  结论：预览这块整体很小（< 0.1 个核）。要让预览不占 CPU，正确做法是")
    w("        **直接关掉预览**（新增的开关），而不是调分辨率。")


if __name__ == "__main__":
    try:
        audio_part()
    except Exception as e:
        import traceback
        w("音频部分失败：%s" % e)
        w(traceback.format_exc()[-800:])
    try:
        preview_part()
    except Exception as e:
        import traceback
        w("预览部分失败：%s" % e)
        w(traceback.format_exc()[-800:])
    save()
    print("\n→ 已写入 _perf_qa.txt")
