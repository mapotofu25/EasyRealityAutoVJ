# -*- coding: utf-8 -*-
r"""本次修复的离线复算：①BPM 一致性校验（C 组误识别）②上一帧 off 连续性 hint（A 组对齐）。

为什么离线跑而不是再用 live harness：
  · live harness 要「重建 exe + 声卡 loopback + 13s 轮间隔」，一轮 C+D+A+B 要十几分钟；
  · 但它**测的东西**（4.5s 窗口 → fp.match → fp_lock_gate）在离线是完全可复现的，
    这里就是拿**真实音频 + 真实指纹库 + 真实 _Analyzer** 跑同一条链，只是把
    「声卡回环」换成「soundfile 直读同一个文件」。
  · 因此这里的数字可直接与 QA 的 `[R]` 统计口径对齐（off 相邻差中位偏差 / 误识曲目数）。

三组（口径照 QA 报告）：
  A：本地 5 首，滑窗 4.5s（hop 1.0s，前 120s），比较 **hint 关 vs 开** 的
     「off 相邻差相对 dt 的中位偏差」（QA 现状 6~7s）与 >30s 跳变次数。
  C：B 站 5 首无指纹歌，滑窗 → 期望 **0 首被误锁**（现状 2 首锁到「西行谣」142BPM）。
  D：librosa 变速 5 首 → 期望 **仍 5/5 认回原曲**（BPM 容差不能误杀 ±8%）。

用法：venv\Scripts\python.exe -u tools/_qa_ab_fix.py
"""
import os
import statistics
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))
sys.path.insert(0, _HERE)

import fp                                                          # noqa: E402
import audio_engine as AE                                          # noqa: E402
import _qa_fp_accuracy as QA                                       # noqa: E402

SR = fp.SAMPLE_RATE          # 与 audio_engine 的 _FP_SR 同一个常量
WIN_SEC = 4.5
HOP_SEC = 1.0
C_DIR = os.path.join(os.environ.get("TEMP", "."), "fp_eval", "c_wav")
C_FILES = ["BV1CEnBzjEAo.wav", "BV1UU4y1Z7N4.wav", "BV1YjJHzSE9H.wav",
           "BV1gu41197Si.wav", "BV1j8ju6aEKk.wav"]
D_DIR = os.path.join(os.environ.get("TEMP", "."), "fp_eval", "speed")
D_SONGS = [
    ("Alcohol x1.03", "Rolling Contact - Alcohol Duper -Hardstyle VIP-__x1.03.wav",
     {463268676}),
    ("Black Warrior x0.97", "Rolling Contact - Black Warrior (2025 Remaster)__x0.97.wav",
     {3725530601, 3814861417}),
    ("Cosmic String x1.05", "Rolling Contact - Cosmic String__x1.05.wav", {4047590539}),
    ("Crescent Horizon x0.95", "Rolling Contact - Crescent Horizon (Rebuild)__x0.95.wav",
     {3006646210, 3558790698}),
    ("Demon's Mischief x1.08", "Rolling Contact - Demon's Mischief__x1.08.wav",
     {2926381684, 1810498096}),
]
A_NAMES = ["Demon's Mischief", "Black Warrior", "Endless & Limitless", "Dust",
           "Cosmic String"]


def load_audio(path, max_sec=120.0):
    import soundfile as sf
    try:
        info = sf.info(path)
        sr = int(info.samplerate)
        frames = int(min(info.frames, max_sec * sr))
        d, _ = sf.read(path, dtype="float32", always_2d=True, frames=frames)
        if d.size == 0:
            return None, 0
        return np.ascontiguousarray(d.mean(axis=1), dtype=np.float32), sr
    except Exception as e:                                        # noqa: BLE001
        print("  !! 读失败 %s: %s" % (os.path.basename(path)[:40], e))
        return None, 0


def live_bpm(path):
    """用生产同一套 `_Analyzer` 跑一遍，取 `state.bpm_display`（生产 BPM 校验用的就是它）。"""
    sig, sr = load_audio(path, 60.0)
    if sig is None:
        return 0.0, 0.0
    an = AE._Analyzer(sr, block=1024)
    st = AE.AudioState()
    blk = max(1, int(sr * 1024 / 48000))
    for k in range(0, len(sig) - blk + 1, blk):
        an.feed(sig[k:k + blk].reshape(-1, 1), st)
    return float(getattr(st, "bpm_display", 0.0) or 0.0), float(getattr(an, "bpm_smooth", 0.0) or 0.0)


