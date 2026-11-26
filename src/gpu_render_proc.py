# -*- coding: utf-8 -*-
"""独立 GPU 渲染进程。

职责：
  · 拥有唯一的 GL 上下文 / 原生窗口 / Spout 发送器；
  · 从共享内存读取引擎写好的图层帧；
  · 逐屏合成 1080p，主屏上屏，所有屏按需 Spout；
  · 把主屏缩成 480×270 写回共享内存供主界面预览。

本进程不启动 QApplication，只使用 QImage 作为纹理载体；窗口/事件由 GLFW 负责。
"""
from __future__ import annotations

import multiprocessing as _mp
import os
import struct
import time
import traceback
from multiprocessing import shared_memory

# 必须在 import PySide6 之前声明 DPI 感知：Qt 一旦初始化会把进程钉在
# DPI-unaware，GLFW 随后只能看到 1280x720（150% 缩放），原生窗缩在左上角。
try:
    import ctypes as _ct
    try:
        _ct.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:                                      # noqa: BLE001
        _ct.windll.user32.SetProcessDPIAware()
except Exception:                                          # noqa: BLE001
    pass

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

_PREVIEW_W, _PREVIEW_H = 480, 270


def _log(path, msg):
    try:
        line = "[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:                                          # noqa: BLE001
        pass


class _ShmImages:
    """共享内存 → QImage 包装缓存。静态图复用对象，视频帧按 version 新建。"""

    def __init__(self):
        self._shms = {}
        self._imgs = {}
        self._order = []

    def _shm(self, name, need):
        shm = self._shms.get(name)
        if shm is None or shm.size < need:
            if shm is not None:
                try:
                    shm.close()
                except Exception:                              # noqa: BLE001
                    pass
            shm = shared_memory.SharedMemory(name=name)
            self._shms[name] = shm
        return shm

    @staticmethod
    def _fmt(bgra, premul):
        if int(bgra):
            return (QImage.Format_ARGB32 if int(premul)
                    else QImage.Format_ARGB32_Premultiplied)
        return (QImage.Format_RGBA8888 if int(premul)
                else QImage.Format_RGBA8888_Premultiplied)

    def get(self, desc):
        if not desc:
            return None
        name = str(desc.get("buf") or "")
        # 纹理尺寸用 tex_w/tex_h（旧的 w/h 仅作兼容读取）；图层的 w/h 是
        # 画布目标尺寸，不能被这里的源图尺寸覆盖。
        w = int(desc.get("tex_w") or desc.get("w") or 0)
        h = int(desc.get("tex_h") or desc.get("h") or 0)
        ver = int(desc.get("version") or 0)
        if not name or w <= 0 or h <= 0:
            return None
        key = (name, w, h, ver)
        img = self._imgs.get(key)
        if img is not None:
            return img
        need = w * h * 4
        try:
            shm = self._shm(name, need)
            mv = memoryview(shm.buf)[:need]
            img = QImage(mv, w, h, w * 4,
                         self._fmt(desc.get("bgra"), desc.get("premul")))
            self._imgs[key] = img
            self._order.append(key)
            if len(self._order) > 96:
                for k in self._order[:32]:
                    self._imgs.pop(k, None)
                self._order = self._order[32:]
            return img
        except Exception as e:                                 # noqa: BLE001
            _log(os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ",
                              "gpu_render.log"),
                 "共享内存帧包装失败 %s：%s" % (name, e))
            return None

    def close(self):
        self._imgs.clear()
        self._order.clear()
        for shm in self._shms.values():
            try:
                shm.close()
            except Exception:                                  # noqa: BLE001
                pass
        self._shms.clear()


def _sink_write(sinks, desc, img):
    """把成品帧写进引擎侧共享内存（双缓冲 + seq 发布）。"""
    if not desc or img is None or img.isNull():
        return
    name = str(desc.get("name") or "")
    w, h = int(desc.get("w") or 0), int(desc.get("h") or 0)
    if not name or w <= 0 or h <= 0:
        return
    need = 32 + w * h * 4 * 2
    shm = sinks.get(name)
    try:
        if shm is None or shm.size < need:
            if shm is not None:
                shm.close()
            shm = shared_memory.SharedMemory(name=name)
            sinks[name] = shm
        seq, idx = struct.unpack_from("<qq", shm.buf, 0)
        nxt = 0 if int(idx) != 0 else 1
        off = 32 + nxt * w * h * 4
        bpl = int(img.bytesPerLine())
        need_row = w * 4
        if bpl == need_row:
            src = np.frombuffer(img.constBits(), dtype=np.uint8,
                                count=w * h * 4)
            arr = np.frombuffer(shm.buf, dtype=np.uint8,
                                count=w * h * 4, offset=off)
            np.copyto(arr, src)
        else:
            mv = memoryview(img.constBits())
            arr = np.frombuffer(shm.buf, dtype=np.uint8,
                                count=w * h * 4, offset=off)
            for y in range(h):
                s = y * bpl
                arr[y * need_row:(y + 1) * need_row] = np.frombuffer(
                    mv[s:s + need_row], dtype=np.uint8)
        struct.pack_into("<q", shm.buf, 8, nxt)
        struct.pack_into("<q", shm.buf, 0, int(seq) + 1)
    except Exception as e:                                     # noqa: BLE001
        _log(os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ",
                          "gpu_render.log"),
             "路由成品帧写入失败 %s：%s" % (name, e))


