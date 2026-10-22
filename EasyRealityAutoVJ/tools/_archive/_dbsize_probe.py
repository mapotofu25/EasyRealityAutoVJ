# -*- coding: utf-8 -*-
"""探针 v7：指纹库写库耗时随「库内行数」的扩展性（合成数据，最大到 800 首规模）。

结论用于回答：扫描 800 首时，指纹写库是否会随进度越来越慢（→ 平均 9s/首）。
不触碰用户数据，临时库写 P:/AutoVJ/tools/_probe_scale.db。
"""
import os
import sys
import time
import hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "_probe_scale.db")
N_PER_SONG = 30000          # 每首指纹行数（实测 29k~45k）
MILESTONES = [3_000_000, 12_000_000, 24_000_000]   # ≈100 / 400 / 800 首


def ms(t0):
    return (time.perf_counter() - t0) * 1000


def gen_rows(start, n, song_id):
    rows = []
    for i in range(start, start + n):
        h = hashlib.sha1(str(i).encode()).hexdigest()[:20]      # 固定 20 hex
        rows.append((h, song_id, (i * 7) % 200000))
    return rows


def bench(conn, label):
    # a) 单首指纹：DELETE(无索引全表扫描) + INSERT(N_PER_SONG 行)
    sid = 10 ** 9 + 1
    conn.execute("BEGIN")
    t0 = time.perf_counter()
    conn.execute("DELETE FROM fingerprints WHERE song_id=?", (sid,))
    d_del = ms(t0)
    rows = gen_rows(0, N_PER_SONG, sid)
    t0 = time.perf_counter()
    conn.executemany("INSERT INTO fingerprints VALUES (?,?,?)", rows)
    d_ins = ms(t0)
    conn.commit()
    d_tx = d_del + d_ins
    n = conn.execute("select count(*) from fingerprints").fetchone()[0]
    print(f"  [{label}] rows={n:>11,}  DELETE={d_del:8.0f}ms  INSERT={d_ins:8.0f}ms  "
          f"单首写库≈{d_tx:8.0f}ms  db={os.path.getsize(DB)/1048576:.0f}MB")
    # 清掉刚插入的测试曲，别让库一直涨
    conn.execute("DELETE FROM fingerprints WHERE song_id=?", (sid,))
    conn.commit()
    return d_tx


def main():
    for f in (DB, DB + "-wal", DB + "-shm"):
        try:
            os.remove(f)
        except OSError:
            pass
    import sqlite3
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("CREATE TABLE fingerprints (hash TEXT NOT NULL, song_id INTEGER NOT NULL,"
                 " offset INTEGER NOT NULL)")
    conn.execute("CREATE INDEX idx_hash ON fingerprints(hash)")
    conn.commit()

    print("building synthetic fingerprint DB ...")
    counters = {"n": 0, "sid": 1}
    res = {}

    def fill_to(target):
        CHUNK = 200_000
        while counters["n"] < target:
            k = min(CHUNK, target - counters["n"])
            conn.executemany("INSERT INTO fingerprints VALUES (?,?,?)",
                             gen_rows(counters["n"], k, counters["sid"]))
            counters["n"] += k
            if counters["n"] % 1_000_000 < CHUNK:
                counters["sid"] += 1
        conn.commit()

    for m in MILESTONES:
        fill_to(m)
        res[m] = bench(conn, f"{m//30000}首")
    conn.close()
    for f in (DB, DB + "-wal", DB + "-shm"):
        try:
            os.remove(f)
        except OSError:
            pass
    print("\n扩展性：单首「指纹写库」耗时随库内总行数的变化")
    for m, v in res.items():
        print(f"  {m:>11,} 行（≈{m//30000:>3} 首） → {v:8.0f} ms/首")


if __name__ == "__main__":
    main()
