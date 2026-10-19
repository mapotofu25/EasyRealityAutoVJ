# -*- coding: utf-8 -*-
"""复现「运行时素材突然卡在某一帧，直到下一个切进来才恢复」——面向 media_manager 三层播放器。

设计：
  · 用**自己合成的小视频**（cv2 写 mp4 / PyAV 写带 alpha 的 qtrle mov）喂进真实播放器，
    素材一律落在 `tempfile.mkdtemp()`，**绝不写进项目根**。
  · 只测 media_manager 层，**不启动任何 GUI 主窗口**（QImage 无需 QApplication）。
  · 用「故障注入」制造真实世界会发生的失败（外部盘/网盘抖动、文件被占用、容器句柄失效、
    解码器重开失败），断言**播放器在 N 秒内必须重新出帧**（帧内容签名必须变化）。
  · 每个场景都用 `Runner` 精确记录解码线程是否退出，并**等线程真正退出**再进下一场景
    （避免同一条解码线程被并发复用 ⇒ OpenCV 段错误）。
  · 当前会失败的场景 = bug（脚本会明确标出）。

用法：venv/Scripts/python.exe -u tools/_test_freeze2.py
退出码 0 = 全部通过；1 = 有场景失败（即仍有卡帧根因）。
"""
import os
import sys
import time
import shutil
import atexit
import tempfile

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

import media_manager as mm                                       # noqa: E402
import _thread                                                    # noqa: E402

FAILS = []
GIVEUP_LOG = []


