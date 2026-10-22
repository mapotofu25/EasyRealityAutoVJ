# -*- coding: utf-8 -*-
"""批量跑用户标注的 5 首歌：BPM 验证 + 八拍乐句边界。

用法: python tools/_beat_eval.py
结果同时打印并写入 P:\\AutoVJ\\_beat_eval.txt
"""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import beat_analyze as ba          # noqa: E402

DIR = "W:/临时结界相关/2of/tofu"
CASES = [
    ("Rolling Contact - Outburst Oxygen (Sharp Remaster).flac", 128,
     [(0, 30, 64), (60, 91, 64)]),
    ("Rolling Contact - The Lost (135 Extended).flac", 135,
     [(58, 85, 64)]),
    ("Rolling Contact - Crescent Horizon (Rebuild).flac", 145,
     [(80, 106, 64)]),
    ("Rolling Contact - Dust.flac", 128,
     [(15, 29, 32), (75, 105, 64)]),
    ("Rolling Contact - Endless & Limitless.flac", 155,
     [(51, 75, 64)]),
]

OUT = os.path.join(os.path.dirname(HERE), "_beat_eval.txt")


class Tee:
    def __init__(self, *fhs):
        self.fhs = fhs

    def write(self, s):
        for f in self.fhs:
            try:
                f.write(s)
            except Exception:
                pass

    def flush(self):
        for f in self.fhs:
            try:
                f.flush()
            except Exception:
                pass


def main():
    orig = sys.stdout
    fh = open(OUT, "w", encoding="utf-8")
    sys.stdout = Tee(orig, fh)
    results = []
    try:
        for fn, ub, segs in CASES:
            path = os.path.join(DIR, fn)
            mx = max(t1 for _, t1, _ in segs) + 15.0
            print("=" * 78)
            print("# %s    标注 BPM %d" % (fn, ub))
            print("=" * 78)
            if not os.path.isfile(path):
                print("  [!] 文件不存在: %s" % path)
                continue
            try:
                r = ba.segment_report(path, seg=segs, user_bpm=ub,
                                      maxsec=mx, verbose=True)
                results.append((fn, ub, r))
            except Exception:
                traceback.print_exc()
            print()

        # ---------------- 汇总 ----------------
        print("=" * 78)
        print("汇总：BPM 验证")
        print("=" * 78)
        print("%-34s %6s %8s  %-28s" % ("曲目", "标注", "区间推", "算法 Top3 候选"))
        print("-" * 78)
        for fn, ub, r in results:
            short = fn.replace("Rolling Contact - ", "")[:32]
            top3 = "  ".join("%.1f(%.1f)" % (b, v) for b, v in r["bpm_peaks"][:3])
            seg0 = r["segs"][0] if r["segs"] else {}
            print("%-34s %6d %8.2f  %s"
                  % (short, ub, seg0.get("bpm_from_seg", 0), top3))

        print()
        print("=" * 78)
        print("汇总：区间拍数 / 端点贴合 / 八拍")
        print("=" * 78)
        print("%-30s %5s %6s %6s %7s %7s %6s"
              % ("曲目 区间", "标注", "检出", "差", "起点偏", "终点偏", "拍/半拍"))
        print("-" * 78)
        for fn, ub, r in results:
            short = fn.replace("Rolling Contact - ", "")[:22]
            for it in r["segs"]:
                print("%-30s %5d %6d %+6d %+7.2f %+7.2f %6.2f"
                      % ("%s %s~%s" % (short, ba.fmt_t(it["t0"]), ba.fmt_t(it["t1"])),
                         it["beats_claim"], it["n_beats_grid"],
                         it["n_beats_grid"] - it["beats_claim"],
                         it["t0_off_beats"], it["t1_off_beats"],
                         it.get("beat_vs_half", 0)))
        print()
        print("说明：起点偏/终点偏 = 标注时刻距最近拍位的偏移（单位：拍，±0.3 内=贴合）；")
        print("      拍/半拍 = 拍位与半拍位的低频强度比（≈1 四踩；≫1 存在半速歧义）。")
    finally:
        sys.stdout = orig
        fh.close()
        print("全文已写入 %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
