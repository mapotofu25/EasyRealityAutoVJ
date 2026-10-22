# -*- coding: utf-8 -*-
"""构造「真实 per-song grid」缓存：给 tools/_gridfix4_accept.py 用。

- 优先 VDJ 真值（database.xml，只读）：BPM/beat_len/anchor 可信；
- 无 VDJ 记录的曲目回退本地分析 `compute_grid(allow_local=True)`（慢，但可行）；
- 用户 config 里**没有** grid（已核实）⇒ 不能直接取。

输出 tools/_gridfix4_grids.json：{basename: {bpm, beat_len, anchor, src, conf}}。
素材只读；不碰用户数据。
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, ROOT)
import glob  # noqa: E402

import beatgrid as bg  # noqa: E402

TOFU = r"W:\临时结界相关\2of\tofu"
OUT = os.path.join(ROOT, "tools", "_gridfix4_grids.json")
MAXSEC = int(os.environ.get("GF4_MAXSEC", "120"))


def main():
    vdj = bg.load_vdj()
    files = sorted(glob.glob(os.path.join(TOFU, "*.flac")))
    out = {}
    if os.path.exists(OUT):
        out = json.load(open(OUT, encoding="utf-8"))
    for f in files:
        base = os.path.basename(f)
        if base in out and out[base].get("beat_len"):
            continue
        t0 = time.time()
        # 先试 VDJ（快）：do_scan=False 时若有 VDJ 数据会直接跳过本地 BPM 扫描
        g = None
        try:
            e = bg.vdj_lookup(vdj, f, bg._file_duration(f))
        except Exception:                                              # noqa: BLE001
            e = None
        try:
            if e is not None and e.get("anchor") is not None and e.get("beat_len"):
                g = {"bpm": e["bpm"], "beat_len": e["beat_len"], "anchor": e["anchor"],
                     "src": "vdj", "conf": 0.9}
            else:
                g2 = bg.compute_grid(f, vdj=vdj, maxsec=MAXSEC, allow_local=True)
                if g2:
                    g = {"bpm": g2.get("bpm"), "beat_len": g2.get("beat_len"),
                         "anchor": g2.get("anchor"), "src": g2.get("src"),
                         "conf": g2.get("conf")}
        except Exception as ex:                                        # noqa: BLE001
            print("  !! %s 失败：%r" % (base[:40], ex))
        if g:
            out[base] = g
            print("  %-50s bpm=%7.2f bl=%.4f anchor=%.3f src=%s (%.1fs)"
                  % (base[:50], g["bpm"] or 0, g["beat_len"] or 0,
                     g["anchor"] or 0, g["src"], time.time() - t0), flush=True)
        else:
            print("  -- %s 无网格" % base[:50], flush=True)
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("已写 %s（%d 首）" % (OUT, len(out)))


if __name__ == "__main__":
    main()
