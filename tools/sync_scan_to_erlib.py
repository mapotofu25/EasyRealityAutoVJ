# -*- coding: utf-8 -*-
"""把 AutoVJ 曲库的**扫描结果**同步进 ERLIB 的工作库。

用户 2026-10-31：「把目前的曲库扫描同步到 erlib 这个软件」。

ERLIB（`<工作区>\\ERLIB_曲库分析`）原本是"选文件夹 → 扫描 → 导出 `.eravjlib`"，
它的扫描成果存在**工作库** `%LOCALAPPDATA%\\AutoVJ\\erlib_scan.eravjlib`（SQLite）。
AutoVJ 这边已经把整库扫好了（指纹 + 拍网格 + 分段 + 封面），没必要在 ERLIB 里再扫一遍
（2.4 秒/首 × 90 首 ≈ 4 分钟，且 ERLIB 不产出分段）。

本工具把 AutoVJ 的成果直接写进工作库：
  · songs        ← `cfg["music_meta"][path]`（grid / sections / cover）
  · fingerprints ← `fingerprints.db`（按 song_id，两边算法一致：`int(md5(path)[:8],16)`）
  · covers       ← `thumbs/covers/*.jpg`

⚠ 只写 ERLIB 的工作库，**绝不改** AutoVJ 的 config / fingerprints.db（全程只读打开）。
⚠ 写之前自动备份工作库（`*.bak-<时间戳>`），可随时回滚。

用法：
  venv/Scripts/python.exe -u tools/sync_scan_to_erlib.py --dry      # 只报告
  venv/Scripts/python.exe -u tools/sync_scan_to_erlib.py            # 真写（先备份）
  venv/Scripts/python.exe -u tools/sync_scan_to_erlib.py --limit 5  # 先试 5 首
"""
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
sys.path.insert(0, _SRC)

from erlib.format import LibReader, LibWriter, song_id_of             # noqa: E402

LOCAL = os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ")
CFG = os.path.join(LOCAL, "config.json")
FP_DB = os.path.join(LOCAL, "fingerprints.db")
WORK = os.path.join(LOCAL, "erlib_scan.eravjlib")

DRY = "--dry" in sys.argv
# ★ 2026-10-31（用户「详情页还得能播放，只是导出的文件不要带位置」）：
#   `--only-src` = **只补本机路径列**（`songs.src_path`），不动指纹/封面/网格。
#   用途：① 本次新加 `src_path` 列，已有的工作库全是空的 ⇒ 详情页不能试听；
#        ② 以后用户把音乐文件夹改名/搬家，也可用它一条命令刷新，比整库重扫快得多。
ONLY_SRC = "--only-src" in sys.argv
LIMIT = 0
for _a in sys.argv[1:]:
    if _a.startswith("--limit="):
        LIMIT = int(_a.split("=", 1)[1])
    elif _a.startswith("--pkg="):
        WORK = _a.split("=", 1)[1]
print("· AutoVJ 扫描数据：%s" % LOCAL)
print("· ERLIB 工作库  ：%s" % WORK)
print("· 模式：%s" % ("只补 src_path 路径列（不备份）" if ONLY_SRC
                      else ("只报告（--dry）" if DRY else "真写（先备份）")))


