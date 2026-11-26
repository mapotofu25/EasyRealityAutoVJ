# -*- coding: utf-8 -*-
"""GPU 合成进程的引擎侧客户端（共享内存帧通道）。

引擎进程只做：
  · 解码 → QImage；
  · 把每层像素写进 SharedMemory（三重缓冲）；
  · 把“场景描述”（小 dict）投给渲染进程；
  · 从共享内存读回 480×270 预览。

渲染进程（gpu_render_proc.py）拥有 GL 上下文、原生窗口、Spout，不受 GUI GIL 影响。
"""
from __future__ import annotations

import hashlib
import multiprocessing as mp
import os
import struct
import time
import uuid
from multiprocessing import shared_memory

import numpy as np
from PySide6.QtGui import QImage

_PREVIEW_W, _PREVIEW_H = 480, 270
_SLOT_BUFS = 3
_SINK_HDR = 32


def _fmt_of(img):
    """返回 (bgra, premul)；不支持的格式返回 (None, None)。"""
    f = img.format()
    if f in (QImage.Format_ARGB32_Premultiplied, QImage.Format_RGB32):
        return 1, 0
    if f == QImage.Format_ARGB32:
        return 1, 1
    if f == QImage.Format_RGBA8888_Premultiplied:
        return 0, 0
    if f == QImage.Format_RGBA8888:
        return 0, 1
    return None, None


