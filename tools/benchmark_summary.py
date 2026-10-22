"""Compare legacy aggregation and transactional counters on synthetic inventories."""
import argparse
import json
import statistics
import tempfile
import time
from pathlib import Path

from migraflow.state import database, summary
from migraflow.state_stats import refresh_deletions


def benchmark(directory, rows, fast):
    directory.mkdir()
    with database(directory) as db:
        if not fast:
            # Disposable benchmark DB only: emulate pre-v6 writes and summary reads.
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
                db.execute('DROP TRIGGER "' + row[0].replace('"', '""') + '"')
            db.execute("PRAGMA user_version=5")
        started = time.perf_counter()
        for job in ("base", "new"):
            db.execute("INSERT INTO plans VALUES(?,?,?,?,?,?,?,?)",
                       (job, "VERIFIED", "now", "now", "{}", "fingerprint", "{}", None))
            db.executemany("INSERT INTO entries VALUES(?,?,?,?,?,?,?,?)",
                           ((job, str(i).encode(), str(i).encode(), "file", 1, 1, "COPY", None) for i in range(rows)))
        db.execute("INSERT INTO plan_lineage VALUES('new','base',1)")
        if fast:
            refresh_deletions(db, "new")
        db.commit()
        write_seconds = time.perf_counter() - started
        elapsed = []
        for _ in range(4):
            started = time.perf_counter()
            result = summary(db, "new")
            elapsed.append(time.perf_counter() - started)
            assert result["files"] == rows and result["deletion_candidates_count"] == 0
        sizes = {path.name: path.stat().st_size for path in directory.glob("migraflow.sqlite3*")}
        return {"insert_and_finalize_seconds": write_seconds,
                "summary_median_seconds": statistics.median(elapsed), "sizes": sizes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--rows", type=int, default=200000)
    args = parser.parse_args()
    if args.rows < 1:
        parser.error("--rows must be positive")
    args.directory.mkdir(parents=True, exist_ok=True)
    # Retain results, never remove or overwrite an existing user directory.
    directory = Path(tempfile.mkdtemp(prefix="summary-", dir=args.directory))
    results = {"directory": str(directory), "rows_per_plan": args.rows}
    for fast in (False, True):
        results["counters" if fast else "legacy"] = benchmark(directory / str(fast), args.rows, fast)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
