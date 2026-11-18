"""Local-only performance smoke test of SQLite CAS contention.

Measures concurrent compare-and-set operations against a newly created
*temporary* SQLite file. No project database, web server, Discord or Supabase.
This is a baseline smoke harness, NOT an application throughput benchmark.
"""
import argparse
import concurrent.futures
import os
from pathlib import Path
import statistics
import tempfile
import time
from sqlalchemy import create_engine, Table, MetaData, Column, Integer, String, insert, update


def percentile(sorted_values, fraction):
    index = max(0, min(len(sorted_values)-1, int((len(sorted_values)-1)*fraction + .999999)))
    return sorted_values[index]


def run(workers=8, attempts=100):
    if not 1 <= workers <= 32 or not 1 <= attempts <= 5000:
        raise ValueError("Refusing unbounded load")
    with tempfile.TemporaryDirectory(prefix="l2p_perf_") as d:
        path = Path(d) / "disposable_cas.sqlite"
        engine = create_engine(f"sqlite:///{path.as_posix()}",
                               connect_args={"timeout": 15}, pool_size=workers)
        metadata = MetaData()
        t = Table("test_claims", metadata, Column("id", Integer, primary_key=True),
                  Column("status", String(20), nullable=False))
        metadata.create_all(engine)
        with engine.begin() as c:
            c.execute(insert(t), [{"id": i+1, "status": "PENDING"} for i in range(attempts)])
        def claim(i):
            start = time.perf_counter()
            with engine.begin() as c:
                won = c.execute(update(t).where(t.c.id == (i % attempts)+1,
                         t.c.status == "PENDING").values(status="CLAIMED")).rowcount
            return (time.perf_counter()-start)*1000, won
        started = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(claim, range(attempts*2)))
        elapsed = time.perf_counter()-started
        times = sorted(r[0] for r in results)
        wins = sum(r[1] for r in results)
        assert wins == attempts, "Atomic CAS integrity failed"
        engine.dispose()
    return dict(operations=len(results), successful_claims=wins,
                contenders_rejected=len(results)-wins, workers=workers,
                rps=round(len(results)/elapsed,2),
                p50_ms=round(percentile(times,.50),2),
                p95_ms=round(percentile(times,.95),2),
                p99_ms=round(percentile(times,.99),2))

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--workers",type=int, default=8)
    p.add_argument("--attempts",type=int, default=100)
    a=p.parse_args()
    print(run(a.workers, a.attempts))