class GpuRenderClient:
    def __init__(self, log=None, name="autovj_gpu_render"):
        self._log = log or (lambda _m: None)
        self._ctx = mp.get_context("spawn")
        self._q = None
        self._proc = None
        self._preview = None
        self._preview_seq = None
        self._preview_last = 0
        self._slots = {}
        self._sinks = {}
        self._thumbs = {}
        self._gen = 0
        self._run = "%d_%s" % (os.getpid(), uuid.uuid4().hex[:8])
        self._started = False
        self._name = name
        self._last_job = 0

    # ------------------------------------------------------------------ 生命周期
    def start(self):
        if self._started:
            return True
        try:
            self._preview = shared_memory.SharedMemory(
                create=True, size=_PREVIEW_W * _PREVIEW_H * 4,
                name="avj_prev_%s" % self._run)
            self._preview_seq = self._ctx.Value("q", 0)
            self._q = self._ctx.Queue(maxsize=2)
            from gpu_render_proc import render_main
            self._proc = self._ctx.Process(
                target=render_main,
                args=(self._q, self._preview.name, self._preview_seq,
                      os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                   "AutoVJ", "gpu_render.log")),
                name=self._name, daemon=True)
            self._proc.start()
            self._started = True
            self._log("GPU 渲染进程已启动 pid=%s" % self._proc.pid)
            return True
        except Exception as e:                                 # noqa: BLE001
            self._log("GPU 渲染进程启动失败：%s" % e)
            self.close()
            return False

    def alive(self):
        return bool(self._started and self._proc is not None
                    and self._proc.is_alive())

    def close(self):
        try:
            if self._q is not None:
                try:
                    self._q.put_nowait("STOP")
                except Exception:                              # noqa: BLE001
                    pass
            if self._proc is not None:
                try:
                    self._proc.join(timeout=1.0)
                except Exception:                              # noqa: BLE001
                    pass
                try:
                    if self._proc.is_alive():
                        self._proc.terminate()
                except Exception:                              # noqa: BLE001
                    pass
            self._proc = None
        except Exception:                                      # noqa: BLE001
            pass
        for st in list(self._slots.values()):
            for shm in st.get("bufs") or []:
                try:
                    shm.close()
                except Exception:                              # noqa: BLE001
                    pass
                try:
                    shm.unlink()
                except Exception:                              # noqa: BLE001
                    pass
        self._slots.clear()
        for st in list(self._sinks.values()):
            try:
                st["shm"].close()
                st["shm"].unlink()
            except Exception:                                  # noqa: BLE001
                pass
        self._sinks.clear()
        for st in list(self._thumbs.values()):
            try:
                st["shm"].close()
                st["shm"].unlink()
            except Exception:                              # noqa: BLE001
                pass
        self._thumbs.clear()
        if self._preview is not None:
            try:
                self._preview.close()
                self._preview.unlink()
            except Exception:                                  # noqa: BLE001
                pass
            self._preview = None
        self._started = False

    # ------------------------------------------------------------------ 共享内存槽
    def _ensure_slot(self, key, w, h):
        need = int(w) * int(h) * 4
        st = self._slots.get(key)
        if st is not None and st.get("size", 0) >= need:
            return st
        # 尺寸变化/首次：关旧建新（渲染进程按新名字 attach；旧名字由它自己 close）
        if st is not None:
            for shm in st.get("bufs") or []:
                try:
                    shm.close()
                    shm.unlink()
                except Exception:                          # noqa: BLE001
                    pass
        bufs = []
        self._gen = int(getattr(self, "_gen", 0)) + 1
        gen = self._gen
        tag = hashlib.sha1(str(key).encode("utf-8", "ignore")).hexdigest()[:10]
        for i in range(_SLOT_BUFS):
            nm = "avj_%s_%s_g%d_%d" % (self._run, tag, gen, i)
            try:
                shm = shared_memory.SharedMemory(create=True, size=need,
                                                 name=nm)
            except FileExistsError:
                shm = shared_memory.SharedMemory(name=nm)
            bufs.append(shm)
        st = {"w": int(w), "h": int(h), "size": need, "bufs": bufs,
              "gen": gen, "turn": 0, "ver": 0, "last_key": None, "desc": None}
        self._slots[key] = st
        return st

    def _write(self, key, img):
        if img is None or img.isNull():
            return None
        bgra, premul = _fmt_of(img)
        if bgra is None:
            img = img.convertToFormat(QImage.Format_ARGB32_Premultiplied)
            bgra, premul = 1, 0
        w, h = int(img.width()), int(img.height())
        if w <= 0 or h <= 0:
            return None
        st = self._ensure_slot(key, w, h)
        if st is None:
            return None
        # 静态图：同一对象/同一内容不重复搬
        try:
            ck = int(img.cacheKey())
        except Exception:                                      # noqa: BLE001
            ck = id(img)
        if (st.get("desc") is not None and st.get("last_key") == (ck, w, h)
                and st.get("w") == w and st.get("h") == h):
            return st["desc"]
        st["turn"] = (int(st.get("turn", 0)) + 1) % _SLOT_BUFS
        shm = st["bufs"][st["turn"]]
        need = w * 4
        try:
            arr = np.frombuffer(shm.buf, dtype=np.uint8, count=w * h * 4)
            bpl = int(img.bytesPerLine())
            if bpl == need:
                src = np.frombuffer(img.constBits(), dtype=np.uint8,
                                    count=w * h * 4)
                np.copyto(arr, src)
            else:
                mv = memoryview(img.constBits())
                for y in range(h):
                    s = y * bpl
                    arr[y * need:(y + 1) * need] = np.frombuffer(
                        mv[s:s + need], dtype=np.uint8)
        except Exception as e:                                 # noqa: BLE001
            self._log("共享内存写帧失败 %s：%s" % (key, e))
            return None
        st["ver"] = int(st.get("ver", 0)) + 1
        st["w"], st["h"] = w, h
        st["last_key"] = (ck, w, h)
        # ⚠ 不能把纹理尺寸叫 w/h：`submit()` 会把本 desc 合并进图层描述，
        #   而图层的 w/h 是**画布上的目标四边形尺寸**（用户缩放/cover 后的
        #   结果）。同名会把它覆盖成源图尺寸 ⇒ 大小/位置调整全部失效。
        desc = {"buf": shm.name, "tex_w": w, "tex_h": h,
                "bgra": int(bgra), "premul": int(premul),
                "version": int(st["ver"])}
        st["desc"] = desc
        return desc

    # ------------------------------------------------------------------ 路由成品帧
    def _ensure_sink(self, sid, w, h):
        need = _SINK_HDR + int(w) * int(h) * 4 * 2
        st = self._sinks.get(sid)
        if st is not None and st.get("w") == w and st.get("h") == h \
                and st.get("shm").size >= need:
            return st
        if st is not None:
            try:
                st["shm"].close()
                st["shm"].unlink()
            except Exception:                                  # noqa: BLE001
                pass
        self._gen = int(getattr(self, "_gen", 0)) + 1
        tag = hashlib.sha1(("sink:" + str(sid)).encode("utf-8", "ignore")).hexdigest()[:10]
        nm = "avj_sink_%s_%s_g%d" % (self._run, tag, self._gen)
        try:
            shm = shared_memory.SharedMemory(create=True, size=need, name=nm)
        except FileExistsError:
            shm = shared_memory.SharedMemory(name=nm)
        st = {"shm": shm, "name": nm, "w": int(w), "h": int(h),
              "seq": 0}
        self._sinks[sid] = st
        return st

    def _ensure_thumb(self, sid, w=320, h=180):
        st = self._thumbs.get(sid)
        need = _SINK_HDR + int(w) * int(h) * 4 * 2
        if st is not None and st.get("shm").size >= need:
            return st
        if st is not None:
            try:
                st["shm"].close()
                st["shm"].unlink()
            except Exception:                              # noqa: BLE001
                pass
        self._gen = int(getattr(self, "_gen", 0)) + 1
        tag = hashlib.sha1(("thumb:" + str(sid)).encode("utf-8", "ignore")).hexdigest()[:10]
        nm = "avj_thumb_%s_%s_g%d" % (self._run, tag, self._gen)
        try:
            shm = shared_memory.SharedMemory(create=True, size=need, name=nm)
        except FileExistsError:
            shm = shared_memory.SharedMemory(name=nm)
        st = {"shm": shm, "name": nm, "w": int(w), "h": int(h), "seq": 0}
        self._thumbs[sid] = st
        return st

    def poll_thumbs(self):
        out = {}
        for sid, st in list(self._thumbs.items()):
            try:
                shm = st["shm"]
                seq, idx = struct.unpack_from("<qq", shm.buf, 0)
                if seq == st.get("seq"):
                    continue
                st["seq"] = seq
                w, h = int(st["w"]), int(st["h"])
                off = _SINK_HDR + (0 if int(idx) == 0 else w * h * 4)
                raw = bytes(shm.buf[off:off + w * h * 4])
                out[sid] = QImage(raw, w, h, w * 4,
                                  QImage.Format_ARGB32_Premultiplied).copy()
            except Exception:                                  # noqa: BLE001
                continue
        return out

    def poll_route_frames(self):
        """返回 {sid: QImage}，只给本 tick 有新帧的屏。"""
        out = {}
        for sid, st in list(self._sinks.items()):
            try:
                shm = st["shm"]
                seq, idx = struct.unpack_from("<qq", shm.buf, 0)
                if seq == st.get("seq"):
                    continue
                st["seq"] = seq
                w, h = int(st["w"]), int(st["h"])
                off = _SINK_HDR + (0 if int(idx) == 0 else w * h * 4)
                raw = bytes(shm.buf[off:off + w * h * 4])
                out[sid] = QImage(raw, w, h, w * 4,
                                  QImage.Format_ARGB32_Premultiplied).copy()
            except Exception:                                  # noqa: BLE001
                continue
        return out

    # ------------------------------------------------------------------ 提交
    def submit(self, screens):
        """screens: [{'sid','w','h','alpha','display','spout','layers',
        'lut','blackout'}], layers 的 img 为 QImage。"""
        if not self.alive():
            try:
                self._log("提交失败：渲染进程不存活 started=%s exitcode=%s"
                          % (self._started,
                             getattr(self._proc, "exitcode", None)))
            except Exception:                                  # noqa: BLE001
                pass
            return False
        try:
            out = []
            for sc in screens:
                layers = []
                for lay in (sc.get("layers") or []):
                    desc = self._write(
                        str(lay.get("slot") or ("L:%d" % id(lay.get("img")))),
                        lay.get("img"))
                    if desc is None:
                        continue
                    d = dict(lay)
                    d.pop("img", None)
                    d.update(desc)
                    layers.append(d)
                lut = None
                li = sc.get("lut")
                if li is not None and not li.isNull():
                    ld = self._write("LUT:%s" % sc.get("sid"), li)
                    if ld is not None:
                        lut = ld
                sid = str(sc.get("sid") or "")
                w = int(sc.get("w") or 1920)
                h = int(sc.get("h") or 1080)
                ow = int(sc.get("out_w") or w)
                oh = int(sc.get("out_h") or h)
                sink = None
                if sc.get("ndi") or sc.get("capture"):
                    _s = self._ensure_sink(sid, ow, oh)
                    sink = {"name": _s["name"], "w": ow, "h": oh}
                _t = self._ensure_thumb(sid)
                thumb = {"name": _t["name"], "w": 320, "h": 180}
                out.append({"sid": sid,
                            "w": w, "h": h,
                            "out_w": ow, "out_h": oh,
                            "alpha": bool(sc.get("alpha")),
                            "flash": int(sc.get("flash") or 0),
                            "display": bool(sc.get("display")),
                            "preview": bool(sc.get("preview")),
                            "display_monitor": int(sc.get("display_monitor") or 0),
                            "title": str(sc.get("title") or "Easy Reality Output"),
                            "spout": str(sc.get("spout") or ""),
                            "ndi": bool(sc.get("ndi")),
                            "sink": sink,
                            "thumb": thumb,
                            "layers": layers, "lut": lut,
                            "blackout": bool(sc.get("blackout"))})
            job = {"seq": int(time.time() * 1000) & 0x7FFFFFFF,
                   "screens": out}
            self._last_job = job["seq"]
            try:
                self._q.put_nowait(job)
            except Exception as e1:
                try:
                    self._q.get_nowait()
                except Exception:                              # noqa: BLE001
                    pass
                try:
                    self._q.put_nowait(job)
                except Exception as e2:
                    self._log("提交队列满，丢弃本帧：%s / %s" % (e1, e2))
                    return True          # 丢一帧不算故障，绝不能因此回退 CPU
            return True
        except Exception as e:                                 # noqa: BLE001
            self._log("GPU 渲染提交失败：%s" % e)
            return False

    # ------------------------------------------------------------------ 预览
    def poll_preview(self):
        if self._preview is None or self._preview_seq is None:
            return None
        try:
            seq = int(self._preview_seq.value)
            if seq == self._preview_last:
                return None
            self._preview_last = seq
            raw = bytes(self._preview.buf[:_PREVIEW_W * _PREVIEW_H * 4])
            img = QImage(raw, _PREVIEW_W, _PREVIEW_H, _PREVIEW_W * 4,
                         QImage.Format_ARGB32_Premultiplied).copy()
            return img
        except Exception:                                      # noqa: BLE001
            return None
