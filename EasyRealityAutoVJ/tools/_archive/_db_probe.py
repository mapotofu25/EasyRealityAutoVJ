# -*- coding: utf-8 -*-
"""探针 v4：指纹写库耗时随库增大而恶化的程度。

复制用户 fingerprints.db 到临时目录后操作（不改用户数据）。
"""
import os
import sys
import time
import shutil

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

SRC_DB = r"C:\Users\Administrator\AppData\Local\AutoVJ\fingerprints.db"
HERE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(HERE, "_probe_big.db")
AUDIO = r"W:/临时结界相关/2of/tofu/Rolling Contact - Cosmic String.flac"


def ms(t0):
    return (time.perf_counter() - t0) * 1000


def main():
    import numpy as np
    import soundfile as sf
    import sqlite3
    from fp import FingerprintDB, fingerprint_audio

    # 复制用户库到临时（只读源）
    for f in (TMP, TMP + "-wal", TMP + "-shm"):
        try:
            os.remove(f)
        except OSError:
            pass
    print("copying 140MB db ...")
    shutil.copy2(SRC_DB, TMP)
    nrows = sqlite3.connect(TMP).execute("select count(*) from fingerprints").fetchone()[0]
    print(f"temp db rows = {nrows}, size = {os.path.getsize(TMP)/1048576:.0f} MB")

    data, sr = sf.read(AUDIO, dtype="float32", always_2d=True)
    sig = np.mean(data, axis=1)
    hs = fingerprint_audio(sig, sr)
    rows = [(h, 999999999, off) for h, off in hs]
    print(f"this song -> {len(rows)} hash rows\n")

    conn = sqlite3.connect(TMP)
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()

    # 1) 单独测 DELETE ... WHERE song_id=?（无 song_id 索引 → 全表扫描）
    t0 = time.perf_counter()
    conn.execute("DELETE FROM fingerprints WHERE song_id=?", (123456,))
    t_del = ms(t0)
    print(f"[1] DELETE WHERE song_id=? (全表扫描)      {t_del:8.0f} ms")

    # 2) 单独测 executemany INSERT（4 万行 + 索引维护）
    t0 = time.perf_counter()
    conn.executemany("INSERT INTO fingerprints VALUES (?,?,?)", rows)
    t_ins = ms(t0)
    print(f"[2] INSERT {len(rows)} 行 + idx_hash 维护      {t_ins:8.0f} ms")

    conn.rollback()  # 不留痕

    # 3) DELETE + INSERT 同一隐式事务（= add_song 实际行为）
    conn.execute("BEGIN")
    t0 = time.perf_counter()
    conn.execute("DELETE FROM fingerprints WHERE song_id=?", (777,))
    conn.executemany("INSERT INTO fingerprints VALUES (?,?,?)", rows)
    t_tx = ms(t0)
    conn.rollback()
    print(f"[3] DELETE+INSERT 同事务（≈add_song 写库）  {t_tx:8.0f} ms")

    # 4) 模拟随库增大的 EXPLAIN：确认 DELETE 用不到索引
    plan = conn.execute("EXPLAIN QUERY PLAN DELETE FROM fingerprints WHERE song_id=?",
                        (1,)).fetchall()
    print(f"[4] DELETE 查询计划: {plan}")

    conn.close()
    for f in (TMP, TMP + "-wal", TMP + "-shm"):
        try:
            os.remove(f)
        except OSError:
            pass
    print("\n(note) 用户 800 首 ≈ 24M 行 / ~1.7GB：DELETE 全表扫描与 INSERT 索引维护都会进一步恶化。")


if __name__ == "__main__":
    main()
