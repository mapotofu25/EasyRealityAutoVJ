# -*- coding: utf-8 -*-
"""PyInstaller `--runtime-hook`：**仅在 `ERCUE_SMOKE=1` 时激活**的冻结包自检钩子。

产品是 `--windowed`（无控制台），而 `tools/` 无权改 `src/ercue/main.py`。
运行时钩子在**主脚本之前**执行，因此可以在这里给 `MainWindow.show` 打一个惰性补丁：
产品正常启动（未设 ERCUE_SMOKE）时本文件只是被解释一遍、什么都不做；设了
ERCUE_SMOKE=1 时，窗口建好后跑自检，把 JSON 写到 `ERCUE_SMOKE_OUT`，然后退出。

自检内容：
  1. import 依赖（numpy / av / mutagen / sounddevice / PySide6）
  2. 确认主窗口（含时间轴 / 预览 / 音频）已建出
  3. 用真实 `render.compositor` 合成一帧并读回中心像素
  4. ★ 真解码（需设 `ERCUE_SMOKE_MEDIA=<测试素材目录>`，里面要有 `smoke_video.mp4`
     与 `smoke_audio.wav`）——走**产品自己的解码代码**：
       · 视频：`media.probe.probe()` + `media.frames.CueFrameSource.frame_exact()` 真取帧
       · 音频：`audio.player.AudioTrack()` 真解码 + 重采样到 48k/立体声
     断言尺寸/采样率，并把结果写进自检 JSON。
     ⚠ 测试素材**不被打进产品包**，路径由环境变量传入。

隔离用环境变量（诊断退出阶段 abort 用，默认不设即产品正常行为）：
  ERCUE_SMOKE_SKIP=av,sounddevice   逗号分隔，跳过对这些模块的 import
  ERCUE_SMOKE_STAGE=run|close|hard  run=自检后 app.exit(0)（默认）
                                    close=自检后 win.close()（等价用户关窗，走正常退出）
                                    hard =自检后 os._exit(0)（硬退出，不跑解释器/Qt 收尾）

⚠ 本文件被打进产品 exe；正常启动路径上**没有任何副作用**（全部逻辑在 `if` 内）。
"""
import os

