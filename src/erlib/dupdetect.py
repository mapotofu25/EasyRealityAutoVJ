# -*- coding: utf-8 -*-
r"""导入 `.eravjlib` **之前**的重复检测 —— 「这个包里有没有我本机已经有的歌」。

## 为什么需要这一层（2026-10-31 用户原话）
  「导入了怎么 识别不到 …… 而且 导入重复的歌也没提醒」
  （截图：曲库里两条 `📦 Digimind - Addicted 2 Love`，而这两个 mp3 本机早就有）

## 根因（已用真实数据核实，别再重复调研）
1. `song_id = int(md5(完整路径)[:8], 16)` —— **对路径字符串敏感**；
2. 本机 `cfg["music_library"]` 里那两首的路径是 `<盘符>:/目录/02-....mp3`
   （盘符后是**正**斜杠、目录是反斜杠），sid = 1299159966 / 4106797223；
3. 同一个文件被 ERLIB 用 `<盘符>:\...`（全反斜杠）扫进包，包内 sid = 828227951 / 1037575596；
4. ⇒ **同一首歌两套 song_id**：曲库面板把"本机行"和 `@imported:<sid>` 行各列一遍
   （用户看到重复），`fingerprints.db` 里也有两套内容相同的指纹
   （现场识别命中哪一套不稳定 ⇒ 歌名/封面与用户认知的文件对不上）。

## 判据（按可靠度组合，宁少勿多）
1. **同 song_id** —— 严格、零误判；
2. **同 file_name（忽略大小写）+ 时长差 ≤ `DUR_TOL`(0.5s)** ——
   专门解决上面那种"同一文件、不同路径写法"⇒ 两套 sid 的情况。
   ⚠ 两边**都必须有**时长（缺一个就不算重复）：
     拿不到时长时按文件名硬判会把"翻唱/同名不同版"误藏掉 —— 用户丢歌比看到重复严重。
3. **指纹重合率**（★★ 2026-11-01 新增）—— 前面两条都判不出来的"同一首的不同母带"。

## 判据 ③ 为什么必须加（2026-11-01 用户原话）
  「**没做指纹对比吗**」
现场（主理人已实测，别重复调研）：
  · 导入包 `(08) [Amane Oikawa] Extra Pain (2025 Remaster).flac`（229.04s）
  · 本机库 `(09) [Amane Oikawa] Extra Pain.flac`（230.20s）
同一个人声/编曲的**两个母带**：
  · 判据 ① 不中 —— 文件名不同 ⇒ `song_id`（md5 路径）必然不同；
  · 判据 ② 不中 —— 名字多了 ` (2025 Remaster)`，时长还差 1.16s（> `DUR_TOL`）。
  · 但两者指纹的**共同 hash 占较小集合 38.0%**（同一文件 ≈100%、不相干 ≈0%）
    ⇒ 这就是"同一首"最硬的证据，而且**不需要解码音频**（包 `.eravjlib` 自带
    `fingerprints` 表，本机 `fingerprints.db` 有 `idx_hash` 索引）。

### 为什么是"抽样 + 重合率"而不是"逐条全比"
- 包内这首 2.7 万条、本机 280 万条：全比要几十秒，导入前的提示不能卡这么久；
- 按 offset 排序后**等距抽样 `DUP_FP_SAMPLE`(1200) 条**再问本机库：
  1200 条里命中 456 条（38%）和全量算出来的 38% 只差零点几个百分点，
  而查询量降到 1/20（`WHERE hash IN (…)` 走 `idx_hash`；实测本机 282 万条时
  取 hash 约 20ms + 查库约 65ms = **85ms/首**，20 首的包 ≈ 1.7s）。
- **确定性**：`offset` 排序 + 等距取第 `int(i*step)` 条 ⇒ 同一个包跑一万次，
  采样结果和 `overlap` **完全相等**（`_test_import_dup_fp.py` (d) 钉住这点）。

### 判据 ③ 的"宁少勿多"护栏
- 只有 `overlap >= DUP_FP_MIN`(0.25) 才算（实测同一首不同母带 38%、不相干 < 1%，
  0.25 落在两者中间且偏向"别误判"）；
- **长度护栏 `FP_DUR_RATIO`(0.90)**：两个时长差 10% 以上 ⇒ 不判
  （防 `Radio Edit` vs `Extended Mix`：电台版几乎是加长版的子集，
  单看重合率会有 90% ⇒ 用户点「跳过」就丢了一个版本）；
- 命中数按**去重 hash** 统计（同一 hash 在库里可能有多行 offset，`COUNT(*)` 会虚高）；
- **本机指纹库不存在 / 表缺失 / 空表 ⇒ 静默跳过判据 ③**（不报错、不影响导入速度）：
  没建过指纹库的用户一样能导入，只是少一层判重。

## 为什么单独一个模块（而不是塞进 transfer / ui_main）
- 比对逻辑是**纯函数**，可以脱离 GUI 直接跑（`tools/_test_import_dup.py`）；
- 三处要用同一套判据：导入前提示（ui_main）、导入时跳过（transfer）、
  曲库面板隐藏重复行（panels）—— 判据写两遍必然漂移。
"""

