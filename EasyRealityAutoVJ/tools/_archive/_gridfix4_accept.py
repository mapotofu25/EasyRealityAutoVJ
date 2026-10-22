# -*- coding: utf-8 -*-
"""第四轮验收：H2（闭环自证偏差）/ 真实 BPM 范围 / R3（已锁定后被连续强错窗推走）。

数据：tools/_fix3_hint_seq.json（**真实**逐窗投票直方图，21 首真实 FLAC）。
      tools/_gridfix4_grids.json（**真实 per-song grid**：VDJ 真值 BPM/beat_len/anchor）。
方法：把「生产闭环」原样搬进离线重放 —— 每个识别窗用「引擎上一帧发布的预测位置」当
      hint 调 fp.FingerprintDB._pick 得到匹配结果，再逐帧喂现役 `_beat_pos_grid`。

基线（before）用「当前方法源码」字符串还原：把 H2/R3 两处改动**逆向替换**回改前形态
（未锁定时发布临时 hint + 固定 32 帧窗 + 无 R3）。改后（after）= 现役源码。这样两版
除目标改动外**逐字一致**。

用法： P:\\AutoVJ\\venv\\Scripts\\python.exe tools/_gridfix4_accept.py
"""
import json
import math
import os
import statistics
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import fp as FP          # noqa: E402
import engine as E       # noqa: E402

DT = 1.0 / 60.0
HOP = FP.HOP_SIZE
SR = FP.SAMPLE_RATE
SEQ = os.path.join(ROOT, "tools", "_fix3_hint_seq.json")
GRIDS = os.path.join(ROOT, "tools", "_gridfix4_grids.json")


class FT:
    t = 0.0

    @staticmethod
    def perf_counter():
        return FT.t


E.time = FT


# ------------------------------------------------------------------ 方法构造
def _engine_src():
    # ★ 第五轮后 `src/engine.py` 已重构（未锁定不再进网格模式）；本脚本是**第四轮口径的历史基线**，
    #   故改用冻结快照 tools/_engine_fix4_snapshot.py（= 第四轮结束时的 on-disk 源码）做字符串还原。
    #   当前源码的三方对照见 tools/_gridfix5_accept.py。
    with open(os.path.join(ROOT, "tools", "_engine_fix4_snapshot.py"), encoding="utf-8") as f:
        return f.read()


def _extract_method(src, name):
    key = "    def %s(" % name
    i = src.index(key)
    j = src.find("\n    def ", i + 1)
    seg = src[i:j] if j != -1 else src[i:]
    lines = seg.rstrip().splitlines()
    return "\n".join(l[4:] if l.startswith("    ") else l for l in lines) + "\n"


_UNLOCKED_OLD = "            _publish_grid_hint(self, -1.0)\n            return pos\n"
_UNLOCKED_TMP = "            _publish_grid_hint(self, anchor + pos * bl - 4.5 * r)\n            return pos\n"

_LOCKED_NEW = (
    "    if clock.hint_hold() > 0:\n"
    "        _publish_grid_hint(self, -1.0)\n"
    "    else:\n"
    "        _publish_grid_hint(self, anchor + pos * bl - 4.5 * r,\n"
    "                           hint_window_frames(bl))\n"
    "    return pos\n")
_LOCKED_R3_FIXEDW = (
    "    if clock.hint_hold() > 0:\n"
    "        _publish_grid_hint(self, -1.0)\n"
    "    else:\n"
    "        _publish_grid_hint(self, anchor + pos * bl - 4.5 * r)\n"
    "    return pos\n")
_LOCKED_NOR3_BEATS = (
    "    _publish_grid_hint(self, anchor + pos * bl - 4.5 * r,\n"
    "                       hint_window_frames(bl))\n"
    "    return pos\n")
_LOCKED_NOR3_FIXEDW = (
    "    _publish_grid_hint(self, anchor + pos * bl - 4.5 * r)\n"
    "    return pos\n")


