# -*- coding: utf-8 -*-
"""第五轮验收：**未锁定阶段完全不进入网格模式**（before / after(H2) / now 三方对照）。

口径与 tools/_gridfix4_accept.py **完全一致**（真实逐窗投票直方图 + 真实 per-song grid，
同步闭环重放）：每个识别窗用「引擎上一帧发布的预测位置」当 hint 调 fp._pick 得匹配结果，
再逐帧喂拍位时钟。

「误锁」定义 = 引擎产出**网格坐标拍位**且 |err|>1 拍 的累计时长：
  · before：未锁定也返回网格**临时**拍位（且发布临时 hint）+ 固定 32 帧窗 + 无 R3。
  · after ：未锁定返回网格**临时**拍位但**不发 hint**（H2）+ 节拍窗 + R3。   ← 上一轮 on-disk
  · now   ：**未锁定完全不进入网格模式**（走原有拍钟，不计误锁）+ 节拍窗 + R3。← 本轮
            （_grid_state 在未锁定时返回 None，见 src/engine.py）

before/after 用 tools/_engine_fix4_snapshot.py（改动前 on-disk 源码）字符串还原，除目标改动外
逐字一致；now 直接驱动 src/engine.py 现役 `_beat_pos`（含 _grid_observe 喂时钟 / 锁定判定）。

用法： P:\\AutoVJ\\venv\\Scripts\\python.exe tools/_gridfix5_accept.py
"""
import importlib.util
import json
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
SNAPSHOT = os.path.join(ROOT, "tools", "_engine_fix4_snapshot.py")


class FT:
    t = 0.0

    @staticmethod
    def perf_counter():
        return FT.t


E.time = FT


def _load_snapshot():
    spec = importlib.util.spec_from_file_location("engine_fix4", SNAPSHOT)
    m = importlib.util.module_from_spec(spec)
    sys.modules["engine_fix4"] = m
    spec.loader.exec_module(m)
    m.time = FT
    return m


E4 = _load_snapshot()


# ------------------------------------------------------------------ 构造 before/after
def _extract_method(src, name):
    key = "    def %s(" % name
    i = src.index(key)
    j = src.find("\n    def ", i + 1)
    seg = src[i:j] if j != -1 else src[i:]
    lines = seg.rstrip().splitlines()
    return "\n".join(l[4:] if l.startswith("    ") else l for l in lines) + "\n"


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


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
_LOCKED_NOR3_FIXEDW = (
    "    _publish_grid_hint(self, anchor + pos * bl - 4.5 * r)\n"
    "    return pos\n")


def build_method(unlocked_temp, r3, beats_window):
    """从**改动前快照**源码还原一个 `_beat_pos_grid` 函数（行为可切换）。

    unlocked_temp: 未锁定阶段是否发布临时 hint（before=True / after(H2)=False）。
    r3:            是否启用「>jump_max 重锚后暂停 hint」（after/now=True）。
    beats_window:  锁定时 hint 窗是否按拍换算（after/now=True）；False ⇒ 固定 HINT_W 帧（before）。
    """
    body = _extract_method(_read(SNAPSHOT), "_beat_pos_grid")
    if unlocked_temp:
        assert body.count(_UNLOCKED_OLD) == 1, "unlocked 目标不唯一"
        body = body.replace(_UNLOCKED_OLD, _UNLOCKED_TMP)
    assert body.count(_LOCKED_NEW) == 1, "locked 目标不唯一"
    if r3 and beats_window:
        repl = _LOCKED_NEW
    elif r3 and not beats_window:
        repl = _LOCKED_R3_FIXEDW
    else:
        repl = _LOCKED_NOR3_FIXEDW
    body = body.replace(_LOCKED_NEW, repl)
    ns = {}
    exec("import math\n" + body, ns)
    fn = ns["_beat_pos_grid"]
    fn.__globals__["time"] = E.time
    fn.__globals__["_publish_grid_hint"] = E4._publish_grid_hint
    fn.__globals__["hint_window_frames"] = FP.hint_window_frames
    return fn