def run_track(db, path, hint_on, sid_bpm, accept=None, max_sec=120.0, bpm_gate=False):
    """滑窗跑一首；返回 (off_track, locks)。locks = [(t, sid, off, bpm_rej)]。"""
    sig, sr = load_audio(path, max_sec)
    if sig is None:
        return [], []
    n = int(WIN_SEC * sr)
    hop = int(HOP_SEC * sr)
    off_track, locks = [], []
    prev_off = None
    live = 0.0
    if bpm_gate:
        live, _ = live_bpm(path)
    t = 0.0
    for i in range(0, max(1, len(sig) - n + 1), hop):
        win = sig[i:i + n]
        if len(win) < n // 2:
            break
        hd = hw = None
        if hint_on and prev_off is not None:
            hd = prev_off * fp.SAMPLE_RATE / fp.HOP_SIZE
            hw = AE.FP_PREV_HINT_SEC * fp.SAMPLE_RATE / fp.HOP_SIZE
        sid, votes, off = db.match(win, sr, hint_delta=hd, hint_w=hw)
        bpm_rej = False
        if sid >= 0:
            sv = db.last_song_votes or {}
            sb = db.last_song_best or {}
            tot = int(sv.get(sid, 0))
            stot = max((int(v) for k, v in sv.items() if k != sid), default=0)
            sb2 = max((int(v) for k, v in sb.items() if k != sid), default=0)
            hr = float(getattr(db, "last_hit_rate", 0.0) or 0.0)
            ok = AE.fp_lock_gate(sid, int(votes or 0), tot, sb2, stot, hr)
            if ok and bpm_gate:
                lb = float(sid_bpm.get(int(sid), 0.0) or 0.0)
                if lb > 0 and live > 0:
                    rel = abs(lb - live) / lb
                    okt = rel <= AE.BPM_MATCH_TOL
                    if not okt:
                        ratio = live / lb
                        for kk in AE.BPM_OCTAVE_SET:
                            if abs(ratio - kk) <= AE.BPM_MATCH_TOL:
                                okt = True
                                break
                    if not okt:
                        ok = False
                        bpm_rej = True
            if ok:
                locks.append((t, int(sid), float(off), bpm_rej))
                off_track.append((t, float(off), int(sid)))
        if sid >= 0:
            prev_off = float(off)
        t += HOP_SEC
    return off_track, locks


def a_metric(off_track, exp_sid=None):
    """QA 口径：相邻两窗的 |Δoff − Δt| 中位值；另给 >30s 的跳变次数。"""
    seq = [x for x in off_track if (exp_sid is None or x[2] == exp_sid)]
    devs, jumps = [], 0
    for (ta, oa, _sa), (tb, ob, _sb) in zip(seq, seq[1:]):
        dt = tb - ta
        if dt <= 0:
            continue
        dev = abs((ob - oa) - dt)
        devs.append(dev)
        if dev > 30.0:
            jumps += 1
    if not devs:
        return None, 0, len(seq)
    return statistics.median(devs), jumps, len(seq)