import os
import sqlite3

# 判据 ② 的时长容差（秒）。0.5s 的来历：
#   · 同一文件在两台机器上解码，时长应该**完全相等**（时长写在容器头里）；
#   · 但老库里 `grid.dur` 可能是"解码时长"（浮点、有截断误差），
#     实测同一首两次扫描最多差 0.02s ⇒ 0.5s 足够宽松，又不会把
#     "3:29 和 3:30 的两个不同混音版"算成同一首。
DUR_TOL = 0.5

# 判据 ③（指纹重合）的两个旋钮 —— 2026-11-01 用户「没做指纹对比吗」。
#
# `DUP_FP_MIN` = 判重门槛（命中数 / 采样数）。实测三个锚点：
#   · 同一个文件（两套 sid）      ≈ 100%
#   · 同一首的不同母带（现场那对） = 38.0%（8396 / 22083）
#   · 不相干的两首歌              ≈ 0%（<1%）
# 取 0.25：离"同一首"有 13 个点的余量（母带差异大一点也还在门槛上方），
# 离"不相干"有 25 个点的余量。**宁可少判，绝不误判** —— 误判 = 用户以为歌已经有了，
# 点了「跳过这些」就真丢歌了。
DUP_FP_MIN = 0.25
# `DUP_FP_SAMPLE` = 每首抽多少条 hash 去查本机库。
# 1200 条：抽查比例 1200/27000 ≈ 4.4%，重合率的抽样标准差 ≈ sqrt(p(1-p)/n)
# = sqrt(0.38*0.62/1200) ≈ 1.4% ⇒ 38% 的样本几乎不可能被抽到 25% 以下（~9σ）。
# 同时查询量是本机的 1/20，`WHERE hash IN (...)` 走 `idx_hash`，一首 < 20ms。
DUP_FP_SAMPLE = 1200
# 一次 `WHERE hash IN (…)` 塞多少个参数。SQLite 老版本 `SQLITE_MAX_VARIABLE_NUMBER`
# 是 999，分块 400 留足余量（同时不影响索引走位）。
FP_QUERY_CHUNK = 400

# 判据 ③ 的**长度护栏**（`min(两个时长) / max(两个时长)` 的下限）。
#
# ★ 为什么判据 ③ 需要它（2026-11-01 自查，用户的老规矩是「宁少勿多」）：
#   判据 ③ 的分母是**包里那首**的采样条数。如果包里是 `Radio Edit`（3:30）、
#   本机是 `Extended Mix`（6:00），那么电台版几乎是加长版的**子集** ⇒
#   电台版的 hash 有八九成能在本机那首里找到 ⇒ overlap ≈ 90% ⇒ 会被判成"同一首"，
#   用户点「跳过这些」就把**另一个版本**丢掉了。
#   而判据 ③ 要解决的现场是 229.04s vs 230.20s（ratio = 0.995）。
#   取 0.90：现场那种"同一母带重新灌制"（差 1~2s）稳过，任何剪辑版/加长版
#   （通常差 15% 以上）一律不判 ⇒ 和判据 ② 的"时长差 ≤0.5s"是同一个思路的两档。
#   ⚠ 两边时长**有一个未知**时不套这条护栏：指纹本身已经是很硬的证据，
#     没时长只是没法再加一道尺，不该因此让判据 ③ 完全失效。
FP_DUR_RATIO = 0.90

# 判据 ③ 在**拿不到本机时长**时的替代门槛（★★ 2026-11-01 QA 提的 P1 盲区，出包前必修）。
#
# ## 盲区怎么来的（QA 实测）
#   `by_sid` 只来自 `local_tracks(cfg)`（读 `music_meta` / `music_library`），**不读
#   `imported_meta.db`** ⇒ 「之前已经导入过这个包、再导一次」这条路径上，
#   对照那一份的 `dur` 恒为 0 ⇒ `dl > 0` 条件不成立 ⇒ **长度护栏整条被跳过**
#   ⇒ 剪辑版（Pre-edit 55%）会被判成"同一首"（QA 实测 overlap 45%）。
#   现场那 9 首 `dl` 全是 0，只是它们 overlap 97~99.5% 才没出事。
#
# ## 为什么"未知时长"要用**更严**的门槛，而不是放宽
#   长度护栏是唯一能挡住"剪辑版 / 加长版"的证据；这条证据没了，
#   就只剩重合率可用 ⇒ 必须把门槛提到两个真实锚点中间：
#     · 同一个文件（换路径/换容器）实测 97.4~99.5%  ⇒ 必须判得出；
#     · 剪辑版（取前 55% 指纹）     实测 ~45%       ⇒ 必须判不出；
#   0.90 卡在中间（离剪辑版 45 个点，离同文件 7~9 个点）。
#   ⚠ 与 `DUP_FP_MIN`(0.25) 的关系：`dl > 0` 时仍用 0.25（有时长证据 ⇒ 敢放宽到
#     "不同母带"那条线）；`dl == 0` 时用 `max(0.25, 0.90)`。
FP_DUR_UNKNOWN_MIN = 0.90

