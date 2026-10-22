"""验证 audio_engine 新增的「采集门控快照（变化触发）」+「首次转发」两处诊断逻辑。

不启动真实引擎（避免 NDI/ASIO DLL 卸载 abort），只把那两段逻辑抽出来照原样跑：
  1. 值变化才写一行（同一值连续多块只写一次）
  2. 值从 False→True 会补写一行（覆盖"启动后才被打开"的场景）
  3. 值从 True→False 也会写一行（覆盖"运行时被回滚"的场景）
  4. 首次转发只写一次

判据：全部用例 PASS 即通过；退出码 0/1。
"""
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="ndi_gate_verify_")
LOG = os.path.join(TMP, "output_health.log")
os.environ["LOCALAPPDATA"] = TMP
# _health_write 走 %LOCALAPPDATA%\AutoVJ\output_health.log
os.makedirs(os.path.join(TMP, "AutoVJ"), exist_ok=True)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import ndi_out  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))


def read_lines():
    try:
        with open(ndi_out._health_path(), encoding="utf-8") as f:
            return [ln for ln in f.read().splitlines() if "采集门控快照" in ln]
    except Exception:
        return []


def clear_log():
    try:
        os.remove(ndi_out._health_path())
    except Exception:
        pass
    ndi_out._LOGGED.clear()


# ---- 复刻采集循环里的那段逻辑（与 audio_engine.py:1619 逐字一致）----
def run_gate_sequence(seq, sr=48000, mono=True):
    """seq = [(ndi_audio_on, ndi_feed_connected), ...]；返回写出的行数"""
    _ndi_diag_last = None
    for on, feed_conn in seq:
        _gate_now = (bool(on), bool(feed_conn))
        if _gate_now != _ndi_diag_last:
            _ndi_diag_last = _gate_now
            ndi_out._health_write(
                "采集门控快照：ndi_audio_on=%r  ndi_feed=%s  sr=%s  mono=%r"
                % (_gate_now[0],
                   "已接" if _gate_now[1] else "None(未接)",
                   sr, bool(mono)))
    return len(read_lines())


# T1：同一值连续 10 块只写 1 行
clear_log()
n = run_gate_sequence([(False, True)] * 10)
check("T1 同值连续 10 块只写 1 行", n == 1, "写出行数=%d" % n)

# T2：False(关) → True(开) 会补写 → 共 2 行
clear_log()
n = run_gate_sequence([(False, True)] * 5 + [(True, True)] * 50)
check("T2 关→开补写一行（共 2 行）", n == 2, "写出行数=%d" % n)

# T3：True → False（运行时被回滚）也会写 → 共 2 行
clear_log()
n = run_gate_sequence([(True, True)] * 5 + [(False, True)] * 5)
check("T3 开→关补写一行（共 2 行）", n == 2, "写出行数=%d" % n)

# T4：ndi_feed 从 None → 已接 也会写
clear_log()
n = run_gate_sequence([(True, False)] * 5 + [(True, True)] * 5)
check("T4 feed None→已接补写一行（共 2 行）", n == 2, "写出行数=%d" % n)

# T5：真正"两个都 True"的稳态场景（用户正常路径）只写 1 行
clear_log()
n = run_gate_sequence([(True, True)] * 100)
check("T5 稳态(都 True)只写 1 行", n == 1, "写出行数=%d" % n)

# T6：首次转发只写一次（复刻 _ndi_audio_loop 的逻辑）
clear_log()
_fwd_logged = False
fwd_written = 0
for _ in range(30):
    if not _fwd_logged:
        _fwd_logged = True
        ndi_out._health_write("NDI 音频转发线程首次取到 PCM（队列非空）：开始投递 feed_audio")
        fwd_written += 1
try:
    with open(ndi_out._health_path(), encoding="utf-8") as f:
        cnt = sum(1 for ln in f.read().splitlines() if "首次取到 PCM" in ln)
except Exception:
    cnt = 0
check("T6 首次转发只写一次", cnt == 1, "写出行数=%d" % cnt)

# T7：确认写出的行里 **两个量都带上**（现场可辨 ①/②/③）
clear_log()
run_gate_sequence([(True, False)])
try:
    with open(ndi_out._health_path(), encoding="utf-8") as f:
        line = [ln for ln in f.read().splitlines() if "采集门控快照" in ln][0]
except Exception:
    line = ""
check("T7 行内含 ndi_audio_on 与 ndi_feed 两个量",
      "ndi_audio_on=" in line and "ndi_feed=" in line and "None(未接)" in line,
      line[:120])

print("=" * 68)
ok_all = True
for name, ok, detail in results:
    print("%-46s %s   %s" % (name, "PASS" if ok else "FAIL", detail))
    ok_all = ok_all and ok
print("=" * 68)
print("结论：%d/%d PASS" % (sum(1 for _, ok, _ in results if ok), len(results)))
sys.exit(0 if ok_all else 1)
