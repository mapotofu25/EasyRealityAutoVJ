# -*- coding: utf-8 -*-
r"""常驻能量日志（1 Hz CSV）+ 采集事件日志。

## 为什么要有它（用户 2026-11-02）
用户：「看一下今天00.00之后的日志 能量大部分时候貌似还是很低」。
查 `cue_health.log` 才发现它是**环形裁剪**（超 120KB 只留最后一段）——
程序 23:50 启动、01:14 退出，日志只剩 01:00:56–01:14:35，**00:00–01:00 的曲线没了**，
于是"那次到底低不低"根本无法回答。

⇒ 每次运行单独落一个 `energy_YYYYMMDD.csv`（**不裁剪**，只按天分文件 + 体积兜底轮转），
   1 秒一行，字段足够把"能量低"拆成三种原因：
     · `energy` 低但 `level` 正常  ⇒ 能量算法/阈值问题
     · `level` 也是 0            ⇒ 电平被清零（采集重启 / 断流）
     · `blk_delta` = 0 且 `dt` 大  ⇒ **采集根本没在喂数据**（这一列才是关键证据）

## 字段
    iso            本地时间（含毫秒）
    energy         st.energy（界面显示的那个）
    tier           引擎当前能量档位（high/mid/low，带滞回）
    level          st.level（0..1，≤-30dBFS 时正好 0.0000）
    lv_db          原始 dBFS（分辨"真安静"和"被清零"就看它）
    silent_sec     st.silent_sec（rms<1e-4 才累加，所以它常年 0）
    bpm            st.bpm
    sid            st.recognized_song_id（-1 = 没认出来）
    blocks         分析器累计块数（**每次采集重启会归零**）
    blk_delta      距上一行的块增量（0 = 这一秒没有音频块进来）
    dt             ���上一行的墙钟间隔（>2s = 采样线程被拖住）

## 采集事件
`capture.log` 追加 `[HH:MM:SS] [CAP] start/exit gen=.. dev=.. sr=..`，
用来和上面的 `blk_delta=0` 对齐：到底是"采集重启了"还是"设备没声音"。

⚠ 全部失败静默（写日志绝不能反过来影响演出），且文件超 `max_bytes` 自动轮转成 `.1`。
"""
import os
import threading
import time
import datetime

# ★ 2026-11-02：末尾追加两列（**只追加**，按列号解析的旧脚本不受影响）：
#   `lib_bpm` = 库内 BPM（认到歌才有值）；`spd` = **素材实际播放倍速**。
#   `spd` 的算法在 `content_speed_from_snap()`，与 `engine.content_speed` 是同一套优先级
#   （锚点 > 实时 + 库内谐波修正 > 库内原曲值 × 实测倍率），用来一眼验证
#   「200BPM 的歌素材到底跑了几倍」。见 `_test_bpm_lock_release.py`。
#
# ★★★ 2026-11-04：`spd` 列以前是**手抄**了 `content_speed` 的公式，抄漏了「库内 BPM
#   还要乘变速倍率」那一步 —— CSV 里记的 1.094 与真实素材倍速 1.308 分叉，
#   等于排查时被自己的日志骗。现在：
#     · 公式只有一份（下面的 `content_speed_from_snap`），`sample()` 直接调；
#     · 与 `engine.content_speed` 的一致性由 `tools/_test_content_speed.py` 的
#       「对拉」用例**逐值（float 完全相等）**锁定，谁改一边不同步就会红。
# ★ 2026-11-05：末尾追加 `rate`（**原始** `recognized_rate`，未过死区/钳位）。
#   为什么必须有：`content_speed_from_snap` 的库内分支现在是 `lib_bpm × rate / 128`，
#   而"装表期素材速度到不到位"取决于那一刻 rate 到底有没有收敛。没有这一列，
#   `spd=1.094` 到底是「r 还没测出来（乘 1.0，合理）」还是「r 有值却没用上（bug）」
#   —— 从日志上**完全分不出来**（2026-11-05 排查时就卡在这里）。
#   · `0.000` = 快照里没有这个键（引擎还没开始识别）
#   · `1.000` = 有值但等于 1.0（没变速，或还在收敛）
#   · 其它   = 实测倍率
#   ⚠ 只追加在**行尾**：既有按列号解析的脚本/正则一律不受影响（铁律）。
HEADER = ("iso,energy,tier,level,lv_db,silent_sec,bpm,sid,blocks,blk_delta,dt,"
          "lib_bpm,spd,anchor,rate\n")