# ⚠⚠ **`DUP_FP_MIN`(0.25) 与「码表包」的 0.40 是同一件事的两档门槛 ——
#    两处不同是有意的，别再合成一个常量**
#   （2026-11-01 主理人订正：用户 2026-10-30 02:41 就说过「我期望的是用指纹对比」——
#     「一首歌它有两个版本，就只是专辑不同 歌曲的时长啊，这些东西完全都是一样的」+
#     「导入时给个弹窗提示？」。那一轮落地的是**码表包 `.ercue`** 导入时的指纹对账：
#      `src/cue_fp_pack.py::match_pack_local()` + `cfg["cue"]["fp_same"]` +
#      `cue_page._same_song_popup()`，门槛 `DEFAULT_SAME_RATIO = 0.40`。
#     10-31 做曲库包 `.eravjlib` 判重时只写了"文件名+时长"，**漏掉了这套指纹对比**
#     ⇒ 本判据是**补遗漏**，不是新需求。两套的关系必须写在这里，否则以后必然分叉。）
#
#   为什么码表用 0.40、这里用 0.25：
#     · 码表场景 = 「同一个文件换容器/换码率」⇒ 重合率 ~100%（实测同一文件 100%、
#       同曲不同混音 11.5%、无关噪声 8.7~9.1%）⇒ 0.40 已经够用且更保守；
#     · 曲库场景 = 「同一首的不同**母带**」⇒ 实测 `Extra Pain` 那对只有 **38%**
#       （母带过 EQ/压限改掉了一部分频谱哈希）⇒ 用 0.40 会**漏判**（正好漏掉用户
#       这次报的那一对）⇒ 只能取 0.25。
#     · 两边都远高于实测噪声顶（不相干曲目 0.4%~11.4%）⇒ 0.25 仍有 2 倍余量。
#   ⚠ 语义不同（"同一文件"vs"同一首的不同母带"），**不要为了统一把两个常量合并**。


def norm_name(name):
    r"""文件名比较用的归一化键：取 basename + 小写 + 去首尾空白。

    ⚠ 必须同时兼容两种存在形式：
      · 本机曲库给的是**绝对路径**（`<盘符>:\目录\a.mp3`）；
      · 包里 `file_name` 是纯文件名，但老包的 `rel_path` 可能是相对路径。
    所以先按两种分隔符切掉目录，再小写比较。
    """
    s = str(name or "").strip()
    if not s:
        return ""
    s = s.replace("\\", "/").rstrip("/")
    s = s.rsplit("/", 1)[-1]
    return s.strip().lower()


def to_dur(v):
    """把任意时长值收敛成 `float`；拿不到/非正数一律返回 0.0（= "未知"）。"""
    try:
        d = float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return d if d > 0.0 else 0.0


def imported_tracks(base_dir):
    """**已导入**的曲目（`<base_dir>/imported_meta.db`）整理成同结构表；读不到 ⇒ `[]`。

    ★★ 2026-11-01（QA 提的 P1/P2/P3，出包前修）：
      判据 ③ 的"本机时长"原来只来自 `cfg["music_meta"]`，而**反复重导入同一个包**
      这条路径上，对照的那一份只在 `imported_meta.db` 里（导入不会往 `music_library`
      写东西）⇒ `dur` 恒为 0 ⇒ 长度护栏被整条跳过（P1 误判剪辑版）。
      把导入库并进来一次解决三件事：
        · P1：`dl` 有值 ⇒ 正常走 `FP_DUR_RATIO` 护栏；
        · P2：判据 ① （同 song_id）能命中"上次导过同一个包"；
        · P3：弹窗能显示导入库里的文件名（`src="imported"`）。
    ⚠ **只读**打开（`mode=ro`）：导入前的提示环节不许有写权限。
    ⚠ 不传 `base_dir` 的调用方（`panels._dedupe_imported` 等展示层）**行为不变**：
      展示层若把"导入行"并进本机集合，会把自己判成重复而全部隐掉。
    """
    if not base_dir:
        return []
    p = os.path.join(str(base_dir), "imported_meta.db")
    if not os.path.isfile(p):
        return []
    out = []
    conn = None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % p.replace("?", "%3f"), uri=True)
        rows = conn.execute(
            "SELECT song_id, file_name, duration, rel_path, pack FROM songs"
        ).fetchall()
    except Exception:                                            # noqa: BLE001
        return []
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:                                        # noqa: BLE001
            pass
    for sid, fname, dur, rel, pack in rows:
        try:
            isid = int(sid)
        except (TypeError, ValueError):
            continue
        if not isid:
            continue
        nm = str(fname or rel or "")
        out.append({"sid": isid, "name": norm_name(nm), "dur": to_dur(dur),
                    # `path` 留空：导入歌在本机**没有**文件路径（只有元数据+指纹）；
                    # 界面/日志要显示的是文件名 ⇒ 看 `disp`。
                    "path": "", "disp": os.path.basename(nm.replace("\\", "/")),
                    "src": "imported", "pack": str(pack or "")})
    return out