def build_method(unlocked_temp, r3, beats_window):
    """返回一个 `_beat_pos_grid` 函数。

    unlocked_temp: 未锁定阶段是否发布临时 hint（before=True / after=False）。
    r3:            是否启用「>jump_max 重锚后暂停 hint」（after=True）。
    beats_window:  锁定时窗是否按拍换算（after=True）；False ⇒ 固定 HINT_W 帧（before）。
    """
    body = _extract_method(_engine_src(), "_beat_pos_grid")
    if unlocked_temp:
        assert body.count(_UNLOCKED_OLD) == 1, "unlocked 目标不唯一"
        body = body.replace(_UNLOCKED_OLD, _UNLOCKED_TMP)
    # 锁定分支（当前源码为 r3+beats，逆向替换为所需形态）
    assert body.count(_LOCKED_NEW) == 1, "locked 目标不唯一"
    if r3 and beats_window:
        repl = _LOCKED_NEW
    elif r3 and not beats_window:
        repl = _LOCKED_R3_FIXEDW
    elif (not r3) and beats_window:
        repl = _LOCKED_NOR3_BEATS
    else:
        repl = _LOCKED_NOR3_FIXEDW
    body = body.replace(_LOCKED_NEW, repl)
    ns = {}
    exec("import math\n" + body.replace(
        "def _beat_pos_grid(", "def _beat_pos_grid(", 1), ns)
    ns["time"] = E.time
    ns["_publish_grid_hint"] = E._publish_grid_hint
    ns["hint_window_frames"] = FP.hint_window_frames
    # exec 的 globals 里没有上面注入的名字会 NameError，重新绑定
    fn = ns["_beat_pos_grid"]
    fn.__globals__["time"] = E.time
    fn.__globals__["_publish_grid_hint"] = E._publish_grid_hint
    fn.__globals__["hint_window_frames"] = FP.hint_window_frames
    return fn


POLICY_OLD = build_method(True, False, False)     # 改前（H2 未修 + 固定帧窗 + 无 R3）
POLICY_NEW = build_method(False, True, True)      # 现役（H2 修 + 节拍窗 + R3）
POLICY_NOR3 = build_method(False, False, True)    # 现役但不含 R3（隔离 R3 效果）
POLICY_FIXEDW = build_method(False, True, False)  # 现役但固定帧窗（跨 BPM 对照）


# ------------------------------------------------------------------ 重放
def make_self(method):
    s = types.SimpleNamespace()
    s._pos_ref = None
    s.switch_pos = 0.0
    s._next_due = None
    s._due_off = 0.0
    s._bar_off_user = None
    s.mode = "normal"
    s.cfg = {"auto": {"beat_grid": True}, "mode": {"sensitivity": 1}}
    s._grid_clock = E.GridClock()
    s._grid_active_sid = None
    s._grid_coord_delta = 0.0
    s._grid_last_pos = 0.0
    s._grid_last_out = None
    s._grid_pending = None
    s._grid_seen = None
    s._grid_prov = None
    s.audio = types.SimpleNamespace(grid_pred_orig=-1.0, grid_pred_w=None)
    s._beat_pos_grid = types.MethodType(method, s)
    s.GRID_MIN_VOTES = E.AutoVJEngine.GRID_MIN_VOTES
    s.GRID_STRONG_VOTES = E.AutoVJEngine.GRID_STRONG_VOTES
    s.GRID_CONSIST_LO = E.AutoVJEngine.GRID_CONSIST_LO
    s.GRID_CONSIST_HI = E.AutoVJEngine.GRID_CONSIST_HI
    s.GRID_PROV_TOL_BEATS = 4.0   # 快照里的常量（第五轮已从源码删除）
    return s


def snap(sid, off, votes, ref_t):
    return {"bpm": 128.0, "beat_active": True, "beat_count": 0, "beat_phase": 0.0,
            "downbeat_off": 0.0, "energy": 0.5, "energy_trend": 0.0,
            "recognized_song_id": sid, "recognized_offset": off, "recognized_rate": 1.0,
            "recognized_t": ref_t, "recognized_votes": votes}


