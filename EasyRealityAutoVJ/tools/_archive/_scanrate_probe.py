# -*- coding: utf-8 -*-
"""探针 v8（精简）：无索引全表扫描 → 定位 DELETE-by-song_id 随行数的线性扩展。
先无索引大量灌入（快），再测 `WHERE song_id=?` 的全表扫描耗时。
"""
import os, sys, time, hashlib, sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "_probe_scan.db")
MILESTONES = [3_000_000, 12_000_000, 24_000_000]


def gen(start, n, sid):
    out = []
    for i in range(start, start + n):
        out.append((hashlib.sha1(str(i).encode()).hexdigest()[:20], sid, (i * 7) % 200000))
    return out


def main():
    for f in (DB, DB + "-wal", DB + "-shm"):
        try: os.remove(f)
        except OSError: pass
    c = sqlite3.connect(DB)
    c.execute("PRAGMA journal_mode=OFF")
    c.execute("PRAGMA synchronous=OFF")
    c.execute("CREATE TABLE fingerprints (hash TEXT NOT NULL, song_id INTEGER NOT NULL, offset INTEGER NOT NULL)")
    c.commit()
    n = 0
    t_build0 = time.perf_counter()
    for m in MILESTONES:
        while n < m:
            k = min(500_000, m - n)
            c.executemany("INSERT INTO fingerprints VALUES (?,?,?)", gen(n, k, 1))
            n += k
            c.commit()
        # 测全表扫描：WHERE song_id=? （DELETE 无索引时等价）
        t0 = time.perf_counter()
        c.execute("SELECT count(*) FROM fingerprints WHERE song_id=?", (999999999,)).fetchone()
        d_scan = (time.perf_counter() - t0) * 1000
        # EXPLAIN 确认扫描
        plan = c.execute("EXPLAIN QUERY PLAN SELECT * FROM fingerprints WHERE song_id=?", (1,)).fetchall()
        print(f"rows={n:>11,}  WHERE song_id=? 全表扫描 = {d_scan:8.0f} ms   db={os.path.getsize(DB)/1048576:.0f}MB  plan={[p[-1] for p in plan]}", flush=True)
    print(f"build total {time.perf_counter()-t_build0:.0f}s", flush=True)
    c.close()
    for f in (DB, DB + "-wal", DB + "-shm"):
        try: os.remove(f)
        except OSError: pass


if __name__ == "__main__":
    main()
