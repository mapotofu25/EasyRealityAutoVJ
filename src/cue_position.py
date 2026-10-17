# -*- coding: utf-8 -*-
"""码表**位置层**：把「指纹识别结果」变成「码内拍位」，并带**回滞**（不抖断）。

════════════════════════════════════════════════════════════════════════
链路（每 ~0.5s 一次，**不进渲染热路径**）
════════════════════════════════════════════════════════════════════════
    audio_engine 指纹线程
        → state.recognized_song_id / recognized_offset(原曲秒) / recognized_votes / recognized_rate
        → 本模块 `CuePositionTracker.feed()`（回滞 + 平滑推进）
        → `pos_beats = offset_sec / beat_len`（`beat_len = 60/bpm`；bpm=0 退化 1 拍=1 秒）
        → `engine.set_cue_position(pos_beats, "fp")`

**两条纪律**
1. **不改 `beat_len` / anchor 公式**：位置相位只能进"位置换算"，绝不反向污染"文件换算"
   （设计 §①；那条铁律是 Step1 已用 parity 钉死的）。
2. **回滞/大跳变沿用 Step1 runtime 的阈值**（`0.15 拍` / `>8 拍`）—— 本模块只负责
   "把秒换成拍、并在丢票时不抖断"，**不重造**运行时那套。
"""
from __future__ import annotations

DEFAULT_BEAT_LEN = 1.0      # bpm=0 ⇒ 1 拍 = 1 秒（与 Cue 端同口径）
CONFIRM_N = 2               # 连续 N 次识别到同一首"新歌"才切换（防抖动误切）
HOLD_N = 3                  # 连续 M 次丢票仍保持（不抖断），超过才判定"丢了"
MIN_VOTES = 1               # 对齐票数门槛（低于它视为本次不可用）


