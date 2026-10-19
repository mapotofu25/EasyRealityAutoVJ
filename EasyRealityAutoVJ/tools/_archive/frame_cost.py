# -*- coding: utf-8 -*-
r"""单帧耗时拆解：当前（纯 CPU）渲染管线到底把钱花在哪。

为什么要它：讨论「硬解 / GPU 渲染」之前必须先有数据 —— 否则不知道
该把哪一段搬到 GPU、能省多少。这个脚本量三块：

  A) 引擎一整帧（合成 + 颜色 + 后处理 + 输出派发）—— cProfile 看占比
  B) 输出侧独立基准：画布→目标分辨率的缩放绘制、颜色 LUT、帧拷贝
  C) 解码侧：从素材取一帧的等待时间（解码线程是否跟得上）

用法：venv\Scripts\python.exe tools\frame_cost.py [帧数]
输出：P:\AutoVJ\_frame_cost.txt
"""
import cProfile
import io
import os
import pstats
import shutil
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np  # noqa: E402
from PySide6.QtCore import QRectF  # noqa: E402
from PySide6.QtGui import QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication([])

out = []


def w(s=""):
    out.append(str(s))
    print(s, flush=True)


def ms(fn, n=1):
    t = time.perf_counter()
    for _ in range(n):
        fn()
    return (time.perf_counter() - t) * 1000.0 / n