def replay(method, wins, exp, bl, anchor, start_t=0.0):
    """闭环重放。返回 (errs:[(t,err)], max|err|)。"""
    s = make_self(method)
    win_by_start = {w["t"]: w for w in wins}
    t_end = wins[-1]["t"]
    t = float(start_t)
    cur = None
    cur_ref = 1000.0 + t
    errs = []
    while t <= t_end + DT:
        w = win_by_start.get(round(t, 3))
        if w is not None:
            hint_f = None
            hint_w = None
            p = s.audio.grid_pred_orig
            if p is not None and p >= 0.0:
                hint_f = p * SR / HOP
                pw = s.audio.grid_pred_w
                if pw is not None and float(pw) > 0.0:
                    hint_w = float(pw)
            hist = {(c[0], c[1]): c[2] for c in w["cand"]}
            picked = FP.FingerprintDB._pick(hist, hint_f, hint_w)
            if picked is None:
                cur = None
            else:
                (sid, delta), votes = picked
                cur = {"sid": int(sid), "off": delta * HOP / SR, "votes": int(votes)}
            cur_ref = 1000.0 + t
        FT.t = 1000.0 + t
        grid_on = (cur is not None and cur["sid"] == exp and cur["votes"] > 0)
        if grid_on:
            off_end = float(cur["off"]) + 4.5
            gs = ({"beat_len": bl, "anchor": anchor}, bl, anchor, off_end, 1.0, exp)
            pos = s._beat_pos_grid(snap(exp, off_end, int(cur["votes"]), cur_ref), gs)
            errs.append((t, pos - (t + 4.5 - anchor) / bl))
        else:
            s.audio.grid_pred_orig = -1.0
            s.audio.grid_pred_w = None
        t += DT
    mx = max((abs(e) for _, e in errs), default=0.0)
    return errs, mx


def bad_dur(errs, thr=1.0):
    return sum(DT for _, e in errs if abs(e) > thr)


# ------------------------------------------------------------------ 数据
def load():
    seq = json.load(open(SEQ, encoding="utf-8"))
    grids = json.load(open(GRIDS, encoding="utf-8"))
    out = []
    for d in seq:
        if int(d.get("exp", -1)) < 0:
            continue
        g = grids.get(d["song"])
        if not g or not g.get("beat_len"):
            continue
        out.append({"song": d["song"], "exp": int(d["exp"]), "wins": d["wins"],
                    "bl": float(g["beat_len"]), "anchor": float(g["anchor"] or 0.0),
                    "bpm": float(g["bpm"] or 0.0)})
    return out


def secA_H2_scan(data, starts):
    """A) H2：全曲 × 全起点扫描，before vs after。"""
    print("\n" + "=" * 78)
    print("A) H2 全曲×全起点扫描（热 cue 起播）：before vs after")
    print("   起点=%s（共 %d 首 × %d 起点 = %d 例）" % (starts, len(data), len(starts),
                                                       len(data) * len(starts)))
    print("=" * 78)
    rows = []
    for d in data:
        for st in starts:
            _, mx_o = replay(POLICY_OLD, d["wins"], d["exp"], d["bl"], d["anchor"], st)
            e_n, mx_n = replay(POLICY_NEW, d["wins"], d["exp"], d["bl"], d["anchor"], st)
            bd_o = bad_dur(replay(POLICY_OLD, d["wins"], d["exp"], d["bl"], d["anchor"], st)[0])
            bd_n = bad_dur(e_n)
            rows.append((d["song"], st, mx_o, mx_n, bd_o, bd_n))
    # 汇总
    stretched = [r for r in rows if r[4] - r[5] > 2.0]       # 误锁时长明显变长
    worst_imp = max((r[4] - r[5] for r in rows), default=0.0)
    print("  before：误锁(>1拍)时长 max=%.1fs；后 20 例被拉长" % max(r[4] for r in rows))
    print("  after ：误锁(>1拍)时长 max=%.1fs" % max(r[5] for r in rows))
    print("  before max|err| 全局 max = %.1f 拍；after 全局 max = %.1f 拍"
          % (max(r[2] for r in rows), max(r[3] for r in rows)))
    print("  ★ 被拉长例数（before 误锁时长 − after > 2s）= %d / %d；最长改善 = %.1fs"
          % (len(stretched), len(rows), worst_imp))
    # 逐首最差 5 例
    rows.sort(key=lambda r: -(r[4] - r[5]))
    print("  改善最大的前 6 例（song / 起点 / before max|err| / after max|err| / "
          "before 误锁 s / after 误锁 s）：")
    for r in rows[:6]:
        print("    %-40s t=%3.0f  mx %8.1f→%6.1f  dur %7.1f→%5.1f"
              % (r[0][:40], r[1], r[2], r[3], r[4], r[5]))
    return rows


