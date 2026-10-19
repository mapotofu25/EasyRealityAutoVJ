# -*- coding: utf-8 -*-
"""指纹识别「实时播放」实测：挑 N 首真歌，按运行时的节奏喂窗口，量四个指标。

回答用户 2026-09-28 的问题：
  · 指纹识别能否对齐时间？
  · 实时播放/采集后能否精准匹配？多久匹配成功？
  · 匹配出来的实时时间与真正播放时间对不对得上？
  · 会不会「错对段」（相似段落/重复段落错配）？

方法（与运行时**同一套参数**）：
  · 识别窗口 4.5s、每 0.5s 一次（audio_engine._recognize_loop 的实际节奏）
  · 直接用真歌解码后的音频按时间轴切窗（等价于"实时播放时每 0.5s 取最近 4.5s"，
    只是快进 —— 不影响匹配算法本身的结论）
  · 单窗耗时也记下来（这就是"匹配一次要多久"，运行时每 0.5s 跑一次必须 <0.5s）

用法：
  venv/Scripts/python.exe -u tools/_fp_realtime_test.py [首数=20] [每首测秒数=60]
"""
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

import numpy as np                                        # noqa: E402

from config import Config                                # noqa: E402
from fp import FingerprintDB, SAMPLE_RATE                 # noqa: E402

WIN_SEC = 4.5          # 识别窗口
STEP_SEC = 0.5         # 每 0.5s 一次（与 audio_engine 一致）
ADMIT = 20             # ChargeBarEngine 的入场票门槛（低于它算"噪声票"）


def load_mono(path, max_sec=None):
    import soundfile as sf
    info = sf.info(path)
    n = int(info.frames) if not max_sec else min(int(info.frames), int(max_sec * info.samplerate))
    data, sr = sf.read(path, dtype="float32", always_2d=True, frames=n)
    return np.mean(data, axis=1), sr, float(info.duration)


