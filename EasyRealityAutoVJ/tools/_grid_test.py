# -*- coding: utf-8 -*-
"""验证 src/beatgrid.py：VDJ 命中 / BPM 一致性 / 八拍相位 / 性能。

用法: python tools/_grid_test.py [目录]
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import beatgrid as bg          # noqa: E402

DIR = sys.argv[1] if len(sys.argv) > 1 else "W:/临时结界相关/2of/tofu"


def main():
    t0 = time.time()
    vdj = bg.load_vdj()
    print("VDJ 库: %d 首（%s）" % (len(vdj), bg.vdj_db_path() or "未找到"))
    files = sorted(f for f in os.listdir(DIR)
                   if f.lower().endswith((".flac", ".mp3", ".wav", ".m4a", ".aiff")))
    print("目录: %s（%d 首）\n" % (DIR, len(files)))
    print("%-50s %7s %7s %6s %6s %5s %7s %6s"
          % ("文件", "VDJ", "自算", "Δ", "锚点", "短语", "对比度", "秒"))
    print("-" * 104)
    hits = agree = 0
    tot_t = 0.0
    phrases = []
    for f in files:
        p = os.path.join(DIR, f)
        t1 = time.time()
        g = bg.compute_grid(p, vdj)
        el = time.time() - t1
        tot_t += el
        if not g:
            print("%-50s   [算不出]" % f[:48])
            continue
        hit = g["src"] == "vdj"
        if hit:
            hits += 1
        d = g.get("vdj_delta", 0.0)
        ok = hit and abs(d) < 0.6
        if ok:
            agree += 1
        phrases.append(g["phrase"])
        print("%-50s %7.2f %7.2f %+6.2f %6.3f %5d %7.2f %6.2f%s"
              % (f.replace("Rolling Contact - ", "")[:48], g.get("vdj_bpm", 0),
                 g["bpm"], d, g["anchor"], g["phrase"], g["contrast"], el,
                 "" if ok else ("  ← 有 VDJ" if hit else "  ← 仅本地")))
    print("-" * 104)
    n = len([1 for f in files])
    print("VDJ 命中 %d/%d　BPM 一致 %d/%d　总耗时 %.1fs（平均 %.2fs/首）"
          % (hits, n, agree, n, tot_t, tot_t / max(1, n)))
    if phrases:
        print("八拍短语偏移分布: 0 → %d 首, 4 → %d 首"
              % (phrases.count(0), phrases.count(4)))
    print("推算：1421 首全算一遍 ≈ %.0f 分钟（单线程）" % (tot_t / max(1, n) * 1421 / 60))

    # 快照：拿第一首演示「演出中要的东西」
    if files:
        g = bg.compute_grid(os.path.join(DIR, files[0]), vdj)
        if g:
            print("\n示例（%s）：BPM %.2f，八拍头序列 = anchor + 8n×%.4f"
                  % (files[0][:40], g["bpm"], g["beat_len"]))
            for off in (0.0, 10.0, 30.0):
                nxt = bg.grid_anchor_after(g, off)
                idx = bg.grid_beat_index(g, off)
                print("   播放到 %5.1fs → 八拍组第 %d 拍，下一个八拍头 %.3fs（还有 %.2fs）"
                      % (off, idx, nxt, nxt - off))
    return 0


if __name__ == "__main__":
    sys.exit(main())
