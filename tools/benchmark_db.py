"""Measure candidate indexes on an isolated synthetic SQLite database."""
import argparse
import json
import statistics
import tempfile
import time
from pathlib import Path

from migraflow.state import database, summary


INDEXES = (
    "CREATE INDEX result_status ON file_results(plan_id,status)",
    "CREATE INDEX result_errors ON file_results(plan_id,path) WHERE error IS NOT NULL",
    "CREATE INDEX entry_directories ON entries(plan_id,length(path) DESC) WHERE kind='directory'",
    "CREATE INDEX attempt_outcomes ON attempts(plan_id,outcome)",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=50000)
    parser.add_argument("--directory", type=Path, default=Path("work/db-benchmarks"))
    args = parser.parse_args()
    if not 1000 <= args.rows <= 500000:
        parser.error("rows must be 1000..500000")
    args.directory.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="run-", dir=args.directory)).resolve()
    with database(root) as connection:
        # database() adds production indexes; remove them to measure the isolated before case.
        for sql in INDEXES:
            connection.execute("DROP INDEX IF EXISTS " + sql.split()[2])
        connection.executemany("INSERT INTO plans VALUES(?,'VERIFIED','0','0','{}','benchmark','{}',NULL)",
                               [(f"plan-{index}",) for index in range(20)])
        for start in range(0, args.rows, 500):
            entries, results = [], []
            for index in range(start, min(start + 500, args.rows)):
                path = f"file-{index:08d}".encode()
                kind = "directory" if index % 20 == 0 else "file"
                entries.append(("plan-0", path, path, kind, 0 if kind == "directory" else 1024, 0, "COPY_NEW", None))
                if kind == "file":
                    results.append(("plan-0", path, "VERIFIED", "sha256", "[1,2,1024,0,0]", "a" * 64, "a" * 64, None, 1))
            connection.executemany("INSERT INTO entries VALUES(?,?,?,?,?,?,?,?)", entries)
            connection.executemany("INSERT INTO file_results VALUES(?,?,?,?,?,?,?,?,?)", results)
        connection.execute("UPDATE file_results SET error='test failure',status='VERIFY_FAILED' WHERE path=?", (f"file-{args.rows - 1:08d}".encode(),))
        connection.executemany("INSERT INTO attempts(plan_id,batch,attempt,created_at,outcome) VALUES(?,1,1,'0','RETRY')",
                               ((f"plan-{index % 20}",) for index in range(20000)))
        connection.commit()

        def size():
            return connection.execute("PRAGMA page_count").fetchone()[0] * connection.execute("PRAGMA page_size").fetchone()[0]

        def measure():
            samples = []
            for _ in range(10):
                started = time.perf_counter()
                summary(connection, "plan-0")
                samples.append(time.perf_counter() - started)
            started = time.perf_counter()
            cursor = connection.execute("SELECT * FROM entries WHERE plan_id=? AND kind='directory' ORDER BY length(path) DESC", ("plan-0",))
            while cursor.fetchmany(500):
                pass
            directories = time.perf_counter() - started
            updates = [("plan-0", f"file-{index:08d}".encode()) for index in range(1, 1001) if index % 20]
            started = time.perf_counter()
            connection.executemany("UPDATE file_results SET status='TRANSFERRED' WHERE plan_id=? AND path=?", updates)
            connection.commit()
            update_seconds = time.perf_counter() - started
            connection.executemany("UPDATE file_results SET status='VERIFIED' WHERE plan_id=? AND path=?", updates)
            connection.commit()
            return {"summary_median_seconds": statistics.median(samples), "directories_seconds": directories,
                    "updates_seconds": update_seconds, "logical_db_bytes": size()}

        before = measure()
        for sql in INDEXES:
            connection.execute(sql)
        connection.commit()
        after = measure()
        report = {"rows": args.rows, "before": before, "after": after, "indexes": INDEXES, "directory": str(root)}
    (root / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