def main():
    frames = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    w("=" * 78)
    w("单帧耗时拆解   %s" % time.strftime("%H:%M:%S"))
    w("=" * 78)

    # 用 exe 的真实配置（含用户图层/后处理/颜色设置）；跑完删掉，不留痕
    src_cfg = os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ", "config.json")
    tmp_cfg = os.path.join(ROOT, "config.json")
    made_tmp = False
    if os.path.exists(src_cfg) and not os.path.exists(tmp_cfg):
        shutil.copy2(src_cfg, tmp_cfg)
        made_tmp = True
    try:
        import config as cfgmod
        from audio_engine import AudioState
        from engine import AutoVJEngine
        from media_manager import MediaItem

        cfg = cfgmod.Config()
        lib = {}
        for p in cfg["library"]:
            try:
                lib[p] = MediaItem(p)
            except Exception:
                pass
        w("素材库 %d 个（配置里登记）" % len(lib))
        audio = AudioState()
        eng = AutoVJEngine(cfg, audio, lib)
        eng.rebuild_from_cfg()
        snaps = dict(audio.snapshot())
        snaps.update({"energy": 0.55, "beat_active": True, "bpm": 128.0,
                      "beat_count": 100, "beat_phase": 0.0, "energy_trend": 0.0})
        eng.running = True

        # 让每个图层的解码器真正活跃，并预热（模拟开始 VJ 后的稳定状态）
        nlay = 0
        for lay in eng.layers:
            if lay.player is not None:
                try:
                    lay.player.set_active(True)
                except Exception:
                    pass
                nlay += 1
        w("活动图层 %d 个" % nlay)
        for _ in range(30):
            eng._tick()
        w("预热 30 帧完成")
        w("")

        # ---------- A) 引擎整帧（手动计时 + cProfile 占比）----------
        w("[A] 引擎单帧（含合成/颜色/后处理/输出派发）")
        if nlay == 0:
            w("    没有活动图层（配置里没素材）→ 跳过；请先导入素材并建图层")
        t0 = time.perf_counter()
        for _ in range(frames):
            eng._tick()
        total_ms = (time.perf_counter() - t0) * 1000.0 / frames
        w("    单帧合计 %.2f ms  →  理论上限 %.0f fps" % (total_ms, 1000.0 / max(total_ms, 1e-6)))

        pr = cProfile.Profile()
        pr.enable()
        for _ in range(frames):
            eng._tick()
        pr.disable()
        s = io.StringIO()
        ps = pstats.Stats(pr, stream=s)
        ps.sort_stats("tottime").print_stats(22)
        w("")
        w("    -- 自身耗时 TOP（tottime，cProfile 有放大，看占比）--")
        for line in s.getvalue().splitlines():
            if "{" in line or ".py:" in line:
                w("    " + line)
        del ps

        # ---------- B) 输出侧独立基准 ----------
        w("")
        w("[B] 输出侧独立基准（1080p 画布）")
        canvas = QImage(1920, 1080, QImage.Format_RGB32)
        canvas.fill(0x203040)
        tgt = QImage(1920, 1080, QImage.Format_RGB32)
        tgt720 = QImage(1280, 720, QImage.Format_RGB32)

        def draw_same():
            p = QPainter(tgt720)
            p.drawImage(QRectF(0, 0, 1280, 720), canvas, QRectF(0, 0, 1920, 1080))
            p.end()
        w("    ① 画布→720p 缩放绘制(QPainter)      %6.2f ms" % ms(draw_same, 40))

        def draw_full():
            p = QPainter(tgt)
            p.drawImage(QRectF(0, 0, 1920, 1080), canvas, QRectF(0, 0, 1920, 1080))
            p.end()
        w("    ② 画布→1080p 原尺寸绘制             %6.2f ms" % ms(draw_full, 40))

        w("    ③ 帧拷贝 QImage.copy()              %6.2f ms"
          % ms(lambda: canvas.copy(), 40))

        buf = np.frombuffer(canvas.bits(), dtype=np.uint8, count=canvas.bytesPerLine() * 1080)

        def lut():
            import cv2
            table = np.arange(256, dtype=np.uint8)
            cv2.LUT(buf.reshape(1080, canvas.bytesPerLine()), table)
        try:
            w("    ④ 颜色 LUT(cv2.LUT, 1080p)          %6.2f ms" % ms(lut, 30))
        except Exception as e:  # noqa
            w("    ④ cv2 不可用: %s" % str(e)[:60])

        # Spout 上传（若可用）
        try:
            from SpoutGL import SpoutSender
            from SpoutGL.enums import GL_BGRA_EXT
            sp = SpoutSender()
            sp.setSenderName("AutoVJ_CostProbe")
            sp.createOpenGL()
            b = np.frombuffer(canvas.bits(), dtype=np.uint8, count=canvas.bytesPerLine() * 1080)
            sp.sendImage(b, 1080, 1920, GL_BGRA_EXT, True, 0)   # 预热
            w("    ⑤ Spout sendImage(1080p, CPU 上传)   %6.2f ms"
              % ms(lambda: sp.sendImage(b, 1080, 1920, GL_BGRA_EXT, True, 0), 20))
            sp.releaseSender()
        except Exception as e:  # noqa
            w("    ⑤ Spout 不可用: %s" % str(e)[:60])

        # NDI 发送
        try:
            from ndi_out import get_ndi
            ndi = get_ndi("AutoVJ_CostProbe")
            if ndi._ensure(1920, 1080):     # ⚠ 必须给真实分辨率（发送端尺寸只能在 open 前定死）
                b = np.frombuffer(canvas.bits(), dtype=np.uint8, count=canvas.bytesPerLine() * 1080)
                ndi.send_video(b, 1920, 1080)
                w("    ⑥ NDI 发一帧(1080p, BGRA)            %6.2f ms"
                  % ms(lambda: ndi.send_video(b, 1920, 1080), 20))
                w("       NDI 连接数 = %s（0 = 没有接收端，这时开销最小）"
                  % getattr(ndi._sender, "get_num_connections", lambda: "?")())
                ndi.close()
            else:
                w("    ⑥ NDI 不可用")
        except Exception as e:  # noqa
            w("    ⑥ NDI 异常: %s" % str(e)[:80])

        # ---------- C) 解码侧 ----------
        w("")
        w("[C] 解码侧（从解码线程取最新帧）")
        got = 0
        t0 = time.perf_counter()
        for _ in range(60):
            for lay in eng.layers:
                if lay.player is not None and lay.player.current() is not None:
                    got += 1
        el = (time.perf_counter() - t0) * 1000.0
        w("    取帧 %d 次 / %.1f ms  → 单次 %.2f ms（取最新帧几乎不阻塞）" % (got, el, el / max(got, 1)))
        for lay in eng.layers[:3]:
            pl = lay.player
            if pl is not None:
                img = pl.current()
                w("    图层 %-8s 分辨率 %s  帧率上限 %s" %
                  (lay.name if hasattr(lay, "name") else "?", 
                   (img.width(), img.height()) if img is not None else "无帧",
                   getattr(pl, "fps", "?")))

        w("")
        w("=" * 78)
        w("结论")
        w("=" * 78)
        w("详见上面各项毫秒数：引擎单帧 %.2f ms 是「CPU 合成」这条路的成本，" % total_ms)
        w("其中【B】里 ①/② 是输出窗口每帧还要额外付的钱，⑤/⑥ 是 Spout/NDI 的额外拷贝成本。")
        w("搬到 GPU 的核心收益 = 干掉这几块 + 让 Spout 走纹理零拷贝。")
    finally:
        if made_tmp:
            try:
                os.remove(tmp_cfg)
            except Exception:
                pass
    open(r"P:\AutoVJ\_frame_cost.txt", "w", encoding="utf-8").write("\n".join(out))
    os._exit(0)


if __name__ == "__main__":
    main()
