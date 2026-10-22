"""Observe one pinned job through short read-only SQLite snapshots."""
import shutil
import time

from .config import ConfigError
from .progress import Dashboard
from .state import database, summary


def watch(state, job, console, *, interval=2):
    if not console.is_terminal:
        raise ConfigError("status --watch requires an interactive terminal.")
    if not 1 <= interval <= 60:
        raise ConfigError("Watch interval must be 1..60 seconds.")
    with Dashboard(console, True) as observer:
        if observer is None:
            raise ConfigError("Cannot open the status watch screen.")
        while True:
            with database(state, readonly=True) as connection:
                connection.execute("BEGIN")
                result = summary(connection, job)
                connection.rollback()
            job = result["id"]
            size = 0
            for path in (state / "migraflow.sqlite3", state / "migraflow.sqlite3-wal"):
                try:
                    size += path.stat().st_size
                except FileNotFoundError:
                    pass  # WAL may disappear when the last connection closes.
            observer("watch", result=result, database_bytes=size, state_free_bytes=shutil.disk_usage(state).free)
            time.sleep(interval)
