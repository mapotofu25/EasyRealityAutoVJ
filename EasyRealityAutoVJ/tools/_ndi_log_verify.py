# -*- coding: utf-8 -*-
"""验证 ndi_out 新增的落盘日志：不丢、不死锁、按 key 去重、心跳周期化。

用法：venv\\Scripts\\python tools\\_ndi_log_verify.py
"""
import os
import sys
import tempfile
import threading
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

# 用临时 LOCALAPPDATA，别污染用户真实日志
_tmp = tempfile.mkdtemp(prefix="autovj_ndilog_")
os.environ["LOCALAPPDATA"] = _tmp
import ndi_out                                                  # noqa: E402

ok = 0
fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print("  ✅ %s" % name)
    else:
        fail += 1
        print("  ❌ %s %s" % (name, extra))


logpath = ndi_out._health_path()
print("日志路径:", logpath)

# --- T1：_log 落盘 ---
ndi_out._LOGGED.clear()
ndi_out._log("测试落盘-甲", key="t1")
txt = open(logpath, encoding="utf-8").read()
check("T1 落盘", "测试落盘-甲" in txt, repr(txt[-200:]))

# --- T2：按 key 去重（同 key 不重复写）---
ndi_out._log("测试落盘-甲", key="t1")
txt = open(logpath, encoding="utf-8").read()
check("T2 同 key 去重", txt.count("测试落盘-甲") == 1,
      "count=%d" % txt.count("测试落盘-甲"))

# --- T3：不同 key 各写一行 ---
ndi_out._log("测试落盘-乙", key="t3")
txt = open(logpath, encoding="utf-8").read()
check("T3 不同 key 都写", "测试落盘-乙" in txt)

# --- T4：不死锁（关键！_log 与 _health_write 不能共用同一把锁）---
done = threading.Event()

def worker():
    for i in range(50):
        ndi_out._log("并发-%d" % i, key="t4-%d" % i)
    done.set()

t = threading.Thread(target=worker, daemon=True)
t.start()
finished = done.wait(timeout=10.0)
check("T4 不死锁（10s 内跑完 50 次落盘）", finished,
      "→ 超时 = 自锁死")

# --- T5：多线程并发落盘不崩、不串行死等 ---
errs = []

def worker2(n):
    try:
        for i in range(30):
            ndi_out._log("多线程-%d-%d" % (n, i), key="t5-%d-%d" % (n, i))
    except Exception as e:                                     # noqa: BLE001
        errs.append(e)

ths = [threading.Thread(target=worker2, args=(n,)) for n in range(4)]
for th in ths:
    th.start()
for th in ths:
    th.join(timeout=10.0)
check("T5 4 线程×30 次并发落盘无异常", not errs and all(not th.is_alive() for th in ths),
      repr(errs[:2]))

# --- T6：写盘失败不影响（把 LOCALAPPDATA 指到一个"是文件不是目录"的路径）---
saved = os.environ.get("LOCALAPPDATA")
blocker = os.path.join(_tmp, "blocker_file")
with open(blocker, "w", encoding="utf-8") as fh:
    fh.write("x")
os.environ["LOCALAPPDATA"] = blocker      # 里面再 makedirs 会失败
try:
    ndi_out._log("非法路径也不许抛", key="t6")
    check("T6 写盘失败被吞", True)
except Exception as e:                                         # noqa: BLE001
    check("T6 写盘失败被吞", False, repr(e))
os.environ["LOCALAPPDATA"] = saved

# --- T7：心跳 key 按 60 秒桶（同分钟内只一行，跨分钟会换 key）---
src = open(os.path.join(ROOT, "src", "ndi_out.py"), encoding="utf-8").read()
check("T7 心跳 key 已改为时间桶", "key=\"audio-hb-%d\" % bucket" in src,
      "仍是旧 key（恒为 audio-hb-1 → 只打一次）")

print("\n结果：%d 通过 / %d 失败" % (ok, fail))
import shutil
shutil.rmtree(_tmp, ignore_errors=True)
sys.exit(1 if fail else 0)
