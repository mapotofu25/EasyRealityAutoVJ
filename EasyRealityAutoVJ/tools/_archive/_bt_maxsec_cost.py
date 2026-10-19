# -*- coding: utf-8 -*-
"""Beat This：**分析窗口长度** → 单首耗时 与 相位正确性。

回答"引入 Beat This 后扫描要慢多少、能不能靠缩短窗口压回来"。

做法：对 maxsec ∈ {180, 90, 60, 30} 各跑一遍 tools/_beat_this_probe.py（21 首），
解析每首的 onnx 耗时与「拍位偏移（拍）」，汇总成表。
判据：|拍位偏移| < 0.25 拍 ⇒ 小节头对齐（正确）；否则错。

用法：venv/Scripts/python.exe -u tools/_bt_maxsec_cost.py [--model 路径] [--n 21]
输出：控制台 + P:/AutoVJ/_bt_maxsec_cost.txt
"""
import argparse
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, "venv", "Scripts", "python.exe")
PROBE = os.path.join(ROOT, "tools", "_beat_this_probe.py")
MODELS = os.path.join(ROOT, "tools", "_beat_this_models")
OUT = os.path.join(ROOT, "_bt_maxsec_cost.txt")

RE_ONNX = re.compile(r"onnx([\d.]+)s")
RE_MEL = re.compile(r"mel([\d.]+)s")
RE_READ = re.compile(r"读([\d.]+)s")
RE_PHASE = re.compile(r"拍位偏移\s*([+-]?[\d.]+)\s*拍")
RE_MS = re.compile(r"绝对中位\s*([\d.]+)\s*ms")
RE_NAME = re.compile(r"^#\s+(.+)$")


def run_one(model, maxsec, n):
    cmd = [PY, "-u", PROBE, "--model", model, "--maxsec", str(maxsec), "--n", str(n)]
    t0 = time.perf_counter()
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    wall = time.perf_counter() - t0
    rows = []
    cur = None
    for line in (p.stdout or "").splitlines():
        m = RE_NAME.match(line.strip())
        if m:
            cur = {"name": m.group(1).strip()}
            rows.append(cur)
            continue
        if cur is None:
            continue
        for key, rx in (("onnx", RE_ONNX), ("mel", RE_MEL), ("read", RE_READ)):
            mm = rx.search(line)
            if mm:
                cur[key] = float(mm.group(1))
        mm = RE_PHASE.search(line)
        if mm and "downbeat" in line:
            cur["phase"] = float(mm.group(1))
        mm = RE_MS.search(line)
        if mm and "downbeat" in line:
            cur["ms"] = float(mm.group(1))
    return rows, wall, p.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=os.path.join(MODELS, "small0_fixed.onnx"))
    ap.add_argument("--n", type=int, default=21)
    ap.add_argument("--secs", default="180,90,60,30")
    a = ap.parse_args()
    secs = [float(x) for x in a.secs.split(",") if x.strip()]

    lines = []

    def out(s=""):
        print(s)
        lines.append(s)

    out("模型: %s（%.1f MB）" % (os.path.basename(a.model),
                                os.path.getsize(a.model) / 1048576))
    out("样本: 前 %d 首 | 窗口档位: %s 秒" % (a.n, secs))
    out("")
    out("%-8s %9s %9s %9s %9s %9s   %s"
        % ("窗口(s)", "onnx均(s)", "mel均(s)", "读均(s)", "合计/首", "总墙钟", "相位对齐"))
    out("-" * 88)

    detail = {}
    for s in secs:
        rows, wall, raw = run_one(a.model, s, a.n)
        ok = sum(1 for r in rows if abs(r.get("phase", 99)) < 0.25)
        tot = len(rows)
        onnx = [r.get("onnx", 0) for r in rows]
        mel = [r.get("mel", 0) for r in rows]
        rd = [r.get("read", 0) for r in rows]
        m_onnx = sum(onnx) / max(tot, 1)
        m_mel = sum(mel) / max(tot, 1)
        m_rd = sum(rd) / max(tot, 1)
        out("%-8.0f %9.2f %9.2f %9.2f %9.2f %8.0fs   %d/%d"
            % (s, m_onnx, m_mel, m_rd, m_onnx + m_mel + m_rd, wall, ok, tot))
        detail[s] = rows

    out("")
    out("=== 逐首相位偏移（拍；|·|<0.25 视为对齐）===")
    names = [r["name"].replace("Rolling Contact - ", "")[:34] for r in detail[secs[0]]]
    hdr = "%-36s" % "曲目" + "".join("%10s" % ("%ds" % s) for s in secs)
    out(hdr)
    out("-" * len(hdr))
    for i, nm in enumerate(names):
        cells = []
        for s in secs:
            r = detail[s][i] if i < len(detail[s]) else {}
            cells.append("%10s" % ("%.3f" % r["phase"] if "phase" in r else "—"))
        out("%-36s%s" % (nm, "".join(cells)))

    out("")
    out("=== 结论 ===")
    base = [r.get("onnx", 0) for r in detail[secs[0]]]
    b = sum(base) / max(len(base), 1)
    out("以最长窗口（%.0fs）为基准，单首 onnx 推理 %.2fs：" % (secs[0], b))
    for s in secs[1:]:
        v = [r.get("onnx", 0) for r in detail[s]]
        mv = sum(v) / max(len(v), 1)
        out("  缩到 %.0fs → %.2fs/首（省 %.0f%%）" % (s, mv, (1 - mv / b) * 100 if b else 0))

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n已写入 %s" % OUT)
    print("OK")


if __name__ == "__main__":
    main()
