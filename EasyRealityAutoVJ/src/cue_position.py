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
# ★★★ 2026-10-18（用户："视频一直跳"）：识别结果**过期**多久就不许再外推。
#   现场日志：`off=80.89` 卡住不动 20+ 秒，而 `pb` 一路涨到 342（涨了 14 秒）——
#   因为去重后 `pos_beats_at()` 会从**陈旧的 offset** 一直按 rate 外推，
#   位置就越跑越偏（dmax 到 30~90 秒），视频被目标拖着不停 seek/加速 = 一直跳。
#   正确做法：offset 超过 STALE_SEC 没更新 ⇒ **位置直接判不可信**（返回 None），
#   交给引擎侧的"自由运行"最多外推 2 秒，然后卸画面。宁可短暂没画面，也不要瞎跑。
STALE_SEC = 1.5
# ★ 2026-10-18 真机实测：指纹偶尔会**锁到歌里另一处**（同曲、票数不低，位置差 175 秒）
#   ⇒ 位置被瞬间拉走 ⇒ 码表跳到没有内容的时间段 ⇒ **闪出普通图层**、回来再闪一下。
#   对付这种"单次野匹配"：大跳变**要求连续 2 次一致**才认（真拖动 0.4s 内就会确认）。
JUMP_SEC = 3.0               # 超过它算"大跳变"，先当可疑
JUMP_AGREE_SEC = 1.0         # 两次候选相差在它之内 = 一致
# ★ 真机实测：野匹配的票数通常明显偏低（抓到的那次只有 18 票，正常段 30~100）。
#   大跳变额外要求足够票数，两道闸门一起用。真拖动时新位置很快就会重新拿到高票。
JUMP_MIN_VOTES = 40
# ★★ 2026-10-18（变速真机，铁律 0）：**低票时的救命通道**。
#   变速播放（±10~20%）票数天然掉到 3~26，永远到不了 40 ⇒ 旧逻辑"票不够就 hold"
#   会把一次野匹配变成**永久错误位置**（实测 `pb` 冲到 1038 拍 = 444s，那一段没内容
#   ⇒ `sub=3/0` 闪出普通图层）。⇒ 连续这么多次**落在同一处**就认（≈1.5s）。
JUMP_STUCK_N = 3
# ★ 2026-10-18：低票时允许的**修正位移上限**（秒）。野匹配通常 ±100~250s，
#   真拖动/真 seek 会有高票。超过它就必须票数够。
JUMP_LOW_VOTE_MAX_SEC = 30.0
# ★★ 2026-10-18（真机最狠的一次野匹配跳了 214 秒）：**硬上限**。
#   任何位置修正超过它一律不接受（不看票数）。理由：歌曲内的真实位移（DJ 拖动）
#   在演出里极少见，而一次 200 秒的野匹配观众立刻看得见；宁可挡掉，
#   需要重新定位时停止/重新开始 VJ 即可重新锁定。
JUMP_ABS_MAX_SEC = 60.0