if os.environ.get("ERCUE_SMOKE") == "1":
    _OUT = os.environ.get("ERCUE_SMOKE_OUT", "")
    _SKIP = {s.strip() for s in os.environ.get("ERCUE_SMOKE_SKIP", "").split(",") if s.strip()}
    _STAGE = os.environ.get("ERCUE_SMOKE_STAGE", "run")
    _MEDIA = os.environ.get("ERCUE_SMOKE_MEDIA", "")
    _DXV = os.environ.get("ERCUE_SMOKE_DXV", "")

    def _dump(payload):
        try:
            import json
            txt = json.dumps(payload, ensure_ascii=False, default=str)
        except Exception:                                    # noqa: BLE001
            txt = '{"ok": false, "error": "json dump failed"}'
        if _OUT:
            try:
                with open(_OUT, "w", encoding="utf-8") as f:
                    f.write(txt)
            except Exception:                                # noqa: BLE001
                pass

    def _decode_test(media_dir):
        """真解码：走产品自己的 probe / CueFrameSource / AudioTrack 代码路径。"""
        import os as _os
        out = {"ok": False}
        vpath = _os.path.join(media_dir, "smoke_video.mp4")
        apath = _os.path.join(media_dir, "smoke_audio.wav")
        out["video_exists"] = _os.path.isfile(vpath)
        out["audio_exists"] = _os.path.isfile(apath)
        if not (out["video_exists"] and out["audio_exists"]):
            out["error"] = "测试素材不存在：%s" % media_dir
            return out
        try:
            from ercue.media.probe import probe
            from ercue.media.frames import CueFrameSource
            from ercue.audio.player import AudioTrack

            # ---- 视频：探测 + 真取帧（产品解码路径）----
            pr = probe(vpath)
            out["video_probe"] = {"ok": pr.ok, "w": pr.width, "h": pr.height,
                                  "fps": round(pr.fps, 2), "codec": pr.codec,
                                  "pix_fmt": pr.pix_fmt, "error": pr.error}
            src = CueFrameSource()
            a0 = src.frame_exact(vpath, 0.0)
            a5 = src.frame_exact(vpath, 0.5)
            src.close()
            out["video_frame_shape"] = list(a0.shape) if a0 is not None else None
            out["video_frames_differ"] = bool(a0 is not None and a5 is not None
                                              and not (a0 == a5).all())

            # ---- 音频：整首解码 + 重采样到 48k/立体声（产品解码路径）----
            t = AudioTrack(apath)
            out["audio"] = {"ok": t.ok, "samplerate": t.samplerate,
                            "channels": t.channels, "frames": t.frames,
                            "duration_sec": round(t.duration_sec, 3), "error": t.error}

            ok = (pr.ok and pr.width == 320 and pr.height == 180
                  and out["video_frame_shape"] == [180, 320, 4]
                  and out["video_frames_differ"]
                  and t.ok and t.samplerate == 48000 and t.channels == 2 and t.frames > 0)
            out["ok"] = bool(ok)
            if not ok:
                out["error"] = "解码结果与预期不符（视 320x180 / 帧 (180,320,4) / 音频 48k 立体声）"
        except Exception as e:                               # noqa: BLE001
            import traceback
            out["error"] = "%s: %s" % (type(e).__name__, e)
            out["tb"] = traceback.format_exc()
        return out

    def _dxv_test(lib_dir):
        """★ 真实 DXV 素材测试（需设 `ERCUE_SMOKE_DXV=<素材库目录>`）。

        为什么单独测这一路：**用户的素材库主体就是 DXV `.mov`**，而 DXV 有三个已知坑，
        此前只在源码层验证过，**从没在冻结包里用真实素材跑过**：
          1. **alpha 两层判定** —— DXV 解码器一律输出 `rgba`，只看 `pix_fmt` 会**大量误判**
             （实测 120 个真实 DXV：pix_fmt 判 120 个带 alpha，实解只有 8 个真带）
          2. **`metadata_errors="replace"`** —— 库里大量 GBK 元数据的 `.mov`，不加会抛 `UnicodeDecodeError`
          3. **`seek(backward=True)`** —— DXV 的随机访问
        素材库**只读**，绝不修改。
        """
        import os as _os
        import random
        import time
        out = {"ok": False, "lib": lib_dir}
        if not _os.path.isdir(lib_dir):
            out["error"] = "素材库目录不存在"
            return out
        movs = sorted(f for f in _os.listdir(lib_dir) if f.lower().endswith(".mov"))
        out["total_mov"] = len(movs)
        if not movs:
            out["error"] = "素材库里没有 .mov"
            return out

        from ercue.media.frames import CueFrameSource
        from ercue.media.probe import probe
        from ercue.media.thumbs import make_thumb

        random.seed(20261001)
        sample = random.sample(movs, min(20, len(movs)))
        t0 = time.perf_counter()
        ok_n, errs = 0, []
        alpha_names = []
        for name in sample:
            p = _os.path.join(lib_dir, name)
            try:
                pr = probe(p)
                if getattr(pr, "error", ""):
                    errs.append("%s: %s" % (name, pr.error))
                else:
                    ok_n += 1
                if getattr(pr, "has_alpha", False) and len(alpha_names) < 3:
                    alpha_names.append(name)
            except Exception as e:                           # noqa: BLE001
                errs.append("%s: %s: %s" % (name, type(e).__name__, e))

        # 真带 alpha 的素材在全库里只有约 6.7%（实测 120 个里 8 个），抽样 20 个经常一个都抽不到。
        # 凑不够 3 个就继续往下扫（封顶 80 个，避免冒烟太慢）。
        if len(alpha_names) < 3:
            for name in movs[:80]:
                if len(alpha_names) >= 3:
                    break
                if name in sample:
                    continue
                try:
                    if getattr(probe(_os.path.join(lib_dir, name)), "has_alpha", False):
                        alpha_names.append(name)
                except Exception:                            # noqa: BLE001
                    pass
        alpha_n = len(alpha_names)
        out["scanned"] = len(sample)
        out["probe_ok"] = ok_n
        out["real_alpha"] = alpha_n
        out["alpha_samples"] = alpha_names
        out["errors"] = errs[:8]

        # ---- alpha 素材：缩略图必须真的保留透明通道 ----
        alpha_png_ok, alpha_png_checked, alpha_min_seen = 0, 0, []
        try:
            import av as _av
            import io as _io
            for name in alpha_names[:3]:
                p = _os.path.join(lib_dir, name)
                blob = make_thumb(p)
                if not blob:
                    continue
                alpha_png_checked += 1
                with _av.open(_io.BytesIO(blob)) as c:
                    fr = next(c.decode(video=0))
                    arr = fr.to_ndarray(format="rgba")
                amin = int(arr[:, :, 3].min())
                alpha_min_seen.append(amin)
                if amin < 255:                               # 真带透明
                    alpha_png_ok += 1
        except Exception as e:                               # noqa: BLE001
            out["alpha_png_error"] = "%s: %s" % (type(e).__name__, e)
        out["alpha_png_checked"] = alpha_png_checked
        out["alpha_png_ok"] = alpha_png_ok
        out["alpha_min_seen"] = alpha_min_seen

        # ---- 随机访问：同一素材取两帧，内容必须不同 ----
        seek_ok = False
        try:
            sp = _os.path.join(lib_dir, sample[0])
            src = CueFrameSource()
            f0 = src.frame_exact(sp, 0.0)
            f1 = src.frame_exact(sp, 1.0)
            src.close()
            seek_ok = bool(f0 is not None and f1 is not None
                           and f0.shape == f1.shape and not (f0 == f1).all())
            out["seek_shape"] = list(f0.shape) if f0 is not None else None
        except Exception as e:                               # noqa: BLE001
            out["seek_error"] = "%s: %s" % (type(e).__name__, e)
        out["seek_frames_differ"] = seek_ok
        out["elapsed_sec"] = round(time.perf_counter() - t0, 2)

        # 判据：抽样全部 probe 成功（不抛 UnicodeDecodeError）+ 至少命中一个真 alpha +
        #       其缩略图确实保留透明 + 随机访问两帧不同
        out["ok"] = bool(ok_n == len(sample) and alpha_n >= 1
                         and alpha_png_ok >= 1 and seek_ok)
        return out

    def _run_smoke(win):
        import types
        info = {"ok": True, "stage": _STAGE, "skipped": sorted(_SKIP)}
        try:
            import numpy as np
            import PySide6
            info["pyside6"] = PySide6.__version__
            info["numpy"] = np.__version__
            if "av" not in _SKIP:
                import av
                info["av"] = av.__version__
            if "mutagen" not in _SKIP:
                import mutagen
                info["mutagen"] = mutagen.version_string
            if "sounddevice" not in _SKIP:
                import sounddevice
                info["sounddevice"] = getattr(sounddevice, "__version__", "?")

            info["main_window"] = win.__class__.__name__
            info["has_timeline"] = bool(getattr(win, "timeline", None))
            info["has_preview"] = bool(getattr(win, "preview", None))
            info["has_audio"] = bool(getattr(win, "audio", None))

            # 构建版本号是否真的被固化进包（window._app_version → _build_ver.BUILD_VER）
            try:
                from ercue.ui.window import APP_VERSION as _APPVER
                info["app_version"] = _APPVER
            except Exception as _e:                          # noqa: BLE001
                info["app_version"] = "ERR: %s" % _e
            try:
                from PySide6.QtWidgets import QApplication as _QApp
                _a = _QApp.instance()
                info["qt_application_version"] = (_a.applicationVersion() if _a else "")
            except Exception as _e:                          # noqa: BLE001
                info["qt_application_version"] = "ERR: %s" % _e

            from ercue.render import compositor
            w, h = 160, 90
            frame = np.empty((h, w, 4), np.uint8)
            frame[:, :, 0] = 60         # B
            frame[:, :, 1] = 120        # G
            frame[:, :, 2] = 200        # R
            frame[:, :, 3] = 255        # A
            st = types.SimpleNamespace(
                active=True, missing=False, opacity=1.0, alpha=1.0,
                clip_id="c1", play_sec=0.0, blend="screen",
                trans="cut", trans_p=1.0, xform={}, crop=None)
            img = compositor.compose((w, h), [st], lambda cid, ps: frame)
            px = img.pixelColor(w // 2, h // 2)
            info["compose_size"] = [img.width(), img.height()]
            info["compose_px_rgba"] = [px.red(), px.green(), px.blue(), px.alpha()]

            # ---- 4) 真解码（产品自己的解码代码，素材由环境变量传入）----
            if _MEDIA:
                info["decode"] = _decode_test(_MEDIA)
                if not info["decode"].get("ok"):
                    info["ok"] = False
            else:
                info["decode"] = {"skipped": "未设 ERCUE_SMOKE_MEDIA"}

            # ---- 5) ★ 真实 DXV 素材（用户的素材库主体就是它）----
            if _DXV:
                info["dxv"] = _dxv_test(_DXV)
                if not info["dxv"].get("ok"):
                    info["ok"] = False
            else:
                info["dxv"] = {"skipped": "未设 ERCUE_SMOKE_DXV"}

            # ---- 6) ★ 波形数据层（`ercue.audio.waveform`）----
            #   为什么要单独验：它是**惰性 import**（在 timeline_widget 的函数内
            #   `from ercue.audio import waveform`），PyInstaller 静态分析容易漏 ⇒
            #   漏掉的包"能启动、但时间轴画不出真实波形"，只有用户用时才发现。
            #   这里自造 1 秒 440 Hz 正弦 ⇒ 走它自己的解码链路（av + numpy）⇒
            #   验证模块在包里 + 时长正确。
            try:
                from ercue.audio import waveform as _wf
                import tempfile as _tf
                import wave as _wave
                _sr = 16000
                _n = _sr                                  # 1 秒
                _t = np.arange(_n) / float(_sr)
                _sig = (np.sin(2.0 * 3.141592653589793 * 440.0 * _t)
                        * 0.5 * 32767).astype(np.int16)
                _fd, _p = _tf.mkstemp(suffix=".wav")
                os.close(_fd)
                try:
                    with _wave.open(_p, "wb") as _w:
                        _w.setnchannels(1)
                        _w.setsampwidth(2)
                        _w.setframerate(_sr)
                        _w.writeframes(_sig.tobytes())
                    _d = _wf.compute(_p)
                    _ok = bool(_d is not None and abs(float(_d.duration_sec) - 1.0) < 0.15)
                    info["waveform"] = {
                        "ok": _ok,
                        "duration": (round(float(_d.duration_sec), 3) if _d is not None else None),
                        "columns": (int(_d.columns) if _d is not None else None),
                    }
                    if not _ok:
                        info["ok"] = False
                finally:
                    try:
                        os.unlink(_p)
                    except OSError:
                        pass
            except Exception as _e:                          # noqa: BLE001
                info["waveform"] = {"ok": False,
                                    "error": "%s: %s" % (type(_e).__name__, _e)}
                info["ok"] = False
        except Exception as e:                               # noqa: BLE001
            import traceback
            info = {"ok": False, "stage": _STAGE,
                    "error": "%s: %s" % (type(e).__name__, e),
                    "tb": traceback.format_exc()}
        _dump(info)

        if _STAGE == "hard":
            os._exit(0 if info.get("ok") else 4)
        if _STAGE == "close":
            try:
                win.close()          # 走 closeEvent → shutdown → 事件循环自然退出
            except Exception:                                # noqa: BLE001
                pass
            return
        try:
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is not None:
                app.exit(0 if info.get("ok") else 4)
        except Exception:                                    # noqa: BLE001
            pass

    try:
        from PySide6.QtCore import QTimer
        from ercue.ui import window as _win

        _orig_show = _win.MainWindow.show

        def _show(self):
            _orig_show(self)
            QTimer.singleShot(0, lambda: _run_smoke(self))

        _win.MainWindow.show = _show
    except Exception as e:                                   # noqa: BLE001
        import traceback
        _dump({"ok": False, "stage": "hook",
               "error": "%s: %s" % (type(e).__name__, e),
               "tb": traceback.format_exc()})