def cfg_get(cfg, key, default=None):
    """读配置项 —— **兼容 `config.Config` 与普通 dict**。

    ★★★ 2026-11-01 18:15（用户原话「**怎么还是正常导入了呢**」）：
      真机上导入前判重**一次都没生效过**，因为 `ui_main._ask_import_duplicates`
      传的是 `self.cfg`（`config.Config` 实例），而 `Config` **只有 `[]`、没有 `.get()`**
      （项目里早有这条：`⚠ Config 没有 .get() → cfg[k]`）。
      `local_tracks` 原来写 `cfg.get("music_meta")` ⇒ `AttributeError` ⇒ 调用方
      `except Exception: return set()` 吞掉 ⇒ 不弹窗、直接全部导入，且**一声不响**。
      ⚠ 所有测试都用 dict 当夹具（`{"music_library": [...]}`），所以永远是绿的 ——
      这正是"夹具必须与生产接线逐字一致"那条铁律的又一次现场。
      ⇒ 读 cfg 一律走这个函数，**别再直接 `cfg.get(...)`**。
    """
    if cfg is None:
        return default
    g = getattr(cfg, "get", None)          # dict / 带 get 的包装
    if callable(g):
        try:
            v = g(key)
            if v is not None:
                return v
        except Exception:                                        # noqa: BLE001
            pass
    try:                                    # Config 走 []（它有 __getitem__）
        v = cfg[key]
        if v is not None:
            return v
    except Exception:                                            # noqa: BLE001
        pass
    d = getattr(cfg, "data", None)          # 兜底：Config 的裸 dict
    if isinstance(d, dict):
        return d.get(key, default)
    return default


def local_tracks(cfg, base_dir=None):
    """把**本机已有曲目**整理成可比对的表：`[{"sid","name","dur","path","disp","src"}]`。

    - `sid`：优先用 `cfg["music_meta"][path]["song_id"]`（扫描时写下的，就是本机的真 id）；
      没有就**现算** `erlib.format.song_id_of(path)` —— 与扫描/打包用的是同一个算法，
      ⚠ **不改算法**（改了会作废用户已有的整库指纹）。
    - `dur`：`music_meta[path]["grid"]["dur"]` 优先（曲库面板显示的也是它），
      退回 `music_meta[path]["duration"]`；都没有 ⇒ 0.0（= 未知，不参与判据 ②）。
    - 覆盖范围 = `music_library` ∪ `music_meta` 的键（有些歌被移出曲库但分析数据还在，
      它们仍算"本机有"，宁可多拦截一条重复）。
    - `base_dir` 给了 ⇒ 再把**已导入**的曲目（`imported_meta.db`）并进来
      （`src="imported"`；已经被本机曲库收录的 sid **以曲库那条为准**，不重复）。
      ★ 2026-11-01 P1/P2/P3，理由见 `imported_tracks`。
      ⚠ 导入前提示（`scan_package`）**要传**；展示层去重（panels）**不要传**。
    """
    cfg = cfg or {}
    meta = cfg_get(cfg, "music_meta") or {}
    lib = list(cfg_get(cfg, "music_library") or [])
    seen = set()
    paths = []
    for p in list(lib) + list(meta.keys()):
        ps = str(p or "")
        if not ps or ps in seen:
            continue
        seen.add(ps)
        paths.append(ps)
    out = []
    for p in paths:
        rec = meta.get(p)
        if not isinstance(rec, dict):
            rec = {}
        sid = 0
        try:
            sid = int(rec.get("song_id") or 0)
        except (TypeError, ValueError):
            sid = 0
        if not sid:
            try:
                from erlib.format import song_id_of
                sid = int(song_id_of(p))
            except Exception:                                    # noqa: BLE001
                sid = 0
        g = rec.get("grid") if isinstance(rec.get("grid"), dict) else {}
        dur = to_dur(g.get("dur")) or to_dur(rec.get("duration"))
        base = os.path.basename(p.replace("\\", "/"))
        # `disp` = **原样**文件名（不折小写/不切目录）：只给弹窗文案用
        #（判据 ③ 提示「… ↔ 本机 `Extra Pain.flac`」，要让人一眼认出是哪首）。
        out.append({"sid": sid, "name": norm_name(base),
                    "dur": dur, "path": p, "disp": base, "src": "library"})
    if base_dir:
        # 已导入的曲目并进来（P1/P2/P3）。⚠ 同 sid 已在曲库里 ⇒ **以曲库那条为准**
        #（曲库那条有真路径 + grid.dur，信息更全）。
        have = {t.get("sid") for t in out if t.get("sid")}
        for t in imported_tracks(base_dir):
            if t.get("sid") in have:
                continue
            have.add(t.get("sid"))
            out.append(t)
    return out