POLICY_BEFORE = build_method(True, False, False)    # 未锁定临时拍位 + 临时 hint + 固定帧窗 + 无 R3
POLICY_AFTER = build_method(False, True, True)      # H2 + 节拍窗 + R3（上一轮 on-disk）
POLICY_NOR3 = build_method(False, False, True)      # after 但不含 R3（隔离 R3）


# ------------------------------------------------------------------ 重放基础设施
def _base_self(cfg=None):
    s = types.SimpleNamespace()
    s._pos_ref = None
    s.switch_pos = 0.0
    s._next_due = None
    s._due_off = 0.0
    s._bar_off_user = None
    s.mode = "normal"
    s.cfg = cfg or {"auto": {"beat_grid": True}, "mode": {"sensitivity": 1}}
    s._grid_clock = E.GridClock()
    s._grid_active_sid = None
    s._grid_coord_delta = 0.0
    s._grid_last_pos = 0.0
    s._grid_last_out = None
    s._grid_pending = None
    s._grid_seen = None
    s._song_grid_lookup = {}
    s.audio = types.SimpleNamespace(grid_pred_orig=-1.0, grid_pred_w=None)
    s.GRID_MIN_VOTES = E.AutoVJEngine.GRID_MIN_VOTES
    s.GRID_STRONG_VOTES = E.AutoVJEngine.GRID_STRONG_VOTES
    s.GRID_CONSIST_LO = E.AutoVJEngine.GRID_CONSIST_LO
    s.GRID_CONSIST_HI = E.AutoVJEngine.GRID_CONSIST_HI
    return s


def make_direct(method):
    """before/after：直接驱动 `_beat_pos_grid`（旧 API，内部自带未锁定分支）。"""
    s = _base_self()
    s._beat_pos_grid = types.MethodType(method, s)
    return s


def make_now(exp, bl, anchor):
    """now：驱动现役 `_beat_pos`（经 `_grid_state`/`_grid_observe` 门控）。"""
    s = _base_self()
    s._song_grid_lookup = {exp: {"beat_len": bl, "anchor": anchor, "bpm": 120.0, "src": "vdj"}}
    for nm in ("_beat_pos", "_beat_pos_grid", "_grid_observe", "_grid_state"):
        setattr(s, nm, types.MethodType(getattr(E.AutoVJEngine, nm), s))
    return s


def _snap_direct(exp, off_end, votes, ref_t):
    return {"bpm": 128.0, "beat_active": True, "beat_count": 0, "beat_phase": 0.0,
            "downbeat_off": 0.0, "energy": 0.5, "energy_trend": 0.0,
            "recognized_song_id": exp, "recognized_offset": off_end, "recognized_rate": 1.0,
            "recognized_t": ref_t, "recognized_votes": votes}


def _snap_now(sid, off_end, votes, ref_t):
    return {"bpm": 128.0, "beat_active": True, "beat_count": 0, "beat_phase": 0.0,
            "downbeat_off": 0.0, "energy": 0.5, "energy_trend": 0.0,
            "recognized_song_id": sid, "recognized_offset": off_end, "recognized_rate": 1.0,
            "recognized_t": ref_t, "recognized_votes": votes}


def _pick_win(s, w):
    """用引擎上一帧发布的 hint 调 fp._pick，得到本窗匹配结果 cur。"""
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
        return None
    (sid, delta), votes = picked
    return {"sid": int(sid), "off": delta * HOP / SR, "votes": int(votes)}


def replay_direct(method, wins, exp, bl, anchor, start_t=0.0):
    """before/after 重放：每个识别到正确歌的窗喂 `_beat_pos_grid`，记录网格拍位误差。"""
    s = make_direct(method)
    win_by_start = {w["t"]: w for w in wins}
    t_end = wins[-1]["t"]
    t = float(start_t)
    cur = None
    cur_ref = 1000.0 + t
    errs = []
    while t <= t_end + DT:
        w = win_by_start.get(round(t, 3))
        if w is not None:
            cur = _pick_win(s, w)
            cur_ref = 1000.0 + t
        FT.t = 1000.0 + t
        if cur is not None and cur["sid"] == exp and cur["votes"] > 0:
            off_end = float(cur["off"]) + 4.5
            gs = ({"beat_len": bl, "anchor": anchor}, bl, anchor, off_end, 1.0, exp)
            pos = method(s, _snap_direct(exp, off_end, int(cur["votes"]), cur_ref), gs)
            errs.append((t, pos - (t + 4.5 - anchor) / bl))
        else:
            s.audio.grid_pred_orig = -1.0
            s.audio.grid_pred_w = None
        t += DT
    return errs, None


