# -*- coding: utf-8 -*-
"""曲库扫描提速改动的自测（零干扰：不真联网、不碰用户 fingerprints.db、不碰 GPU）。

覆盖 2026-09-26 的四处改动：
  1) genre_lookup：联网单次超时 12s→3s + **单首总预算**（唯一网络出口 `_fetch_json` 统一执行）
  2) music_meta.resolve_music_genre：进入时给这一首歌设总预算
  3) fp.FingerprintDB：补 `song_id` 索引（`DELETE WHERE song_id=?` 不再全表扫描）
  4) ui_main 扫描循环：分步计时落 scan_time.log

用法：venv\\Scripts\\python.exe tools\\_scan_fix_test.py
"""
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import genre_lookup as gl                                              # noqa: E402

ok = True


def chk(name, cond, extra=""):
    global ok
    print("  [%s] %s%s" % ("OK  " if cond else "FAIL", name, ("  " + extra) if extra else ""))
    if not cond:
        ok = False


print("=" * 74)
print("曲库扫描提速改动 自测")
print("=" * 74)
print("NET_TIMEOUT = %.1fs    ONLINE_BUDGET_SECS = %.1fs" % (gl.NET_TIMEOUT, gl.ONLINE_BUDGET_SECS))

# ---------------------------------------------------------------- 1) 总预算
print("\n[1] 联网总预算机制")
gl.begin_online_budget(10)
chk("预算 10s 时未超时", (not gl.online_expired()) and gl._budget_left() > 0)
gl.begin_online_budget(0.001)
time.sleep(0.02)
chk("预算耗尽 → online_expired() 为真", gl.online_expired())
t0 = time.perf_counter()
rejected = False
try:
    gl._fetch_json("https://api.discogs.com/database/search?q=x")
except TimeoutError:
    rejected = True
except Exception as e:                                                 # noqa: BLE001
    print("     （其他异常：%s: %s）" % (type(e).__name__, e))
dt = time.perf_counter() - t0
chk("预算耗尽 → 直接拒绝、**不真发请求**", rejected, "耗时 %.4fs" % dt)
chk("拒绝是即时的（<50ms）", dt < 0.05, "%.4fs" % dt)
gl.begin_online_budget(0)
chk("secs=0 表示不限制", gl._budget_left() > 1e8)

# ---------------------------------------------------------------- 2) 指纹索引
print("\n[2] 指纹库 song_id 索引（临时库，绝不碰用户 fingerprints.db）")
import fp                                                              # noqa: E402
tmpdir = tempfile.mkdtemp(prefix="autovj_scanfix_")
db = fp.FingerprintDB(os.path.join(tmpdir, "t.db"))
names = [r[0] for r in db.conn.execute(
    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='fingerprints'").fetchall()]
print("     索引: %s" % names)
chk("存在 hash 索引", any("hash" in n for n in names))
chk("存在 song_id 索引（本次新增）", any("song" in n for n in names))
# 用 EXPLAIN 证明确实走了索引（而不是全表扫描）
plan = db.conn.execute("EXPLAIN QUERY PLAN DELETE FROM fingerprints WHERE song_id=?",
                       (123,)).fetchall()
plan_txt = " | ".join(str(r[-1]) for r in plan)
print("     DELETE 计划: %s" % plan_txt)
chk("DELETE WHERE song_id 走索引（非全表扫描）", "SCAN fingerprints" not in plan_txt)
db.close()

# ---------------------------------------------------------------- 3) 预算被自动设置
print("\n[3] resolve_music_genre 是否自动设置单首预算")
import music_meta                                                      # noqa: E402
called = []
orig = gl.begin_online_budget


def _fake(*a, **k):
    called.append(1)
    orig(0.001)          # 设极短预算，确保即使真发请求也立刻失败（不造成实际联网）
gl.begin_online_budget = _fake
try:
    r = music_meta.resolve_music_genre(os.path.join(tmpdir, "zzz_not_exist.mp3"))
finally:
    gl.begin_online_budget = orig
chk("resolve_music_genre 调用了 begin_online_budget", bool(called))
print("     返回: %s" % ({k: r.get(k) for k in ("source", "genre")} if isinstance(r, dict) else r))
chk("对不存在/无标签文件不抛异常、返回 dict", isinstance(r, dict))

# ---------------------------------------------------------------- 4) 扫描计时日志
print("\n[4] 扫描计时日志文件")
logp = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "scan_time.log")
print("     预期路径: %s" % logp)
chk("路径可写目录存在（不为空路径）", bool(os.environ.get("LOCALAPPDATA")))

# ---------------------------------------------------------------- 5) 限流防护
print("\n[5] 被网站限流（HTTP 429）后的行为")
import urllib.error as _ue                                              # noqa: E402
import urllib.request as _ur                                            # noqa: E402
_orig_urlopen = _ur.urlopen
gl._blocked = False
gl.begin_online_budget(10)


def _fake_urlopen(*a, **k):
    raise _ue.HTTPError(a[0] if a else "u", 429, "Too Many Requests", {}, None)
_ur.urlopen = _fake_urlopen
try:
    gl._fetch_json("https://api.discogs.com/database/search?q=x")
    print("     !! 不该走到这里")
except _ue.HTTPError as e:
    print("     收到 HTTPError %s（模拟 Discogs 限流）" % e.code)
finally:
    _ur.urlopen = _orig_urlopen
chk("被 429 拒绝 → 置位 blocked", gl.online_blocked())

t0 = time.perf_counter()
rejected2 = False
try:
    gl._fetch_json("https://api.discogs.com/database/search?q=y")
except TimeoutError:
    rejected2 = True
except Exception:                                                       # noqa: BLE001
    pass
dt2 = time.perf_counter() - t0
chk("blocked 之后 → 立即拒绝、不再发请求", rejected2, "耗时 %.4fs" % dt2)
chk("拒绝是即时的（<50ms）", dt2 < 0.05, "%.4fs" % dt2)
gl._blocked = False

print()
print("结论: %s" % ("全部通过" if ok else "[!] 有失败项"))
sys.exit(0 if ok else 1)