def package_tracks(reader):
    """把**包里的歌**整理成同结构的表：`[{"sid","file_name","name","dur"}]`。

    `dur` = `songs.duration` 优先，退回 `grid.dur`（两个都写过的老包以 duration 为准：
    它是文件头里的真实时长，`grid.dur` 只是"参与网格分析的那段"的长度）。
    """
    out = []
    try:
        songs = reader.songs()
    except Exception:                                            # noqa: BLE001
        return out
    for s in songs:
        try:
            sid = int(s.get("song_id") or 0)
        except (TypeError, ValueError):
            sid = 0
        g = s.get("grid") if isinstance(s.get("grid"), dict) else {}
        name = s.get("file_name") or s.get("rel_path") or ""
        out.append({"sid": sid, "file_name": str(name),
                    "name": norm_name(name),
                    "dur": to_dur(s.get("duration")) or to_dur(g.get("dur"))})
    return out


def local_fp_db_path():
    """本机指纹库的路径（`<数据目录>/fingerprints.db`）。

    ⚠ 必须**运行时**取 `config.app_base_dir`（模块属性，不用 `from … import`）：
      测试/打包两种运行方式下数据目录不同，且 `tools/_test_import_dup_fp.py`
      靠"临时替换 `config.app_base_dir`"把整套流程关进 `tempfile.mkdtemp()` ——
      绝不碰用户真实的 `%LOCALAPPDATA%\\AutoVJ`。
    """
    try:
        import config
        base = config.app_base_dir()
    except Exception:                                            # noqa: BLE001
        return ""
    try:
        return os.path.join(base, "fingerprints.db")
    except Exception:                                            # noqa: BLE001
        return ""


def sample_hashes(reader, sid, n=DUP_FP_SAMPLE):
    """从**包内**某一首取 `n` 条 hash 去本机库查（按 offset 排序后**等距**采样）。

    为什么按 offset 排序：`fingerprints_of` 的返回顺序是 SQLite 的物理顺序，
      同一首在两台机器上可能不同 ⇒ 不排序的话"等距采样"会抽到不同位置，
      `overlap` 就不是确定的数（本文件的判据必须可复现，见模块注释）。

    为什么等距而不是随机：等距覆盖整首歌的**所有时间段**（前奏/主歌/副歌/尾奏），
      随机抽样在小样本下可能一头扎在某一段里 ⇒ 母带差异集中在某段时重合率虚高/虚低。
      等距 + 排序 ⇒ 同一首歌抽到的位置永远是同一批 ⇒ `overlap` 可复现（测试 (d)）。

    包内没这张表 / 这首没有指纹 ⇒ 返回 `[]`（调用方据此跳过判据 ③，绝不抛）。
    """
    n = int(n or 0)
    if n <= 0:
        return []
    try:
        rows = reader.fingerprints_of(int(sid))
    except Exception:                                            # noqa: BLE001
        return []
    if not rows:
        return []
    # 排序键里带 hash：极端情况下（同一 offset 被写两次）采样结果也与物理顺序无关。
    rows = sorted(rows, key=lambda t: (int(t[1]), str(t[0])))
    total = len(rows)
    if total <= n:
        return [str(h) for h, _o in rows]
    step = float(total) / float(n)
    out = []
    for i in range(n):
        idx = int(i * step)
        if idx >= total:              # 浮点尾巴兜底（step 精确时不会触发）
            idx = total - 1
        out.append(str(rows[idx][0]))
    return out


