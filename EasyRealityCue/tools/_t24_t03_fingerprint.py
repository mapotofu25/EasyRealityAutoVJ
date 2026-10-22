# -*- coding: utf-8 -*-
"""★ T03 独立验证 · 受保护函数源码指纹复验（只读，绝不修改源码）。

目的（team-lead 铁律）：确认 T03 活跃写者**没有改动**
  `_paint_ruler` / `_paint_waveform` / `wave_sample`（既有波形绘制，刚验收过）。

做法：
  1. 读「19:36 快照」= 我接单前对旧版 `timeline_widget.py` 的一次只读抓取
     （cat -n 文本，逐行 `   N→code`）。
  2. 读「当前源码」。
  3. 各抽取三个函数的**逐行源码**，算 md5 / 行数，逐一对比。

用法：
    <Cue>\\venv\\Scripts\\python.exe tools/_t24_t03_fingerprint.py [SNAPSHOT] [CURRENT]
"""
import hashlib
import os
import re
import sys

SNP_DEFAULT = (r"C:\Users\<用户名>\.workbuddy\projects"
               r"\c-Users-Administrator-WorkBuddy-\u81ea\u52a8VJ\u8f6f\u4ef6"
               r"\4ab6e778-417b-403e-94c6-b9013ea43859\subagents"
               r"\agent-4e8cdd1e-8b6e-47e3-b39f-0838ee520753\tool-results"
               r"\call_00_jTi1co2NY1GRvmm58Qnc5151.txt")
CUR_DEFAULT = r"<项目根>\EasyRealityCue\src\ercue\ui\timeline_widget.py"

PROTECTED = ("_paint_ruler", "_paint_waveform", "wave_sample")
_PREFIX = re.compile(r"^\s*\d+\u2192")          # `  123→...`


def _read_snapshot(path):
    """读 cat -n 快照，剥掉 `   N→` 前缀，返回纯源码文本 + 字节偏移映射。"""
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if _PREFIX.match(line):
                line = line.split("\u2192", 1)[1]
            out.append(line)
    return out


def _read_plain(path):
    with open(path, "r", encoding="utf-8") as f:
        return [ln.rstrip("\n") for ln in f]


def _extract(lines, name):
    """抽取 `    def <name>(` 起、到下一个 `^    def `/`^    @`/`^class` 前的源码。"""
    start = None
    for i, ln in enumerate(lines):
        if re.match(r"^    def " + re.escape(name) + r"\(", ln):
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        ln = lines[j]
        if (re.match(r"^    def ", ln) or re.match(r"^    @", ln)
                or ln.startswith("class ") or ln.startswith("def ")):
            end = j
            break
    return lines[start:end]


def _fp(text):
    if text is None:
        return {"present": False, "lines": 0, "md5": None}
    joined = "\n".join(text)
    return {"present": True, "lines": len(text),
            "md5": hashlib.md5(joined.encode("utf-8")).hexdigest()}


def main():
    snp = sys.argv[1] if len(sys.argv) > 1 else SNP_DEFAULT
    cur = sys.argv[2] if len(sys.argv) > 2 else CUR_DEFAULT
    print("=" * 74)
    print("T03 · 受保护函数源码指纹复验")
    print("=" * 74)
    print("快照 :", snp, os.path.isfile(snp))
    print("当前 :", cur, os.path.isfile(cur))
    if not os.path.isfile(snp) or not os.path.isfile(cur):
        print("!! 缺文件，无法比对"); return 2
    old = _read_snapshot(snp)
    new = _read_plain(cur)
    print("快照行数 %d / 当前行数 %d" % (len(old), len(new)))
    print("-" * 74)
    all_same = True
    for nm in PROTECTED:
        a, b = _fp(_extract(old, nm)), _fp(_extract(new, nm))
        same = (a["md5"] == b["md5"] and a["present"] and b["present"])
        all_same &= same
        print("· %-16s 快照[%s %d行]  当前[%s %d行]  %s"
              % (nm, (a["md5"] or "-")[:12], a["lines"],
                 (b["md5"] or "-")[:12], b["lines"],
                 "一致 ✓" if same else "★已变★"))
    print("-" * 74)
    print("结论：受保护三函数 %s" % ("全部一字未改 ✓" if all_same else "有改动，需复看！"))
    print("VERDICT:", "PROTECTED_UNCHANGED" if all_same else "PROTECTED_CHANGED")
    return 0 if all_same else 1


if __name__ == "__main__":
    raise SystemExit(main())