def secB_norm_start(data):
    print("\n" + "=" * 78)
    print("B) H2 不退化：从 t=0 正常起播（before vs after，逐首）")
    print("=" * 78)
    mo = mn = 0.0
    ro = rn = 0.0
    rows = []
    for d in data:
        eo, a = replay(POLICY_OLD, d["wins"], d["exp"], d["bl"], d["anchor"], 0.0)
        en, b = replay(POLICY_NEW, d["wins"], d["exp"], d["bl"], d["anchor"], 0.0)
        mo = max(mo, a)
        mn = max(mn, b)
        ro += bad_dur(eo)
        rn += bad_dur(en)
        rows.append((d["song"], a, b, bad_dur(eo), bad_dur(en)))
    rows.sort(key=lambda r: -(r[4] - r[3]))
    for nm, a, b, do, dn in rows[:6]:
        if abs(do - dn) > 1.0 or a > 1.0 or b > 1.0:
            print("    %-42s max|err| %6.2f→%6.2f  误锁 %5.1f→%5.1f s" % (nm[:42], a, b, do, dn))
    print("  before：全局 max|err|=%.2f 拍，总误锁=%.0fs" % (mo, ro))
    print("  after ：全局 max|err|=%.2f 拍，总误锁=%.0fs" % (mn, rn))
    print("  ⚠ 旧阈值 3.78 拍是在缺省 beat_len=0.5s 下测的；用**真实 per-song beat_len** 后，"
          "同一物理误差换算出不同拍数 ⇒ 该阈值不再可比，应按 before↔after 对比判退化。")


def secC_bpm(data, starts):
    print("\n" + "=" * 78)
    print("C) 真实 per-song grid（VDJ 真值，128~155BPM）+ 跨 BPM 合成扫描（90~200BPM）")
    print("=" * 78)
    # C1) 真实 per-song grid（每首用自身 VDJ beat_len）—— after 策略，**从 t=0 正常起播**
    #     （对应验收 #3：真实 BPM 范围、max|err|、|err|<1 占比）
    print("  C1) 真实 per-song grid（after 策略，t=0 正常起播）")
    allmx = 0.0
    ratios = []
    ratios_ss = []          # 排除前 5s（锁定获取期）后的占比
    for d in data:
        errs, mx = replay(POLICY_NEW, d["wins"], d["exp"], d["bl"], d["anchor"], 0.0)
        if not errs:
            continue
        good = sum(1 for _, e in errs if abs(e) < 1.0) / len(errs)
        gss = sum(1 for t, e in errs if t >= 5.0 and abs(e) < 1.0) / max(
            1, sum(1 for t, _ in errs if t >= 5.0))
        ratios.append(good)
        ratios_ss.append(gss)
        allmx = max(allmx, mx)
        print("    %-42s bpm=%6.1f  max|err|=%6.2f  |err|<1 %5.1f%%（稳态 %5.1f%%）"
              % (d["song"][:42], d["bpm"], mx, 100 * good, 100 * gss))
    print("    合计：max|err| = %.2f 拍（<8 断言 %s）" % (allmx, "PASS" if allmx < 8.0 else "FAIL"))
    print("    |err|<1 占比：中位 %.1f%% / 最小 %.1f%%（稳态中位 %.1f%% / 最小 %.1f%%）"
          % (100 * statistics.median(ratios), 100 * min(ratios),
             100 * statistics.median(ratios_ss), 100 * min(ratios_ss)))

    # C2) 跨 BPM 合成：真实直方图 × 合成 beat_len，跨起点，比较 固定帧窗 vs 节拍窗
    cst = [0, 6, 12, 18, 24]
    print("\n  C2) 跨 BPM 合成扫描（真实直方图 × 合成 beat_len；起点 %s）" % cst)
    print("      BPM   beat_len   固定窗(HINT_W=32帧)       节拍窗(HINT_W_BEATS=%.1f拍)" % FP.HINT_W_BEATS)
    print("                           总误锁s  最长s  #>8拍    总误锁s  最长s  #>8拍")
    for bpm in (90, 105, 120, 132, 150, 175, 200):
        bl = 60.0 / bpm
        row = {}
        for name, pol in (("fixed", POLICY_FIXEDW), ("beats", POLICY_NEW)):
            tot = 0.0
            mx = 0.0
            n8 = 0
            for d in data:
                for st in cst:
                    errs, m = replay(pol, d["wins"], d["exp"], bl, d["anchor"], st)
                    tot += bad_dur(errs)
                    mx = max(mx, m)
                    if m >= 8.0:
                        n8 += 1
            row[name] = (tot, mx, n8)
        print("      %3d   %.4f      %8.0f %6.1f  %3d      %8.0f %6.1f  %3d"
              % (bpm, bl, row["fixed"][0], row["fixed"][1], row["fixed"][2],
                 row["beats"][0], row["beats"][1], row["beats"][2]))

    # C3) HINT_W_BEATS 取值扫描（真实 per-song grid，跨起点）
    print("\n  C3) HINT_W_BEATS 取值扫描（真实 per-song grid，起点 %s）" % cst)
    print("      W(拍)   总误锁s  最长s  #(>5s)")
    base = FP.HINT_W_BEATS
    for w in (1.5, 2.0, 2.5, 3.0, 3.5):
        FP.HINT_W_BEATS = w
        tot = 0.0
        mx = 0.0
        nb = 0
        for d in data:
            for st in cst:
                errs, m = replay(POLICY_NEW, d["wins"], d["exp"], d["bl"], d["anchor"], st)
                bd = bad_dur(errs)
                tot += bd
                mx = max(mx, bd)
                if bd > 5.0:
                    nb += 1
        print("      %.1f    %8.0f %6.1f  %3d%s" % (w, tot, mx, nb,
                                                    "   ←现役" if abs(w - base) < 1e-9 else ""))
    FP.HINT_W_BEATS = base