def main():
    if not os.path.isfile(CFG):
        print("!! 找不到 config.json")
        return 1
    if not os.path.isfile(FP_DB):
        print("!! 找不到 fingerprints.db")
        return 1
    c = json.load(open(CFG, encoding="utf-8"))
    meta_all = c.get("music_meta") or {}
    lib = [p for p in (c.get("music_library") or []) if os.path.isfile(p)]
    if LIMIT:
        lib = lib[:LIMIT]
    print("· AutoVJ 曲目 %d 首（有扫描结果 %d）"
          % (len(lib), sum(1 for p in lib if meta_all.get(p))))

    # ---- 只读打开 AutoVJ 指纹库（绝不写）----
    fpc = sqlite3.connect("file:%s?mode=ro" % FP_DB.replace("\\", "/"), uri=True)
    fp_ver = 0
    try:
        fp_ver = int(fpc.execute("PRAGMA user_version").fetchone()[0])
    except Exception:                                          # noqa: BLE001
        pass
    print("· 指纹库 user_version = %d（erlib 要求 2）" % fp_ver)

    # ---- 已存在多少（用于报告"新增/更新"）----
    seen = set()
    if os.path.isfile(WORK):
        try:
            seen = {int(r[0]) for r in
                    LibReader(WORK).conn.execute("SELECT song_id FROM songs")}
        except Exception as e:                                 # noqa: BLE001
            print("· 读旧工作库失败（将新建）：%s" % e)
    print("· ERLIB 现有 %d 首" % len(seen))

    # ---- 备份 ----
    # ⚠ `--only-src` 只 UPDATE 一列（不改指纹/封面），**不备份**：
    #   工作库 280 MB，每跑一次就多一份副本（本机"删除"是送回收站、不释放空间）。
    if not DRY and not ONLY_SRC and os.path.isfile(WORK):
        bak = "%s.bak-%s" % (WORK, time.strftime("%Y%m%d-%H%M%S"))
        try:
            shutil.copy2(WORK, bak)
            print("· 已备份工作库 → %s（%.1f MB）"
                  % (os.path.basename(bak), os.path.getsize(bak) / 1e6))
        except Exception as e:                                 # noqa: BLE001
            print("!! 备份失败，中止：%s" % e)
            return 1

    if DRY:
        n_new = n_upd = n_fp = 0
        for p in lib:
            sid = song_id_of(p)
            n = fpc.execute("SELECT COUNT(*) FROM fingerprints WHERE song_id=?",
                            (sid,)).fetchone()[0]
            n_fp += n
            if sid in seen:
                n_upd += 1
            else:
                n_new += 1
        print("\n[dry] 将新增 %d 首 / 覆盖 %d 首，共写入 %d 条指纹"
              % (n_new, n_upd, n_fp))
        fpc.close()
        return 0

    # ---- 真写 ----
    writer = LibWriter(WORK, root="", resume=True, app_ver="AutoVJ-sync")
    n_new = n_upd = n_fp = n_cov = 0
    t0 = time.time()
    try:
        if ONLY_SRC:
            # 只更新「本机音频路径」这一列（`LibWriter` 打开时已幂等补好新列）
            n = 0
            for p in lib:
                cur = writer.conn.execute(
                    "UPDATE songs SET src_path=? WHERE song_id=?",
                    (os.path.abspath(p), song_id_of(p)))
                n += max(0, int(cur.rowcount))
            writer.conn.commit()
            print("\n· 只补本机路径：更新 %d 首的 src_path（其余数据一字未动）" % n)
            return 0
        for i, p in enumerate(lib, 1):
            m = meta_all.get(p) or {}
            sid = song_id_of(p)
            g = m.get("grid") or {}
            cover = str(m.get("cover") or "")
            cov_bytes = None
            if cover and os.path.isfile(cover):
                try:
                    cov_bytes = open(cover, "rb").read()
                    n_cov += 1
                except Exception:                              # noqa: BLE001
                    cov_bytes = None
            fps = [(str(h), int(o)) for h, o in fpc.execute(
                "SELECT hash, offset FROM fingerprints WHERE song_id=?", (sid,))]
            meta = {
                # ★ 2026-10-31（用户「erlib 不要带文件原位置，dj 和 vj 不是一台电脑」）：
                #   `rel_path` 只存**文件名**，不写本机绝对路径 ——
                #   它会跟着导出包到演出机，既没用又误导（见下一行 `src_path`）。
                "rel_path": os.path.basename(p),
                # ★★ 2026-10-31（用户「这里还是得要能播放的，只是说导出的文件不要带位置」）：
                #   本机绝对路径存到**单独一列** `src_path`：工作库/详情页试听靠它，
                #   `transfer.export_subset()` 导出时整列丢弃 ⇒ 导出包照旧不带位置。
                #   ⚠ 不能塞回 `rel_path`（那列是跟着包走的）。
                "src_path": os.path.abspath(p),
                "file_name": os.path.basename(p),
                "file_size": os.path.getsize(p),
                "duration": float(g.get("dur") or g.get("duration") or 0.0),
                "artist": str(m.get("artist") or ""),
                "title": str(m.get("title") or os.path.splitext(os.path.basename(p))[0]),
                "grid": g,
                # ★★ 2026-10-31（实测踩到）：**绝不能把 sections 塞进 `manual.segments`**。
                #   `beatgrid.effective_grid()` 见到非空 `manual.segments` 就走"分段模式"
                #   ⇒ BPM（来源）列显示成 `150.0（manual）`（实测），而且 erlib 导出包、
                #   AutoVJ 再导入时也会按"分段网格"消费 —— 而 AutoVJ 的 sections
                #   只有 `{t0,t1,energy,tier,name}`，没有每段的 bpm/beat_len/anchor，
                #   格式根本不是那种东西。⇒ `manual` 留空，段落走**独立列**。
                "manual": None,
                # ★★ 2026-10-31（用户「erlib 的扫描要和 eravj 一样」）：
                #   能量段落与主程序扫描产出**同一份**数据，落 `songs.sections` /
                #   `songs.sections_raw`（新列）。导入到演出机后由
                #   `imported_lib.lookup_sections()` 注入引擎 ⇒ 导入曲库也有段落色带、
                #   也按段落切素材（以前导入歌完全没有，这就是"不一样"）。
                "sections": m.get("sections") or [],
                "sections_raw": m.get("sections_raw") or [],
            }
            # 波形：主程序的 `.wj` 缓存（有就直接搬）；没缓存就**现算一次**
            #   —— 主程序扫描时会顺手生成，而没在曲库列表里浏览过的歌没有缓存；
            #      为了「和主程序扫描结果一样」，这里补算（~0.9s/首，只算缺的那些）。
            wf = None
            try:
                import waveform as _wf
                wf = _wf.waveform_load(p)
                if not wf:
                    import numpy as _np
                    import soundfile as _sf
                    _d, _sr = _sf.read(p, dtype="float32", always_2d=True)
                    wf = _wf.waveform_from_signal(_np.mean(_d, axis=1), _sr)
                    if wf:
                        try:
                            _wf.waveform_save(p, wf)       # 顺手补缓存，下次不用再算
                        except Exception:                  # noqa: BLE001
                            pass
            except Exception:                              # noqa: BLE001
                wf = None
            writer.add_song(sid, meta, fps, cov_bytes, commit=False,
                            waveform=wf)
            n_fp += len(fps)
            if sid in seen:
                n_upd += 1
            else:
                n_new += 1
            if i % 10 == 0:
                writer.conn.commit()
                print("   %3d/%d …（已写 %d 首 / %d 指纹）"
                      % (i, len(lib), n_new + n_upd, n_fp))
        writer.conn.commit()
    finally:
        try:
            writer.conn.close()
        except Exception:                                      # noqa: BLE001
            pass
        fpc.close()

    # ---- 校验（用 erlib 自己的读取器）----
    print("\n· 写入完成：新增 %d / 覆盖 %d，指纹 %d 条，封面 %d 张，用时 %.1fs"
          % (n_new, n_upd, n_fp, n_cov, time.time() - t0))
    try:
        rd = LibReader(WORK)
        n_all = rd.conn.execute("SELECT COUNT(*) FROM songs").fetchone()[0]
        n_fp_all = rd.conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0]
        n_grid = rd.conn.execute(
            "SELECT COUNT(*) FROM songs WHERE grid IS NOT NULL AND grid<>''").fetchone()[0]
        print("· 校验：工作库现有 %d 首 / %d 指纹 / %d 首有网格"
              % (n_all, n_fp_all, n_grid))
        miss = [s for s in (song_id_of(p) for p in lib)
                if rd.conn.execute("SELECT 1 FROM songs WHERE song_id=?", (s,)).fetchone()
                is None]
        print("· 校验：本次 %d 首是否都能查到 → %s"
              % (len(lib), "全部命中" if not miss else "缺 %d 首" % len(miss)))
        rd.conn.close()
    except Exception as e:                                     # noqa: BLE001
        print("!! 校验失败：%s" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