class CuePositionTracker:
    """把 fps 的识别结果流变成稳定的 `pos_beats`。

    · `feed()` 由**定时器/节拍回调**驱动（~2~5Hz），不是每帧；
    · `pos_beats_at(t)` 可以在两次 `feed()` 之间**按挂钟平滑推进**（避免 0.5s 一次跳一下）；
    · 丢票：前 `HOLD_N` 次**保持最后位置不动**（画面继续按拍走），超过才 `status="lost"`。
    """

    def __init__(self, confirm_n=CONFIRM_N, hold_n=HOLD_N, min_votes=MIN_VOTES):
        self.confirm_n = int(confirm_n)
        self.hold_n = int(hold_n)
        self.min_votes = int(min_votes)
        self.song_id = -1
        self.beat_len = DEFAULT_BEAT_LEN
        self.rate = 1.0
        self._pos_sec = -1.0        # 最近一次可信的"原曲秒"
        self._t_last = 0.0          # 该次可信结果的挂钟时刻
        self._miss = 0              # 连续丢票次数
        self._cand = -1             # 候选新歌
        self._cand_n = 0
        self.paused = False
        self.last_status = "lost"

    # ---------------- 状态查询 ----------------
    def pos_beats_at(self, t_wall):
        """两次 `feed()` 之间按挂钟推进（`rate` = 播放倍率）。不可信时返回 None。"""
        if self._pos_sec < 0.0 or self.song_id < 0:
            return None
        if self.paused:
            return self._pos_sec / self.beat_len
        dt = max(0.0, float(t_wall) - self._t_last)
        pos = self._pos_sec + dt * max(0.0, float(self.rate or 1.0))
        return pos / self.beat_len

    def reset(self):
        self.song_id = -1
        self._pos_sec = -1.0
        self._t_last = 0.0
        self._miss = 0
        self._cand = -1
        self._cand_n = 0
        self.last_status = "lost"

    def set_paused(self, paused):
        """暂停 ⇒ 位置**冻结**（不推进），恢复后从冻结处继续（由下一次 feed 校准）。"""
        self.paused = bool(paused)

    def set_beat_len(self, beat_len):
        """由 bpm 换算好的 `beat_len`（秒/拍）。bpm=0 ⇒ 调用方传 1.0。"""
        try:
            bl = float(beat_len)
        except (TypeError, ValueError):
            bl = DEFAULT_BEAT_LEN
        self.beat_len = bl if bl > 1e-6 else DEFAULT_BEAT_LEN

    # ---------------- 输入 ----------------
    def feed(self, song_id, offset_sec, votes, t_wall, beat_len=None):
        """喂一次识别结果，返回 `(status, pos_beats, song_id)`。

        status：
          · `"ok"`     有票且同曲：位置已更新（`pos_beats` 可用）
          · `"hold"`   本次丢票，但还在容忍期：**保持最后位置**（不抖断）
          · `"lost"`   丢票超过容忍期 / 从未识别到：`pos_beats = None`（不接管）
          · `"switch"` 确认切换到另一首歌（`song_id` 已更新，位置用本次 offset 重置）
        """
        if beat_len is not None:
            self.set_beat_len(beat_len)
        sid = int(song_id) if song_id is not None else -1
        v = int(votes or 0)
        ok = (sid >= 0) and (v >= self.min_votes)

        # ---- 丢票 ----
        if not ok:
            self._miss += 1
            self._cand, self._cand_n = -1, 0
            if self.song_id >= 0 and self._miss <= self.hold_n:
                self.last_status = "hold"
                pb = self.pos_beats_at(t_wall)
                return self.last_status, pb, self.song_id
            self.last_status = "lost"
            return self.last_status, None, self.song_id

        # ---- 有票 ----
        self._miss = 0
        if self.song_id < 0:
            # 首次识别：立即接受（没有"旧歌"可保护）
            self.song_id = sid
            self._pos_sec = max(0.0, float(offset_sec or 0.0))
            self._t_last = float(t_wall)
            self.last_status = "ok"
            return self.last_status, self.pos_beats_at(t_wall), self.song_id

        if sid == self.song_id:
            self._cand, self._cand_n = -1, 0
            self._pos_sec = max(0.0, float(offset_sec or 0.0))
            self._t_last = float(t_wall)
            self.last_status = "ok"
            return self.last_status, self.pos_beats_at(t_wall), self.song_id

        # 换了另一首：要连续 N 次才认（防"歧义一票"把画面切走）
        if sid == self._cand:
            self._cand_n += 1
        else:
            self._cand, self._cand_n = sid, 1
        if self._cand_n >= self.confirm_n:
            self.song_id = sid
            self._pos_sec = max(0.0, float(offset_sec or 0.0))
            self._t_last = float(t_wall)
            self._cand, self._cand_n = -1, 0
            self.last_status = "switch"
            return self.last_status, self.pos_beats_at(t_wall), self.song_id
        # 还没确认：保持旧歌（继续按挂钟推进），不切
        self.last_status = "hold"
        return self.last_status, self.pos_beats_at(t_wall), self.song_id


# ==========================================================================
def choose_sheet(matches, prefer=None, track_meta_id=""):
    """从 `[(摘要, 级别)]`（Step2 已按可靠性排序）里挑一张。

    策略（**说明理由**）：
      1. **级别最高者优先** —— 判定可靠性由 `match_track` 给，不该被"最近用过"盖过；
      2. **同级多张** ⇒ 用 `cfg["cue"]["prefer"][<meta_id>]` 记住的**用户/上次选择**；
      3. 没有偏好 ⇒ 取 `mtime` **最新**的那张（作者最近改过的版本更可能是当前要用的）；
      4. 仍并列 ⇒ 按路径**字典序**（稳定，避免每次选到不同的表）。
    `prefer`：`{meta_id: sheet_path}`。
    """
    if not matches:
        return None
    best_level = matches[0][1]
    same = [(e, lv) for e, lv in matches if lv == best_level]
    if len(same) == 1:
        return same[0][0]
    pref = (prefer or {})
    want = str(pref.get(track_meta_id or "", "") or "")
    if want:
        for e, _lv in same:
            if str(e.get("path") or "") == want:
                return e
    same.sort(key=lambda x: (-float(x[0].get("mtime", 0.0) or 0.0),
                            str(x[0].get("path") or "")))
    return same[0][0]