class LocalFingerprints:
    """本机 `fingerprints.db` 的**只读**查询器 —— 判据 ③ 的"另一半"。

    ## 为什么自己开一个连接（而不是复用 `fp.FingerprintDB`）
    1. `FingerprintDB.__init__` 会 `CREATE TABLE/INDEX`、会按 `user_version`
       **清库重扫** —— 导入前的"提示"环节只该读，绝不能有写权限；
    2. 这里只要一句 `SELECT … WHERE hash IN (…)`，用 `mode=ro` URI 打开最安全
       （连误写都物理不可能：SQLite 直接拒绝写事务）。

    ## 不可用时的语义（关键）
    指纹库不存在 / 表缺失 / 空表 ⇒ `available()` 为 False ⇒ 调用方**静默跳过**
      判据 ③。理由：用户的机器可能还没扫过指纹库，导入照样得能用；
      不能让"判重增强"反过来把导入本身挡住。
    """

    def __init__(self, path=None):
        self.path = str(path if path is not None else local_fp_db_path())
        self.conn = None
        self.rows = 0
        if not self.path or not os.path.isfile(self.path):
            return
        conn = None
        try:
            # 只读 URI（`?` 转义，文件名里真有问号也不会拼坏 URI）
            conn = sqlite3.connect(
                "file:%s?mode=ro" % self.path.replace("?", "%3f"), uri=True)
            self.rows = int(conn.execute(
                "SELECT COUNT(*) FROM fingerprints").fetchone()[0])
        except Exception:                                        # noqa: BLE001
            try:
                if conn is not None:
                    conn.close()
            except Exception:                                    # noqa: BLE001
                pass
            return
        if self.rows <= 0:
            try:
                conn.close()
            except Exception:                                    # noqa: BLE001
                pass
            return
        self.conn = conn

    def available(self):
        """能不能用（库在、表在、有条目）。False ⇒ 判据 ③ 静默跳过。"""
        return self.conn is not None

    def overlaps(self, hashes, sample_size=0):
        """`hashes` 在本机库里的重合率：返回 `{本机 song_id: overlap(0~1)}`。

        `overlap` 的分母 = **采样条数**（默认取 `len(hashes)`；调用方可另传
          `sample_size`，用于 hashes 去过重的场景）。分子 = **命中的不同 hash 数**。

        ⚠ 分子必须按 hash **去重**：本机库对同一首可能有多行同 hash（不同 offset），
          `SELECT song_id, COUNT(*) … GROUP BY song_id` 会把它们重复计数 ⇒ 重合率虚高
          （实测同一首会出现 "命中 3200/采样 1200" 这种 >100% 的荒谬值）。
          所以这里 `GROUP BY song_id, hash`，再在 Python 里数行数。
        """
        if self.conn is None:
            return {}
        uniq = []
        seen = set()
        for h in (hashes or ()):
            hs = str(h)
            if hs in seen:
                continue
            seen.add(hs)
            uniq.append(hs)
        if not uniq:
            return {}
        denom = float(int(sample_size) if int(sample_size or 0) > 0 else len(uniq))
        hits = {}
        for i in range(0, len(uniq), FP_QUERY_CHUNK):
            chunk = uniq[i:i + FP_QUERY_CHUNK]
            sql = ("SELECT song_id, hash FROM fingerprints WHERE hash IN (%s) "
                   "GROUP BY song_id, hash" % ",".join("?" * len(chunk)))
            try:
                rows = self.conn.execute(sql, chunk).fetchall()
            except Exception:                                    # noqa: BLE001
                # 表结构不对 / 库被占用 / 损坏 ⇒ 这条判据**静默失效**（不挡导入）
                return {}
            for sid, _h in rows:
                try:
                    k = int(sid)
                except (TypeError, ValueError):
                    continue
                hits[k] = hits.get(k, 0) + 1
        out = {}
        for sid, cnt in hits.items():
            out[sid] = min(1.0, float(cnt) / denom)
        return out

    def close(self):
        try:
            if self.conn is not None:
                self.conn.close()
        except Exception:                                        # noqa: BLE001
            pass
        self.conn = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


def make_fp_lookup(reader, fpdb, n=DUP_FP_SAMPLE):
    """造判据 ③ 的查询函数：`track -> {本机 song_id: overlap}`（带缓存）。

    包内同一首只会被问一次（`find_duplicates` 逐首遍历），缓存是为了
    "同一首在包里出现两次"（老包可能重复）时不重复查库。
    """
    cache = {}

    def look(track):
        try:
            sid = int(track.get("sid") or 0)
        except (TypeError, ValueError):
            return {}
        if not sid:
            return {}
        if sid in cache:
            return cache[sid]
        hs = sample_hashes(reader, sid, n)
        got = fpdb.overlaps(hs, sample_size=len(hs)) if hs else {}
        cache[sid] = got
        return got

    return look


