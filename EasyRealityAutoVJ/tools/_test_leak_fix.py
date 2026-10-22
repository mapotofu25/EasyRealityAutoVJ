# -*- coding: utf-8 -*-
"""独立复验 _ascii_safe_path 泄漏修复：四种属性都要真的成立

不调 AutoVJ 主程序，只 import media_manager 里的三个公开点。
⚠ 所有临时目录/文件自造，落系统临时目录之外的一个自建子目录，跑完删干净。
"""
import os, sys, time, shutil, tempfile, hashlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import media_manager as mm

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("  [PASS] " if cond else "  [FAIL] ") + name + ("  " + detail if detail else ""))

# 自建沙箱目录（⚠ 不落在 Temp 根，避免和真实 Temp 混淆）
sandbox = tempfile.mkdtemp(prefix="autovj_leakverify_")

# ---- 造一个「非 ASCII 路径」的源文件 ----
src_dir = os.path.join(sandbox, "素材库")
os.makedirs(src_dir, exist_ok=True)
src = os.path.join(src_dir, "测试素材.bin")
with open(src, "wb") as f:
    f.write(os.urandom(64 * 1024))

print("=" * 70)
print("沙箱:", sandbox)
print("源文件:", src)

print("\n[1] 非 ASCII 路径 → 产生副本，且副本名带 size+mtime 签名")
p1 = mm._ascii_safe_path(src)
check("返回的是 ASCII 路径", p1.isascii(), p1)
check("不是原始路径（确实复制了）", p1 != src)
check("副本存在", os.path.exists(p1))

st = os.stat(src)
sig = "%d_%d" % (st.st_size, int(st.st_mtime))
h = hashlib.md5(src.encode("utf-8")).hexdigest()[:12]
expect_name = "autovj_%s_%s.bin" % (h, sig)
check("副本命名含 <md5(path)12>_<size>_<mtime>",
      os.path.basename(p1) == expect_name,
      "实际=%s 期望=%s" % (os.path.basename(p1), expect_name))
check("副本落在 gettempdir() 下",
      os.path.dirname(p1) == tempfile.gettempdir())

print("\n[2] 同一路径再次调用 → 复用（不产生第二个文件）")
before = set(os.listdir(tempfile.gettempdir()))
p2 = mm._ascii_safe_path(src)
after = set(os.listdir(tempfile.gettempdir()))
new_files = [x for x in (after - before) if x.startswith("autovj_")]
check("返回同一路径", p1 == p2)
check("未新增任何 autovj_ 文件", len(new_files) == 0, "新增=%s" % new_files)

print("\n[3] 内容变了（size/mtime 变）→ 不再命中旧副本（旧缺陷：同名换内容仍读旧副本）")
time.sleep(1.1)
with open(src, "wb") as f:
    f.write(os.urandom(128 * 1024))     # 换内容 → size 变
p3 = mm._ascii_safe_path(src)
check("新副本路径 != 旧副本路径", p3 != p1, "p1=%s p3=%s" % (os.path.basename(p1), os.path.basename(p3)))
check("新副本确实存在", os.path.exists(p3))

print("\n[4] 纯 ASCII 路径 → 不复制，原样返回")
plain = os.path.join(sandbox, "plain.bin")
with open(plain, "wb") as f:
    f.write(b"x" * 100)
p4 = mm._ascii_safe_path(plain)
check("原样返回（未复制）", p4 == plain, p4)

print("\n[5] 进程退出清理：_cleanup_registered_copies() 删除本进程登记的全部副本")
registered = sorted(mm._TMP_COPIES)
check("登记表非空（产出的副本都登记了）", len(registered) >= 2, "登记 %d 个" % len(registered))
mm._cleanup_registered_copies()
gone = [p for p in registered if not os.path.exists(p)]
check("登记的副本全部被删除", len(gone) == len(registered),
      "已删 %d / %d" % (len(gone), len(registered)))
check("登记表已清空", len(mm._TMP_COPIES) == 0, "剩 %d" % len(mm._TMP_COPIES))

print("\n[6] 启动期孤儿回收：只删 autovj_ 文件、跳过目录、跳过太新的")
# 造孤儿：一个「老」的 autovj_ 文件
old_f = os.path.join(tempfile.gettempdir(), "autovj_orphan_old_1_1.bin")
with open(old_f, "wb") as f:
    f.write(b"orphan")
old_t = time.time() - 48 * 3600
os.utime(old_f, (old_t, old_t))
# 造一个「新」的 autovj_ 文件（不该被删）
new_f = os.path.join(tempfile.gettempdir(), "autovj_orphan_new_2_2.bin")
with open(new_f, "wb") as f:
    f.write(b"fresh")
# 造一个 autovj_ 目录（不该被删）
dir_p = os.path.join(tempfile.gettempdir(), "autovj_orphan_dir_probe")
os.makedirs(dir_p, exist_ok=True)
# 造一个非 autovj_ 文件（绝不该被删）
other_f = os.path.join(tempfile.gettempdir(), "notautovj_keepme.bin")
with open(other_f, "wb") as f:
    f.write(b"keepme")

rm, lk, keep = mm.cleanup_stale_ascii_copies(ttl_hours=24.0)
check("老孤儿被删", not os.path.exists(old_f), "removed=%d skipped=%d kept=%d" % (rm, lk, keep))
check("太新的被保留", os.path.exists(new_f))
check("autovj_ 目录被跳过", os.path.isdir(dir_p))
check("非 autovj_ 文件未被动", os.path.exists(other_f))
check("返回值语义正确（removed>=1, kept>=1）", rm >= 1 and keep >= 1,
      "removed=%d skipped=%d kept=%d" % (rm, lk, keep))

print("\n[7] cleanup_stale_ascii_copies(directory=...) 隔离性：只扫指定目录")
iso = tempfile.mkdtemp(prefix="autovj_isolscan_")
iso_file = os.path.join(iso, "autovj_iso_old_3_3.bin")
with open(iso_file, "wb") as f:
    f.write(b"iso")
os.utime(iso_file, (old_t, old_t))
rm2, _, _ = mm.cleanup_stale_ascii_copies(ttl_hours=24.0, directory=iso)
check("指定目录内的老副本被删", not os.path.exists(iso_file), "removed=%d" % rm2)
check("未波及真实 Temp 的新文件", os.path.exists(new_f))

# ---- 清理 ----
for p in (old_f, new_f, other_f, iso_file):
    try:
        if os.path.exists(p):
            os.remove(p)
    except OSError:
        pass
try:
    os.rmdir(dir_p)
except OSError:
    pass
shutil.rmtree(iso, ignore_errors=True)
shutil.rmtree(sandbox, ignore_errors=True)

print("\n" + "=" * 70)
fails = [r for r in results if not r[1]]
print("结论：%d 项断言，%d 通过，%d 失败" % (len(results), len(results) - len(fails), len(fails)))
for n, ok, d in fails:
    print("   FAILED:", n, d)
print("VERDICT:", "ALL PASS" if not fails else "HAS FAILURE")
sys.exit(0 if not fails else 1)
