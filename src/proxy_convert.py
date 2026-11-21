# -*- coding: utf-8 -*-
"""代理转码队列：无 alpha -> 1080p DXV DXT1；alpha -> 1080p ProRes 4444。

只在后台线程跑；一次一个文件；演出中暂停；原子替换；失败保留原文件。
"""
import hashlib
import os
import shutil
import threading
import time
from fractions import Fraction

import av
import cv2
import numpy as np

from PySide6.QtCore import QThread, Signal

import media_manager as mm


def _inside_lib(path, lib_dir):
    try:
        p = os.path.abspath(path)
        root = os.path.abspath(lib_dir)
        return p == root or p.startswith(root + os.sep)
    except Exception:                                          # noqa: BLE001
        return False


def _proxy_dst(src, lib_dir):
    root = os.path.join(os.path.abspath(lib_dir), "已转码")
    if _inside_lib(src, lib_dir):
        rel = os.path.relpath(os.path.abspath(src), os.path.abspath(lib_dir))
    else:
        h = hashlib.md5(os.path.abspath(src).encode("utf-8", "replace")).hexdigest()[:8]
        rel = os.path.join("external", h + "_" + os.path.basename(src))
    dst = os.path.join(root, rel)
    base, ext = os.path.splitext(dst)
    if ext.lower() != ".mov":
        dst = base + ".mov"
    d = os.path.dirname(dst)
    if d:
        os.makedirs(d, exist_ok=True)
    return dst


def _alpha_plane(frame):
    pl = frame.planes[3]
    return np.frombuffer(memoryview(pl), np.uint8).reshape(pl.height, pl.line_size)[:, :pl.width].copy()


def _iter_bgra(job):
    """统一产出 BGRA。DXV-YUV 的颜色必须用 OpenCV；alpha 用 PyAV 平面。"""
    src = job["path"]
    codec, pf = job["codec"], job["pf"]
    if codec == "dxv" and pf in ("yuv420p", "yuva420p"):
        cap = cv2.VideoCapture(mm._ascii_safe_path(src))
        if not cap.isOpened():
            raise RuntimeError("OpenCV 打不开 DXV-YUV 源")
        av_c = av.open(src, metadata_errors="replace") if pf == "yuva420p" else None
        av_s = av_c.streams.video[0] if av_c is not None else None
        try:
            while True:
                ok, bgr = cap.read()
                if not ok:
                    break
                alpha = None
                if av_c is not None:
                    try:
                        fr = next(av_c.decode(av_s))
                        alpha = _alpha_plane(fr)
                    except StopIteration:
                        alpha = None
                bgra = cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA)
                if alpha is not None and alpha.shape[:2] == bgra.shape[:2]:
                    bgra[:, :, 3] = alpha
                yield bgra
        finally:
            cap.release()
            if av_c is not None:
                av_c.close()
        return
    if codec == "dxv" and pf in ("rgba", "bgra"):
        # ★ 2026-11-12：DXV rgba 不要走 PyAV 的 BGRA reformatter。
        #   2048×858 的 rgba 在内存紧张时会抛 `Cannot allocate memory`；
        #   而且 reformat 每帧还要多一次拷贝（实测首帧 80ms）。
        #   原生 `to_ndarray()` 已经是 (h,w,4) rgba，直接换通道即可。
        c = av.open(src, metadata_errors="replace")
        try:
            for fr in c.decode(video=0):
                try:
                    if fr.format.name == "rgba":
                        yield cv2.cvtColor(fr.to_ndarray(), cv2.COLOR_RGBA2BGRA)
                    elif fr.format.name == "bgra":
                        yield fr.to_ndarray()
                    else:
                        yield fr.to_ndarray(format="bgra")
                except av.error.MemoryError:
                    # 再退一步：先解成 rgb24（少 1/4 内存），再补 alpha=255
                    a = fr.to_ndarray(format="rgb24")
                    bgra = cv2.cvtColor(a, cv2.COLOR_RGB2BGRA)
                    bgra[:, :, 3] = 255
                    yield bgra
        finally:
            c.close()
        return
    if codec == "mjpeg":
        cap = cv2.VideoCapture(mm._ascii_safe_path(src))
        if not cap.isOpened():
            raise RuntimeError("OpenCV 打不开 MJPEG 源")
        try:
            while True:
                ok, bgr = cap.read()
                if not ok:
                    break
                yield cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA)
        finally:
            cap.release()
        return
    # qtrle/prores/其它：PyAV 直接 BGRA
    c = av.open(src, metadata_errors="replace")
    try:
        for fr in c.decode(video=0):
            try:
                yield fr.to_ndarray(format="bgra")
            except Exception:                                  # noqa: BLE001
                yield fr.to_ndarray(format="rgba")
    finally:
        c.close()


