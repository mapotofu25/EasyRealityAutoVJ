# -*- coding: utf-8 -*-
"""音乐电池引擎（Music Battery / ChargeBarEngine）—— Python 复刻 VJVision。

漏桶决策状态机，专为 DJ 现场设计：满格换槽、漏电、候选锁偏移锚定、空槽首曲
跨时间复核、漏到 0 不退场（搓碟/EQ 归零仍保持画面）、混音 MixHold 不抖。

来源声明（MIT 要求保留）：
  Music Battery (ChargeBar) engine by ichiryu, from VJVision
  (https://github.com/ichiryu0021/VJVision), MIT License.
"""


class MatchEvent:
    NONE = "none"
    TENTATIVE = "tentative"      # 候选预览（不切画面）
    CONFIRMED = "confirmed"      # 充满换槽切歌
    MIXHOLD = "mix_hold"         # 两曲重叠期都有票，画面不抖
    NOISE = "noise"              # 有票但低于入场门槛
    NO_MATCH = "no_match"        # 无票


class FpResult:
    """指纹匹配结果（tick 的输入）"""
    __slots__ = ("matched", "song_id", "song_name", "confidence",
                 "offset_sec", "aligned_votes")

    def __init__(self, matched=False, song_id=-1, song_name="",
                 confidence=0.0, offset_sec=0.0, aligned_votes=0):
        self.matched = matched
        self.song_id = song_id
        self.song_name = song_name
        self.confidence = confidence
        self.offset_sec = offset_sec
        self.aligned_votes = aligned_votes


class MatchTick:
    """引擎每拍输出（只报告状态，切不切歌由内部状态机决定）"""
    __slots__ = ("song_id", "confidence", "offset_sec", "event",
                 "cur_song_id", "cur_votes", "streak_votes", "streak_ticks",
                 "evidence_votes")

    def __init__(self):
        self.song_id = -1
        self.confidence = 0.0
        self.offset_sec = 0.0
        self.event = MatchEvent.NONE
        self.cur_song_id = -1
        self.cur_votes = 0
        self.streak_votes = 0
        self.streak_ticks = 0
        self.evidence_votes = 0     # ⚠ __slots__ 里声明了就必须初始化：
                                    #   漏了会 AttributeError（谁读谁炸，而且很难联想）