def replay_now(wins, exp, bl, anchor, start_t=0.0):
    """now 重放：驱动现役 `_beat_pos`；只在**处于网格模式**时记录网格拍位误差。

    返回 (errs, lock_t)：errs=[(t, err)]；lock_t=首次锁定的挂钟相对时刻（None=未锁定）。
    """
    s = make_now(exp, bl, anchor)
    win_by_start = {w["t"]: w for w in wins}
    t_end = wins[-1]["t"]
    t = float(start_t)
    cur = None
    cur_ref = 1000.0 + t
    errs = []
    lock_t = None
    while t <= t_end + DT:
        w = win_by_start.get(round(t, 3))
        if w is not None:
            cur = _pick_win(s, w)
            cur_ref = 1000.0 + t
        FT.t = 1000.0 + t
        sid = cur["sid"] if cur is not None else -1
        off_end = (float(cur["off"]) + 4.5) if cur is not None else -1.0
        votes = int(cur["votes"]) if cur is not None else 0
        s._beat_pos(_snap_now(sid, off_end, votes, cur_ref))
        in_grid = (s._grid_active_sid is not None)
        if in_grid:
            if lock_t is None:
                lock_t = t
            errs.append((t, s._grid_last_pos - (t + 4.5 - anchor) / bl))
        t += DT
    return errs, lock_t


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


def _agg(metric_rows):
    """metric_rows=[(bad_dur, max_err, n5)] -> (Σbad_dur, max bad_dur, Σn5, max max_err)

    注意：**最长误锁 s 是单例 max(bad_dur)**（与 _gridfix4_accept 的 after 口径一致）；
    max|err| 是另一个量（瞬时最大偏差），单列参考，不要混用。"""
    tot = sum(r[0] for r in metric_rows)
    mx_bd = max(r[0] for r in metric_rows)
    n5 = sum(r[2] for r in metric_rows)
    mx_err = max(r[1] for r in metric_rows)
    return tot, mx_bd, n5, mx_err


def secA(data, starts):
    print("\n" + "=" * 80)
    print("A) 全曲×全起点扫描（热 cue 起播）：before / after(H2) / now 三方对照")
    print("   起点=%s（%d 首 × %d 起点 = %d 例）" % (starts, len(data), len(starts),
                                                   len(data) * len(starts)))
    print("=" * 80)
    rows = {"before": [], "after": [], "now": []}
    locks = []
    for d in data:
        for st in starts:
            eb, _ = replay_direct(POLICY_BEFORE, d["wins"], d["exp"], d["bl"], d["anchor"], st)
            ea, _ = replay_direct(POLICY_AFTER, d["wins"], d["exp"], d["bl"], d["anchor"], st)
            en, lt = replay_now(d["wins"], d["exp"], d["bl"], d["anchor"], st)
            locks.append(lt)
            rows["before"].append((bad_dur(eb), max((abs(e) for _, e in eb), default=0.0),
                                   bad_dur(eb) > 5.0))
            rows["after"].append((bad_dur(ea), max((abs(e) for _, e in ea), default=0.0),
                                  bad_dur(ea) > 5.0))
            rows["now"].append((bad_dur(en), max((abs(e) for _, e in en), default=0.0),
                                bad_dur(en) > 5.0))
    print("  口径：总误锁 s = Σ 各例「网格拍位 |err|>1 拍」的时长；最长误锁 s = 单例最大")
    print("        （最长误锁 = 单例 bad_dur 最大值，与 _gridfix4_accept 的 after 口径一致）")
    print("        目标：now 总误锁 显著 < after(850s)")
    b = _agg(rows["before"]); a = _agg(rows["after"]); n = _agg(rows["now"])
    print("  %-12s %10.0f %12.0f %12.0f" % ("总误锁 s", b[0], a[0], n[0]))
    print("  %-12s %10.1f %12.1f %12.1f" % ("最长误锁 s", b[1], a[1], n[1]))
    print("  %-12s %10d %12d %12d" % (">5s 例数", b[2], a[2], n[2]))
    print("  %-12s %10.0f %12.0f %12.0f" % ("max|err| 拍", b[3], a[3], n[3]))
    got = [x for x in locks if x is not None]
    print("  now 锁定覆盖：%d/%d 例锁定；首次锁定时刻 中位 %.1fs / 最大 %.1fs"
          % (len(got), len(locks),
             statistics.median(got) if got else float("nan"),
             max(got) if got else float("nan")))
    return rows


