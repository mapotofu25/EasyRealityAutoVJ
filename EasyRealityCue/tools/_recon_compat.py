# -*- coding: utf-8 -*-
"""端到端兼容实证：EasyRealityCue 导出的 .ercue → 旧实现（AutoVJ 备份）能否读取。

这是「能否在 ERAVJ 里实装」的第一道硬验证。
"""
import os
import sys
import tempfile

CUE_SRC = r"<项目根>\EasyRealityCue\src"
BAK_SRC = r"<项目根>\AutoVJ_码表备份_20260930\src"

sys.path.insert(0, CUE_SRC)
sys.path.insert(0, BAK_SRC)          # 后插 ⇒ 优先级更高，方便 import 顶层 cuesheet

# ---------- 1) 新软件：造 + 存 ----------
from ercue.model import ercue_io                                    # noqa: E402
from ercue.model.cuesheet import CueItem, CompLayer, MediaRef         # noqa: E402

tmp = tempfile.mkdtemp(prefix="compat_")
p = os.path.join(tmp, "compat.ercue")

s = ercue_io.new_sheet("兼容性验证", "recon")
te = ercue_io.new_track("Cosmic String", "Rolling Contact", 372.4, 155.03)
ly = CompLayer(lyr_id="L1", name="背景", z=0, blend="screen", opacity=0.9)
ly.items.append(CueItem(item_id="i1", clip_id="c1", at_sec=12.384, src_in_sec=2.0,
                        speed=1.25, crop=[0.05, 0.0, 0.9, 1.0],
                        trans_in="glitch", trans_out="fade").normalized())
ly.items.append(CueItem(item_id="i2", clip_id="c2", at_sec=37.0,
                        trans_in="cut", trans_out="cut").normalized())
te.composition.layers.append(ly)
s.tracks.append(te)
s.media.append(MediaRef(clip_id="c1", name="a.mp4"))
s.media.append(MediaRef(clip_id="c2", name="b.mp4"))
ercue_io.save(s, p, app_version="2026.10.01.01")
print("[1] 新软件导出 OK：%s（%d bytes）" % (p, os.path.getsize(p)))

# ---------- 2) 旧实现：读 ----------
import cuesheet as old_mod                                          # noqa: E402
print("[2] 旧实现模块路径：%s" % old_mod.__file__)

try:
    res = old_mod.load(p)
except Exception as e:                                              # noqa: BLE001
    print("[2] ★ 旧实现读取失败：%s: %s" % (type(e).__name__, e))
    import traceback
    traceback.print_exc()
    sys.exit(1)

old_sheet, info = res
print("[2] 旧实现读取 OK")
print("    title            = %r" % old_sheet.title)
print("    tracks           = %d" % len(old_sheet.tracks))
print("    media            = %d" % len(old_sheet.media))
print("    warnings         = %s" % info.get("warnings"))
print("    missing_media    = %s" % info.get("missing_media"))

t = old_sheet.tracks[0]
print("    track.identity   = %r / %r / dur=%s / bpm=%s"
      % (t.identity.title, t.identity.artist, t.identity.duration_sec, t.identity.bpm))
print("    track_key        = %r" % t.track_key)

comp = getattr(t, "composition", None)
if comp is None:
    print("    ★ 旧实现的 TrackEntry 没有 composition 属性（结构不一致）")
    print("    TrackEntry 字段:", [f for f in dir(t) if not f.startswith("_")][:20])
else:
    print("    layers           = %d" % len(comp.layers))
    for L in comp.layers:
        print("      layer %r z=%s blend=%r opacity=%s items=%d"
              % (L.name, getattr(L, "z", "?"), getattr(L, "blend", "?"),
                 getattr(L, "opacity", "?"), len(L.items)))
        for it in L.items:
            print("        item %s clip=%s at_beats=%.3f at_sec=%.3f "
                  "trans_in=%r trans_out=%r speed=%s crop=%s"
                  % (it.item_id, it.clip_id, it.at_beats, it.at_sec,
                     it.trans_in, it.trans_out, it.speed, it.crop))

# ---------- 3) 反向：旧实现存 → 新软件读 ----------
p2 = os.path.join(tmp, "back.ercue")
try:
    old_mod.save(old_sheet, p2)
    s2, info2 = ercue_io.load(p2)
    print("[3] 反向（旧存→新读）OK：title=%r layers=%d items=%d"
          % (s2.title, len(s2.tracks[0].layers), len(s2.tracks[0].layers[0].items)))
    print("    反向 warnings = %s" % info2.get("warnings"))
except Exception as e:                                              # noqa: BLE001
    print("[3] ★ 反向失败：%s: %s" % (type(e).__name__, e))
    import traceback
    traceback.print_exc()

print("\n临时目录：%s" % tmp)