def main():
    print("=" * 78)
    print("离线复算：①BPM 一致性校验 ②上一帧 off 连续性 hint")
    print("=" * 78)
    dbf = os.path.join(_ROOT, "tools", "_qa_fp_accuracy.json")
    db = fp.FingerprintDB(QA.copy_db())
    cfg = QA.load_cfg()
    sid_info, fp_groups, whitelist = QA.build_maps(cfg)
    try:
        db.set_allowed(whitelist)
    except Exception:                                             # noqa: BLE001
        pass
    # sid → 库内 BPM（与生产 ui_main._lib_bpm_of 同口径：music_meta.grid.bpm）
    sid_bpm = {}
    for p, v in (cfg.get("music_meta") or {}).items():
        try:
            sid = int(v.get("song_id"))
            g = v.get("grid") or {}
            gm = v.get("grid_manual") or {}
            b = float(gm.get("bpm_manual") or 0.0) or float(g.get("bpm") or 0.0)
            if b > 0:
                sid_bpm[sid] = b
        except Exception:                                         # noqa: BLE001
            continue
    print("库内 BPM 表 %d 首；白名单 %d 个 sid；FP_PREV_HINT_SEC=%.1f BPM_MATCH_TOL=%.2f"
          % (len(sid_bpm), len(whitelist), AE.FP_PREV_HINT_SEC, AE.BPM_MATCH_TOL))

    # ---------------- A 组 ----------------
    print("\n== A 组：off 相邻差中位偏差（hint 关 → 开）==")
    print("%-24s %10s %10s %8s %8s %6s" % ("曲目", "关:中位偏差", "开:中位偏差",
                                           "关:>30s跳", "开:>30s跳", "窗数"))
    a_rows = []
    for p in (cfg.get("music_library") or []):
        base = os.path.splitext(os.path.basename(p))[0]
        if not any(nm.lower() in base.lower() for nm in A_NAMES):
            continue
        if not os.path.isfile(p):
            continue
        exp_sid = int(__import__("hashlib").md5(p.encode("utf-8")).hexdigest()[:8], 16)
        t_off, _l = run_track(db, p, hint_on=False, sid_bpm=sid_bpm, max_sec=120.0)
        m_off, j_off, n_off = a_metric(t_off, exp_sid)
        t_on, _l2 = run_track(db, p, hint_on=True, sid_bpm=sid_bpm, max_sec=120.0)
        m_on, j_on, n_on = a_metric(t_on, exp_sid)
        print("%-24s %10s %10s %8d %8d %6d"
              % (os.path.basename(p)[:24],
                 "—" if m_off is None else "%.2fs" % m_off,
                 "—" if m_on is None else "%.2fs" % m_on, j_off, j_on, n_on))
        a_rows.append((p, m_off, m_on, j_off, j_on))

    # ---------------- C 组 ----------------
    # ★ 实测事实：本机**没有任何一本库**带「西行谣」(sid 3112469840) 的 BPM
    #   （music_meta 无此 sid；cue.fp_songs 只有 title；imported_meta.db / erlib_scan.eravjlib
    #    里都没有这个 sid）⇒ 生产里 `_lib_bpm_of` 返回 0 ⇒ BPM 校验**无从生效**。
    #   所以下面跑两遍，把「判据本身对不对」与「数据缺不缺」分开：
    #     ①不补数据 = 当前生产实际；②用 QA 报告里的 142BPM 补给该曲 = 数据补齐后。
    def run_c(tag, seed, show_before):
        print("\n== C 组 [%s]：B 站 5 首（库里无指纹 ⇒ 期望 0 首被锁）==" % tag)
        print("%-18s %8s %12s %-22s %s"
              % ("文件", "实时BPM", "锁到sid", "曲名", "结论"))
        bad = 0
        for fn in C_FILES:
            p_ = os.path.join(C_DIR, fn)
            if not os.path.isfile(p_):
                print("  !! 缺文件 %s" % fn)
                continue
            live, _smooth = live_bpm(p_)
            bpm_map = dict(sid_bpm)
            bpm_map.update(seed)
            _o, l_a = run_track(db, p_, hint_on=True, sid_bpm=bpm_map, max_sec=58.0,
                                bpm_gate=True)
            first_a = l_a[0] if l_a else None
            n_rej = 0
            for _x in l_a:
                if _x[3]:
                    n_rej += 1
            if first_a:
                bad += 1
            name = sid_info.get(first_a[1], {}).get("title", "?") if first_a else "—"
            note = "无锁定 ✓" if not first_a else "误锁 ✗"
            print("%-18s %8.1f %12s %-22s %s"
                  % (fn.replace(".wav", ""), live,
                     "—" if not first_a else str(first_a[1]), name, note))
            if show_before:
                _o2, l_b = run_track(db, p_, hint_on=False, sid_bpm=bpm_map, max_sec=58.0)
                first_b = l_b[0] if l_b else None
                if first_b:
                    nm = sid_info.get(first_b[1], {}).get("title", "?")
                    print("      修复前（无 BPM 校验）：锁到 sid=%d『%s』lib_bpm=%s off=%.1f"
                          % (first_b[1], nm, bpm_map.get(first_b[1], "缺"), first_b[2]))
                if first_a and n_rej:
                    print("      修复后：该曲被 BPM 否决 %d 个窗（lib=%.0f live=%.0f）"
                          % (n_rej, float(seed.get(first_a[1], 0) or 0), live))
        print("      小结：误锁 %d/5" % bad)
        return bad

    c_bad_no_seed = run_c("不补数据（当前生产实际）", {}, True)
    c_bad_seeded = run_c("补上 142BPM（数据补齐后）", {3112469840: 142.0}, False)

    # ---------------- D 组 ----------------
    print("\n== D 组：变速 5 首（期望仍认回原曲；BPM 容差不能误杀 ±8%）==")
    d_ok = 0
    d_rej = 0
    for label, fn, accept in D_SONGS:
        p_ = os.path.join(D_DIR, fn)
        if not os.path.isfile(p_):
            print("  !! 缺文件 %s" % fn)
            continue
        _o, l = run_track(db, p_, hint_on=True, sid_bpm=sid_bpm, max_sec=58.0,
                          bpm_gate=True)
        got = set(x[1] for x in l)
        n_rej = 0
        for _x in l:
            if _x[3]:
                n_rej += 1
        d_rej += n_rej
        ok = bool(got & accept)
        d_ok += 1 if ok else 0
        print("  %-22s 锁到 %-16s %s（BPM 否决 %d 窗）"
              % (label, (sorted(got)[:2] if got else "—"),
                 "✓ 认出原曲" if ok else "✗ 未认回", n_rej))

    print("\n---------------- 汇总 ----------------")
    if a_rows:
        m_off = [r[1] for r in a_rows if r[1] is not None]
        m_on = [r[2] for r in a_rows if r[2] is not None]
        if m_off and m_on:
            print("A 组 off 中位偏差：hint 关 = %.2fs（>30s 跳 %d 次）→ hint 开 = %.2fs（>30s 跳 %d 次）"
                  % (statistics.median(m_off), sum(r[3] for r in a_rows),
                     statistics.median(m_on), sum(r[4] for r in a_rows)))
    print("C 组误锁：不补 BPM %d/5 ; 补上 BPM %d/5" % (c_bad_no_seed, c_bad_seeded))
    print("D 组认出原曲：%d/5（BPM 否决 %d 窗 ⇒ 容差没有误杀变速样本）" % (d_ok, d_rej))
    db.close()


if __name__ == "__main__":
    main()