def secD_R3(data, starts):
    print("\n" + "=" * 78)
    print("D) R3：现役(with R3) vs 现役(no R3) —— 残余风险量化")
    print("=" * 78)
    diff = []
    mx_r3 = mx_n3 = 0.0
    bd_r3 = bd_n3 = 0.0
    for d in data:
        for st in starts:
            e1, m1 = replay(POLICY_NEW, d["wins"], d["exp"], d["bl"], d["anchor"], st)
            e2, m2 = replay(POLICY_NOR3, d["wins"], d["exp"], d["bl"], d["anchor"], st)
            mx_r3 = max(mx_r3, m1)
            mx_n3 = max(mx_n3, m2)
            bd_r3 = max(bd_r3, bad_dur(e1))
            bd_n3 = max(bd_n3, bad_dur(e2))
            if abs(m1 - m2) > 0.5 or abs(bad_dur(e1) - bad_dur(e2)) > 1.0:
                diff.append((d["song"], st, m2, m1, bad_dur(e2), bad_dur(e1)))
    print("  no-R3：max|err| 全局 max = %.1f 拍；误锁时长 max = %.1fs" % (mx_n3, bd_n3))
    print("  with-R3：max|err| 全局 max = %.1f 拍；误锁时长 max = %.1fs" % (mx_r3, bd_r3))
    print("  两者有差异的样例数 = %d / %d" % (len(diff), len(data) * len(starts)))
    for r in sorted(diff, key=lambda x: -(x[4] - x[5]))[:6]:
        print("    %-40s t=%3.0f  max|err| noR3=%7.1f withR3=%7.1f  dur %6.1f→%5.1f"
              % (r[0][:40], r[1], r[2], r[3], r[4], r[5]))
    return mx_r3, mx_n3, bd_r3, bd_n3


def main():
    data = load()
    bpm_lo = min(d["bpm"] for d in data)
    bpm_hi = max(d["bpm"] for d in data)
    print("载入 %d 首（真实直方图 + 真实 per-song grid）；真实 BPM 范围 %.1f~%.1f"
          % (len(data), bpm_lo, bpm_hi))
    starts = [0, 3, 6, 9, 12, 15, 18, 21, 24, 27, 30]
    secA_H2_scan(data, starts)
    secB_norm_start(data)
    secC_bpm(data, starts)
    secD_R3(data, starts)


if __name__ == "__main__":
    main()
