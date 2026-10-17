# -*- coding: utf-8 -*-
"""两端（Cue 编排端 / AutoVJ 演出端）码表数值对照 —— **测试先行，纯逻辑零 GUI**。

为什么要有它：这些断言验的是"**算法/换算**"，不是"跑起来没崩" ——
专抓"错了也照样跑、但结果全错"的问题（拍位/拍长两端不一致、变速方向反了）。
**不过 = 不许进 Step4（热路径）**。

运行：
    python tools/twoside_parity_check.py --pkg "xxx.ercue" [--bpm 128] [--cue-src <path>]

阶段（附录 A）：
  · **阶段一 参照自检**：只用 **Cue 端 loader/runtime** 跑 ①~⑥，把公式钉死。
  · **阶段二 演出端自检**：同样 6 项改用 **AutoVJ 端 loader/runtime** 跑。
  · **阶段三 交叉比对**：`AutoVJ loader vs Cue loader` **逐值相等**
    （证明两个 exe 读同一包得到同一组数），外加每个采样点 runtime 状态对齐。

口径（★ 附录 A.1 已纠正，别改）：
    `at_sec = at_beats * (60 / bpm)`，**不减任何 anchor**。
    `at_beats` 是**相对曲首 t=0** 的拍数，不是"首拍锚定"的乐句号。
    演出端若为指纹定位引入 beatgrid 相位，那属于"**位置源**"，绝不能反向改写文件换算
    —— 相位只出现在 (1b) 位置换算里，且必须由"位置源 → pos_beats"这一段独立断言。

退出码：全过 0 且打印 `PARITY_OK`；任一失败非 0 且打印逐项 diff 表。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import zipfile

TOL = 1e-6

_HERE = os.path.dirname(os.path.abspath(__file__))
AUTOVJ_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
DEFAULT_CUE_SRC = r"P:\ERAVJ\EasyRealityCue\src"

#: 无包时的兜底样例（与 Cue 端 `ercue_io.SPEC_SAMPLE_JSON` 同一份结构）。
SPEC_SAMPLE = {
    "format_version": 1,
    "generator": {"tool": "Easy Reality AutoVJ", "version": "2026.09.29.03"},
    "created_at": "2026-09-29T03:07:11+0800",
    "min_app_version": "2026.09.29.03",
    "requires_features": ["clip_crop", "clip_fade", "clip_inout", "clip_speed",
                          "layer_multi", "beat_snap", "trans_glitch"],
    "author": {"name": ""},
    "title": "自检码表",
    "notes": "",
    "res_w": 1920, "res_h": 1080,
    "media": [
        {"clip_id": "c1", "name": "a.mp4", "sha256": "", "embedded": False,
         "duration_sec": 12.5, "has_alpha": False},
        {"clip_id": "c2", "name": "b.mp4", "sha256": "", "embedded": False,
         "duration_sec": 0.0, "has_alpha": False},
    ],
    "tracks": [{
        "track_key": "rollingcontact|cosmicstring|372",
        "identity": {"fp_id": "", "file_md5": "", "meta_id": "rollingcontact|cosmicstring|372",
                     "title": "Cosmic String", "artist": "Rolling Contact",
                     "duration_sec": 372.4, "bpm": 155.03, "key": "",
                     "file_name": "Rolling Contact - Cosmic String.flac"},
        "composition": {"layers": [{
            "lyr_id": "L1", "name": "背景", "z": 0, "blend": "screen", "opacity": 0.9,
            "items": [
                {"item_id": "i1", "clip_id": "c1", "at_beats": 32.0, "at_sec": 12.384,
                 "end_beats": None, "src_in_sec": 2.0, "src_out_sec": None, "speed": 1.25,
                 "crop": [0.05, 0.0, 0.9, 1.0],
                 "xform": {"scale": 1.0, "x": 0.0, "y": 0.0},
                 "trans_in": "glitch", "trans_in_beats": 2.0,
                 "trans_out": "fade", "trans_out_beats": 2.0,
                 "snap": "bar", "note": "副歌进来", "blend": "", "opacity": None},
                {"item_id": "i2", "clip_id": "c2", "at_beats": 96.0, "at_sec": 37.152,
                 "end_beats": None, "src_in_sec": 0.0, "src_out_sec": None, "speed": 1.0,
                 "crop": None, "xform": {"scale": 1.0, "x": 0.0, "y": 0.0},
                 "trans_in": "cut", "trans_in_beats": 2.0,
                 "trans_out": "cut", "trans_out_beats": 2.0,
                 "snap": "bar", "note": "", "blend": "", "opacity": None},
            ],
        }]},
    }],
}

# 逐值比对清单（片段级）。`blend/opacity` 比的是**有效值**（继承后）。
ITEM_FIELDS = ("at_beats", "end_beats", "src_in_sec", "src_out_sec", "speed", "crop",
               "xform", "trans_in", "trans_in_beats", "trans_out", "trans_out_beats")
IDENT_FIELDS = ("fp_id", "file_md5", "meta_id", "title", "artist", "duration_sec",
                "bpm", "key", "file_name")


# ==========================================================================
# 结果收集
# ==========================================================================
class Result:
    """一次运行的全部断言结果（按 (阶段, 项) 聚合）。"""

    def __init__(self):
        self.agg = {}          # (phase, no) -> dict
        self.details = []      # 失败明细
        self.notes = []        # N/A 等说明

    def add(self, phase, no, name, ok, expected=None, actual=None, detail=""):
        key = (phase, no)
        a = self.agg.setdefault(key, {"name": name, "n": 0, "bad": 0, "worst": 0.0,
                                      "worst_info": ""})
        a["n"] += 1
        if ok:
            return
        a["bad"] += 1
        diff = _diff(expected, actual)
        if diff is not None and diff >= a["worst"]:
            a["worst"] = diff
            a["worst_info"] = "%s 期望=%s 实得=%s" % (detail or name, _fmt(expected),
                                                     _fmt(actual))
        self.details.append("[%s #%s] %s  期望=%s  实得=%s%s"
                            % (phase, no, detail or name, _fmt(expected), _fmt(actual),
                               "" if diff is None else "  差=%.3e" % diff))

    def note(self, text):
        if text not in self.notes:          # 两个端都会各报一次同样的说明
            self.notes.append(text)

    @property
    def failed(self):
        return sum(a["bad"] for a in self.agg.values())

    def print_table(self):
        print("")
        print("=" * 108)
        print("%-22s %-4s %-30s %6s %6s %11s  %s"
              % ("阶段", "项", "断言", "样本", "失败", "最大差", "结论"))
        print("-" * 108)
        for (phase, no) in sorted(self.agg, key=lambda k: (k[0], k[1])):
            a = self.agg[(phase, no)]
            verdict = "PASS" if a["bad"] == 0 else "FAIL"
            print("%-22s %-4s %-30s %6d %6d %11.3e  %s"
                  % (phase, no, a["name"], a["n"], a["bad"], a["worst"], verdict))
        print("=" * 108)
        if self.notes:
            print("说明（N/A 与范围）：")
            for n in self.notes:
                print("  · " + n)
        if self.details:
            print("")
            print("逐项 diff（失败明细，最多 40 条）：")
            for d in self.details[:40]:
                print("  " + d)
            if len(self.details) > 40:
                print("  … 其余 %d 条省略" % (len(self.details) - 40))


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _diff(expected, actual):
    e, a = _num(expected), _num(actual)
    if e is None or a is None:
        return None
    return abs(e - a)


def _fmt(v):
    if v is None:
        return "None"
    if isinstance(v, float):
        return "%.9f" % v
    return str(v)


def _eq(exp, act, tol=TOL):
    """标量/None/列表/字典的"数值相等"（列表/字典逐元素递归）。"""
    if exp is None or act is None:
        return exp is None and act is None
    if isinstance(exp, (list, tuple)) and isinstance(act, (list, tuple)):
        return len(exp) == len(act) and all(_eq(x, y, tol) for x, y in zip(exp, act))
    if isinstance(exp, dict) and isinstance(act, dict):
        keys = set(exp) | set(act)
        return all(_eq(exp.get(k), act.get(k), tol) for k in keys)
    e, a = _num(exp), _num(act)
    if e is None or a is None:
        return exp == act
    return abs(e - a) <= tol


def _near(exp, act, tol=TOL):
    e, a = _num(exp), _num(act)
    if e is None or a is None:
        return False
    return abs(e - a) <= tol


# ==========================================================================
# 后端：Cue 编排端 / AutoVJ 演出端（都只用零 Qt 模块）
# ==========================================================================
class CueBackend:
    """Cue 端：模型内部是**秒**，`units` 是拍↔秒唯一换算点。"""
    name = "cue"

    def __init__(self):
        import ercue.model.units as units
        import ercue.model.ercue_io as ercue_io
        import ercue.model.cuesheet as cs
        import ercue.model.timeline as timeline
        self.units = units
        self.io = ercue_io
        self.cs = cs
        self.timeline = timeline

    # ---- 换算 ----
    def beat_len(self, bpm):
        return self.units.beat_len(bpm)

    def beats_to_sec(self, beats, bpm):
        return self.units.beats_to_sec(beats, bpm)

    def sec_to_beats(self, sec, bpm):
        return self.units.sec_to_beats(sec, bpm)

    # ---- 读包 ----
    def load(self, pkg):
        sheet, _info = self.io.load(pkg)
        return sheet

    def sheet_res(self, sheet):
        return sheet.aspect()

    def identity(self, track):
        idn = track.identity
        return {k: getattr(idn, k, "") for k in IDENT_FIELDS}

    def layers(self, track):
        return track.sorted_layers()

    def items(self, layer):
        return list(layer.items)

    def item_view(self, layer, item, bpm):
        """统一成"拍 + 秒"双坐标视图（比对方便，且能直接验 1a 换算）。"""
        return {
            "item_id": item.item_id, "clip_id": item.clip_id,
            "at_beats": self.sec_to_beats(item.at_sec, bpm),
            "at_sec": item.at_sec,
            "end_beats": (None if item.end_sec is None
                          else self.sec_to_beats(item.end_sec, bpm)),
            "end_sec": item.end_sec,
            "src_in_sec": item.src_in_sec, "src_out_sec": item.src_out_sec,
            "speed": item.speed, "crop": item.crop, "xform": item.xform,
            "trans_in": item.trans_in,
            "trans_in_beats": self.sec_to_beats(item.trans_in_sec, bpm),
            "trans_in_sec": item.trans_in_sec,
            "trans_out": item.trans_out,
            "trans_out_beats": self.sec_to_beats(item.trans_out_sec, bpm),
            "trans_out_sec": item.trans_out_sec,
        }

    def effective(self, layer, item):
        return layer.effective_blend(item), layer.effective_opacity(item)

    # ---- 构造合成片段（用"拍"入参，Cue 侧自己转秒）----
    def make_track(self, bpm, spec_items):
        cs = self.cs
        te = cs.TrackEntry(track_key="k",
                           identity=cs.Identity(title="T", artist="A",
                                                duration_sec=300.0, bpm=bpm))
        te.identity.ensure_meta_id()
        for li, spec in enumerate(spec_items):
            ly = cs.CompLayer(lyr_id="L%d" % li, name="L%d" % li,
                              z=int(spec.get("z", li)),
                              blend=str(spec.get("layer_blend", "normal")),
                              opacity=float(spec.get("layer_opacity", 1.0)))
            for it in spec["items"]:
                at_b = float(it.get("at_beats", 0.0))
                eb = it.get("end_beats")
                ly.items.append(cs.CueItem(
                    item_id=str(it.get("item_id", "")),
                    clip_id=str(it.get("clip_id", "c1")),
                    at_sec=self.beats_to_sec(at_b, bpm),
                    end_sec=(None if eb is None else self.beats_to_sec(eb, bpm)),
                    src_in_sec=float(it.get("src_in_sec", 0.0)),
                    src_out_sec=it.get("src_out_sec"),
                    speed=float(it.get("speed", 1.0)),
                    crop=it.get("crop"),
                    xform=dict(it.get("xform") or {"scale": 1.0, "x": 0.0, "y": 0.0}),
                    blend=str(it.get("blend", "")),
                    opacity=it.get("opacity"),
                    trans_in=str(it.get("trans_in", "fade")),
                    trans_in_sec=self.beats_to_sec(float(it.get("trans_in_beats", 2.0)), bpm),
                    trans_out=str(it.get("trans_out", "fade")),
                    trans_out_sec=self.beats_to_sec(float(it.get("trans_out_beats", 2.0)),
                                                    bpm),
                    snap="off", note="").normalized())
            te.composition.layers.append(ly)
        return te

    # ---- 运行时 ----
    def runtime(self, sheet, track):
        return self.timeline.CueRuntime(sheet, track, media_paths={})

    def update(self, rt, pos_beats, beat_len):
        """Cue 端 runtime 吃**秒** ⇒ 这里把拍换成秒（等价于位置源换算）。"""
        states = rt.update(pos_beats * beat_len)
        return {st.lyr_id: {"active": bool(st.active), "item_id": st.item_id,
                            "play_sec": float(st.play_sec), "alpha": float(st.alpha),
                            "trans": st.trans, "trans_p": float(st.trans_p),
                            "blend": st.blend, "opacity": float(st.opacity),
                            "missing": bool(st.missing), "src_ended": bool(st.src_ended),
                            "ended": bool(st.ended)} for st in states}


class AutoVJBackend:
    """演出端：模型内部是**拍**（`at_beats` 主坐标），`cuesheet` 是拍↔秒唯一换算点。"""
    name = "autovj"

    def __init__(self):
        import cuesheet as cs
        import cuesheet_runtime as rt
        self.cs = cs
        self.rt_mod = rt

    def beat_len(self, bpm):
        return self.cs.beat_len(bpm)

    def beats_to_sec(self, beats, bpm):
        return self.cs.beats_to_sec(beats, bpm)

    def sec_to_beats(self, sec, bpm):
        return self.cs.sec_to_beats(sec, bpm)

    def load(self, pkg):
        sheet, _info = self.cs.load(pkg)
        return sheet

    def sheet_res(self, sheet):
        return sheet.aspect()

    def identity(self, track):
        idn = track.identity
        return {k: getattr(idn, k, "") for k in IDENT_FIELDS}

    def layers(self, track):
        return track.sorted_layers()

    def items(self, layer):
        return list(layer.items)

    def item_view(self, layer, item, bpm):
        return {
            "item_id": item.item_id, "clip_id": item.clip_id,
            "at_beats": item.at_beats, "at_sec": item.at_sec,
            "end_beats": item.end_beats,
            "end_sec": (None if item.end_beats is None
                        else self.beats_to_sec(item.end_beats, bpm)),
            "src_in_sec": item.src_in_sec, "src_out_sec": item.src_out_sec,
            "speed": item.speed, "crop": item.crop, "xform": item.xform,
            "trans_in": item.trans_in, "trans_in_beats": item.trans_in_beats,
            "trans_in_sec": self.beats_to_sec(item.trans_in_beats, bpm),
            "trans_out": item.trans_out, "trans_out_beats": item.trans_out_beats,
            "trans_out_sec": self.beats_to_sec(item.trans_out_beats, bpm),
        }

    def effective(self, layer, item):
        return layer.effective_blend(item), layer.effective_opacity(item)

    def make_track(self, bpm, spec_items):
        cs = self.cs
        te = cs.TrackEntry(track_key="k",
                           identity=cs.Identity(title="T", artist="A",
                                                duration_sec=300.0, bpm=bpm))
        te.identity.meta_id = cs.Identity.meta_key("T", "A", 300.0)
        for li, spec in enumerate(spec_items):
            ly = cs.CompLayer(lyr_id="L%d" % li, name="L%d" % li,
                              z=int(spec.get("z", li)),
                              blend=str(spec.get("layer_blend", "normal")),
                              opacity=float(spec.get("layer_opacity", 1.0)))
            for it in spec["items"]:
                ly.items.append(cs.CueItem(
                    item_id=str(it.get("item_id", "")),
                    clip_id=str(it.get("clip_id", "c1")),
                    at_beats=float(it.get("at_beats", 0.0)),
                    end_beats=it.get("end_beats"),
                    src_in_sec=float(it.get("src_in_sec", 0.0)),
                    src_out_sec=it.get("src_out_sec"),
                    speed=float(it.get("speed", 1.0)),
                    crop=it.get("crop"),
                    xform=dict(it.get("xform") or {"scale": 1.0, "x": 0.0, "y": 0.0}),
                    blend=str(it.get("blend", "")),
                    opacity=it.get("opacity"),
                    trans_in=str(it.get("trans_in", "fade")),
                    trans_in_beats=float(it.get("trans_in_beats", 2.0)),
                    trans_out=str(it.get("trans_out", "fade")),
                    trans_out_beats=float(it.get("trans_out_beats", 2.0)),
                    snap="off", note="").normalized())
            te.layers.append(ly)
        return te

    def runtime(self, sheet, track):
        return self.rt_mod.CueRuntime(sheet, track, media_paths={})

    def update(self, rt, pos_beats, beat_len):
        """演出端 runtime 直接吃**拍**（与位置源同一坐标系）。"""
        states = rt.update(pos_beats, beat_len=beat_len)
        return {st.lyr_id: {"active": bool(st.active), "item_id": st.item_id,
                            "play_sec": float(st.play_sec), "alpha": float(st.alpha),
                            "trans": st.trans, "trans_p": float(st.trans_p),
                            "blend": st.blend, "opacity": float(st.opacity),
                            "missing": bool(st.missing), "src_ended": bool(st.src_ended),
                            "ended": bool(st.ended)} for st in states}


# ==========================================================================
# ① 文件换算 / ② 时长恒等式 / ③ 变速方向 / ④ src_out 冻结 / ⑤ fade α² / ⑥ 几何
# ==========================================================================
def _read_raw(pkg):
    """读包内 `cuesheet.json`（**原始文件值**，不做任何换算）。

    ① 文件换算必须拿它当"真值来源"：否则用 loader 自己的 at_sec 去验公式是**自证循环**。
    样例包里 `at_sec=12.384` 是旧实现截断的冗余值，而 `at_beats*(60/bpm)=12.3846997`
    ⇒ 只有"信任 at_beats、忽略文件 at_sec"的 loader 才过得了这一项。
    """
    if zipfile.is_zipfile(pkg):
        with zipfile.ZipFile(pkg) as z:
            names = z.namelist()
            name = "cuesheet.json" if "cuesheet.json" in names else next(
                (n for n in names if n.endswith(".json") and "/" not in n), "")
            if not name:
                raise SystemExit("包里没有 cuesheet.json：%s" % pkg)
            return json.loads(z.read(name).decode("utf-8", "replace"))
    with open(pkg, encoding="utf-8") as f:
        return json.load(f)


def check_1a(res, backend, sheet, pkg, bpm_override):
    """① 文件换算：`at_sec == at_beats*(60/bpm)`，**不减 anchor**；end/trans 同理。

    ★ 真值来源 = **原始 JSON 的 at_beats**（不是 loader 自己算出来的 at_sec），
      这样才验得出"loader 是否信任 at_beats、忽略文件里截断的 at_sec"。
    """
    phase = "阶段1·参照(Cue)" if backend.name == "cue" else "阶段2·演出(AutoVJ)"
    raw = _read_raw(pkg)
    raw_tracks = raw.get("tracks") or []
    for ti, track in enumerate(sheet.tracks):
        raw_t = raw_tracks[ti] if ti < len(raw_tracks) else {}
        raw_layers = ((raw_t.get("composition") or {}).get("layers") or [])
        bpm = bpm_override if bpm_override else float(track.identity.bpm or 0.0)
        bl = backend.beat_len(bpm)
        for li, layer in enumerate(backend.layers(track)):
            raw_items = (raw_layers[li].get("items") if li < len(raw_layers) else None) or []
            for ii, item in enumerate(backend.items(layer)):
                raw_it = raw_items[ii] if ii < len(raw_items) else {}
                v = backend.item_view(layer, item, bpm)
                tag = "t%d/%s/%s" % (ti, layer.lyr_id, v["item_id"])
                # loader 必须逐值保留"拍"主坐标
                res.add(phase, "1a", "文件换算 at/end/trans",
                        _near(raw_it.get("at_beats"), v["at_beats"]),
                        raw_it.get("at_beats"), v["at_beats"], tag + ".at_beats(主坐标)")
                # 秒 = 拍 × beat_len（**忽略文件 at_sec**）
                res.add(phase, "1a", "文件换算 at/end/trans",
                        _near(v["at_beats"] * bl, v["at_sec"]),
                        v["at_beats"] * bl, v["at_sec"], tag + ".at_sec")
                if raw_it.get("end_beats") is None:
                    res.add(phase, "1a", "文件换算 at/end/trans", v["end_sec"] is None,
                            None, v["end_sec"], tag + ".end_sec(None)")
                else:
                    res.add(phase, "1a", "文件换算 at/end/trans",
                            _near(raw_it.get("end_beats"), v["end_beats"]),
                            raw_it.get("end_beats"), v["end_beats"], tag + ".end_beats")
                    res.add(phase, "1a", "文件换算 at/end/trans",
                            _near(v["end_beats"] * bl, v["end_sec"]),
                            v["end_beats"] * bl, v["end_sec"], tag + ".end_sec")
                res.add(phase, "1a", "文件换算 at/end/trans",
                        _near(raw_it.get("trans_in_beats"), v["trans_in_beats"]),
                        raw_it.get("trans_in_beats"), v["trans_in_beats"],
                        tag + ".trans_in_beats")
                res.add(phase, "1a", "文件换算 at/end/trans",
                        _near(v["trans_in_beats"] * bl, v["trans_in_sec"]),
                        v["trans_in_beats"] * bl, v["trans_in_sec"], tag + ".trans_in_sec")
                res.add(phase, "1a", "文件换算 at/end/trans",
                        _near(v["trans_out_beats"] * bl, v["trans_out_sec"]),
                        v["trans_out_beats"] * bl, v["trans_out_sec"],
                        tag + ".trans_out_sec")


def check_1b(res, backend):
    """①b 位置换算（**只验位置源**，与文件无关）：`pos_beats = t / beat_len`，anchor=0。

    退化态（bpm≤0）⇒ 1 拍 = 1 秒 ⇒ `pos_beats == t`。
    ⚠ 演出端若给指纹位置加 beatgrid 相位，那是位置源的事，绝不能改写文件换算（1a）。
    """
    phase = "阶段1·参照(Cue)" if backend.name == "cue" else "阶段2·演出(AutoVJ)"
    for bpm in (155.03, 128.0, 0.0, -1.0):
        bl = backend.beat_len(bpm)
        for t in (0.0, 0.5, 12.384699735535058, 100.0):
            want = t if bpm <= 0 else t / bl
            got = backend.sec_to_beats(t, bpm)
            res.add(phase, "1b", "位置换算 t→拍（anchor=0）",
                    _near(want, got), want, got, "bpm=%g t=%g" % (bpm, t))
            # 往返：拍→秒 必须回到原值（否则"位置源 → pos_beats"会漂）
            back = backend.beats_to_sec(got, bpm)
            res.add(phase, "1b", "位置换算 t→拍（anchor=0）",
                    _near(t, back), t, back, "bpm=%g 往返 t=%g" % (bpm, t))
        if bpm <= 0:
            res.add(phase, "1b", "位置换算 t→拍（anchor=0）",
                    bl == 1.0, 1.0, bl, "退化 beat_len(bpm=%g)" % bpm)


def check_2(res, backend, sheet, bpm_override):
    """② 时长恒等式：`(end-at) == (src_out-src_in)/speed`（秒域，两端同一恒等式）。

    包内片段：`src_out is None` ⇒ `N/A`（不算失败，设计附录 A.2 允许）。
    合成片段（3 个 speed）保证此项**非空**。
    """
    phase = "阶段1·参照(Cue)" if backend.name == "cue" else "阶段2·演出(AutoVJ)"
    n_applicable = 0
    for ti, track in enumerate(sheet.tracks):
        bpm = bpm_override if bpm_override else float(track.identity.bpm or 0.0)
        bl = backend.beat_len(bpm)
        for layer in backend.layers(track):
            for item in backend.items(layer):
                v = backend.item_view(layer, item, bpm)
                if v["end_beats"] is None or v["src_out_sec"] is None:
                    continue
                n_applicable += 1
                dur = (v["end_beats"] - v["at_beats"]) * bl
                want = (float(v["src_out_sec"]) - float(v["src_in_sec"])) / float(v["speed"])
                res.add(phase, "2", "时长恒等式", _near(want, dur), want, dur,
                        "t%d/%s/%s" % (ti, layer.lyr_id, v["item_id"]))
    if n_applicable == 0:
        res.note("② 包内 %d 个片段全部 `end/src_out` 缺失 ⇒ 包内部分 N/A（由合成片段覆盖）"
                 % sum(len(backend.items(l)) for t in sheet.tracks for l in backend.layers(t)))

    # 合成：3 个 speed（src_in=0 / src_out=10 / end 按恒等式给）
    bpm = bpm_override if bpm_override else 128.0
    bl = backend.beat_len(bpm)
    for spd in (0.5, 1.0, 2.0):
        used = 10.0
        dur_sec = used / spd
        spec = [{"items": [{"item_id": "x", "at_beats": 4.0,
                            "end_beats": 4.0 + dur_sec / bl,
                            "src_in_sec": 0.0, "src_out_sec": 10.0, "speed": spd,
                            "trans_in": "cut", "trans_out": "cut"}]}]
        track = backend.make_track(bpm, spec)
        v = backend.item_view(backend.layers(track)[0],
                              backend.items(backend.layers(track)[0])[0], bpm)
        dur = (v["end_beats"] - v["at_beats"]) * bl
        res.add(phase, "2", "时长恒等式", _near(dur_sec, dur), dur_sec, dur,
                "合成 speed=%g" % spd)


def check_3(res, backend, bpm):
    """③ 变速方向：`timeline_len = (src_out-src_in)/speed` ⇒ **20 / 10 / 5 秒**。

    三重口径（缺一不可，专防"换了实现也能过"）：
      a) 公式：`(src_out-src_in)/speed`；
      b) 端到端：播 2 秒 ⇒ `play_sec == 2×speed`；
      c) **运行时实测时间轴长度**：用 runtime 自己的 `src_ended` 边沿反推"源跨度播完用了几秒"。
    若得到 5/10/20 ⇒ **FAIL（用了除法=慢放）**，现场与预览方向相反。
    """
    phase = "阶段1·参照(Cue)" if backend.name == "cue" else "阶段2·演出(AutoVJ)"
    bl = backend.beat_len(bpm)
    for spd, want_len in ((0.5, 20.0), (1.0, 10.0), (2.0, 5.0)):
        spec = [{"items": [{"item_id": "s", "at_beats": 0.0, "src_in_sec": 0.0,
                            "src_out_sec": 10.0, "speed": spd,
                            "trans_in": "cut", "trans_out": "cut"}]}]
        track = backend.make_track(bpm, spec)
        sheet = backend.cs.CueSheet(title="x")
        layer = backend.layers(track)[0]
        item = backend.items(layer)[0]
        # a) 时间轴长度（纯公式）：源跨度 10s / speed
        got_len = float(getattr(item, "src_out_sec") - getattr(item, "src_in_sec")) / \
            float(getattr(item, "speed"))
        res.add(phase, "3", "变速方向 ×speed", _near(want_len, got_len), want_len, got_len,
                "speed=%g 时间轴长度(公式)" % spd)
        # b) 端到端：播 2 秒 ⇒ play_sec 必须 = 2×speed（除法会得到 2/1/0.5）
        rt = backend.runtime(sheet, track)
        pos_beats = 2.0 / bl
        st = backend.update(rt, pos_beats, bl)
        play = st[layer.lyr_id]["play_sec"]
        res.add(phase, "3", "变速方向 ×speed", _near(2.0 * spd, play), 2.0 * spd, play,
                "speed=%g 播 2s 后 play_sec" % spd)
        # c) 运行时实测：单调推进，找到 src_ended 变 True 的第一个采样（分辨率 0.01s）
        rt2 = backend.runtime(sheet, track)
        measured = None
        t = 0.0
        while t <= want_len + 2.0:
            cur = backend.update(rt2, t / bl, bl)[layer.lyr_id]
            if cur["src_ended"]:
                measured = t
                break
            t += 0.01
        res.add(phase, "3", "变速方向 ×speed",
                measured is not None and abs(measured - want_len) <= 0.02,
                want_len, measured, "speed=%g 实测时间轴长度(±0.02s)" % spd)


def check_4(res, backend, bpm):
    """④ `src_out` 冻结：`frame_t(t) = min(src_in+(t-at)*speed, src_out)`，越界 3 点恒等。"""
    phase = "阶段1·参照(Cue)" if backend.name == "cue" else "阶段2·演出(AutoVJ)"
    bl = backend.beat_len(bpm)
    at, src_in, src_out, spd = 0.0, 1.0, 6.0, 1.0
    spec = [{"items": [{"item_id": "f", "at_beats": at, "src_in_sec": src_in,
                        "src_out_sec": src_out, "speed": spd,
                        "trans_in": "cut", "trans_out": "cut"}]}]
    track = backend.make_track(bpm, spec)
    sheet = backend.cs.CueSheet(title="x")
    layer = backend.layers(track)[0]
    rt = backend.runtime(sheet, track)
    end_t = (src_out - src_in) / spd            # 该片段源跨度播完所需的（曲内）秒数
    out_pos_beats = at + end_t / bl
    for k, extra in enumerate((0.01, 1.0, 5.0)):
        pos = out_pos_beats + extra / bl
        st = backend.update(rt, pos, bl)
        cur = st[layer.lyr_id]
        res.add(phase, "4", "src_out 冻结最后一帧", _near(src_out, cur["play_sec"]),
                src_out, cur["play_sec"], "越界采样%d (+%.2fs)" % (k + 1, extra))
        res.add(phase, "4", "src_out 冻结最后一帧", cur["src_ended"] is True,
                True, cur["src_ended"], "越界采样%d src_ended" % (k + 1))
        res.add(phase, "4", "src_out 冻结最后一帧", cur["active"] is True,
                True, cur["active"], "越界采样%d 不黑(active)" % (k + 1))
    # 未到出点：必须还在走（证明不是"一上来就冻"）
    st = backend.update(backend.runtime(sheet, track), (at + end_t / 2.0 / bl), bl)
    cur = st[layer.lyr_id]
    res.add(phase, "4", "src_out 冻结最后一帧", cur["src_ended"] is False,
            False, cur["src_ended"], "未到出点 src_ended=False")
    res.add(phase, "4", "src_out 冻结最后一帧",
            _near(src_in + end_t / 2.0 * spd, cur["play_sec"]),
            src_in + end_t / 2.0 * spd, cur["play_sec"], "未到出点 play_sec 递增")
    # src_out=None ⇒ 不冻结（N/A 说明）
    res.note("④ `src_out=None` 的场景不冻结，按附录 A.2 记 N/A（不参与断言）")


def check_5(res, backend, bpm):
    """⑤ fade 曲线 α²：`opacity=1 / trans_in=fade / L=trans_in` ⇒ 有效 α = 0 / 0.25 / 1.0。

    有效 α = `opacity × alpha × trans_p`（`trans_p = a_in`）⇒ `a_in²`（契约②）。
    """
    phase = "阶段1·参照(Cue)" if backend.name == "cue" else "阶段2·演出(AutoVJ)"
    bl = backend.beat_len(bpm)
    at_beats, L_beats = 4.0, 2.0
    spec = [{"items": [{"item_id": "a", "at_beats": at_beats, "src_in_sec": 0.0,
                        "src_out_sec": None, "speed": 1.0, "opacity": 1.0,
                        "trans_in": "fade", "trans_in_beats": L_beats,
                        "trans_out": "cut", "trans_out_beats": 0.0}]}]
    track = backend.make_track(bpm, spec)
    sheet = backend.cs.CueSheet(title="x")
    layer = backend.layers(track)[0]
    rt = backend.runtime(sheet, track)
    for frac, want in ((0.0, 0.0), (0.5, 0.25), (1.0, 1.0)):
        pos = at_beats + L_beats * frac
        cur = backend.update(rt, pos, bl)[layer.lyr_id]
        eff = cur["opacity"] * cur["alpha"] * cur["trans_p"]
        res.add(phase, "5", "fade α²", _near(want, eff), want, eff,
                "t=at+%.1fL 有效α" % frac)
        res.add(phase, "5", "fade α²", cur["trans"] == "fade", "fade", cur["trans"],
                "t=at+%.1fL trans" % frac)


def check_6(res, backend, sheet):
    """⑥ 几何 cover/contain：G1 / G2 两个样例 + 两端"该用哪种"判据一致。"""
    phase = "阶段1·参照(Cue)" if backend.name == "cue" else "阶段2·演出(AutoVJ)"

    def ks(src_w, src_h, dst_w, dst_h):
        return (max(dst_w / float(src_w), dst_h / float(src_h)),
                min(dst_w / float(src_w), dst_h / float(src_h)))

    # G1：源 1920×1080 → 画布 1440×1080
    cover, contain = ks(1920, 1080, 1440, 1080)
    res.add(phase, "6", "几何 cover/contain", _near(1.0, cover), 1.0, cover, "G1 cover")
    res.add(phase, "6", "几何 cover/contain", _near(0.75, contain), 0.75, contain,
            "G1 contain")
    # G2：源 1440×1080 → 画布 1920×1080
    cover, contain = ks(1440, 1080, 1920, 1080)
    res.add(phase, "6", "几何 cover/contain", _near(1.3333333333333333, cover),
            1.3333333333333333, cover, "G2 cover")
    res.add(phase, "6", "几何 cover/contain", _near(1.0, contain), 1.0, contain,
            "G2 contain")
    # 判据：xform 全默认 ⇒ cover；有任一变换 ⇒ contain（逐片段验两端口径）
    for ti, track in enumerate(sheet.tracks):
        bpm = float(track.identity.bpm or 0.0)
        for layer in backend.layers(track):
            for item in backend.items(layer):
                v = backend.item_view(layer, item, bpm)
                xf = v["xform"] or {}
                is_default = (abs(float(xf.get("scale", 1.0) or 1.0) - 1.0) < 1e-9
                              and abs(float(xf.get("x", 0.0) or 0.0)) < 1e-9
                              and abs(float(xf.get("y", 0.0) or 0.0)) < 1e-9
                              and abs(float(xf.get("rot", 0.0) or 0.0)) < 1e-9)
                local = getattr(item, "xform_is_default", None)
                if callable(local):
                    got = bool(local())
                else:                       # Cue 端无该便捷函数 ⇒ 用同一套字段判据
                    got = is_default
                res.add(phase, "6", "几何 cover/contain", got == is_default,
                        is_default, got, "t%d/%s/%s xform默认判据"
                        % (ti, layer.lyr_id, v["item_id"]))


# ==========================================================================
# 阶段三：交叉比对（同一包 → 同一组数）
# ==========================================================================
def cross_check(res, cue, avj, pkg, bpm_override):
    cue_sheet = cue.load(pkg)
    avj_sheet = avj.load(pkg)

    if not _eq(len(cue_sheet.tracks), len(avj_sheet.tracks)):
        res.add("阶段3·交叉", "X0", "曲目数", False, len(cue_sheet.tracks),
                len(avj_sheet.tracks), "tracks.len")
    res.add("阶段3·交叉", "X0", "画布比例 res_w/res_h",
            _eq(list(cue.sheet_res(cue_sheet)), list(avj.sheet_res(avj_sheet))),
            cue.sheet_res(cue_sheet), avj.sheet_res(avj_sheet), "sheet.aspect()")

    n_items = 0
    for ti, (t_c, t_a) in enumerate(zip(cue_sheet.tracks, avj_sheet.tracks)):
        ic, ia = cue.identity(t_c), avj.identity(t_a)
        for k in IDENT_FIELDS:
            res.add("阶段3·交叉", "X1", "identity 逐字段", _eq(ic[k], ia[k]),
                    ic[k], ia[k], "t%d.identity.%s" % (ti, k))
        lc, la = cue.layers(t_c), avj.layers(t_a)
        res.add("阶段3·交叉", "X1", "层数", _eq(len(lc), len(la)), len(lc), len(la),
                "t%d layers" % ti)
        for (lyc, lya) in zip(lc, la):
            bpm = bpm_override if bpm_override else float(t_c.identity.bpm or 0.0)
            res.add("阶段3·交叉", "X1", "层属性 lyr_id/z/blend/opacity",
                    _eq((lyc.lyr_id, lyc.z, lyc.blend, lyc.opacity),
                        (lya.lyr_id, lya.z, lya.blend, lya.opacity)),
                    (lyc.lyr_id, lyc.z, lyc.blend, lyc.opacity),
                    (lya.lyr_id, lya.z, lya.blend, lya.opacity),
                    "t%d/%s" % (ti, lyc.lyr_id))
            ics, ias = cue.items(lyc), avj.items(lya)
            res.add("阶段3·交叉", "X1", "片段数", _eq(len(ics), len(ias)), len(ics),
                    len(ias), "t%d/%s items" % (ti, lyc.lyr_id))
            for (ic_, ia_) in zip(ics, ias):
                n_items += 1
                vc = cue.item_view(lyc, ic_, bpm)
                va = avj.item_view(lya, ia_, bpm)
                tag = "t%d/%s/%s" % (ti, lyc.lyr_id, ic_.item_id)
                for k in ITEM_FIELDS:
                    res.add("阶段3·交叉", "X2", "片段逐值（拍/秒/速度/crop/xform）",
                            _eq(vc[k], va[k]), vc[k], va[k], tag + "." + k)
                ec, ea = cue.effective(lyc, ic_), avj.effective(lya, ia_)
                res.add("阶段3·交叉", "X2", "片段有效 blend/opacity",
                        _eq(ec[0], ea[0]), ec[0], ea[0], tag + ".effective_blend")
                res.add("阶段3·交叉", "X2", "片段有效 blend/opacity",
                        _eq(ec[1], ea[1]), ec[1], ea[1], tag + ".effective_opacity")

    # X3：同一包、同一采样点，两端 runtime 状态一致（active/item/play_sec/blend/opacity）
    n_samples = 0
    for t_c, t_a in zip(cue_sheet.tracks, avj_sheet.tracks):
        bpm = bpm_override if bpm_override else float(t_c.identity.bpm or 0.0)
        bl = cue.beat_len(bpm)
        rt_c = cue.runtime(cue_sheet, t_c)
        rt_a = avj.runtime(avj_sheet, t_a)
        # 采样：0 ~ (最后一个 end/at + 8 拍)，步长 0.25 拍（模拟逐帧单调推进）
        last = 0.0
        for layer in avj.layers(t_a):
            for item in avj.items(layer):
                last = max(last, float(item.end_beats if item.end_beats is not None
                                       else item.at_beats))
        total = max(last + 8.0, 8.0)
        pos = 0.0
        while pos <= total + 1e-9:
            sc = cue.update(rt_c, pos, bl)
            sa = avj.update(rt_a, pos, bl)
            n_samples += 1
            for lyr in sorted(set(sc) | set(sa)):
                c = sc.get(lyr, {})
                a = sa.get(lyr, {})
                for key in ("active", "item_id", "play_sec", "missing", "blend", "opacity"):
                    ok = _eq(c.get(key), a.get(key))
                    res.add("阶段3·交叉", "X3", "runtime 采样一致", ok, c.get(key), a.get(key),
                            "%s@%.2f拍.%s" % (lyr, pos, key))
            pos += 0.25
    res.note("阶段3·交叉 X3 采样点 %d 个 × 每层 active/item_id/play_sec/blend/opacity"
             "（`alpha/trans_p` 的逐点对齐见项⑤；Cue 端 #17『相邻同设置合并』"
             "属编排端增强、演出端本步不实现，故意不纳入逐点断言）" % n_samples)
    res.note("阶段3·交叉共比对 %d 个片段 × %d 个字段" % (n_items, len(ITEM_FIELDS)))


# ==========================================================================
# 入口
# ==========================================================================
def _prepare_pkg(pkg, bpm_override=0.0):
    """落一个"本脚本要用"的包路径：缺省用内置样例；`--bpm` 时先改写 bpm 再落包。

    ★ `--bpm` 必须在**解析之前**生效（改写 `cuesheet.json` 的 `identity.bpm` 后重打包）：
      两端的模型一个是秒坐标、一个是拍坐标，**加载后**再改 bpm 会让"拍/秒"互不自洽
      （①文件换算立刻自相矛盾）。用"改写文件 → 两端各自重新读"才是干净口径。
    返回 `(路径, 是否为内置样例, 说明文字)`。
    """
    if pkg:
        if not os.path.isfile(pkg):
            raise SystemExit("找不到码表包：%s" % pkg)
        raw = _read_raw(pkg)
        src_desc = "真实包"
    else:
        raw = json.loads(json.dumps(SPEC_SAMPLE))
        src_desc = "内置样例"
    if bpm_override > 0:
        for t in (raw.get("tracks") or []):
            ident = t.get("identity")
            if isinstance(ident, dict):
                ident["bpm"] = float(bpm_override)
            else:
                t["identity"] = {"bpm": float(bpm_override)}
        src_desc += "（bpm 覆盖为 %g）" % bpm_override
    tmp = os.path.join(tempfile.mkdtemp(prefix="parity_"), "parity_input.ercue")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("cuesheet.json", json.dumps(raw, ensure_ascii=False, indent=1))
    return tmp, (not pkg), src_desc


def main(argv=None):
    ap = argparse.ArgumentParser(description="两端码表数值对照（零 GUI）")
    ap.add_argument("--pkg", default="", help="真实 .ercue 包路径（缺省用内置样例）")
    ap.add_argument("--bpm", type=float, default=0.0, help="覆盖 bpm（0=用包内 identity.bpm）")
    ap.add_argument("--cue-src", default=DEFAULT_CUE_SRC, help="Cue 端 src 目录")
    ap.add_argument("--autovj-src", default=AUTOVJ_SRC,
                    help="AutoVJ 端 src 目录（默认仓库 src/；用于变异测试/CI 钉版本）")
    args = ap.parse_args(argv)

    # ---- 两个编译器路径（都只含零 Qt 模块）----
    avj_src = os.path.abspath(args.autovj_src)
    for p in (avj_src, os.path.abspath(args.cue_src)):
        if not os.path.isdir(p):
            raise SystemExit("源码目录不存在：%s" % p)
        if p not in sys.path:
            sys.path.insert(0, p)

    bpm_override = float(args.bpm or 0.0)
    pkg, is_sample, src_desc = _prepare_pkg(args.pkg, bpm_override)
    print("[parity] 输入 = %s%s" % (args.pkg or src_desc, "（内置样例）" if is_sample else ""))
    if bpm_override:
        print("[parity] bpm 覆盖 = %g（已改写 cuesheet.json 后重打包）" % bpm_override)
    print("[parity] AutoVJ src = %s" % avj_src)
    print("[parity] Cue    src = %s" % os.path.abspath(args.cue_src))
    print("[parity] TOL = %.1e" % TOL)

    cue = CueBackend()
    avj = AutoVJBackend()
    res = Result()

    # ---- 阶段一 / 阶段二：同一套 6 项断言，各跑一遍 ----
    for backend in (cue, avj):
        sheet = backend.load(pkg)
        check_1a(res, backend, sheet, pkg, bpm_override)
        check_1b(res, backend)
        check_2(res, backend, sheet, bpm_override)
        bpm_fx = bpm_override if bpm_override else float(
            sheet.tracks[0].identity.bpm or 128.0) if sheet.tracks else 128.0
        check_3(res, backend, bpm_fx)
        check_4(res, backend, bpm_fx)
        check_5(res, backend, bpm_fx)
        check_6(res, backend, sheet)

    # ---- 阶段三：交叉比对 ----
    cross_check(res, cue, avj, pkg, bpm_override)

    res.print_table()
    if res.failed:
        print("")
        print("PARITY_FAIL  失败断言 %d 条（口径见 docs/码表实装_设计.md 附录 A）" % res.failed)
        return 1
    print("")
    print("PARITY_OK  两端对同一包得到同一组数值；6 项算法口径一致（TOL=%.1e）" % TOL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