CAP_LOG = "capture.log"
CAP_MAX = 256 * 1024          # 采集事件日志上限（超了轮转成 .1）


# ==================== 素材倍速：与 `engine.content_speed` 同口径 ====================
# 下面五个常量**必须与引擎/显示层逐值一致**，由 `tools/_test_content_speed.py`
# 的对拉用例 + 常量断言锁死：
#   `_LIB_HARM_TOL`   == engine.LIB_HARM_TOL
#   `_RATE_*`         == ui_main._BPM_RATE_MIN / _MAX / _DEADBAND
#                        （也等于 `audio_engine` 发布 `recognized_rate` 前的钳位区间）
_LIB_HARM_TOL = 0.06
_RATE_MIN = 0.78
_RATE_MAX = 1.25
_RATE_DEADBAND = 0.01
_SPD_BASE = 128.0             # 素材基准 BPM（`参照 BPM / 128`）
_SPD_MIN = 0.3
_SPD_MAX = 2.5


def _num_of(snap, key, default=0.0):
    """`snap[key]` → float；任何畸形输入都退回 default。失败静默。

    与 `EnergyCsvLogger._num` 同语义（那个现在只是转调本函数，保证只有一份实现）。
    """
    try:
        v = snap.get(key, default)
        return default if v is None else float(v)
    except Exception:                                            # noqa: BLE001
        return default


def _rate_of(snap):
    """`snap["recognized_rate"]` → 可信倍率；不可信一律 1.0。

    与 `engine.content_speed` 里的 rate 解析**逐条同口径**：
      · None / 缺键 / 非数字字符串 / bool / NaN / 越界 [_RATE_MIN, _RATE_MAX] ⇒ 1.0
      · |r-1| < _RATE_DEADBAND（1%）⇒ 1.0（1.0× 播放时 r 有 ±0.005 的噪声）
    """
    try:
        v = snap.get("recognized_rate", 1.0)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return 1.0
        v = float(v)
        if v != v or not (_RATE_MIN <= v <= _RATE_MAX):
            return 1.0
        if abs(v - 1.0) < _RATE_DEADBAND:
            return 1.0
        return v
    except Exception:                                            # noqa: BLE001
        return 1.0


def content_speed_from_snap(snap, sync_on=True):
    """从音频快照算「素材实际播放倍速」，**必须与 `engine.content_speed` 逐值一致**。

    为什么要单独一个函数：`spd` 是 CSV 里唯一的「素材速度跟没跟上」硬证据
    （`energy_log` 1Hz 落盘）。它和引擎是两个独立实现，一旦分叉，排查时就会被
    自己的日志骗（2026-11-04 就发生过：CSV 记 1.094、实际 1.308）。
    抽成模块级纯函数后，`tools/_test_content_speed.py` 可以把同一批 snap 同时喂给
    本函数和 `engine.content_speed`，断言 float 完全相等。

    三条出口的语义（与引擎一字不差）：
      ① **离线锚点 `bpm_anchor`**：离线对整段音频测的**听感值**，本身已含变速
         （1.20× 时给 172.3 而不是 140）⇒ **不乘 rate**，乘了就重复计速。
      ② **实时 `bpm`**（听感值）：库内值只做「谐波修正」（0.5×/1×/2× 折一下后与
         库内值差 ≤ `_LIB_HARM_TOL` 才采信库内值）。实时值本身已是听感值 ⇒
         这条**也不乘 rate**；谐波命中的那条同理（前提就是 r≈1）。
      ③ **只有库内值**（实时还没测出来）：库内是**原曲** BPM ⇒ 必须
         `lib_bpm × rate / 128`。这就是 2026-11-04 补的那一步：
         1.20× 下 140×1.196/128 = 1.308（而不是 140/128 = 1.094，慢 20%）。

    纯函数：`snap` 进、float 出。不读 `self`、不 IO、不加锁、不开线程
    （本模块跑在 1Hz 采样线程上，写日志绝不能反过来拖慢采样）。

    ⚠ **已知限制**：`EnergyCsvLogger.sample()` 没有拿 `bpm_speed_sync` 开关的途径
      （它构造时只有 `state/tier_fn/blk_fn`），所以这里恒按 `sync_on=True` 跑。
      用户**关掉**「素材速度同步」时，引擎返回 1.0 而本列仍会算出一个非 1 的值。
      要修得给 `EnergyCsvLogger` 传一个开关 getter（超出本次改动范围）。
    """
    d = snap if isinstance(snap, dict) else {}
    bpm = _num_of(d, "bpm")
    lib_bpm = _num_of(d, "lib_bpm")
    anchor = _num_of(d, "bpm_anchor")
    rate = _rate_of(d)
    if not sync_on:
        return 1.0
    # ① 离线锚点（听感值，不乘 rate）
    if anchor > 0.0:
        if bpm <= 0.0 or 0.4 <= (bpm / anchor) <= 2.5:
            return max(_SPD_MIN, min(_SPD_MAX, anchor / _SPD_BASE))
        # 分歧过大 ⇒ 锚点可能是上一首的，退回实时
    # ② 实时值（听感值，不乘 rate）；库内值只做谐波修正
    if bpm > 0.0:
        src = bpm
        if lib_bpm > 0.0:
            best = min((bpm * 0.5, bpm, bpm * 2.0), key=lambda x: abs(x - lib_bpm))
            if abs(best - lib_bpm) / lib_bpm <= _LIB_HARM_TOL:
                src = lib_bpm
        return max(_SPD_MIN, min(_SPD_MAX, src / _SPD_BASE))
    # ③ 只能用库内值：**原曲** BPM ⇒ 必须乘实测倍率才是"耳朵听到的拍速"
    if lib_bpm > 0.0:
        return max(_SPD_MIN, min(_SPD_MAX, lib_bpm * rate / _SPD_BASE))
    return 1.0


