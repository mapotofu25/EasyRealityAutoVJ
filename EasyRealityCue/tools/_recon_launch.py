# -*- coding: utf-8 -*-
"""侦察：启动 EasyRealityCue 并捕获输出（判断能否正常起窗口）。"""
import io
import subprocess
import sys
import time

CUE = r"<项目根>\EasyRealityCue"
PY = CUE + r"\venv\Scripts\python.exe"
LOG = CUE + r"\_shots\recon2.log"

p = subprocess.Popen(
    [PY, r"src\ercue\main.py"],
    cwd=CUE,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
)
print("已启动 PID=%d，等待 15 秒…" % p.pid)
time.sleep(15)

alive = p.poll() is None
print("存活：", alive)
if not alive:
    print("退出码：", p.returncode)

# 尝试读输出（非阻塞：若已退出，read 会立即返回）
try:
    if not alive:
        out = p.stdout.read()
        txt = out.decode("utf-8", "replace") if out else "(无输出)"
        io.open(LOG, "w", encoding="utf-8").write(txt)
        print("=== 输出（前 3000 字）===")
        print(txt[:3000])
    else:
        print("（进程仍在运行 —— 窗口应已出现）")
except Exception as e:                                            # noqa: BLE001
    print("读输出失败：", e)

if alive:
    print("\nPID=%d 保留运行，供后续截图/操作。" % p.pid)
else:
    print("\n进程已退出 —— 这就是「无法正常使用」的直接证据。")
sys.exit(0)