def find_duplicates(pkg_tracks, local, dur_tol=DUR_TOL,
                    fp_of=None, fp_min=None):
    """比对：返回 `(dup_sids:set[int], items:list[dict])`。

    `items` 每条 = `{"sid","file_name","how","local","overlap"}`，
    `how` ∈ {"id","name","fingerprint"}：
    · `"id"`          —— 同 song_id（严格判据）；
    · `"name"`        —— 同名 + 时长差 ≤ 容差（解决"同一文件不同路径写法"）；
    · `"fingerprint"` —— 指纹重合 ≥ `fp_min`（同一首的不同母带，2026-11-01 新增）。

    ## 优先级（`song_id` > `name` > `fingerprint`）
    前者命中就**不再算后者**：判据 ① 严格零误判，没必要再看指纹；
    判据 ② 命中说明"就是同一个文件"，也没必要（且能省下 3 次查库/首）。

    ## 判据 ③ 的两道闸
    重合率 ≥ `fp_min`（默认 `DUP_FP_MIN`）**且** 两个时长之比 ≥ `FP_DUR_RATIO`
    （防"电台版 vs 加长版"—— 后者是前者的超集，单看重合率会被误判成同一首）。

    ## `fp_of` 为 None ⇒ 判据 ③ **整体关闭**（= 2026-11-01 之前的行为）
    `panels._dup_imported_items` 等旧调用方传两个参数就自动走这条路径，
    行为与改动前逐值一致；只有 `scan_package`（导入前提示）会传 `fp_of`。

    `fp_min=None` ⇒ 运行时读模块常量 `DUP_FP_MIN`。**故意不用默认参数绑定**
    （`fp_min=DUP_FP_MIN` 会在 import 时定格）：测试要能把门槛临时改掉/关掉，
    否则没法做反向复现。
    """
    dur_tol = to_dur(dur_tol) or DUR_TOL
    thr = DUP_FP_MIN if fp_min is None else float(fp_min)
    by_sid = {}
    by_name = {}
    for t in (local or ()):
        sid = int(t.get("sid") or 0)
        if sid and sid not in by_sid:
            by_sid[sid] = t
        nm = t.get("name") or ""
        if nm:
            by_name.setdefault(nm, []).append(t)
    dup_sids = set()
    items = []
    for t in (pkg_tracks or ()):
        sid = int(t.get("sid") or 0)
        hit, how, overlap = None, "", 0.0
        if sid and sid in by_sid:
            hit, how = by_sid[sid], "id"
        else:
            du = to_dur(t.get("dur"))
            if du > 0.0:
                for c in by_name.get(t.get("name") or "", ()):
                    cd = to_dur(c.get("dur"))
                    # ⚠ 两边都要有时长才允许用判据 ②（未知 ≠ 相同，见模块注释）
                    if cd > 0.0 and abs(du - cd) <= dur_tol:
                        hit, how = c, "name"
                        break
            # ③ 指纹重合：①② 都没中才进来（省查询 + 优先级见 docstring）
            if hit is None and fp_of is not None and thr < 1.0:
                cands = {}
                try:
                    cands = fp_of(t) or {}
                except Exception:                                # noqa: BLE001
                    cands = {}
                du_pkg = du          # 已在上面算过（判据② 用过），别重复取
                # 本机时长未知时的替代门槛（P1，见 FP_DUR_UNKNOWN_MIN）。
                # ⚠ 读模块属性（不是默认参数）：测试 `--reverse` 要能把它改回 0。
                thr_unk = max(thr, float(FP_DUR_UNKNOWN_MIN))
                best_sid, best_ov = 0, 0.0
                for lsid, ov in cands.items():
                    try:
                        lsid = int(lsid)
                        ov = float(ov)
                    except (TypeError, ValueError):
                        continue
                    # 长度护栏：剪辑版/加长版（差 10% 以上）不算"同一首的母带差异"
                    # （理由见 FP_DUR_RATIO）
                    lt = by_sid.get(lsid) or {}
                    dl = to_dur(lt.get("dur"))
                    if du_pkg > 0.0 and dl > 0.0:
                        lo, hi = (du_pkg, dl) if du_pkg <= dl else (dl, du_pkg)
                        if hi > 0.0 and (lo / hi) < FP_DUR_RATIO:
                            continue
                        need = thr
                    else:
                        # ★★ 两边有一个时长拿不到 ⇒ 长度护栏用不上 ⇒ **门槛必须更严**
                        #   （否则剪辑版只靠 45% 的重合率就能骗过判重，QA P1 实测过）
                        need = thr_unk
                    if ov >= need and ov > best_ov:
                        best_sid, best_ov = lsid, ov
                if best_sid:
                    # 本机库里有、但当前曲库已移除的 sid：`by_sid` 查不到 ⇒
                    # 用占位条目（照样判重 —— 指纹还在库里，只是没在曲库列表里）。
                    hit = by_sid.get(best_sid) or {
                        "sid": best_sid, "name": "", "dur": 0.0, "path": "",
                        "disp": "", "src": ""}
                    how, overlap = "fingerprint", best_ov
        if hit is not None:
            if sid:
                dup_sids.add(sid)
            items.append({"sid": sid,
                          "file_name": t.get("file_name") or t.get("name") or "",
                          "how": how, "local": hit.get("path") or "",
                          "local_name": hit.get("disp") or hit.get("name") or "",
                          "local_src": hit.get("src") or "",
                          "overlap": round(overlap, 4)})
    return dup_sids, items


def scan_package(pkg_path, cfg, dur_tol=DUR_TOL, fp_min=None, use_fp=True,
                 base_dir=None):
    """打开包、与本机比对（**只读**，不改任何东西）。

    返回 `{"total": int, "tracks": [...], "dups": [...], "dup_sids": [int, ...]}`。
    包打不开 / 读不了 ⇒ 返回空结果（绝不抛：导入前的"提示"环节不该挡住导入本身，
    真正的版本校验在 `transfer.import_into` 里，那里才该报错）。

    `use_fp=False` / 本机指纹库不可用 ⇒ 判据 ③ 静默跳过（其余两条照常）。

    `base_dir` = 数据目录。给了 ⇒ 把 `imported_meta.db` 里**已导入**的曲目并进
    "本机已有"（P1 护栏盲区 + P2 同 sid + P3 文案，见 `imported_tracks`）。
    不给 ⇒ 用本机指纹库所在目录（同一个数据目录，语义一致）。
    """
    from erlib.format import LibReader
    tracks = []
    fpdb = None
    if base_dir is None:
        _fp = local_fp_db_path()
        base_dir = os.path.dirname(_fp) if _fp else ""
    try:
        r = LibReader(pkg_path)
    except Exception:                                            # noqa: BLE001
        return {"total": 0, "tracks": [], "dups": [], "dup_sids": []}
    try:
        try:
            tracks = package_tracks(r)
        except Exception:                                        # noqa: BLE001
            tracks = []
        fp_of = None
        if use_fp and tracks:
            fpdb = LocalFingerprints()
            if fpdb.available():
                fp_of = make_fp_lookup(r, fpdb, DUP_FP_SAMPLE)
        dup_sids, items = find_duplicates(
            tracks, local_tracks(cfg, base_dir), dur_tol,
            fp_of=fp_of, fp_min=fp_min)
    finally:
        if fpdb is not None:
            fpdb.close()
        try:
            r.close()
        except Exception:                                        # noqa: BLE001
            pass
    return {"total": len(tracks), "tracks": tracks, "dups": items,
            "dup_sids": sorted(dup_sids)}


