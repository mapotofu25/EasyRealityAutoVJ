# -*- coding: utf-8 -*-
"""第五轮验收 ★交接专项：未锁定(4 拍) → 锁定(8 拍) 那一刻的交接行为。

问题（team-lead 明确要求，别含糊）：
  1) 从原拍钟（4 拍对齐）切到网格（八拍对齐）时，`_beat_pos` **是否连续不跳**？
  2) 锁定发生在 Loop 期间 / 识别中断后重锁 / 换歌 时各是什么表现？
  3) ⚠ 若「八拍相位」与当时「4 拍相位」不一致，交接瞬间**会不会出现一次错切**？
     有则明确说出来并给方案（例：只在"下一个八拍头"边界交接）。

驱动**现役** src/engine.py 的 `_beat_pos` / `_grid_state` / `_grid_observe` /
`_beat_pos_grid` / `_next_switch_due` / `_auto_switch_tick`（含坐标续接）。
同步检测每一次自动切换（`_auto_switch_tick` 推进 `switch_pos` 即一次切换），
并把切换落点换算成「真实拍位」，与网格真值八拍头比对。

用法： P:\\AutoVJ\\venv\\Scripts\\python.exe tools/_gridfix5_handoff.py
"""
import math
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import engine as E  # noqa: E402

DT = 1.0 / 60.0


class FT:
    t = 0.0

    @staticmethod
    def perf_counter():
        return FT.t


E.time = FT

BL = 0.4          # 150 BPM ⇒ 1 拍 = 0.4s


def make_engine(sid, anchor, bl=BL, downbeat_off=0.0, beat_grid=True, bar_off_old=False):
    s = types.SimpleNamespace()
    s._pos_ref = None
    s.switch_pos = 0.0
    s._next_due = None
    s._due_off = 0.0
    s._bar_off_user = None
    s.mode = "normal"
    s.locked = False
    s.layers = []
    s.cfg = {"auto": {"beat_grid": beat_grid}, "mode": {"sensitivity": 1}}
    s._song_grid_lookup = {sid: {"beat_len": bl, "anchor": anchor, "bpm": 150.0, "src": "vdj"}}
    s._grid_clock = E.GridClock()
    s._grid_active_sid = None
    s._grid_coord_delta = 0.0
    s._grid_last_pos = 0.0
    s._grid_last_out = None
    s._grid_pending = None
    s._grid_seen = None
    s.audio = types.SimpleNamespace(grid_pred_orig=-1.0, grid_pred_w=None)
    for nm in ("_beat_pos", "_beat_pos_grid", "_grid_observe", "_grid_state",
               "_align_width", "_bar_off", "_bar_floor", "_next_switch_due",
               "_current_interval", "_auto_switch_tick", "switch_countdown",
               "beats_to_next_switch"):
        setattr(s, nm, types.MethodType(getattr(E.AutoVJEngine, nm), s))
    s.GRID_MIN_VOTES = E.AutoVJEngine.GRID_MIN_VOTES
    s.GRID_STRONG_VOTES = E.AutoVJEngine.GRID_STRONG_VOTES
    s.GRID_CONSIST_LO = E.AutoVJEngine.GRID_CONSIST_LO
    s.GRID_CONSIST_HI = E.AutoVJEngine.GRID_CONSIST_HI
    s.ALIGN_MIN_OFFSET = E.AutoVJEngine.ALIGN_MIN_OFFSET
    s._downbeat_off = downbeat_off
    if bar_off_old:
        # 第五轮前的旧 `_bar_off`：网格模式（w==8）返回 0.0（忽略 delta 相位）
        def _old_bar_off(self, snap):
            w = self._align_width(snap)
            u = self._bar_off_user
            if u is not None:
                return float(u) % w
            if w == 8.0:
                return 0.0
            return float(snap.get("downbeat_off", 0) or 0)
        s._bar_off = types.MethodType(_old_bar_off, s)
    return s


def mono_snap(sid, off, ref_t, downbeat_off=0.0, t=0.0):
    bc = int(t / BL)
    ph = (t / BL) - bc
    return {"bpm": 150.0, "beat_active": True, "beat_count": bc, "beat_phase": ph,
            "downbeat_off": downbeat_off, "energy": 0.5, "energy_trend": 0.0,
            "recognized_song_id": sid, "recognized_offset": off, "recognized_rate": 1.0,
            "recognized_t": ref_t, "recognized_votes": 300}


