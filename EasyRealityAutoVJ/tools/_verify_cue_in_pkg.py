# -*- coding: utf-8 -*-
"""核对**新打的包**里确实含本轮码表修复（解 PYZ 取源码逐串验证）。

判据（缺任何一个都说明包不是最新的）：
  engine     : `_cue_pos_beat_len` / `_cue_accept_pos` / `_cue_bound_song_id`
  cue_ui     : `没填 BPM`（新文案）
  i18n_map   : `两端一致，位置不会因此错位`
  cue_index  : `library_rows`（曲库派生行）
  fp         : `signature_from_hashes`（指纹签名）
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

EXE = sys.argv[1] if len(sys.argv) > 1 else r"dist_bpmsem\EasyRealityAutoVJ\EasyRealityAutoVJ.exe"
if not os.path.isfile(EXE):
    print("找不到 exe：", EXE); sys.exit(2)

import tempfile
_d = tempfile.mkdtemp(prefix="cuechk_")
car = CArchiveReader(EXE)
_p = os.path.join(_d, "PYZ.pyz")
with open(_p, "wb") as f:
    f.write(car.extract("PYZ.pyz"))
pyz = ZlibArchiveReader(_p)      # ← 必须传**导出到磁盘的路径**（传 exe 路径会 TypeError）

WANT = {
    "engine": ["_cue_pos_beat_len", "_cue_accept_pos", "_cue_bound_song_id"],
    "cue_ui": ["没填 BPM"],
    # ⚠ i18n_map 不能靠"扫字符串常量"验：大 dict 字面量在 CPython 3.13 里**不落 co_consts**，
    #   扫出来会是"缺"（假警报）。这里单独用 exec 跑包里那个模块，直接查 ZH2EN。
    "i18n_map": [],
    "cue_index": ["library_rows"],
    "fp": ["signature_from_hashes", "file_md5"],
    "beatgrid": ["BPM_PRIOR"],
}
bad = 0
# ---- i18n_map 专项：exec 后查真表 ----
try:
    _ns = {}
    exec(pyz.extract("i18n_map"), _ns)
    _zh = _ns.get("ZH2EN") or {}
    _k = "这张码表的曲目没填 BPM：按「1 拍 = 1 秒」换算（两端一致，位置不会因此错位）。想按小节查看或对齐时再补 BPM。"
    if _k in _zh:
        print("  %-10s ✓ 新文案已进词表（%d 条）" % ("i18n_map", len(_zh)))
    else:
        print("  %-10s ✗ 新文案不在词表（旧文案残留？）" % "i18n_map"); bad += 1
except Exception as _e:                                       # noqa: BLE001
    print("  %-10s ✗ exec 失败：%s" % ("i18n_map", _e)); bad += 1

for mod, needles in WANT.items():
    if not needles:
        continue
    try:
        obj = pyz.extract(mod)
    except Exception as e:                                    # noqa: BLE001
        print("  %-10s ✗ 取不到模块：%s" % (mod, e)); bad += 1; continue
    src = getattr(obj, "co_consts", None)
    # 把整个 code 对象递归拼成文本（常量 + 名字），足够做"字符串存在性"检查
    parts = []
    def walk(c):
        if not hasattr(c, "co_consts"):
            return
        for k in c.co_consts:
            if isinstance(k, str):
                parts.append(k)
            elif hasattr(k, "co_consts"):
                walk(k)
        parts.extend(getattr(c, "co_names", ()) or ())
        for k in c.co_consts:
            if hasattr(k, "co_consts"):
                walk(k)
    walk(obj)
    blob = "\n".join(parts)
    miss = [n for n in needles if n not in blob]
    if miss:
        print("  %-10s ✗ 缺：%s" % (mod, ", ".join(miss))); bad += 1
    else:
        print("  %-10s ✓ %s" % (mod, " / ".join(needles)))
print()
print("结论：%s" % ("✅ 本轮码表修复确实在包里" if bad == 0 else "❌ 有 %d 个模块缺标记，包不是最新" % bad))
sys.exit(0 if bad == 0 else 1)