def check(name, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (("   " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)



def detectable(pl, secs=1.5):
    """这个播放器是否**可被上层发现"已经不出帧了"**。

    这是本测试真正要守的不变式（2026-10-01 修复后）：
    允许"卡了一下然后自愈"，**不允许"无声的永久僵尸"** ——
    即：要么真的恢复了，要么 `_ended` / `stalled()` 能报出来，
    让引擎的卡帧看门狗（`engine._heal_stalled`）能重建它。
    """
    try:
        if getattr(pl, "_ended", False):
            return True
        return bool(pl.stalled(secs))
    except Exception:                                          # noqa: BLE001
        return False

try:
    import stallwatch
    stallwatch.log_line = lambda m: GIVEUP_LOG.append(m)
except Exception:                                                   # noqa: BLE001
    pass


# ---------------------------------------------------------------- 合成素材
def make_mp4(path, n=40, w=64, h=48, fps=30.0):
    import cv2
    vw = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    assert vw.isOpened(), "VideoWriter 打不开"
    for i in range(n):
        f = np.zeros((h, w, 3), dtype=np.uint8)
        f[:, :, 0] = (i * 6) % 256
        f[:, :, 1] = (i * 3) % 256
        f[:, i % w:i % w + 4] = 255
        vw.write(f)
    vw.release()


def make_alpha_mov(path, n=40, w=64, h=48, fps=30):
    import av
    oc = av.open(path, mode="w")
    st = oc.add_stream("qtrle", rate=fps)
    st.width, st.height, st.pix_fmt = w, h, "argb"
    for i in range(n):
        arr = np.zeros((h, w, 4), dtype=np.uint8)
        arr[:, :, 0] = (i * 6) % 256
        arr[:, :, 1] = (i * 3) % 256
        arr[:, :, 3] = 200
        for pkt in st.encode(av.VideoFrame.from_ndarray(arr, format="argb")):
            oc.mux(pkt)
    for pkt in st.encode():
        oc.mux(pkt)
    oc.close()


# ---------------------------------------------------------------- 工具
def sig(img):
    try:
        if img is None or img.isNull():
            return None
        bpl = img.bytesPerLine()
        return (img.width(), img.height(), hash(bytes(img.constBits())[:bpl * img.height()]))
    except Exception:                                               # noqa: BLE001
        return None


def wait_sig(p, prev, timeout=4.0):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        s = sig(getattr(p, "_img", None))
        if s is not None and s != prev:
            return True, s
        time.sleep(0.02)
    return False, sig(getattr(p, "_img", None))


class Runner:
    """启动播放器解码线程，并精确记录线程是否已退出。"""
    def __init__(self):
        self.died = False
        self._done = False

    def start(self, p):
        self.died = False
        self._done = False

        def _run():
            try:
                p._loop()
            finally:
                self.died = True
                self._done = True
        _thread.start_new_thread(_run, ())
        return self

    def wait_died(self, timeout=5.0):
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < timeout and not self._done:
            time.sleep(0.02)
        return self._done


def bare_video(path):
    """不启动线程地构造 VideoPlayer（场景 1/2 不能复用已跑起来的线程：会双线程读同一个 cap ⇒ 段错误）。"""
    p = mm.VideoPlayer.__new__(mm.VideoPlayer)
    p.path = path
    p.cap = None
    p.fps = 30.0
    p.speed = 1.0
    p._img = None
    p._active = True
    p._warm = 0
    p._lock = False
    p._dead = False
    p._max_w = 0
    p._max_h = 0
    p._t = None
    return p


def bare_alpha(path, av):
    p = mm.AvAlphaPlayer.__new__(mm.AvAlphaPlayer)
    p.path = path
    p._av = av
    p.c = None
    p.st = None
    p.fps = 30.0
    p.speed = 1.0
    p._img = None
    p._active = True
    p._warm = 0
    p._dead = False
    p._max_w = 0
    p._max_h = 0
    p._t = None
    return p


TMP = tempfile.mkdtemp(prefix="autovj_freeze2_")
# 新铁律：mkdtemp 必须清理。用 atexit：本脚本开解码线程，atexit 在所有场景
# wait_died() 之后、进程退出时统一回收，天然满足"清理在线程 join 之后"。
atexit.register(shutil.rmtree, TMP, True)
MP4 = os.path.join(TMP, "clip.mp4")
MOV = os.path.join(TMP, "alpha.mov")
make_mp4(MP4)
make_alpha_mov(MOV)
import av                                                         # noqa: E402
print("夹具目录（临时）：%s" % TMP)
print("  mp4=%d B   mov=%d B" % (os.path.getsize(MP4), os.path.getsize(MOV)))


# ================================================================
print("\n【场景 1】VideoPlayer：打开容器失败一次（外部盘/网盘抖动）⇒ 必须自愈重开")
p = bare_video(MP4)
orig_open = p._open
_calls = {"n": 0}


def _flaky_open():
    _calls["n"] += 1
    if _calls["n"] == 1:
        return None                     # 注入：这一次打开失败
    return orig_open()


p._open = _flaky_open
r = Runner().start(p)
prev = sig(p._img)
time.sleep(0.5)
ok1, s1 = wait_sig(p, prev, 4.0)
check("打开失败一次后仍能出帧（VideoPlayer 无重试 ⇒ 永久死亡）", ok1,
      "打开尝试 %d 次，线程已退出=%s" % (_calls["n"], r.died))
check("不变式：要么自愈、要么可被检测（不允许无声僵尸）",
      ok1 or detectable(p),
      "自愈=%s 可检测=%s（_ended=%s）" % (ok1, detectable(p), getattr(p, "_ended", None)))
p._dead = True
r.wait_died()


# ================================================================
print("\n【场景 2】VideoPlayer：容器中途永久读失败（句柄失效）⇒ 必须重开容器再出帧")
p = bare_video(MP4)
p._open = orig_open
r = Runner().start(p)
ok0, s0 = wait_sig(p, None, 4.0)
check("正常能出首帧（基线）", ok0, "sig=%s" % (s0,))
time.sleep(0.3)

real_cap = p.cap
assert real_cap is not None, "cap 还没打开"


class _DeadCap:
    """包装真 cap，但 read 恒失败、set 无效 —— 模拟容器句柄已失效。"""
    def isOpened(self):
        return True

    def get(self, *a):
        return real_cap.get(*a)

    def set(self, *a):
        return False

    def read(self):
        return False, None

    def release(self):
        try:
            real_cap.release()
        except Exception:                                           # noqa: BLE001
            pass


p.cap = _DeadCap()
prev = sig(p._img)
time.sleep(0.3)
ok2, s2 = wait_sig(p, prev, 4.0)
check("容器读失败后能重开容器继续出帧（VideoPlayer 无重开 ⇒ 永久冻帧）", ok2,
      "sig 变了吗=%s，线程已退出=%s" % (ok2, r.died))
check("不变式：容器坏了也要么自愈、要么可被检测",
      ok2 or detectable(p),
      "自愈=%s 可检测=%s（_ended=%s）" % (ok2, detectable(p), getattr(p, "_ended", None)))
p._dead = True
r.wait_died()


# ================================================================
print("\n【场景 3】AvAlphaPlayer：打开失败一次 ⇒ 必须自愈重开")
p = bare_alpha(MOV, av)
orig_open2 = p._open
_calls2 = {"n": 0}


def _flaky_open2():
    _calls2["n"] += 1
    if _calls2["n"] == 1:
        raise RuntimeError("注入：打开失败")
    return orig_open2()


p._open = _flaky_open2
r = Runner().start(p)
prev = sig(p._img)
time.sleep(0.5)
ok3, s3 = wait_sig(p, prev, 4.0)
check("打开失败一次后仍能出帧（AvAlphaPlayer 无重试 ⇒ 永久死亡）", ok3,
      "打开尝试 %d 次，线程已退出=%s" % (_calls2["n"], r.died))
check("不变式：AvAlpha 也要么自愈、要么可被检测",
      ok3 or detectable(p),
      "自愈=%s 可检测=%s（_ended=%s）" % (ok3, detectable(p), getattr(p, "_ended", None)))
p._dead = True
r.wait_died()


# ================================================================
print("\n【场景 4】AvAlphaPlayer：解码中途重开容器失败 ⇒ 线程/生成器如何收场")
p = bare_alpha(MOV, av)
p._open = orig_open2
r = Runner().start(p)
ok0, s0 = wait_sig(p, None, 4.0)
check("正常能出首帧（基线）", ok0, "sig=%s" % (s0,))
time.sleep(0.3)


class _DeadReader:
    """包装真容器：decode 立刻结束、seek 失败 ⇒ 逼 _iter_frames 走"重开容器"分支。"""
    def decode(self, video=0):
        return iter(())

    def seek(self, *a):
        raise RuntimeError("注入：seek 失败")

    def close(self):
        pass


class _DeadAvMod:
    def open(self, *a, **k):
        raise RuntimeError("注入：重开容器失败")


p.c = _DeadReader()
p._av = _DeadAvMod()
# ⚠ 注入只在"当前这一趟 for frame in self.c.decode() 跑完、又走到 _rewind"时才生效
#   （for 语句在进入时就把迭代器绑到了**旧容器**上）⇒ 至少等一整趟（素材 40 帧 ≈1.33s）。
time.sleep(2.5)
prev = sig(p._img)
ok4, s4 = wait_sig(p, prev, 4.0)
state = ("线程已静默死亡（异常逃逸出 _loop）" if r.died
         else "线程仍活着，但生成器已关闭 ⇒ 卡在 0.05s 空转，永久冻帧")
check("重开失败后仍能出帧（异常逃逸 ⇒ 线程死亡或生成器永久关闭）", ok4,
      "sig 变了吗=%s；%s" % (ok4, state))
check("不变式：重开失败也要么自愈、要么可被检测（含放弃日志）",
      ok4 or detectable(p) or bool(GIVEUP_LOG),
      "自愈=%s 可检测=%s 日志=%s" % (ok4, detectable(p), GIVEUP_LOG[:1]))
p._dead = True
r.wait_died()


# ================================================================
print("\n【场景 5】GpuDxvPlayer（对照，上一轮修过）：打开失败前两次、第三次成功 ⇒ 必须自愈出帧")
class _FakeFrame:
    def __init__(self, v=1):
        self.v = v

    def to_ndarray(self, format="bgra"):
        return np.full((4, 4, 4), self.v, dtype=np.uint8)


def _mk_gpu(open_fail_times, frames_fn):
    q = mm.GpuDxvPlayer.__new__(mm.GpuDxvPlayer)
    q.path = "fake.mov"
    q._dead = False
    q._active = True
    q._warm = 0
    q.fps = 30.0
    q.speed = 1.0
    q._img = None
    q._max_w = 0
    q._max_h = 0
    q._qfmt = 4
    q.gpu = False
    q.gpu_want = False
    q.c = None
    q.st = None
    q._opens = 0

    def _open():
        q._opens += 1
        if q._opens <= open_fail_times:
            raise RuntimeError("注入：打开失败 #%d" % q._opens)
        return object(), object()

    q._open = _open
    q._release = lambda: setattr(q, "c", None)
    q._soft_frames = lambda: frames_fn(q)
    return q


def _one_frame_then_end(q):
    yield _FakeFrame(9)
    return


GIVEUP_LOG.clear()
q = _mk_gpu(2, _one_frame_then_end)
rq = Runner().start(q)
t0 = time.perf_counter()
while time.perf_counter() - t0 < 4.0 and q._img is None:
    time.sleep(0.02)
check("打开失败两次后仍然出帧（上一轮 `_loop` 自愈生效）", q._img is not None,
      "打开尝试 %d 次" % q._opens)
q._dead = True
rq.wait_died()


print("\n【场景 6】GpuDxvPlayer（对照）：取帧生成器收工后必须重开再出帧")
GIVEUP_LOG.clear()
q2 = _mk_gpu(0, _one_frame_then_end)
rq2 = Runner().start(q2)
t0 = time.perf_counter()
while time.perf_counter() - t0 < 3.0 and q2._opens < 2:
    time.sleep(0.02)
check("生成器收工后重开了容器（自愈）", q2._opens >= 2, "打开次数=%d" % q2._opens)
q2._dead = True
rq2.wait_died()


# ================================================================
print("\n【场景 7】GpuDxvPlayer：连续失败收工后，对象是否会变成「缓存里的僵尸」")
GIVEUP_LOG.clear()
mm._REOPEN_MAX = 2                     # 缩小阈值，快速触发"真收工"
q3 = _mk_gpu(999, _one_frame_then_end)  # 永远打不开 ⇒ 连续失败 ⇒ 收工
rq3 = Runner().start(q3)
rq3.wait_died(8.0)
check("连续失败后线程确实收工（不忙等）", rq3.died, "线程存活=%s" % (not rq3.died))
check("★ 收工后 _dead 仍为 False（引擎缓存命中会把它当健康播放器继续用 ⇒ 永久僵尸）",
      q3._dead is False, "_dead=%s c=%s" % (q3._dead, q3.c))
check("收工时有放弃日志（可观测）", any("放弃" in m for m in GIVEUP_LOG), str(GIVEUP_LOG[:1])[:90])
mm._REOPEN_MAX = 6


# ================================================================
# 收尾清理（新铁律：mkdtemp 必须清理，且清理必须在线程 join 之后）
# ⚠ 本脚本的 alpha 播放器（PyAV）在退出时可能仍持有 TMP/alpha.mov 句柄，
#   导致 rmtree 删不掉目录。先显式停掉所有播放器、GC，再带重试 rmtree。
import gc                                                        # noqa: E402
for _obj in (locals().get("q2"), locals().get("q3"), locals().get("q1")):
    try:
        if _obj is not None:
            _obj._dead = True
    except Exception:                                            # noqa: BLE001
        pass
for _r in (locals().get("rq2"), locals().get("rq3"), locals().get("rq1")):
    try:
        if _r is not None:
            _r.wait_died(3.0)
    except Exception:                                            # noqa: BLE001
        pass
try:
    del p
except Exception:                                                # noqa: BLE001
    pass
gc.collect()
for _ in range(5):                                               # 句柄释放有时间差，重试几轮
    try:
        shutil.rmtree(TMP, ignore_errors=False)
        break
    except OSError:
        time.sleep(0.3)
        gc.collect()
else:
    shutil.rmtree(TMP, ignore_errors=True)                       # 兜底：不再报错

print()
if FAILS:
    print("结果：失败 %d 项 ✗ —— 以下仍然不满足「要么自愈、要么可被检测」：" % len(FAILS))
    for f in FAILS:
        print("   · " + f)
else:
    print("结果：全部通过 ✓")
sys.exit(1 if FAILS else 0)