def drive(sid, anchor, bl=BL, T=40.0, downbeat_off=0.0, beat_grid=True,
          recog=None, tag="", bar_off_old=False):
    """驱动一帧序列。recog(t)->(sid,off) 决定该帧识别结果（None=未识别）。

    返回 dict：max_jump / jump_at_lock / lock_t / delta / delta_mod8 /
               due_shift_at_lock / switches:[(t, pos_beats, head_err_beats)]。
    """
    s = make_engine(sid, anchor, bl, downbeat_off, beat_grid, bar_off_old)
    t = 0.0
    prev_pos = None
    max_jump = 0.0
    jump_at_lock = None
    lock_t = None
    delta = None
    due_shift = None
    switches = []
    prev_sw = 0.0
    prev_due = None
    prev_ingrid = False
    while t <= T + 1e-9:
        FT.t = 1000.0 + t
        rec = recog(t) if recog is not None else (sid, t)
        if rec is None:
            rsid, roff = -1, -1.0
        else:
            rsid, roff = rec
        snap = mono_snap(rsid, roff, 1000.0 + round(t * 2) / 2, downbeat_off, t)
        pos = s._beat_pos(snap)
        ingrid = s._grid_active_sid is not None
        if prev_pos is not None:
            jump = pos - prev_pos
            max_jump = max(max_jump, abs(jump))
            if ingrid and not prev_ingrid:
                jump_at_lock = jump
                lock_t = t
                delta = s._grid_coord_delta
        due = s._next_switch_due(snap)
        if due is not None:
            if (ingrid and not prev_ingrid) and prev_due is not None:
                due_shift = (due - prev_due) - (pos - prev_pos)
            prev_due = due
        s._auto_switch_tick(snap)
        if s.switch_pos != prev_sw:
            # 本帧发生一次自动切换：记录**真实歌曲秒**与到最近八拍头的偏差（拍）
            song_sec = t
            head_err = (((song_sec - anchor) / bl) % 8.0)
            if head_err > 4.0:
                head_err -= 8.0
            switches.append((t, s.switch_pos, head_err))
            prev_sw = s.switch_pos
        prev_pos = pos
        prev_ingrid = ingrid
        t += DT
    return {"max_jump": max_jump, "jump_at_lock": jump_at_lock, "lock_t": lock_t,
            "delta": delta, "delta_mod8": (delta % 8.0) if delta is not None else None,
            "due_shift": due_shift, "switches": switches,
            "anchor": anchor, "bl": bl}


def true_heads(anchor, bl, n=20):
    return [anchor + 8 * k * bl for k in range(n)]


def head_err(sec, anchor, bl):
    """sec（真实秒）到最近八拍头的拍数偏差。"""
    return ((sec - anchor) / bl) % 8.0


def report(name, r, downbeat_off=0.0):
    print("-" * 80)
    print("场景：%s" % name)
    print("  max|pos 跳变| = %.4f 拍" % r["max_jump"])
    if r["lock_t"] is not None:
        print("  锁定时刻 t=%.3f s；交接帧 pos 跳变 = %s 拍；delta=%.4f（mod 8 = %.4f 拍）"
              % (r["lock_t"],
                 ("%.4f" % r["jump_at_lock"]) if r["jump_at_lock"] is not None else "-",
                 r["delta"], r["delta_mod8"]))
    else:
        print("  未锁定")
    if r["due_shift"] is not None:
        print("  交接边沿 due 净位移 = %+.3f 拍（正=目标被推后；>0.5 即可能出现错切节奏）"
              % r["due_shift"])
    print("  自动切换落点（真实歌曲位置到最近八拍头的偏差，拍；|·|<0.05 = 正好在八拍头）：")
    for (t, sp, he) in r["switches"][:10]:
        flag = "OK 对齐" if abs(he) < 0.05 else ("偏 %.2f 拍" % abs(he))
        print("    t=%5.2f s  切换目标 pos=%8.3f  → 真实偏差 %+6.2f 拍  [%s]"
              % (t, sp, he, flag))


