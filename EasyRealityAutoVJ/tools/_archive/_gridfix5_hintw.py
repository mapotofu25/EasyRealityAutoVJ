# -*- coding: utf-8 -*-
"""第五轮 ★附带专项：上一轮报「合成 90BPM 时节拍窗反而比固定 32 帧差（803 vs 489）」的根因。

口径与 tools/_gridfix4_accept.py 的 C2 相同：**真实逐窗投票直方图 × 合成的 beat_len**，
跨起点闭环重放（after 策略：H2 + R3）。比较两类 hint 窗：
  · 固定帧窗：FP.HINT_W（帧）
  · 节拍窗  ：FP.HINT_W_BEATS（拍）→ hint_window_frames(bl) 换算成帧

⚠ 真实素材只有 128~155 BPM；90 / 105 / 175 / 200 是**合成**（把真实直方图按合成 beat_len 折算）。

判定「窗是否过窄漏掉正确候选」：每个识别窗里，取「与引擎发布 hint 最近的正确候选
（sid==exp、delta 最接近）；若其 |delta−hint| > 窗半宽 ⇒ 该窗**漏掉了正确候选**。

用法： P:\\AutoVJ\\venv\\Scripts\\python.exe tools/_gridfix5_hintw.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import fp as FP          # noqa: E402
import _gridfix5_accept as A   # noqa: E402

DT = A.DT
HOP = A.HOP
SR = A.SR
# 固定帧窗策略：发 hint 时**不带 w** ⇒ 识别侧用 FP.HINT_W
POLICY_FIXED = A.build_method(False, True, False)


def replay(method, wins, exp, bl, anchor, start_t=0.0, use_beats=True, w=32.0):
    """与 A.replay_direct 同口径，但按需切换 hint 窗值；额外统计「漏掉正确候选」率。"""
    if use_beats:
        FP.HINT_W_BEATS = float(w)
    else:
        FP.HINT_W = int(round(w))
    s = A.make_direct(method)
    win_by_start = {ww["t"]: ww for ww in wins}
    t_end = wins[-1]["t"]
    t = float(start_t)
    cur = None
    cur_ref = 1000.0 + t
    errs = []
    miss = 0          # 窗内不存在「与 hint 足够近的正确候选」
    nwin = 0
    while t <= t_end + DT:
        ww = win_by_start.get(round(t, 3))
        if ww is not None:
            cur = A._pick_win(s, ww)
            cur_ref = 1000.0 + t
            # —— 漏掉正确候选统计（本窗、按当前 hint 与窗宽）——
            nwin += 1
            hint_o = s.audio.grid_pred_orig
            if hint_o is not None and hint_o >= 0.0:
                hint_f = hint_o * SR / HOP
                halfw = (FP.hint_window_frames(bl) if use_beats else float(FP.HINT_W))
                dmin = None
                for (csid, cdelta, cvotes) in ww["cand"]:
                    if int(csid) == exp:
                        dd = abs(cdelta - hint_f)
                        if dmin is None or dd < dmin:
                            dmin = dd
                if dmin is not None and dmin > halfw:
                    miss += 1
        A.FT.t = 1000.0 + t
        if cur is not None and cur["sid"] == exp and cur["votes"] > 0:
            off_end = float(cur["off"]) + 4.5
            gs = ({"beat_len": bl, "anchor": anchor}, bl, anchor, off_end, 1.0, exp)
            pos = method(s, A._snap_direct(exp, off_end, int(cur["votes"]), cur_ref), gs)
            errs.append((t, pos - (t + 4.5 - anchor) / bl))
        else:
            s.audio.grid_pred_orig = -1.0
            s.audio.grid_pred_w = None
        t += DT
    bd = A.bad_dur(errs)
    mx = max((abs(e) for _, e in errs), default=0.0)
    return bd, mx, miss, nwin


def agg(data, bl_sec, method, use_beats, w, starts):
    tot = 0.0
    mx = 0.0
    n5 = 0
    miss = 0
    nwin = 0
    for d in data:
        for st in starts:
            bd, m, mi, nw = replay(method, d["wins"], d["exp"], bl_sec, d["anchor"], st,
                                   use_beats, w)
            tot += bd
            mx = max(mx, bd)
            if bd > 5.0:
                n5 += 1
            miss += mi
            nwin += nw
    return tot, mx, n5, (100.0 * miss / max(1, nwin))


def main():
    data = A.load()
    starts = [0, 12, 24]
    print("=" * 92)
    print("90BPM 窗口专项（真实直方图 × 合成 beat_len；起点 %s；20 首 = %d 例）"
          % (starts, len(data) * len(starts)))
    print("⚠ 90/105/175/200 为合成 BPM；128~155 为真实素材 BPM。")
    print("=" * 92)

    for bpm in (90, 132, 200):
        bl = 60.0 / bpm
        fr_beats_eq = 2.5 * bl * SR / HOP
        print("\n—— 合成 BPM=%d（beat_len=%.4fs；2.5 拍 ≈ %.1f 帧；固定窗口对照 32 帧≈%.2f 拍）"
              % (bpm, bl, fr_beats_eq, 32.0 * HOP / SR / bl))

        print("   固定帧窗： W(帧)   总误锁s  最长s  #>5s   漏正确候选%")
        for w in (20, 28, 32, 40, 48):
            tot, mx, n5, missp = agg(data, bl, POLICY_FIXED, False, w, starts)
            print("            %5d  %8.0f %7.1f %5d    %6.2f%%" % (w, tot, mx, n5, missp))

        print("   节拍窗  ： W(拍)   总误锁s  最长s  #>5s   漏正确候选%   (≈帧)")
        for w in (1.0, 1.5, 2.0, 2.5, 3.0, 4.0):
            tot, mx, n5, missp = agg(data, bl, A.POLICY_AFTER, True, w, starts)
            print("            %5.1f  %8.0f %7.1f %5d    %6.2f%%   %.1f"
                  % (w, tot, mx, n5, missp, w * bl * SR / HOP))


if __name__ == "__main__":
    main()
