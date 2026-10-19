# -*- coding: utf-8 -*-
"""**导出指纹：音频错绑自愈**回归（用户 2026-10-18："为什么导出不带指纹"）。

现场证据（`%LOCALAPPDATA%\\EasyRealityCue\\autosave\\sheet_sheet4.json`）：

    track0 揽月          → 绑定音频 = Nimbus Spark 1999 - Supa7onyz.flac   ← 错
    track1 西行谣 feat.凛花 → 绑定音频 = 揽月 - Dreamira.flac                ← 错
    track2 Nimbus        → 绑定音频 = Nimbus Spark 1999 - Supa7onyz.flac   ← 对

导出时对每首要"现算签名 == 码表里的 fp_id"才算数 ⇒ 前两首必然不符 ⇒ **静默跳过**
⇒ 包内 `fp/` 只有 1 个成员，导出对话框写"（2 首没带指纹）"。

根因：`track_audio` 是按**曲目序号（index）**存的，序号会随"删曲目/调顺序/重新导入"变，
映射就整体错位。`_fixup_audio_path()` 用**曲目自己记录的 `identity.file_name`** 在已知
目录里找回同名文件（只在 basename 不符且确实找到时才改，绝不猜版本）。

判据：
  A. 错绑的映射经 `_export_fp_targets()` 纠正后，**每条目标的 basename == identity.file_name**；
  B. 纠正后的音频**现算签名与 `fp_id` 逐首一致**（这才是"能带上指纹"的硬判据）；
  C. 反向：用纠正前的映射直接算 ⇒ 至少一首签名不符（证明这条测试钉得住）；
  D. 找不到同名文件时**原样返回**、不瞎猜。

运行：`python tools/_ercue_fp_fixup_test.py`（约 15 秒，要真解码三首）
"""
from __future__ import annotations

import json
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from ercue.ui import identity_scan as IS                        # noqa: E402
from ercue.ui.window import MainWindow                          # noqa: E402

AUTO = os.path.join(os.environ.get("LOCALAPPDATA", ""), "EasyRealityCue",
                    "autosave", "sheet_sheet4.json")
FAIL = []


def ck(name, cond, detail=""):
    if cond:
        print("  [ok  ] %s" % name)
    else:
        print("  [FAIL] %s  %s" % (name, detail))
        FAIL.append(name)


def build_session():
    """用真实自动保存工程造一个最小 session（含**错绑**的 track_audio）。"""
    j = json.load(open(AUTO, encoding="utf-8"))
    sheet = j.get("sheet") or {}
    ta = {int(k): str(v) for k, v in (j.get("track_audio") or {}).items()}
    s = types.SimpleNamespace()
    s.sheet = types.SimpleNamespace()
    s.sheet.tracks = []
    for te in sheet.get("tracks") or []:
        idt = types.SimpleNamespace(**{
            k: (te.get("identity") or {}).get(k, "") for k in
            ("fp_id", "file_md5", "meta_id", "title", "artist", "file_name")})
        idt.duration_sec = float((te.get("identity") or {}).get("duration_sec") or 0.0)
        s.sheet.tracks.append(types.SimpleNamespace(identity=idt))
    s.track_audio = dict(ta)
    s.media_paths = {k: str(v) for k, v in (j.get("media_paths") or {}).items()}
    s.dir = os.path.dirname(next(iter(ta.values()))) if ta else ""
    return s


def as_window():
    w = types.SimpleNamespace()
    w.session = None
    w._log = lambda msg: None
    w._export_fp_targets = types.MethodType(MainWindow._export_fp_targets, w)
    w._fixup_audio_path = MainWindow._fixup_audio_path
    return w


def main():
    if not os.path.isfile(AUTO):
        print("!! 找不到自动保存工程（先打开一次 EasyRealityCue 让它自动保存）：%s" % AUTO)
        return 1
    s = build_session()
    w = as_window()
    w.session = s

    print("A. 纠正后的目标必须对上各自曲目")
    raw = {i: str(s.track_audio.get(i) or "") for i in range(len(s.sheet.tracks))}
    targets, n_id, n_au = w._export_fp_targets()
    ck("每条目标都解析出音频", len(targets) == len(s.sheet.tracks),
       "targets=%d / tracks=%d（无 id %d / 无音频 %d）"
       % (len(targets), len(s.sheet.tracks), n_id, n_au))
    ok_name = True
    for idx, fid, path in targets:
        fn = str(s.sheet.tracks[idx].identity.file_name or "")
        if fn and os.path.basename(path) != fn:
            ok_name = False
            print("       ✗ track%d 仍绑在 %s（应为 %s）" % (idx, os.path.basename(path), fn))
    ck("每条目标的 basename == identity.file_name", ok_name)

    print("B. 现算签名必须与 fp_id 一致（真解码）")
    bad = []
    for idx, fid, path in targets:
        hs, sig = IS.compute_hashes(path)
        if not hs or sig != fid:
            bad.append((idx, os.path.basename(path), sig, fid))
        print("       track%d %-40s hashes=%-6d sig=%s %s"
              % (idx, os.path.basename(path)[:40], len(hs or []), sig,
                 "OK" if sig == fid else "❌"))
    ck("三首全部签名一致（=都能带上指纹）", not bad, str(bad))

    print("C. 反向：纠正前的错绑必然算不对")
    mism = 0
    for idx in range(min(2, len(targets))):
        old = raw.get(idx) or ""
        if not os.path.isfile(old):
            continue
        _hs, sig0 = IS.compute_hashes(old)
        if sig0 != str(s.sheet.tracks[idx].identity.fp_id):
            mism += 1
    ck("错绑的音频至少有一首签名不符（这条测试钉得住）", mism >= 1,
       "不符 %d 首" % mism)

    print("D. 找不到同名文件时不许瞎猜")
    s2 = types.SimpleNamespace(track_audio={0: os.path.join(os.path.dirname(AUTO), "x.flac")},
                               media_paths={}, dir="")
    idt2 = types.SimpleNamespace(file_name="根本不存在_zzz.flac")
    got = MainWindow._fixup_audio_path(s2, 0, idt2, "P:/nope/other.flac")
    ck("原样返回（不改、不猜）", got == "P:/nope/other.flac", repr(got))

    print()
    if FAIL:
        print("结论：FAIL —— %d 项未通过：%s" % (len(FAIL), "、".join(FAIL)))
        return 1
    print("结论：PASS —— 错绑的音频会被 file_name 纠正，三首都能带上指纹。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