def main():
    print("=" * 80)
    print("★交接专项：未锁定(4 拍) → 锁定(8 拍)")
    print("  bl=%.4f（150BPM）；真实八拍头 = anchor + 8n·bl" % BL)
    print("=" * 80)

    # S1：八拍相位与 4 拍相位**一致**（anchor=0 ⇒ 八拍头在真实拍 0,8,16…；downbeat=0）
    r1 = drive(7, 0.0, T=30.0, downbeat_off=0.0, tag="S1")
    report("S1 相位一致（anchor=0，downbeat_off=0）", r1)

    # S2：八拍相位与 4 拍相位**不一致**（anchor=2.0s=5 拍 ⇒ 八拍头在真实拍 5,13,21…；
    #     live downbeat_off=0 ⇒ 4 拍小节线在 0,4,8…）
    r2 = drive(7, 2.0, T=30.0, downbeat_off=0.0, tag="S2")
    report("S2 相位不一致（anchor=2.0s，真实八拍头在拍 5/13/21…；4 拍小节线在 0/4/8…）", r2)
    r2o = drive(7, 2.0, T=30.0, downbeat_off=0.0, tag="S2old", bar_off_old=True)
    report("S2-old 【对照】旧 _bar_off（网格模式返回 0.0，未按 delta 相位修正）", r2o)

    # S2b：对照——同 anchor，但 downbeat_off=5（模拟 live 4 拍相位也落在 5）→ 相位应一致
    r2b = drive(7, 2.0, T=30.0, downbeat_off=1.0, tag="S2b")
    report("S2b anchor=2.0s + downbeat_off=1.0（对齐相位仍可能与八拍头差）", r2b)

    # S3：锁定期间出现 4 拍 Loop（t∈[10,14]：识别 offset 每 4 拍循环）
    def recog_loop(t):
        if 10.0 <= t < 14.0:
            return (7, 5.0 + ((t - 10.0) % (4 * BL)))
        return (7, t)
    r3 = drive(7, 0.0, T=30.0, downbeat_off=0.0, recog=recog_loop)
    report("S3 锁定后出现 4 拍 Loop（t=10~14s）", r3)

    # S4：识别中断后重锁（t∈[10,13] 未识别）
    def recog_gap(t):
        if 10.0 <= t < 13.0:
            return None
        return (7, t)
    r4 = drive(7, 0.0, T=30.0, downbeat_off=0.0, recog=recog_gap)
    report("S4 识别中断 3s 后重锁", r4)

    # S5：换歌（t=12s 起识别到另一首带不同 anchor 的网格）
    def recog_change(t):
        if t < 12.0:
            return (7, t)
        return (8, t)          # 换歌：sid=8（同一 grid 结构但 anchor 不同 → 走另一坐标系）
    s5 = make_engine(7, 0.0, BL, 0.0, True)
    s5._song_grid_lookup[8] = {"beat_len": BL, "anchor": 1.2, "bpm": 150.0, "src": "vdj"}
    # 手动驱动换歌场景
    tt = 0.0
    prev = None
    mx = 0.0
    jumps = []
    while tt <= 30.0:
        FT.t = 1000.0 + tt
        rs = recog_change(tt)
        snap = mono_snap(rs[0], rs[1], 1000.0 + round(tt * 2) / 2, 0.0, tt)
        pos = s5._beat_pos(snap)
        if prev is not None:
            mx = max(mx, abs(pos - prev))
            jumps.append((tt, pos - prev))
        prev = pos
        tt += DT
    print("-" * 80)
    print("场景：S5 换歌（t=12s：sid 7→8，anchor 0.0→1.2）")
    print("  max|pos 跳变| = %.4f 拍；换歌帧(t≈12s)附近跳变：" % mx)
    for (t, j) in jumps:
        if 11.9 <= t <= 12.3:
            print("    t=%.3f 跳变 %+.4f 拍" % (t, j))

    print("\n" + "=" * 80)
    print("结论见上方各场景；下方为机读汇总")
    print("=" * 80)
    for nm, r in (("S1一致", r1), ("S2不一致", r2), ("S2b", r2b), ("S3 Loop", r3), ("S4中断", r4)):
        print("  %-10s max_jump=%.4f  lock_t=%s  delta_mod8=%s  due_shift=%s"
              % (nm, r["max_jump"],
                 ("%.2f" % r["lock_t"]) if r["lock_t"] is not None else "-",
                 ("%.3f" % r["delta_mod8"]) if r["delta_mod8"] is not None else "-",
                 ("%+.3f" % r["due_shift"]) if r["due_shift"] is not None else "-"))


if __name__ == "__main__":
    main()
