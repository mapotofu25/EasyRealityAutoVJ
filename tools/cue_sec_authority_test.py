# -*- coding: utf-8 -*-
"""「按指纹对齐」不变量：**触发时刻只由秒决定，与 BPM 无关**。

## 这条链的等式（用户追问「不能按指纹对齐吗」）

    演出端位置：pos_beats = offset_sec / beat_len        （offset_sec = 指纹算出的原曲秒）
    片段坐标  ：at_beats  = at_sec   / beat_len          （读包时由秒反推）
    触发判据  ：at_beats <= pos_beats  ⟺  at_sec <= offset_sec

⇒ **bpm 在比较里结构性抵消**。所以：
  · BPM 填错 / 留空 / 手改 `.ercue` 里的 bpm  ⇒ 触发时刻不变
  · 一首歌多段变速                                ⇒ 触发时刻不变（位置是真实秒）
  · 唯一要求：`at_sec` 与 `at_beats` 描述的是同一个时刻（编排端就是这么写的）

## 反向复现
把 `from_dict` 改回"信任 at_beats、忽略 at_sec"（旧实现），第 ② 组必须挂 ——
因为那时 bpm 一旦不一致，秒坐标就会整体缩放。
"""
from __future__ import annotations

import copy
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import cuesheet as cs
import cuesheet_runtime as cr

OK, FAIL = [], []


def chk(cond, desc, detail=""):
    (OK if cond else FAIL).append(desc)
    print("  [%s] %s%s" % ("ok  " if cond else "FAIL", desc,
                           ("  —— " + str(detail)) if detail else ""))
    return bool(cond)


def raw_sheet(bpm, items):
    """造一份 cuesheet.json 结构（items 由调用方给 at_beats/at_sec）。"""
    return {
        "format_version": 1, "title": "sec-authority",
        "res_w": 1920, "res_h": 1080,
        "media": [{"clip_id": "c1", "name": "x.mov"}],
        "tracks": [{
            "track_key": "t", "identity": {"title": "T", "artist": "A", "bpm": bpm,
                                          "duration_sec": 300.0, "file_name": "t.flac"},
            "composition": {"layers": [{
                "lyr_id": "L1", "name": "L1", "z": 0, "blend": "normal", "opacity": 1.0,
                "items": items,
            }]},
        }],
    }


def item(iid, at_beats=None, at_sec=None, end_beats=None, end_sec=None):
    it = {"item_id": iid, "clip_id": "c1", "trans_in": "fade", "trans_in_beats": 2.0,
          "trans_out": "fade", "trans_out_beats": 2.0}
    if at_beats is not None:
        it["at_beats"] = at_beats
    if at_sec is not None:
        it["at_sec"] = at_sec
    if end_beats is not None:
        it["end_beats"] = end_beats
    if end_sec is not None:
        it["end_sec"] = end_sec
    return it


def at_sec_of(sheet, item_id="i0"):
    te = sheet.tracks[0]
    for it in te.layers[0].items:
        if it.item_id == item_id:
            return float(it.at_sec)
    return None


def picked_at_sec(sheet, sec):
    """把"原曲第 sec 秒"换算成拍位，问运行时该选哪个片段（模拟真实链路）。"""
    rt = cr.CueRuntime(sheet, sheet.tracks[0], media_paths={"c1": "/tmp/x.mov"})
    pos = sec / float(rt.beat_len)
    return rt.update(pos)[0].item_id


