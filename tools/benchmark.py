"""Compare versions using generated local data only; leaves results in --directory."""
import argparse
import cProfile
import hashlib
import json
import os
import platform
import statistics
import tempfile
import time
import tracemalloc
from pathlib import Path

from migraflow.config import Config, Endpoint
from migraflow.planner import create_plan
from migraflow.rsync_adapter import check_rsync
from migraflow.state import database, summary
from migraflow.transfer import execute


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("work/benchmarks"))
    parser.add_argument("--files", type=int, default=500)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--memory", action="store_true", help="Track Python allocations (adds timing overhead)")
    args = parser.parse_args()
    if not 1 <= args.files <= 100000 or not 1 <= args.rounds <= 10:
        parser.error("files must be 1..100000; rounds must be 1..10")
    prefix = check_rsync()
    args.directory.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="run-", dir=args.directory)).resolve()
    source, target = root / "source", root / "destination"
    source.mkdir()
    target.mkdir()
    for index in range(args.files):
        folder = source / f"group-{index % 10}" / "nested"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"file-{index:06d}").write_bytes(b"x" * 1024)
    # Normalize generated fixture timestamps before measurement, including NTFS directory entries.
    for path in source.rglob("*"):
        os.utime(path, ns=(1700000000000000000, 1700000000000000000))
    os.utime(source, ns=(1700000000000000000, 1700000000000000000))
    config = Config(Endpoint("local", source), Endpoint("local", target), root / "state")
    samples = {name: [] for name in ("plan", "run", "verify", "incremental", "status")}
    peaks = {name: [] for name in samples}

    def measure(name, function):
        if args.memory:
            tracemalloc.start()
        started = time.perf_counter()
        try:
            result = function()
            samples[name].append(time.perf_counter() - started)
            if args.memory:
                peaks[name].append(tracemalloc.get_traced_memory()[1])
            return result
        finally:
            if args.memory:
                tracemalloc.stop()

    for _ in range(args.rounds):
        plan = measure("plan", lambda: create_plan(config))
        assert plan["status"] == "PLANNED", plan
        result = measure("run", lambda: execute(config, plan["id"], overwrite=True, allow_unprotected_source=True))
        assert result["status"] == "VERIFIED", result
        result = measure("verify", lambda: execute(config, plan["id"], verify_only=True))
        assert result["status"] == "VERIFIED", result
        next_plan = create_plan(config, plan["id"])
        result = measure("incremental", lambda: execute(config, next_plan["id"], allow_unprotected_source=True))
        assert result["status"] == "VERIFIED" and result["reused_files"] == args.files, result
        with database(config.state_dir, readonly=True) as connection:
            for _ in range(10):
                measure("status", lambda: summary(connection, next_plan["id"]))
    profile = cProfile.Profile()
    profile.runcall(execute, config, next_plan["id"], verify_only=True)
    import pstats
    stats = pstats.Stats(profile).stats
    hot = [{"file": Path(key[0]).name, "function": key[2], "calls": value[1], "cumulative_seconds": value[3]}
           for key, value in sorted(stats.items(), key=lambda item: item[1][3], reverse=True)[:20]]
    source_hash = hashlib.sha256()
    for file in sorted(Path(__file__).resolve().parents[1].joinpath("src/migraflow").glob("*.py")):
        source_hash.update(file.name.encode())
        source_hash.update(file.read_bytes())
    report = {"platform": platform.platform(), "python": platform.python_version(), "rsync": prefix,
              "files": args.files, "bytes_per_file": 1024, "rounds": args.rounds,
              "source_tree_sha256": source_hash.hexdigest(), "python_allocation_tracking": args.memory,
              "phases": {name: {"median_seconds": statistics.median(values), "seconds": values,
                         "peak_python_bytes": max(peaks[name]) if peaks[name] else None} for name, values in samples.items()},
              "verify_profile": hot, "result_directory": str(root)}
    (root / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
