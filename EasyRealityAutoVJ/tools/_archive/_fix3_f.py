# -*- coding: utf-8 -*-
"""第三轮 ★F 验证（阻塞项 F）：离线网格在「扫描 → 联网补曲风」后是否保住。

F-b：`resolve_music_genre()` 的返回值**不含 grid / song_id**；旧实现 `cfg[...][p] = r2`
     整条替换 ⇒ 命中在线（57/66 真实曲库）的每首歌网格被抹掉。
     修法：合并 `r0 = meta.get(p) or {}; r0.update(r2); meta[p] = r0`。
F-a：增量扫描把「已有元数据、缺网格 / grid_ver 过期」的歌单列 todo_grid，只补网格
     （跳过指纹/联网/本地 AI）。

本脚本用**真实** resolve_music_genre 的返回结构 + 真实 music_meta 库做静态/仿真核对，
不触发联网（只检查返回值键集合、复刻合并表达式、复刻 _need_grid 判据）。

用法： P:\\AutoVJ\\venv\\Scripts\\python.exe tools/_fix3_f.py
"""
import inspect
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

import beatgrid as bg              # noqa: E402
import music_meta as mm            # noqa: E402

UIM = os.path.join(SRC, "ui_main.py")
with open(UIM, "r", encoding="utf-8") as fh:
    UIM_SRC = fh.read()

FAIL = 0


def check(name, cond, detail=""):
    global FAIL
    print("  [%s] %s %s" % ("OK " if cond else "FAIL", name, detail))
    if not cond:
        FAIL += 1


print("=" * 78)
print("★F-b 根因：resolve_music_genre() 返回值是否含 grid / song_id")
print("=" * 78)
src = inspect.getsource(mm.resolve_music_genre)
# 返回语句里的键名（直接在源码里找 result/dict 的键集合）
# 真实返回：dict(artist=..., title=..., genre=..., genres=..., source=..., query=...)
ret_keys = set(re.findall(r'["\']([a-z_]+)["\']\s*:', src))
check("resolve_music_genre 不返回 'grid'", "grid" not in ret_keys,
      "（源码返回键=%s）" % sorted(ret_keys))
check("resolve_music_genre 不返回 'song_id'", "song_id" not in ret_keys)

print()
print("=" * 78)
print("★F-b 修复：ui_main 阶段 2 必须**合并**（r0.update(r2)）而非整条替换（= r2）")
print("=" * 78)
# 取阶段 2 的那段：r2 = resolve_music_genre(p) 之后
m = re.search(r"r2 = resolve_music_genre\(p\)(.*?)_dq = _tm\.perf_counter",
              UIM_SRC, re.S)
seg = m.group(1) if m else ""
check("存在 `r0 = ...get(p) or {}`", "or {}" in seg and "get(p)" in seg)
check("存在 `r0.update(r2)` 合并", "r0.update(r2)" in seg)
check("不存在整条替换 `music_meta\"][p] = r2`",
      not re.search(r'music_meta"\]\[p\]\s*=\s*r2', seg))

# 复刻合并表达式：喂养一个含 grid 的旧记录 + 一个真实结构的 r2，网格必须保留
print()
print("--- 仿真：真实 r2 结构下，合并后网格是否仍在 ---")
cfg = {"music_meta": {}}
p = "X.flac"
grid = {"bpm": 128.0, "beat_len": 0.46875, "anchor": 1.2, "phrase": 4,
        "src": "vdj", "conf": 0.95, "grid_ver": bg.GRID_VER}
cfg["music_meta"][p] = {"grid": grid, "song_id": 123456, "level": "album",
                        "genres": ["House"], "source": "local"}
r2 = {"artist": "A", "title": "T", "genre": "House", "genres": ["House", "Techno"],
      "source": "discogs", "query": "A T", "level": "track"}
# —— 旧实现（= r2）：整条替换
old = dict(cfg["music_meta"])
old[p] = r2
check("旧实现(=r2) 抹掉 grid", "grid" not in old[p],
      "keys=%s" % sorted(old[p].keys()))
# —— 新实现（merge）
r0 = cfg["music_meta"].get(p) or {}
r0.update(r2)
cfg["music_meta"][p] = r0
new = cfg["music_meta"][p]
check("新实现(merge) 保住 grid", isinstance(new.get("grid"), dict)
      and new["grid"].get("beat_len") == 0.46875)
check("新实现(merge) 保住 song_id", new.get("song_id") == 123456)
check("新实现(merge) 采纳在线曲风", new.get("level") == "track"
      and new.get("source") == "discogs")

print()
print("=" * 78)
print("★F-a 修复：增量扫描 todo_grid 判据（缺网格 / grid_ver 过期才补）")
print("=" * 78)
check("存在阶段 1b 补网格代码块", "阶段 1b：**只补网格**" in UIM_SRC)
check("_grid_one 用 grid_from_signal 且传真实 dur",
      "grid_from_signal(sig, sr, vdj_idx, path=p, dur=dur)" in UIM_SRC)
check("todo_grid 计算存在", "todo_grid = [p for p in items" in UIM_SRC)


# 复刻 _need_grid 判据
def need_grid(rec, gver):
    if not isinstance(rec, dict):
        return False
    g = rec.get("grid")
    return not (isinstance(g, dict) and g.get("beat_len")
                and g.get("grid_ver") == gver)


GV = bg.GRID_VER
cases = [
    ("新歌（无元数据）", None, False, "不在 todo_grid（走完整 todo）"),
    ("有元数据·无 grid", {"song_id": 1}, True, "缺网格 → 补"),
    ("有 grid·旧 grid_ver=1",
     {"grid": {"beat_len": 0.5, "grid_ver": 1}}, True, "算法过期 → 补"),
    ("有 grid·当前 grid_ver=%d" % GV,
     {"grid": {"beat_len": 0.5, "grid_ver": GV}}, False, "已最新 → 跳过"),
    ("有 grid·无 grid_ver 字段（隐性 v1）",
     {"grid": {"beat_len": 0.5}}, True, "隐式 v1 → 补"),
]
for nm, rec, exp, why in cases:
    got = need_grid(rec, GV)
    check("%s → %s" % (nm, why), got == exp)

print()
print("=" * 78)
print("总体：" + ("F 修复核对通过" if FAIL == 0 else "存在 %d 项不一致" % FAIL))
print("=" * 78)
sys.exit(1 if FAIL else 0)