def dup_prompt_text(dups, total, limit=5):
    """导入前弹窗的正文（纯文本）。

    ⚠ 放在这里而不是 ui_main：文案要和判据一起被测试（`_test_import_dup.py`），
      否则改判据时提示词很容易变成"说了假话"。

    ★★ 2026-11-01（用户「没做指纹对比吗」）改成**分类报数**：
      判据 ②（同名同长）和判据 ③（指纹重合）性质完全不同，混在一起说会让用户
      以为"同一首的不同版本"也是"重复的文件" ⇒ 他就不敢点「跳过这些」了，
      于是又回到"库里两条一样的歌、识别命中哪一条不稳定"。
      · 「同名同长：N 首」                  —— 就是同一个文件（不同路径写法）；
      · 「同一首的不同版本：M 首（指纹重合 xx%，如 A ↔ 本机 B）」 —— 母带不同但确实是同一首。
      三按钮不变：跳过这些（推荐）/ 仍然全部导入 / 取消。
    """
    dups = list(dups or ())
    n = len(dups)
    limit = max(0, int(limit))
    head = ("这个分析包共 %d 首，其中 %d 首本机曲库里已经有了：\n" % (int(total), n))
    lines = []

    def _items(how):
        return [d for d in dups if str(d.get("how") or "") == how]

    def _pct(v):
        try:
            return int(round(float(v or 0.0) * 100.0))
        except (TypeError, ValueError):
            return 0

    # ① 同 song_id（老判据，保留原措辞：测试 (b5)/现场日志都按它读）
    g_id = _items("id")
    if g_id:
        lines.append("· 编号相同（同一个文件）：%d 首" % len(g_id))
        for it in g_id[:limit]:
            lines.append("    · %s" % str(it.get("file_name") or ""))
        if len(g_id) > limit:
            lines.append("    · … 另外还有 %d 首" % (len(g_id) - limit))
    # ② 同名 + 时长相同
    g_nm = _items("name")
    if g_nm:
        lines.append("· 同名同长：%d 首" % len(g_nm))
        for it in g_nm[:limit]:
            lines.append("    · %s" % str(it.get("file_name") or ""))
        if len(g_nm) > limit:
            lines.append("    · … 另外还有 %d 首" % (len(g_nm) - limit))
    # ③ 指纹重合（同一首的不同母带/修版）—— 带"本机是哪首"，否则用户无从判断
    g_fp = _items("fingerprint")
    if g_fp:
        top = max(_pct(it.get("overlap")) for it in g_fp)
        lines.append("· 同一首的不同版本：%d 首（指纹重合 %d%%，"
                     "母带/修版不同，其实是同一首歌）" % (len(g_fp), top))
        for it in g_fp[:limit]:
            nm = str(it.get("file_name") or "")
            ln = str(it.get("local_name") or "")
            src = str(it.get("local_src") or "")
            # ★ 2026-11-01 P3：对照的那一份可能来自**导入库**（上次导过同一个包），
            #   不能笼统写"本机"，也不能写"曲库列表里没有对应文件"（用户看不懂/以为出错）。
            if ln and src == "imported":
                tail = "　↔ 本机已导入的那份：%s" % ln
            elif ln:
                tail = "　↔ 本机：%s" % ln
            else:
                tail = "　↔ 本机指纹库里已有这一份（曲库和已导入列表里都没有条目）"
            lines.append("    · %s　[指纹重合 %d%%]%s"
                         % (nm, _pct(it.get("overlap")), tail))
        if len(g_fp) > limit:
            lines.append("    · … 另外还有 %d 首" % (len(g_fp) - limit))
    # 兜底：未知 how（将来新增判据时别让弹窗变成"0 首"却说有重复）
    g_ot = [d for d in dups if str(d.get("how") or "") not in
            ("id", "name", "fingerprint")]
    if g_ot:
        lines.append("· 其它可疑重复：%d 首" % len(g_ot))
        for it in g_ot[:limit]:
            lines.append("    · %s" % str(it.get("file_name") or ""))
    tail = ("\n"
            "跳过这些（推荐）：不写元数据、也不写指纹 —— 否则曲库里会出现两条一样的歌，\n"
            "指纹库里也会有两套编号（现场识别命中哪一套不稳定）。\n"
            "仍然全部导入：保持原样（你确定要重复）。")
    return head + "\n".join(lines) + tail