def secB(data):
    print("\n" + "=" * 80)
    print("B) t=0 正常起播逐首（重点：Can We Believe That / The Lost）")
    print("=" * 80)
    print("  %-44s %10s %10s %10s %10s %8s" % ("song", "bef max", "aft max", "now max",
                                               "now 误锁s", "锁定t"))
    for d in data:
        eb, _ = replay_direct(POLICY_BEFORE, d["wins"], d["exp"], d["bl"], d["anchor"], 0.0)
        ea, _ = replay_direct(POLICY_AFTER, d["wins"], d["exp"], d["bl"], d["anchor"], 0.0)
        en, lt = replay_now(d["wins"], d["exp"], d["bl"], d["anchor"], 0.0)
        mb = max((abs(e) for _, e in eb), default=0.0)
        ma = max((abs(e) for _, e in ea), default=0.0)
        mn = max((abs(e) for _, e in en), default=0.0)
        if ma > 1.0 or mn > 1.0 or bad_dur(ea) > 1.0:
            print("  %-44s %10.2f %10.2f %10.2f %10.1f %8s"
                  % (d["song"][:44], mb, ma, mn, bad_dur(en),
                     ("%.1f" % lt) if lt is not None else "-"))
    tot_b = sum(bad_dur(replay_direct(POLICY_BEFORE, d["wins"], d["exp"], d["bl"], d["anchor"], 0.0)[0]) for d in data)
    tot_a = sum(bad_dur(replay_direct(POLICY_AFTER, d["wins"], d["exp"], d["bl"], d["anchor"], 0.0)[0]) for d in data)
    tot_n = sum(bad_dur(replay_now(d["wins"], d["exp"], d["bl"], d["anchor"], 0.0)[0]) for d in data)
    print("  合计 t=0：总误锁 before=%.0fs / after=%.0fs / now=%.0fs" % (tot_b, tot_a, tot_n))


def secC(data):
    print("\n" + "=" * 80)
    print("C) R3 隔离：after(with R3) vs after(no R3) 与 now 的差异")
    print("=" * 80)
    starts = [0, 3, 6, 9, 12, 15, 18, 21, 24, 27, 30]
    diff = 0
    tot_r3 = tot_n3 = 0.0
    for d in data:
        for st in starts:
            e1, _ = replay_direct(POLICY_AFTER, d["wins"], d["exp"], d["bl"], d["anchor"], st)
            e2, _ = replay_direct(POLICY_NOR3, d["wins"], d["exp"], d["bl"], d["anchor"], st)
            tot_r3 += bad_dur(e1)
            tot_n3 += bad_dur(e2)
            if abs(bad_dur(e1) - bad_dur(e2)) > 1.0 or abs(
                    max((abs(e) for _, e in e1), default=0.0)
                    - max((abs(e) for _, e in e2), default=0.0)) > 0.5:
                diff += 1
    print("  after(with R3) 总误锁=%.0fs  after(no R3) 总误锁=%.0fs  差异例数=%d/%d"
          % (tot_r3, tot_n3, diff, len(data) * len(starts)))


def main():
    data = load()
    bpm_lo = min(d["bpm"] for d in data)
    bpm_hi = max(d["bpm"] for d in data)
    print("载入 %d 首（真实直方图 + 真实 per-song grid）；真实 BPM 范围 %.1f~%.1f"
          % (len(data), bpm_lo, bpm_hi))
    starts = [0, 3, 6, 9, 12, 15, 18, 21, 24, 27, 30]
    secA(data, starts)
    secB(data)
    secC(data)


if __name__ == "__main__":
    main()
