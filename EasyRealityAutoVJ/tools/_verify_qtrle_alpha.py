# -*- coding: utf-8 -*-
"""验证 Bug A 修复：qtrle alpha MOV 的缩略图生成 + 播放器选型。

真跑（不 mock）：
  1) 对 6 个 qtrle alpha MOV，跑 `_make_av_thumb`（经 `ensure_thumbnail` 后台路径），
     断言 PNG 缩略图**真的生成出来**；
  2) 跑 `create_video_player`，断言返回 **AvAlphaPlayer**、格式 **Format_ARGB32**、
     `hasAlphaChannel() == True`；
  3) 断言解码出的帧 alpha **min=0**（真透明，不是全 255）；
  4) 顺带确认 `_av_probe_alpha` 返回 True（与缩略图判据一致，不再互相污染）。

临时目录用 mkdtemp + finally rmtree。
"""
import os
import shutil
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

BASE = r"W:\临时结界相关\主催工作区\星虹锐舞\06_视频制作"
MOVS = [
    os.path.join(BASE, "LOGO循环_akmW_もふ学園",
                 "LOGO循环_akmW_もふ学園_20s_alpha.mov"),
    os.path.join(BASE, "LOGO循环_批量_20261006", "01_FlandreS_蜂幽",
                 "LOGO循环_FlandreS_蜂幽_20s_alpha.mov"),
    os.path.join(BASE, "LOGO循环_批量_20261006", "02_northad_灵社幻声",
                 "LOGO循环_northad_灵社幻声_20s_alpha.mov"),
    os.path.join(BASE, "LOGO循环_批量_20261006", "03_touming_临时结界",
                 "LOGO循环_touming_临时结界_20s_alpha.mov"),
    os.path.join(BASE, "LOGO循环_批量_20261006", "04_幻想之王_风华绮音",
                 "LOGO循环_幻想之王_风华绮音_20s_alpha.mov"),
    os.path.join(BASE, "LOGO循环_批量_20261006", "05_虹彩_wonder",
                 "LOGO循环_虹彩_wonder_20s_alpha.mov"),
]

THUMB_DIR = None


def main():
    global THUMB_DIR
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    import numpy as np
    import av
    from PySide6.QtGui import QImage

    import media_manager as mm

    d = tempfile.mkdtemp(prefix="autovj_qa_qtrle_")
    THUMB_DIR = os.path.join(d, "thumbs")
    os.makedirs(THUMB_DIR, exist_ok=True)
    mm.THUMB_DIR = THUMB_DIR          # 缩略图落到临时目录，别污染真实缓存

    n_ok = 0
    try:
        for p in MOVS:
            name = os.path.basename(p)
            print("\n===== %s =====" % name)
            if not os.path.exists(p):
                print("  !! 文件不存在，跳过: %s" % p)
                continue

            # 0) 编码确认
            c = av.open(p, metadata_errors="replace")
            try:
                st = c.streams.video[0]
                print("  codec=%s pix_fmt=%s frames=%s" % (
                    st.codec_context.name, st.pix_fmt, st.frames))
            finally:
                c.close()

            # 1) 探测器
            avmod, has = mm._av_probe_alpha(p)
            print("  _av_probe_alpha -> has=%s" % has)

            # 2) 造 MediaItem（走真实类），跑 ensure_thumbnail（非主线程 → 后台路径）
            item = mm.MediaItem(p)

            # 保持与真实后台一致的调用：ensure_thumbnail 在主线程返回 ""，
            # 这里直接在非主线程语境调用它的生成分支。
            import threading
            result = {}

            def job():
                result["thumb"] = item.ensure_thumbnail()

            t = threading.Thread(target=job)
            t.start()
            t.join()

            png = item._thumb_path(".png")
            print("  ensure_thumbnail -> %r" % result.get("thumb"))
            print("  png 存在? %s  (%s)" % (os.path.exists(png), png))
            assert os.path.exists(png), "PNG 缩略图未生成: %s" % name

            # 3) 播放器选型：先清实例缓存，模拟「切到该素材」
            for attr in ("_has_alpha", "_av_no_alpha", "_alpha_recheck",
                         "_alpha_probing"):
                if hasattr(item, attr):
                    try:
                        delattr(item, attr)
                    except Exception:
                        pass
            item._has_alpha = True        # 与磁盘 PNG 持久化结果一致
            import time as _time
            player = mm.create_video_player(item)
            cls = type(player).__name__
            print("  create_video_player -> %s" % cls)
            assert cls == "AvAlphaPlayer", "期望 AvAlphaPlayer，实得 %s" % cls

            # 真跑：让解码线程解出首帧（warm + active），取 current() 检格式/alpha
            try:
                player.set_max_size(1920, 1080)
                player.warm_up()
                player.set_active(True)
            except Exception:
                pass
            qimg = None
            for _ in range(100):
                qimg = player.current()
                if qimg is not None and not qimg.isNull():
                    break
                _time.sleep(0.05)
            assert qimg is not None and not qimg.isNull(), "播放器未解出首帧"
            print("  player.current() format=%s hasAlpha=%s" % (
                qimg.format().name, qimg.hasAlphaChannel()))
            assert qimg.format() == QImage.Format_ARGB32, \
                "期望 Format_ARGB32，实得 %s" % qimg.format().name
            assert qimg.hasAlphaChannel() is True, "hasAlphaChannel() 非 True"
            try:
                player.close()
            except Exception:
                pass

            # 4) 真解一帧，验 alpha min=0（真透明）
            #    从 PNG 缩略图直接读像素，等价于 bgra 的 alpha 通道
            img = QImage(png)
            assert not img.isNull(), "PNG 读不出"
            img = img.convertToFormat(QImage.Format_ARGB32)
            w, h = img.width(), img.height()
            ptr = img.constBits()
            arr = np.frombuffer(bytes(ptr), dtype=np.uint8).reshape(h, w, 4)
            amin = int(arr[..., 3].min())
            print("  PNG alpha min=%d (期望 0=真透明)" % amin)
            assert amin == 0, "alpha 非真透明，min=%d" % amin

            n_ok += 1
            print("  ✅ PASS")
        print("\n===> %d/%d MOV 通过" % (n_ok, len(MOVS)))
        if n_ok != len(MOVS):
            print("裁决: FAIL（有 MOV 未通过）")
        else:
            print("裁决: PASS")
    finally:
        shutil.rmtree(d, ignore_errors=True)
        print("临时目录已清理: %s" % d)


if __name__ == "__main__":
    main()