def main():
    n_songs = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    per_sec = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0

    cfg = Config()
    meta = cfg["music_meta"] or {}
    lib = [p for p in (cfg["music_library"] or [])
           if os.path.isfile(p) and (meta.get(p) or {}).get("song_id")]
    if not lib:
        print("!! 曲库里没有「已扫描出指纹」的歌，先到「音乐曲库」扫描分析。")
        return 1
    import random
    random.seed(20260928)
    pick = random.sample(lib, min(n_songs, len(lib)))

    db_path = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "fingerprints.db")
    if not os.path.exists(db_path):
        print("!! 找不到指纹库:", db_path)
        return 1
    db = FingerprintDB(db_path)

    print("=" * 108)
    print("指纹实时匹配实测：%d 首 ｜ 窗口 %.1fs / 步进 %.1fs ｜ 每首测前 %.0f 秒 + 中段 %.0fs"
          % (len(pick), WIN_SEC, STEP_SEC, per_sec, per_sec / 2))
    print("=" * 108)
    hdr = "%-38s %7s %8s %8s %9s %9s %8s %8s" % (
        "曲目", "真值票", "首中(s)", "稳中(s)", "偏移误差", "偏移P90", "误配", "单窗ms")
    print(hdr)
    print("-" * 108)

    rows = []
    for p in pick:
        sid_true = int((meta.get(p) or {}).get("song_id"))
        name = os.path.basename(p)
        try:
            sig_full, sr, dur = load_mono(p, max_sec=max(per_sec, 5.0) + 5.0)
        except Exception as e:                                # noqa: BLE001
            print("%-38s  读文件失败：%s" % (name[:38], e))
            continue
        w = int(WIN_SEC * sr)
        step = int(STEP_SEC * sr)
        t0 = time.perf_counter()
        first_ok = stable_ok = None
        errs, votes_ok, wrong = [], 0, 0
        n_win = 0
        times = []
        for k in range(0, len(sig_full) - w, step):
            seg = sig_full[k:k + w]
            tt = (k + w) / sr                     # 该窗口末端对应的"真实播放秒"
            t_a = time.perf_counter()
            sid, votes, off = db.match(seg, sr)
            times.append((time.perf_counter() - t_a) * 1000.0)
            n_win += 1
            if sid == sid_true and votes > 0:
                votes_ok = max(votes_ok, votes)
                if first_ok is None:
                    first_ok = tt
                if stable_ok is None and k >= step:           # 连续两窗都正确
                    prev = None
                    # 简化：第一次正确后再等一窗正确即算稳定
                    stable_ok = tt
                # off 是窗口**起点**在原曲上的秒数
                err = off - (tt - WIN_SEC)
                if abs(err) < 3.0:                            # 只统计没离谱的
                    errs.append(err)
            elif sid != -1 and votes >= ADMIT:
                wrong += 1
        ms_avg = sum(times) / max(1, len(times))
        ms_max = max(times) if times else 0.0
        e_avg = (sum(errs) / len(errs)) if errs else 0.0
        e_p90 = (sorted(abs(x) for x in errs)[int(len(errs) * 0.9)] if errs else 0.0)
        rows.append(dict(name=name, votes=votes_ok, first=first_ok, stable=stable_ok,
                         e_avg=e_avg, e_p90=e_p90, wrong=wrong, ms=ms_avg, ms_max=ms_max,
                         wins=n_win, dur=dur))
        print("%-38s %7d %8s %8s %9s %9s %8d %8.1f" % (
            name[:38], votes_ok,
            ("%.2f" % first_ok) if first_ok is not None else "—",
            ("%.2f" % stable_ok) if stable_ok is not None else "—",
            "%+.3fs" % e_avg if errs else "—",
            "%.3fs" % e_p90 if errs else "—",
            wrong, ms_avg))

    print("-" * 108)
    hit = [r for r in rows if r["first"] is not None]
    print("汇总：%d/%d 首识别成功（%.0f%%）" % (len(hit), len(rows),
                                            100.0 * len(hit) / max(1, len(rows))))
    if hit:
        fs = sorted(r["first"] for r in hit)
        print("  首次正确：中位 %.2fs ｜ 最慢 %.2fs（从播放开始算）" % (
            fs[len(fs) // 2], fs[-1]))
        st = sorted(r["stable"] for r in hit if r["stable"] is not None)
        if st:
            print("  稳定正确：中位 %.2fs ｜ 最慢 %.2fs" % (st[len(st) // 2], st[-1]))
        allerr = [abs(r["e_avg"]) for r in hit if r["e_avg"]]
        if allerr:
            allerr.sort()
            print("  时间对齐：|偏移误差| 中位 %.0f ms ｜ P90 %.0f ms ｜ 最差 %.0f ms" % (
                allerr[len(allerr) // 2] * 1000, allerr[int(len(allerr) * 0.9)] * 1000,
                allerr[-1] * 1000))
        v = sorted(r["votes"] for r in hit)
        print("  票数：中位 %d ｜ 最低 %d（入场门槛 %d，位置兜底门槛 40）" % (
            v[len(v) // 2], v[0], ADMIT))
    tw = sum(r["wrong"] for r in rows)
    tt = sum(r["wins"] for r in rows)
    print("  误配（认成别的歌且票数过入场门槛）：%d 窗 / 共 %d 窗 = %.2f%%" % (
        tw, tt, 100.0 * tw / max(1, tt)))
    ms = [r["ms"] for r in rows]
    msx = [r["ms_max"] for r in rows]
    if ms:
        print("  单窗耗时：中位 %.1f ms ｜ 平均 %.1f ms ｜ 最慢 %.0f ms"
              "（运行时每 0.5s 跑一次，必须 <500ms）" % (
                  sorted(ms)[len(ms) // 2], sum(ms) / len(ms), max(msx)))
    # ---- 额外：从歌中段开始播（模拟切歌进中段 / 跳过前奏）看会不会错对段 ----
    print("-" * 108)
    print("附加测试：从每首的「中段 40%%」开始播（模拟切歌进中段），前 15 秒能否对上：")
    ok_mid = tot_mid = 0
    for p in pick[:min(10, len(pick))]:
        sid_true = int((meta.get(p) or {}).get("song_id"))
        try:
            sig_full, sr, dur = load_mono(p)
        except Exception:                                     # noqa: BLE001
            continue
        start = int(len(sig_full) * 0.40)
        seg = sig_full[start:start + int(15.0 * sr)]
        good = False
        for k in range(0, len(seg) - int(WIN_SEC * sr), int(STEP_SEC * sr)):
            sid, votes, off = db.match(seg[k:k + int(WIN_SEC * sr)], sr)
            if sid == sid_true and votes >= ADMIT:
                good = True
                break
        tot_mid += 1
        ok_mid += 1 if good else 0
    print("  中段起播 15 秒内认对：%d/%d" % (ok_mid, tot_mid))

    # ---- 端到端：把每窗结果喂给 ChargeBarEngine（运行时的真实决策机），看它**确认**了什么 ----
    print("-" * 108)
    print("端到端（经过 ChargeBarEngine 漏桶状态机）：它最终「确认」的是不是真值：")
    from charge_engine import ChargeBarEngine, FpResult, MatchEvent
    ok_end = 0
    bad_confirm = []
    first_t = []
    tot_end = 0
    for p in pick:
        sid_true = int((meta.get(p) or {}).get("song_id"))
        try:
            sig_full, sr, dur = load_mono(p, max_sec=per_sec + 5.0)
        except Exception:                                     # noqa: BLE001
            continue
        eng = ChargeBarEngine()
        w = int(WIN_SEC * sr)
        step = int(STEP_SEC * sr)
        confirmed_ok = None
        wrong_sids = set()
        for k in range(0, len(sig_full) - w, step):
            seg = sig_full[k:k + w]
            tt = (k + w) / sr
            sid, votes, off = db.match(seg, sr)
            out = eng.tick(FpResult(matched=(votes > 0), song_id=sid,
                                    aligned_votes=votes, offset_sec=off), tt)
            if out.event == MatchEvent.CONFIRMED and out.cur_song_id >= 0:
                if out.cur_song_id == sid_true:
                    if confirmed_ok is None:
                        confirmed_ok = tt
                else:
                    wrong_sids.add(out.cur_song_id)
        tot_end += 1
        ok_end += 1 if confirmed_ok is not None else 0
        if confirmed_ok is not None:
            first_t.append(confirmed_ok)
        if wrong_sids:
            bad_confirm.append((os.path.basename(p)[:34], len(wrong_sids)))
    print("  确认正确歌：%d/%d 首" % (ok_end, tot_end))
    if first_t:
        first_t.sort()
        print("  从播放到「确认」用时：中位 %.2fs ｜ 最慢 %.2fs" % (
            first_t[len(first_t) // 2], first_t[-1]))
    print("  曾确认过**错误**歌的曲目：%d 首%s" % (
        len(bad_confirm), (" " + str(bad_confirm[:5])) if bad_confirm else "（无）"))
    db.close()
    print("=" * 108)
    return 0


if __name__ == "__main__":
    sys.exit(main())