def _base_dir():
    try:
        from config import app_base_dir
        return app_base_dir()
    except Exception:                                            # noqa: BLE001
        return os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ")


def log_capture_event(msg, base_dir=None):
    """往 `capture.log` 追加一条采集事件（启动/退出）。失败静默。"""
    try:
        d = base_dir or _base_dir()
        p = os.path.join(d, CAP_LOG)
        if os.path.isfile(p) and os.path.getsize(p) > CAP_MAX:
            try:
                os.replace(p, p + ".1")
            except Exception:                                    # noqa: BLE001
                pass
        line = "[%s] %s\n" % (time.strftime("%H:%M:%S"), msg)
        with open(p, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:                                            # noqa: BLE001
        pass


class EnergyCsvLogger(threading.Thread):
    """每 `interval` 秒采一行能量到 `energy_YYYYMMDD.csv`。daemon，stop() 可干净退出。"""

    def __init__(self, state, base_dir=None, tier_fn=None, blk_fn=None,
                 interval=1.0, max_bytes=8 * 1024 * 1024, name="EnergyCsvLogger"):
        super().__init__(name=name, daemon=True)
        self.state = state
        self.base_dir = base_dir or _base_dir()
        self.tier_fn = tier_fn
        self.blk_fn = blk_fn
        self.interval = float(interval)
        self.max_bytes = int(max_bytes)
        self._stop = threading.Event()
        self._f = None
        self._day = ""
        self._prev_t = None
        self._prev_blk = None
        self.rows = 0               # 供测试断言
        self.last_error = ""

    # ---------------- 文件 ----------------
    def _path(self, day):
        return os.path.join(self.base_dir, "energy_%s.csv" % day)

    def _open(self, day):
        p = self._path(day)
        new = not os.path.isfile(p) or os.path.getsize(p) == 0
        # 体积兜底：单文件超上限就轮转（正常 1Hz 一天约 3~4 MB，几乎不会触发）
        try:
            if not new and os.path.getsize(p) > self.max_bytes:
                try:
                    os.replace(p, p + ".1")
                except Exception:                                # noqa: BLE001
                    pass
                new = True
        except Exception:                                        # noqa: BLE001
            pass
        self._f = open(p, "a", encoding="utf-8", buffering=1)
        if new:
            self._f.write(HEADER)
        self._day = day

    def _close(self):
        try:
            if self._f is not None:
                self._f.close()
        except Exception:                                        # noqa: BLE001
            pass
        self._f = None

    # ---------------- 采样 ----------------
    def _num(self, snap, key, default=0.0):
        """`snap[key]` → float，畸形输入退回 default。实现见模块级 `_num_of`（只一份）。"""
        return _num_of(snap, key, default)

    def sample(self):
        """采一行（返回写出的字符串；失败返回 None）。测试直接调这个。"""
        snap = self.state.snapshot()
        energy = self._num(snap, "energy")
        level = self._num(snap, "level")
        lv_db = self._num(snap, "lv_db", -80.0)
        silent = self._num(snap, "silent_sec")
        bpm = self._num(snap, "bpm")
        # ★ 2026-11-02：库内 BPM + 素材实际倍速。这两列是"素材速度跟没跟上"的唯一硬证据。
        lib_bpm = self._num(snap, "lib_bpm")
        anchor = self._num(snap, "bpm_anchor")
        # ★★ 2026-11-04：`spd` 的算法**整体搬到模块级 `content_speed_from_snap()`**，
        #   因为它必须与 `engine.content_speed` 逐值一致，而"一致"现在由
        #   `tools/_test_content_speed.py` 的「对拉」用例（float **完全相等**）锁定：
        #   同一批 snap 同时喂引擎和本函数，谁改了一边没跟另一边就红。
        #   ⚠ 别再把公式抄回这里 —— 抄一份就是两条会各自漂移的真相（2026-11-04 的坑）。
        #   ⚠ 本列不反映 `bpm_speed_sync` 开关（见函数 docstring 的「已知限制」）。
        spd = content_speed_from_snap(snap)
        # ★ 2026-11-05：**原始**倍率（诊断用，不参与 spd 计算）。
        #   缺键记 0.000，与"有值且等于 1.000"区分开 —— 这是判断
        #   「装表期 r 还没收敛」还是「r 有值但漏乘」的唯一依据。
        rate_raw = _num_of(snap, "recognized_rate", 0.0)
        sid = int(self._num(snap, "recognized_song_id", -1))
        tier = "-"
        if self.tier_fn is not None:
            try:
                tier = str(self.tier_fn() or "-")
            except Exception:                                    # noqa: BLE001
                tier = "-"
        blocks = -1
        if self.blk_fn is not None:
            try:
                blocks = int(self.blk_fn())
            except Exception:                                    # noqa: BLE001
                blocks = -1

        now = time.time()
        dt = (now - self._prev_t) if self._prev_t else 0.0
        blk_delta = (blocks - self._prev_blk) if (self._prev_blk is not None
                                                  and blocks >= 0) else -1
        self._prev_t = now
        self._prev_blk = blocks if blocks >= 0 else None
        iso = datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        return ("%s,%.3f,%s,%.4f,%.1f,%.1f,%.1f,%d,%d,%d,%.2f,%.1f,%.3f,%.1f,%.3f\n"
                % (iso, energy, tier, level, lv_db, silent, bpm, sid,
                   blocks, blk_delta, dt, lib_bpm, spd, anchor, rate_raw))

    def run(self):
        try:
            self._open(time.strftime("%Y%m%d"))
        except Exception as e:                                   # noqa: BLE001
            self.last_error = "open: %s" % e
            return
        try:
            while not self._stop.wait(self.interval):
                try:
                    day = time.strftime("%Y%m%d")
                    if day != self._day:                         # 跨天换文件
                        self._close()
                        self._open(day)
                    row = self.sample()
                    if row:
                        self._f.write(row)
                        self.rows += 1
                        # 体积兜底：跑到上限就当场轮转（不能只在开文件时判，
                        # 否则一次运行跑很久会一直长下去）。测试里 max_bytes 设很小来验这条。
                        try:
                            if self._f.tell() > self.max_bytes:
                                cur = self._path(self._day)
                                self._close()
                                try:
                                    os.replace(cur, cur + ".1")
                                except Exception:                # noqa: BLE001
                                    pass
                                self._open(self._day)
                        except Exception:                        # noqa: BLE001
                            pass
                except Exception as e:                           # noqa: BLE001
                    self.last_error = str(e)
                    self._close()
                    try:
                        self._open(time.strftime("%Y%m%d"))
                    except Exception:                            # noqa: BLE001
                        time.sleep(2.0)
        finally:
            self._close()

    def stop(self):
        self._stop.set()