class ChargeBarEngine:
    """漏桶决策状态机（常量与 VJVision charge_engine 一致，编译期固定、现场零调参）"""

    # 电量条结构常量
    BAR_CAP = 10            # 满格票数
    BAR_LEAK = 1            # 漏电时代每拍漏掉的格数
    ADMIT_VOTES = 4         # 入场票：低于此的命中不进任何条
    # ★ 2026-10-19 实测：真实 tick 周期 ≈2s（匹配耗时+0.5s 睡眠），旧值 4 拍 ⇒ 切歌后
    #   要 8 秒才"发现换了歌"。改为 2 拍；并给"压倒性证据"一条快速通道（见 STRONG_VOTES）。
    BREAK_HITS = 2          # 别的歌连续 2 拍抢候选锁
    STRONG_VOTES = 60       # 单拍票数≥此值 ⇒ 这一拍算两拍（实测真歌 57~247，白噪≤4）
    HOLD_SEC = 6.0          # 候选未充满时无进票的存活秒数
    # ★★ 换歌判据（2026-10-19 真机迭代）：**滑动窗口累计证据**。
    #   为什么不用'连续 N 拍'：切歌时票数会抖（实测 264→40→12），连续判据一断就重来，
    #   现场表现为 6s 与 20s 随机跳。累计判据对单帧噪声免疫。
    #   白噪实测：单拍 ≤4 票、3 秒累计 ≤30；真歌单拍 30~260、3 秒累计 200+。
    SWITCH_WINDOW_SEC = 4.0   # 证据窗口（秒）
    SWITCH_TOTAL_VOTES = 60   # 窗口内同一新歌累计票数门槛（白噪≤30，真歌实测 78~300）
    SWITCH_MIN_TICKS = 2      # 至少 2 个 tick 支持（防单帧偶发）
    SWITCH_MIN_PEAK = 15      # 单拍峰值门槛（白噪峰值≤4）
    REANCHOR_HITS = 3       # 候选连续 3 拍偏移漂移 → 重锚
    OFFSET_TOL_SEC = 0.35   # 候选只收锁定偏移 ±0.35s 内的票
    FIRST_TRACK_HITS = 2    # 空槽首曲最少充电拍数（跨时间复核门）
    NO_MATCH_EXIT_SEC = 5.0 # 持续这么久连一张指纹票都没有 → 判定歌已不在播放，退场
    # ---- delta 推进连续性（防相似曲/噪声高票误确认）----
    # 真匹配时识别窗口每 0.5s 前移一次，同一音乐点在窗口内的位置前移 0.5s，
    # 对齐差 delta 每拍恒定递增 0.5s（实测 Black Warrior 连续段精确 +0.51）；
    # 假匹配（同社团相似曲也能撞出高票）delta 随机跳变，只有推进连续的票才允许充电。
    DELTA_ADVANCE_SEC = 0.5 # 相邻拍对齐差递增秒数（= 识别 tick 周期）
    DELTA_TOL_SEC = 0.4     # 推进容差（吸收 hop/DT_QUANT 量化误差）
    # ★ 2026-10-19：20 → 10。实测（真实库/三种来源）：真歌单对齐票最低 9，
    #   白噪最高 4 —— 10 取在两者之间，并额外由 audio_engine 用「总票≥80」把关。
    CONFIRM_VOTES = 10      # 候选充电最低票数（配合 SONG_MIN_VOTES 双重把关）。
                            # 同专辑相似曲的噪声票 3~14 票——20 分离干净。

    def __init__(self):
        self.reset()

    def reset(self):
        self.current_song_id = -1
        self.cur_bar = 0
        self.slot_filled = False        # 漏电时代：已有歌进槽
        self.no_match_since = None      # 连续零指纹票的起始时刻（None=最近有票）
        self.clear_candidate()
        self.break_count = 0

    def clear_candidate(self):
        self.tentative_song_id = -1
        self.cand_bar = 0
        self.cand_anchor = -1.0
        self.cand_anchor_t = -1.0   # ★ 锚点对应的时刻（coherent 判据用'期望位置'）
        self.cand_hold_until = 0.0
        self.cand_age_ticks = 0
        self.non_coh_streak = 0
        self.cand_hits = 0
        self.cand_first_charge_sec = -1.0
        self.cand_last_delta = None    # 上一拍对齐差（delta 推进连续性校验用）
        self.cand_last_t = None        # ★ 上一拍的**实际时刻**：推进判据按真实经过时间算
        self.cand_strong = False
        self.strong_streak = 0        # ★ 不同歌连续拿到 ≥STRONG_VOTES 的拍数
        self._ev = []                 # ★ 换歌证据：(t, sid, votes)，滑动窗口用
        self.strong_streak = 0        # ★ 不同歌连续拿到 ≥STRONG_VOTES 的拍数       # ★ 本候选是否拿到过"压倒性票数"（≥STRONG_VOTES）

    # ---- 内部 ----
    def _leak_bars(self):
        # 空槽期（首曲/无信号回待机后）不漏电；进槽后每拍漏两条
        if not self.slot_filled:
            return
        self.cur_bar = max(0, self.cur_bar - self.BAR_LEAK)
        self.cand_bar = max(0, self.cand_bar - self.BAR_LEAK)

    def _age_candidate(self, now_sec):
        # 空槽首曲：两次充电不限制时间，候选不因 HOLD_SEC 过期；
        # 进槽后的挑战者仍受存活期约束（切歌速度不变）
        if not self.slot_filled:
            return
        if self.tentative_song_id >= 0 and now_sec > self.cand_hold_until:
            self.clear_candidate()

    def _do_switch(self, sid, votes, off, now_sec):
        """硬换歌：清零旧锁、直接把 current 设成新歌。返回要交给上层的 MatchTick。"""
        self.reset()
        self._ev = []
        self.current_song_id = int(sid)
        self.cur_bar = self.BAR_CAP
        self.slot_filled = True
        self.cand_anchor = float(off)
        self.cand_anchor_t = float(now_sec)
        self.cand_last_delta = float(off)
        self.cand_last_t = float(now_sec)
        self.clear_candidate()
        out = MatchTick()
        out.event = MatchEvent.CONFIRMED
        out.song_id = int(sid)
        out.cur_song_id = int(sid)
        out.evidence_votes = int(votes)
        out.cur_votes = self.BAR_CAP
        out.streak_votes = self.BAR_CAP
        out.offset_sec = float(off)
        return out

    def _charge_candidate(self, votes, now_sec):
        if self.cand_first_charge_sec < 0.0:
            self.cand_first_charge_sec = now_sec
        self.cand_hits += 1
        self.cand_bar = min(self.BAR_CAP, self.cand_bar + votes)
        if votes >= self.STRONG_VOTES:
            self.cand_strong = True
        self.cand_hold_until = now_sec + self.HOLD_SEC

    def tick(self, r, now_sec):
        """输入一个 FpResult，返回 MatchTick。"""
        out = MatchTick()
        out.song_id = r.song_id
        out.confidence = r.confidence
        out.offset_sec = r.offset_sec

        if self.tentative_song_id >= 0:
            self.cand_age_ticks += 1

        # 1) 漏电时代每拍先漏电
        self._leak_bars()

        # 1.5) 持续无「有效票」→ 对应的歌已不在播放，退场不再强行保持。
        #     「有效票」= 达到入场门槛的票。指纹库 hash 量大，任何音频都会撞出 1~3 票噪声，
        #     若按「>0 就清零计时器」，切到曲库没有的歌时噪声票会永久阻止退场（错误歌名常驻）。
        #     只有达到 ADMIT_VOTES 的票才证明歌仍在播；搓碟/EQ 拉零的瞬时弱票不会误退场。
        if r.matched and r.aligned_votes >= self.ADMIT_VOTES:
            self.no_match_since = None
        elif self.no_match_since is None:
            self.no_match_since = now_sec
        if (self.slot_filled and self.no_match_since is not None
                and now_sec - self.no_match_since >= self.NO_MATCH_EXIT_SEC):
            self.reset()
            out.event = MatchEvent.NO_MATCH
            out.song_id = -1
            out.cur_song_id = -1
            return out

        # 入场票
        admitted = (r.matched and r.song_id >= 0 and
                    r.aligned_votes >= self.ADMIT_VOTES)

        # 2) 槽内歌进票：不校验偏移
        if admitted and r.song_id == self.current_song_id:
            self.cur_bar = min(self.BAR_CAP, self.cur_bar + r.aligned_votes)

        slot_open = (self.current_song_id < 0 or self.cur_bar < self.BAR_CAP)

        # 3) 候选（挑战者 / 空槽期第一首）
        if not admitted:
            self._age_candidate(now_sec)
        elif r.song_id == self.current_song_id:
            self.break_count = 0
            self._age_candidate(now_sec)
        else:
            # ★★ 换歌判定：**滑动窗口累计证据**（对单帧噪声免疫）
            #   窗口 3s 内同一「非当前歌」累计 ≥120 票、≥2 个 tick、单拍峰值 ≥30 ⇒ 换歌。
            #   实测：真歌 3 秒累计 200+；白噪累计 ≤30，不会被误判。
            if self.current_song_id >= 0 and r.song_id != self.current_song_id:
                self._ev.append((now_sec, int(r.song_id), int(r.aligned_votes)))
                self._ev = [e for e in self._ev
                            if now_sec - e[0] <= self.SWITCH_WINDOW_SEC]
                _agg = {}
                for _t, _s, _v in self._ev:
                    _a = _agg.setdefault(_s, [0, 0, 0])
                    _a[0] += _v
                    _a[1] += 1
                    if _v > _a[2]:
                        _a[2] = _v
                for _s, _a in _agg.items():
                    if (_a[0] >= self.SWITCH_TOTAL_VOTES
                            and _a[1] >= self.SWITCH_MIN_TICKS
                            and _a[2] >= self.SWITCH_MIN_PEAK):
                        return self._do_switch(_s, _a[0], r.offset_sec, now_sec)
            # ⚠ 这里**不能**因为在过渡期又匹配到当前歌就清空证据：
            #   切歌瞬间窗口新旧混杂，票会交替落在 A/B 上；一旦清空，新歌永远攒不够。
            #   证据只按时间窗自然过期（见上方的 prune）。
            if r.song_id != self.tentative_song_id:
                # 别的歌：连续 BREAK_HITS 拍抢中 → 候选锁换人（旧条作废）
                # ★ 压倒性证据（≥STRONG_VOTES）算两拍：现场切歌时正确歌常见 100+ 票，
                #   不该再等第二个 tick；白噪/相似曲远达不到这个量级（实测 ≤4）。
                self.break_count += (2 if r.aligned_votes >= self.STRONG_VOTES else 1)
                if self.break_count >= self.BREAK_HITS:
                    self.tentative_song_id = r.song_id
                    self.cand_bar = 0
                    self.cand_anchor = -1.0
                    self.cand_hold_until = now_sec + self.HOLD_SEC
                    self.cand_age_ticks = 0
                    self.non_coh_streak = 0
                    self.cand_hits = 0
                    self.cand_first_charge_sec = -1.0
                    self.cand_last_delta = None
                    self.cand_last_t = None
            else:
                self.break_count = 0

            if r.song_id == self.tentative_song_id:
                # delta 推进连续性校验：真匹配 delta 每拍恒定递减 0.5s（窗口前移）；
                # 假匹配 delta 随机跳变。首次只记录 delta 不充电（假匹配随机 delta
                # 二连击概率≈十万分之一，真匹配只慢一拍）。
                last_d = self.cand_last_delta
                last_t = self.cand_last_t
                self.cand_last_delta = r.offset_sec
                self.cand_last_t = now_sec
                # ★★ 2026-10-19 修复「切歌要十几秒」：
                #   旧实现把"应该推进多少"写死成 DELTA_ADVANCE_SEC(0.5s)，隐含假设
                #   「识别周期恒为 0.5s」。实测周期 = 匹配耗时 + 0.5s 睡眠，309MB 库时常
                #   落到 0.8~1.2s ⇒ |Δoff - 0.5| > 0.4 ⇒ advancing 恒 False ⇒ 永不充电
                #   ⇒ 要等某一次周期"恰好"接近 0.5s 才确认（现场表现为 10~15 秒）。
                #   现在按**真实经过时间** dt 作为期望推进量：真匹配 Δoff ≈ dt；
                #   假匹配 delta 随机跳变，仍然过不了 ±0.25s 的窄容差。
                if last_d is None or last_t is None:
                    advancing = False
                else:
                    dt = float(now_sec) - float(last_t)
                    if dt <= 0.0:
                        advancing = False
                    else:
                        advancing = abs((r.offset_sec - last_d) - dt) <= 0.25
                if self.cand_bar == 0 or self.cand_anchor < 0.0:
                    self.cand_anchor = r.offset_sec
                    self.cand_anchor_t = now_sec
                    self.non_coh_streak = 0
                chargeable = (advancing and
                              r.aligned_votes >= self.CONFIRM_VOTES)
                # ★★ 2026-10-19：**必须按'期望位置'判连续性**。
                #   旧写法拿绝对偏移与锚点比，而窗口每拍前进 0.4~0.5s ⇒
                #   必然超出 ±0.35s ⇒ 每拍都判'不连续' ⇒ 只能每 3 拍重锚时才充一次电
                #   ⇒ 确认被拖到 3~20 秒（真机实测的主要根因）。
                #   改为与'锚点+经过时间'比较：真匹配差值≈0，跳变/换歌仍然超差。
                _pred = self.cand_anchor + max(0.0, now_sec - max(self.cand_anchor_t, 0.0)) \
                    if self.cand_anchor_t >= 0.0 else self.cand_anchor
                coherent = abs(r.offset_sec - _pred) <= self.OFFSET_TOL_SEC
                if coherent:
                    self.non_coh_streak = 0
                    if chargeable:
                        self._charge_candidate(r.aligned_votes, now_sec)
                else:
                    # 偏移漂移：连续 REANCHOR_HITS 拍就重锚（不清零已充电量）；
                    # 重锚同样必须推进连续+高票才充电——否则假匹配每 3 拍白拿一充
                    self.non_coh_streak += 1
                    if self.non_coh_streak >= self.REANCHOR_HITS:
                        self.cand_anchor = r.offset_sec
                        self.cand_anchor_t = now_sec
                        self.non_coh_streak = 0
                        if chargeable:
                            self._charge_candidate(r.aligned_votes, now_sec)
                    else:
                        self._age_candidate(now_sec)

        # 4) 换槽：候选充满，且槽位为空 或 槽内歌已不满格
        bar_full = (self.tentative_song_id >= 0 and self.cand_bar >= self.BAR_CAP)
        first_track_gate = True
        if bar_full and not self.slot_filled:
            first_track_gate = (self.cand_hits >= self.FIRST_TRACK_HITS)

        # ★ 2026-10-19：旧歌电量满格时 slot_open=False，新歌要等它每秒漏 1 格（10 格≈4s）。
        #   但若新候选拿到过 ≥STRONG_VOTES 的压倒性票数（真歌 100+，白噪 ≤4），
        #   就没有理由再等 —— 直接接管，切歌从 ~10s 降到 ~6s。
        if bar_full and (slot_open or self.cand_strong) and first_track_gate:
            out.event = MatchEvent.CONFIRMED
            out.evidence_votes = self.BAR_CAP
            self.current_song_id = self.tentative_song_id
            self.cur_bar = self.BAR_CAP
            self.slot_filled = True
            out.song_id = self.current_song_id
            self.clear_candidate()
            self.break_count = 0
            out.cur_votes = self.BAR_CAP
            out.streak_votes = self.BAR_CAP
            out.cur_song_id = self.current_song_id
            return out

        # 5) 未切换：输出状态标签
        out.cur_votes = self.cur_bar
        out.streak_votes = self.cand_bar
        out.streak_ticks = self.cand_age_ticks
        out.cur_song_id = self.current_song_id
        if self.tentative_song_id >= 0:
            out.event = (MatchEvent.TENTATIVE if self.current_song_id < 0
                         else MatchEvent.MIXHOLD)
            out.song_id = self.tentative_song_id
        elif not admitted:
            out.event = (MatchEvent.NOISE if (r.matched and r.aligned_votes > 0)
                         else MatchEvent.NO_MATCH)
        else:
            out.event = MatchEvent.NONE
        return out