def _preview_write(shm, seq, img):
    if img is None or img.isNull():
        return
    try:
        small = img.scaled(_PREVIEW_W, _PREVIEW_H,
                           Qt.AspectRatioMode.IgnoreAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
        if small.format() != QImage.Format_ARGB32_Premultiplied:
            small = small.convertToFormat(QImage.Format_ARGB32_Premultiplied)
        raw = small.constBits()
        n = _PREVIEW_W * _PREVIEW_H * 4
        try:
            src = np.frombuffer(raw, dtype=np.uint8, count=n)
        except Exception:                                      # noqa: BLE001
            src = np.frombuffer(bytes(raw)[:n], dtype=np.uint8)
        arr = np.frombuffer(shm.buf, dtype=np.uint8, count=n)
        np.copyto(arr, src)
        with seq.get_lock():
            seq.value = int(seq.value) + 1
    except Exception:                                          # noqa: BLE001
        pass


def render_main(job_q, preview_name, preview_seq, log_path):
    """子进程入口。job_q 收到 {"screens":[...]}; None 退出。"""
    _log(log_path, "渲染进程启动 pid=%d" % os.getpid())
    preview = None
    images = _ShmImages()
    sinks = {}
    spouts = {}
    stage = None
    native_mon = None
    last_log = 0.0
    last_parent_check = 0.0
    logged_sink = set()
    logged_scene = set()
    try:
        preview = shared_memory.SharedMemory(name=preview_name)
        while True:
            _nowp = time.time()
            if _nowp - last_parent_check >= 1.0:
                last_parent_check = _nowp
                try:
                    if not _mp.parent_process().is_alive():
                        _log(log_path, "父进程已退出，渲染进程自行结束")
                        break
                except Exception:                          # noqa: BLE001
                    pass
            job = None
            try:
                job = job_q.get(timeout=0.05)
            except Exception:                                  # noqa: BLE001
                job = None
            if job is None:
                # 没有新任务：短暂停，避免空转
                if stage is None:
                    time.sleep(0.002)
                continue
            if job == "STOP":
                break
            screens = job.get("screens") or []
            if not screens:
                continue
            main = None
            for sc in screens:
                if sc.get("display"):
                    main = sc
                    break
            if main is None and screens:
                main = screens[0]
            # 首次建 stage：主屏若有 display 则开原生窗口；其余屏只 Spout
            if stage is None:
                from gpu_stage_mgl import SceneStage
                stage = SceneStage(log=lambda m: _log(log_path, m))
                disp = int(main.get("display_monitor") or 0) if main.get("display") else -1
                native_mon = disp
                ok = stage.ensure(int(main.get("w") or 1920),
                                  int(main.get("h") or 1080),
                                  native=(disp >= 0), monitor=disp,
                                  # 不置顶：否则单显示器下主界面被永久盖住，
                                  # 预览面板/设置都点不到；Alt+Tab 可回主界面。
                                  always_top=False,
                                  title=str(main.get("title") or
                                            "Easy Reality Output"))
                if not ok:
                    _log(log_path, "SceneStage 初始化失败，渲染进程退出")
                    break
                try:
                    import glfw as _glfw
                    _wsz = _glfw.get_window_size(stage._window) if stage._window else (0, 0)
                    _fsz = _glfw.get_framebuffer_size(stage._window) if stage._window else (0, 0)
                except Exception:                          # noqa: BLE001
                    _wsz = _fsz = (0, 0)
                _log(log_path, "SceneStage 就绪 native=%s monitor=%s %dx%d"
                     " window=%s fb=%s"
                     % (disp >= 0, disp, int(stage._w), int(stage._h),
                        _wsz, _fsz))
            # Spout/NDI 由**引擎进程**里已验证的 SpoutOut/NDIOutput 发送；
            # 本进程只把成品帧写进共享内存，避免在 GLFW 上下文旁再建 Spout GL。
            # 每屏进程只负责自己这一路：SpoutOut 线程和 GLFW 上下文各自独立。
            for sc in screens:
                sid = str(sc.get("sid") or "")
                nm = str(sc.get("spout") or "").strip()
                if nm:
                    sp = spouts.get(sid)
                    if sp is None or sp.name != nm:
                        if sp is not None:
                            try:
                                sp.close()
                            except Exception:                  # noqa: BLE001
                                pass
                        try:
                            from spout_out import SpoutOut
                            spouts[sid] = SpoutOut(nm)
                            _log(log_path, "SpoutOut 就绪 %s -> %s" % (sid, nm))
                        except Exception as e:                 # noqa: BLE001
                            _log(log_path, "SpoutOut 创建失败 %s：%s" % (nm, e))
            t_all = time.perf_counter()
            for sc in screens:
                sid = str(sc.get("sid") or "")
                layers = []
                for lay in (sc.get("layers") or []):
                    img = images.get(lay)
                    if img is None:
                        continue
                    d = dict(lay)
                    d["img"] = img
                    layers.append(d)
                lut = images.get(sc.get("lut")) if sc.get("lut") else None
                _sw, _sh = int(sc.get("w") or 1920), int(sc.get("h") or 1080)
                _ow, _oh = int(sc.get("out_w") or _sw), int(sc.get("out_h") or _sh)
                if sid not in logged_scene:
                    logged_scene.add(sid)
                    _log(log_path, "scene %s %dx%d -> %dx%d display=%s spout=%s"
                         % (sid, _sw, _sh, _ow, _oh, bool(sc.get("display")),
                            str(sc.get("spout") or "")))
                scene = {"sid": sid,
                         "w": _sw,
                         "h": _sh,
                         "out_w": _ow,
                         "out_h": _oh,
                         "alpha": bool(sc.get("alpha")),
                         "flash": int(sc.get("flash") or 0),
                         "display": bool(sc.get("display")),
                         "spout": False,
                         "blackout": bool(sc.get("blackout")),
                         "layers": layers, "lut": lut}
                readback = bool(sc.get("display") or sc.get("spout")
                                or sc.get("preview"))
                try:
                    out = stage.compose(scene, readback=readback)
                except Exception as e:                         # noqa: BLE001
                    _log(log_path, "compose 异常 %s：%s\n%s"
                         % (sid, e, traceback.format_exc()[-800:]))
                    out = None
                if out is not None:
                    sp = spouts.get(sid)
                    if sp is not None:
                        try:
                            sp.send_image(out)
                        except Exception as e:                 # noqa: BLE001
                            _log(log_path, "SpoutOut 发送失败 %s：%s" % (sid, e))
                    if sc.get("thumb"):
                        try:
                            _td = sc.get("thumb")
                            _tw, _th = int(_td.get("w") or 320), int(_td.get("h") or 180)
                            _small = out.scaled(
                                _tw, _th, Qt.AspectRatioMode.IgnoreAspectRatio,
                                Qt.TransformationMode.SmoothTransformation)
                            _sink_write(sinks, _td, _small)
                        except Exception:                      # noqa: BLE001
                            pass
                    if sc.get("sink"):
                        _sink_write(sinks, sc.get("sink"), out)
                        if sid not in logged_sink:
                            logged_sink.add(sid)
                            _log(log_path, "NDI 成品帧 sink 已写入 sid=%s %s"
                                 % (sid, sc.get("sink").get("name")))
                    if sc.get("preview") or sc.get("display"):
                        _preview_write(preview, preview_seq, out)
            now_l = time.time()
            if now_l - last_log >= 2.0:
                last_log = now_l
                st = stage.stats() if stage is not None else {}
                _sp = []
                for _sid, _spout in spouts.items():
                    try:
                        _sp.append("%s:send=%d/err=%s"
                                   % (_sid[:6],
                                      int(getattr(_spout, "_sent", 0) or 0),
                                      str(_spout.last_error() if hasattr(_spout, "last_error")
                                          else "-")[:60]))
                    except Exception:                      # noqa: BLE001
                        pass
                _log(log_path, "屏=%d 总 %.1fms spout[%s] | %s"
                     % (len(screens),
                        (time.perf_counter() - t_all) * 1000.0,
                        " ".join(_sp), st))
    except Exception as e:                                     # noqa: BLE001
        _log(log_path, "渲染进程异常：%s\n%s" % (e, traceback.format_exc()))
    finally:
        try:
            if stage is not None:
                stage.close()
        except Exception:                                      # noqa: BLE001
            pass
        for sp in list(spouts.values()):
            try:
                sp.close()
            except Exception:                                  # noqa: BLE001
                pass
        spouts.clear()
        for shm in list(sinks.values()):
            try:
                shm.close()
            except Exception:                                  # noqa: BLE001
                pass
        sinks.clear()
        images.close()
        if preview is not None:
            try:
                preview.close()
            except Exception:                                  # noqa: BLE001
                pass
        _log(log_path, "渲染进程退出 pid=%d" % os.getpid())