# ★★★ 2026-10-18 速度式伺服参数（见 `feed` 里的说明）：
#   |误差| ≤ DEAD 视为指纹噪声（offset 本身抖动 ±0.2~0.25s）⇒ 速度不动；
#   超出部分按 KP 转换成速度增量，总速度钳在 [LO, HI] × 实测倍率。
#   ⇒ 位置**永不跳变**，画面上只看到"稍微快一点/慢一点"。
CUE_SERVO_DEAD = 0.25
CUE_SERVO_KP = 0.35
CUE_SERVO_LO = 0.80
CUE_SERVO_HI = 1.20
# ★ 2026-10-18（用户："加个防抖，好几秒的那种，超过这个时间指纹还是严重对不上才切"）
#   严重不符（|误差| > SERIOUS）必须先**持续** DEBOUNCE 秒才算数；
#   在防抖窗口内只做轻微速度修正（±5%），绝不做大幅修正 ⇒ 偶发野匹配完全看不出。
CUE_SERVO_SERIOUS = 3.0
CUE_DEBOUNCE_SEC = 6.0
CUE_SERVO_LIGHT = 0.05
# 中等误差（0.25~2s）按比例收敛，上限 ±12%（既不抖，也能在 ~10 秒内收干净）
CUE_SERVO_MID_HI = 0.12
# 真实拖动（seek）判定：新位置**持续一致**这么久就认（与跳多远无关）
CUE_SEEK_DEBOUNCE = 2.5      # 秒
CUE_SEEK_AGREE = 2.0         # 两次候选相差 <= 它 视为同一处
# ★★ 2026-10-18：**冷启动宽限期** —— 刚锁定的头 2 秒，锚点还不算数，出现相反位置直接改锚。
#   理由：此刻没有历史可以保护旧锚；而变速播放票数上不去，等"一致确认"会白等 1.5 秒画面。
JUMP_YOUNG_SEC = 2.0
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
        self._pos_sec = -1.0        # 最近一次可信的"原曲秒"（可能经过 slew 平滑）
        self._last_off = -1.0       # 上一次**原始** offset（判"是不是同一条识别结果"）
        self._jump_cand = None      # 待确认的大跳变候选（连续 2 次一致才认）
        self._jump_n = 0             # 该候选已连续一致几次（低票通道用，见 JUMP_STUCK_N）
        self._jump_cand_t = 0.0      # 该候选的时刻（比较时要外推，见 feed）
        # ★ 两阶段提交：大跳接受后要**被后续结果验证**，不成立就撤回（见 feed）
        self._verify = None          # (旧位置, 旧时刻, 剩余验证次数)
        self._lock_t = -1e9          # 首次锁定时刻（冷启动宽限期用，见 JUMP_YOUNG_SEC）
        self._slew_n = 0             # 平滑修正次数（取证用）
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
        if dt > STALE_SEC:
            return None                 # ★ 识别结果过期 ⇒ 不可信（不再无限外推）
        # ★ 位置按**速度**推进（速度式伺服）：`_vel` 已含实测倍率与纠偏增量
        vel = float(getattr(self, "_vel", 0.0) or 0.0)
        if vel <= 0.0:
            vel = max(0.0, float(self.rate or 1.0))
        pos = self._pos_sec + dt * vel
        return pos / self.beat_len

    def reset(self):
        self.song_id = -1
        self._pos_sec = -1.0
        self._last_off = -1.0
        self._jump_cand = None
        self._jump_n = 0
        self._jump_cand_t = 0.0
        self._verify = None
        self._lock_t = -1e9
        self._slew_n = 0
        self._t_last = 0.0
        self._miss = 0
        self._cand = -1
        self._cand_n = 0
        self._vel = 1.0             # 位置推进速度（速度式伺服；见 feed）
        self._bad_t = 0.0           # 连续"严重不符"的起始时刻（防抖用）
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
                # 丢票期间继续按当前速度滑行；速度缓慢回到实测倍率（不长期挂 20% 偏差）
                _r0 = max(0.05, float(self.rate or 1.0))
                try:
                    self._vel = float(self._vel or _r0) + (_r0 - float(self._vel or _r0)) * 0.02
                    dt0 = max(0.0, float(t_wall) - float(self._t_last or 0.0))
                    self._pos_sec = max(0.0, self._pos_sec + dt0 * float(self._vel))
                    self._t_last = float(t_wall)
                except (TypeError, ValueError):
                    pass
                pb = self.pos_beats_at(t_wall)
                return self.last_status, pb, self.song_id
            self.last_status = "lost"
            return self.last_status, None, self.song_id

        # ---- 有票 ----
        self._miss = 0
        if self.song_id < 0:
            # 首次识别：立即接受（没有"旧歌"可保护）—— 现场演出不能让画面白等。
            # ★★ 2026-10-18（变速真机）：但**刚锁定的 2 秒内锚点还是"暂定"的**。
            #   实测 0.80× 时票数只有 3~26，锁定那一次可能落在歌里重复段落的另一处
            #   （抓到 off=197 却锁到 445s 那一段）⇒ `pb` 跑到 1038 拍（=444s，那一段没内容）
            #   ⇒ `sub=3/0` **闪出普通图层**，而且之后**所有正确结果都被"大跳变"挡在门外**。
            #   修法：宽限期内（`JUMP_YOUNG_SEC`）出现相反位置**直接改锚**（不卡死、不等待），
            #   过期后恢复"大跳变要一致确认"的常规节奏。见下面的 `_young` 分支。
            self.song_id = sid
            self._pos_sec = max(0.0, float(offset_sec or 0.0))
            self._last_off = self._pos_sec
            self._t_last = float(t_wall)
            self._vel = max(0.05, float(self.rate or 1.0))
            self._lock_t = float(t_wall)
            self._jump_cand = None
            self._jump_n = 0
            self.last_status = "ok"
            return self.last_status, self.pos_beats_at(t_wall), self.song_id

        if sid == self.song_id:
            self._cand, self._cand_n = -1, 0
            _new = max(0.0, float(offset_sec or 0.0))
            # 去重：同一个 offset 会被连喂 2~3 次（0.2s 轮询 vs 0.5s 结果）
            if abs(_new - float(self._last_off)) < 1e-6:
                self.last_status = "ok"
                return self.last_status, self.pos_beats_at(t_wall), self.song_id
            _dt = max(0.0, float(t_wall) - float(self._t_last or 0.0))
            _r = max(0.05, float(self.rate or 1.0))
            _pred = self._pos_sec + _dt * float(getattr(self, "_vel", _r) or _r)
            _err = _new - _pred
            # ══════════════════════════════════════════════════════════════════
            # ★★★ 2026-10-18 第六稿（用户原话："一旦成功识别到指纹，就进入强制播放状态，
            #   画面不再跳动，只允许在加速或减速过程中有相应变化，持续保持到切换歌曲"）
            #
            #   **速度式伺服（velocity-only servo）**：位置**永不跳变**，只调推进速度。
            #     · 位置 = 上一位置 + 经过时间 × 速度（连续、单调、可积，绝不倒退）
            #     · 速度 = 实测倍率 r × (1 + K×误差)，钳在 r×[0.80, 1.20]
            #     · 误差在死区内（指纹本身抖动 ±0.25s）⇒ 速度就取 r，一点不抖
            #   代价（明说）：一次 8 秒的识别断档要 ~40 秒才追平，但这 40 秒里
            #   **画面是连续播放的**，不会跳。用户要的是"不跳"，这是唯一能做到的形态。
            #   野匹配（>JUMP_ABS_MAX_SEC）直接忽略：不改锚、也不改速度。
            # ══════════════════════════════════════════════════════════════════
            # ══════════════════════════════════════════════════════════════════
            # ★★★ 2026-10-18 第七稿（用户：我手动突然跳到三分钟，视频半天都不跳）
            #   旧写法用「位移 >60 秒一律忽略」来挡野匹配 —— 结果把**真实拖动**也挡死了。
            #   真正的判别不是「跳多远」，而是「这个新位置能不能一直站得住」：
            #     · 野匹配：下一次结果又跳到别处（候选一直在变）⇒ 永远不采信
            #     · 真拖动：之后每次结果都指向同一处 ⇒ 2.5 秒内确认，然后**跳过去**
            #   ⇒ 用「远候选一致性」代替距离硬上限：距离多远都能跟，野匹配照样挡掉。
            # ══════════════════════════════════════════════════════════════════
            _now = float(t_wall)
            _e = abs(_err)
            if _e <= CUE_SERVO_DEAD:
                self._vel = _r                            # 噪声区：一点不抖
            elif _e <= CUE_SERVO_SERIOUS:
                # 中等误差：按比例平滑收敛，钳 ±12%（1 秒误差约 8 秒收干净）
                _step = _err - (CUE_SERVO_DEAD if _err > 0 else -CUE_SERVO_DEAD)
                _v = _r * (1.0 + CUE_SERVO_KP * _step)
                self._vel = max(_r * (1.0 - CUE_SERVO_MID_HI),
                                min(_r * (1.0 + CUE_SERVO_MID_HI), _v))
            else:
                # 远：先看这个「新位置」是不是一直站得住
                _prev = float(getattr(self, "_far_cand", 0.0) or 0.0)
                _pt = float(getattr(self, "_far_t", 0.0) or 0.0)
                _pred_c = (_prev + (_now - _pt) * _r) if _pt > 0.0 else None
                if _pred_c is not None and abs(_new - _pred_c) <= CUE_SEEK_AGREE:
                    _persist = _now - _pt
                else:
                    self._far_cand = _new
                    self._far_t = _now
                    _persist = 0.0
                if _persist >= CUE_SEEK_DEBOUNCE:
                    # 站住了 ⇒ 这就是真位置（拖动/跳转）⇒ **跳过去**，速度归位
                    self._vel = _r
                    self._pos_sec = max(0.0, _new)
                    self._last_off = _new
                    self._t_last = _now
                    self._far_t = 0.0
                    self._bad_t = 0.0
                    self.last_status = "ok"
                    return self.last_status, self.pos_beats_at(t_wall), self.song_id
                # 还没站住：只做 ±5% 微调（看不出来），继续观察
                self._vel = _r * (1.0 + (CUE_SERVO_LIGHT if _err > 0 else -CUE_SERVO_LIGHT))
            self._pos_sec = max(0.0, _pred)
            self._last_off = _new
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