class ProxyConvertWorker(QThread):
    progress = Signal(int, int, str, float)      # done, total, name, eta_secs
    file_done = Signal(str, str, bool, object)   # src, dst, ok, meta
    paused = Signal(bool)
    finished = Signal(int, int, str)             # ok, fail, last_error

    def __init__(self, jobs, main, lib_dir):
        super().__init__()
        self.jobs = list(jobs)
        self.main = main
        self.lib_dir = lib_dir
        self._stop = False
        self._last_warn = ""
        # ★ 2026-11-21：多文件并行。DXV 编码器本身是单线程（实测 1080p 只有
        #   ~16 fps，threads/thread_type 都不起作用），单文件串行时整机 CPU
        #   只有 10~15% —— 16 核里 14 核在闲着。改成多个文件同时转，
        #   实测 2 路 1.8×、4 路 2.9×（CPU 允许时）。文件源/目标同盘时受磁盘限制，
        #   所以上限压到 3 路。
        self._lock = threading.Lock()
        self._done = 0
        self._t0 = 0.0

    def stop(self):
        self._stop = True

    def _wait_if_running(self):
        while not self._stop:
            try:
                running = bool(getattr(self.main.engine, "running", False))
            except Exception:                                  # noqa: BLE001
                running = False
            if not running:
                self.paused.emit(False)
                return True
            self.paused.emit(True)
            self.msleep(400)
        return False

    def _path_in_use(self, path):
        """该文件是否正被某个播放器打开（Windows 下打开中的文件不能原子覆盖）。"""
        try:
            eng = getattr(self.main, "engine", None)
            if eng is None:
                return False
            ap = os.path.abspath(path)
            seen = set()
            players = []
            for lay in list(getattr(eng, "layers", []) or []):
                try:
                    players.extend(list((getattr(lay, "cache", None) or {}).values()))
                except Exception:                              # noqa: BLE001
                    pass
                players.extend([getattr(lay, "player", None),
                                getattr(lay, "prev_player", None)])
                try:
                    players.extend(list((getattr(lay, "_cue_players", None)
                                         or {}).values()))
                except Exception:                              # noqa: BLE001
                    pass
            try:
                players.extend(list((getattr(eng, "_cue_players", None)
                                     or {}).values()))
            except Exception:                                  # noqa: BLE001
                pass
            for p in players:
                if p is None or id(p) in seen:
                    continue
                seen.add(id(p))
                pp = getattr(p, "path", None)
                if not pp:
                    try:
                        pp = getattr(getattr(p, "item", None), "path", None)
                    except Exception:                          # noqa: BLE001
                        pp = None
                if pp and os.path.abspath(pp) == ap:
                    return True
        except Exception:                                      # noqa: BLE001
            return False
        return False

    def _convert_one(self, job, idx):
        warn = ""
        src = job["path"]
        fps = max(1.0, float(job.get("fps") or 30.0))
        tw, th = int(job["tw"]), int(job["th"])
        alpha = bool(job.get("alpha", False))
        overwrite = (_inside_lib(src, self.lib_dir)
                     and not self._path_in_use(src))
        dst = src if overwrite else _proxy_dst(src, self.lib_dir)
        tmp = dst + (".tmp.mov" if not dst.lower().endswith(".tmp.mov") else "")
        if not tmp.lower().endswith(".mov"):
            tmp = dst + ".tmp.mov"
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        codec = "prores_ks" if alpha else "dxv"
        pix_fmt = "yuva444p10le" if alpha else "rgba"
        rate = Fraction(1, max(1, int(round(fps))))
        out = av.open(tmp, "w", format="mov")
        st = out.add_stream(codec, rate=Fraction(max(1, int(round(fps)))))
        st.width, st.height, st.pix_fmt = tw, th, pix_fmt
        if alpha:
            st.options = {"profile": "4444"}
        total = max(1, int(round(float(job.get("dur") or 0.0) * fps)))
        t0 = time.perf_counter()
        n = 0
        truncated = False
        try:
            frames = _iter_bgra(job)
            while True:
                try:
                    bgra = next(frames)
                except StopIteration:
                    break
                except Exception as exc:                       # noqa: BLE001
                    # 源文件尾部损坏（实测 1 (22).mov 第 2324/2375 帧起
                    # `avcodec_send_packet() Invalid data`）：已经转好的
                    # 部分仍然可用，别把整个代理丢掉。
                    if n <= 0:
                        raise
                    truncated = True
                    warn = "%s: 第 %d 帧后无法解码（%s）" % (
                        os.path.basename(src), n, type(exc).__name__)
                    break
                if self._stop:
                    raise RuntimeError("已取消")
                # 演出开始就暂停（每 30 帧检查一次，单文件也能及时让出 CPU）
                if n and n % 30 == 0 and not self._wait_if_running():
                    raise RuntimeError("已取消")
                if bgra.shape[1] != tw or bgra.shape[0] != th:
                    bgra = cv2.resize(bgra, (tw, th), interpolation=cv2.INTER_AREA)
                vf = av.VideoFrame.from_ndarray(np.ascontiguousarray(bgra), format="bgra")
                vf = vf.reformat(width=tw, height=th, format=pix_fmt)
                vf.time_base = rate
                vf.pts = n
                for pkt in st.encode(vf):
                    out.mux(pkt)
                n += 1
                if n % 5 == 0 or n == 1:
                    # 并行时用「已完成文件数」做进度、按整体耗时估剩余
                    with self._lock:
                        done = self._done
                        t_start = self._t0 or t0
                    el = time.perf_counter() - t_start
                    avg = el / max(1, done)
                    eta = avg * max(0, len(self.jobs) - done)
                    self.progress.emit(done, len(self.jobs), os.path.basename(src), eta)
            for pkt in st.encode(None):
                out.mux(pkt)
            out.close()
            try:
                os.replace(tmp, dst)
            except PermissionError:
                # 转换期间素材被自动池选中/播放器打开：源文件不能覆盖。
                # 把已经转好的临时文件放进 `素材库\已转码`，改走代理引用。
                if not overwrite:
                    raise
                alt = _proxy_dst(src, self.lib_dir)
                if os.path.abspath(alt) == os.path.abspath(dst):
                    raise
                os.replace(tmp, alt)
                dst, overwrite = alt, False
        except Exception:
            try:
                out.close()
            except Exception:                                  # noqa: BLE001
                pass
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            raise
        meta = {
            "dst": dst,
            "overwrite": bool(overwrite),
            "codec": codec,
            "is_dxv": (codec == "dxv"),
            "has_alpha": bool(alpha),
            "truncated": bool(truncated),
            "decoded_frames": int(n),
            "mtime": os.path.getmtime(dst),
            "warn": warn,
        }
        return dst, meta

    def _run_one(self, idx, job):
        """一个文件的完整转码 + 素材元数据回写（可被多个线程同时调用）。"""
        if self._stop:
            return None
        if not self._wait_if_running():
            return None
        try:
            dst, meta = self._convert_one(job, idx)
        except Exception as e:                                 # noqa: BLE001
            return False, job, "", {"error": "%s: %s" % (
                os.path.basename(job["path"]), str(e)[:160])}
        it = job["item"]
        try:
            it.proxy_path = None if meta["overwrite"] else dst
            it._codec = meta["codec"]
            it._is_dxv = bool(meta["is_dxv"])
            it._has_alpha = bool(meta["has_alpha"])
            it.mtime = meta["mtime"]
            if meta["overwrite"]:
                for ext in (".jpg", ".png"):
                    try:
                        os.remove(it._thumb_path(ext))
                    except OSError:
                        pass
                # 图标缓存只能由 GUI 线程丢；这里交给主线程在缩略图补扫后处理
        except Exception:                                      # noqa: BLE001
            pass
        return True, job, dst, meta

    def run(self):
        from concurrent.futures import ThreadPoolExecutor, as_completed

        ok = fail = 0
        last = ""
        total = len(self.jobs)
        try:
            cpus = int(os.cpu_count() or 4)
        except Exception:                                      # noqa: BLE001
            cpus = 4
        # 2~6 路：DXV 编码单线程，多文件并行才能吃满 CPU。
        #   实测同一块机械盘 6 个 4K 片段：3 路 47.3s、6 路 36.2s（1.31×）；
        #   1080p 源磁盘压力小，6 路收益更大。演出开始会逐帧让出 CPU。
        workers = max(1, min(6, cpus // 2))
        try:
            _env_w = int(os.environ.get("AUTOVJ_PROXY_WORKERS") or 0)
            if _env_w > 0:
                workers = max(1, min(8, _env_w))
        except Exception:                                      # noqa: BLE001
            pass
        self._t0 = time.perf_counter()
        if workers <= 1:
            for i, job in enumerate(self.jobs):
                res = self._run_one(i, job)
                if res is None:
                    break
                good, job, dst, meta = res
                with self._lock:
                    self._done += 1
                if good:
                    ok += 1
                    self.file_done.emit(job["path"], dst, True, meta)
                else:
                    fail += 1
                    last = meta.get("error", "")
                    self.file_done.emit(job["path"], "", False, meta)
        else:
            ex = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="proxyconv")
            try:
                futs = [ex.submit(self._run_one, i, job) for i, job in enumerate(self.jobs)]
                for fut in as_completed(futs):
                    try:
                        res = fut.result()
                    except Exception as e:                     # noqa: BLE001
                        fail += 1
                        last = str(e)[:160]
                        continue
                    if res is None:
                        continue
                    good, job, dst, meta = res
                    with self._lock:
                        self._done += 1
                    if good:
                        ok += 1
                        self.file_done.emit(job["path"], dst, True, meta)
                    else:
                        fail += 1
                        last = meta.get("error", "")
                        self.file_done.emit(job["path"], "", False, meta)
            finally:
                # wait=True：确保停止时正在写的 .tmp.mov 都收尾，避免关程序留下半截文件
                ex.shutdown(wait=True, cancel_futures=True)
        self.finished.emit(ok, fail, last)