def main():
    # ================= ① 秒为准：at_beats 写错也不影响 =================
    print("\n===== ① at_beats 故意写错（250 拍），秒写着 50.0 ⇒ 必须按 50 秒触发 =====")
    d = raw_sheet(120.0, [item("i0", at_beats=250.0, at_sec=50.0)])
    sh = cs.from_dict(d)
    chk(abs(at_sec_of(sh) - 50.0) < 1e-9, "读包后 at_sec == 50.0", at_sec_of(sh))
    chk(picked_at_sec(sh, 49.0) != "i0", "第 49 秒：还没到（不选 i0）", picked_at_sec(sh, 49.0))
    chk(picked_at_sec(sh, 51.0) == "i0", "第 51 秒：已到（选 i0）", picked_at_sec(sh, 51.0))
    # 旧实现会把它当成 250 拍 = 125 秒
    old_sec = cs.beats_to_sec(250.0, 120.0)
    chk(abs(old_sec - 125.0) < 1e-9 and picked_at_sec(sh, 100.0) == "i0",
        "对照：旧口径是 125 秒（本实现第 100 秒已触发 ⇒ 按秒为准）", old_sec)

    # ================= ② bpm 随便乱写，触发时刻不变 =================
    print("\n===== ② 同一份 at_sec，bpm 改成 0 / 120 / 999 ⇒ 触发时刻完全一样 =====")
    base = [item("i0", at_beats=100.0, at_sec=50.0),
            item("i1", at_beats=200.0, at_sec=100.0)]
    for bpm in (0.0, 120.0, 999.0):
        sh = cs.from_dict(raw_sheet(bpm, copy.deepcopy(base)))
        a = picked_at_sec(sh, 49.0)
        b = picked_at_sec(sh, 51.0)
        c = picked_at_sec(sh, 99.0)
        e = picked_at_sec(sh, 101.0)
        chk((a, b, c, e) == ("", "i0", "i0", "i1"),
            "bpm=%-6.1f 第 49/51/99/101 秒 ⇒ %s" % (bpm, (a, b, c, e)),
            (a, b, c, e))

    # ================= ③ 兼容：老包没有 at_sec 键 =================
    print("\n===== ③ 老包（只有 at_beats、没有 at_sec 键）⇒ 行为与改动前一致 =====")
    d = raw_sheet(120.0, [item("i0", at_beats=100.0)])
    sh = cs.from_dict(d)
    chk(abs(at_sec_of(sh) - 50.0) < 1e-9, "at_beats=100@120bpm ⇒ at_sec 回算 50.0",
        at_sec_of(sh))
    chk(picked_at_sec(sh, 51.0) == "i0", "第 51 秒触发", picked_at_sec(sh, 51.0))

    # ================= ④ at_sec == 0 是合法值，不能被当成"缺失" =================
    print("\n===== ④ at_sec == 0.0 必须被尊重（键存在）=====")
    sh = cs.from_dict(raw_sheet(120.0, [item("i0", at_beats=999.0, at_sec=0.0)]))
    chk(abs(at_sec_of(sh)) < 1e-9, "at_sec 读出来是 0.0（不是 999 换算的秒）", at_sec_of(sh))
    chk(picked_at_sec(sh, 0.5) == "i0", "第 0.5 秒就应触发", picked_at_sec(sh, 0.5))

    # ================= ⑤ end_sec 同样以秒为准 =================
    print("\n===== ⑤ end_sec 优先（写错 end_beats 也不影响结束时刻）=====")
    sh = cs.from_dict(raw_sheet(120.0, [
        item("i0", at_beats=100.0, at_sec=50.0, end_beats=999.0, end_sec=60.0)]))
    it = sh.tracks[0].layers[0].items[0]
    chk(abs(cs.beats_to_sec(it.end_beats, 120.0) - 60.0) < 1e-6,
        "end_beats 由 end_sec 反推 ⇒ 折回 60 秒", cs.beats_to_sec(it.end_beats, 120.0))

    # ================= ⑥ 往返：写出去再读回来，秒不变 =================
    print("\n===== ⑥ 导出 → 读回：秒坐标逐值不变 =====")
    import tempfile
    sh = cs.from_dict(raw_sheet(150.0, [
        item("i0", at_beats=0.0, at_sec=12.345), item("i1", at_beats=0.0, at_sec=98.765)]))
    pkg = os.path.join(tempfile.mkdtemp(), "rt.ercue")
    cs.save(sh, pkg)
    back, _info = cs.load(pkg)
    got = [it.at_sec for it in back.tracks[0].layers[0].items]
    chk(all(abs(a - b) < 1e-6 for a, b in zip(got, (12.345, 98.765))),
        "读回后仍是 [12.345, 98.765]", got)

    print("\n" + "=" * 74)
    print("通过 %d / 失败 %d" % (len(OK), len(FAIL)))
    if FAIL:
        print("失败项：\n  - " + "\n  - ".join(FAIL))
        print("RESULT：CUE_SEC_FAIL")
        return 1
    print("RESULT：CUE_SEC_OK  按指纹（秒）对齐 / bpm 无关 / 老包兼容 全过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
