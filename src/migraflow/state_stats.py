"""Transactional summary counters; inventory remains the authoritative evidence."""
import json
import os


def deletion_summary(connection, plan_id, baseline_id):
    missing = """FROM entries previous WHERE previous.plan_id=? AND NOT EXISTS
        (SELECT 1 FROM entries current WHERE current.plan_id=? AND current.path=previous.path)"""
    args = (baseline_id, plan_id)
    count = connection.execute("SELECT COUNT(*) " + missing, args).fetchone()[0]
    sample = [{"path": os.fsdecode(row[0]), "kind": row[1]} for row in connection.execute(
        "SELECT previous.path,previous.kind " + missing + " ORDER BY previous.path LIMIT 20", args)]
    return count, sample


def refresh_deletions(connection, plan_id):
    lineage = connection.execute(
        "SELECT baseline_id FROM plan_lineage WHERE plan_id=? AND scan_complete=1", (plan_id,)).fetchone()
    if lineage:
        count, sample = deletion_summary(connection, plan_id, lineage[0])
        connection.execute("INSERT OR REPLACE INTO deletion_stats VALUES(?,?,?,?)",
                           (plan_id, lineage[0], count, json.dumps(sample)))


def migrate(connection):
    # Called once for pre-v6 writers. No reader performs migrations or cache writes.
    connection.executescript("""
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS inventory_counts (
            plan_id TEXT NOT NULL, action TEXT NOT NULL, count INTEGER NOT NULL,
            files INTEGER NOT NULL, bytes INTEGER NOT NULL, PRIMARY KEY(plan_id,action));
        CREATE TABLE IF NOT EXISTS verification_counts (
            plan_id TEXT NOT NULL, status TEXT NOT NULL, count INTEGER NOT NULL,
            PRIMARY KEY(plan_id,status));
        CREATE TABLE IF NOT EXISTS deletion_stats (
            plan_id TEXT PRIMARY KEY, baseline_id TEXT NOT NULL, count INTEGER NOT NULL, sample TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS deletion_baselines ON deletion_stats(baseline_id);
        DELETE FROM inventory_counts;
        INSERT INTO inventory_counts SELECT plan_id,action,COUNT(*),
            SUM(kind='file'),SUM(CASE WHEN kind='file' THEN size ELSE 0 END)
            FROM entries GROUP BY plan_id,action;
        DELETE FROM verification_counts;
        INSERT INTO verification_counts SELECT plan_id,status,COUNT(*) FROM file_results GROUP BY plan_id,status;
        DELETE FROM deletion_stats;
    """)
    # Fixed internal identifiers only. UPDATE is subtract-old/add-new, including upserts.
    for table, totals, key, values in (
        ("entries", "inventory_counts", "action", ["1", "{row}.kind='file'", "CASE WHEN {row}.kind='file' THEN {row}.size ELSE 0 END"]),
        ("file_results", "verification_counts", "status", ["1"]),
    ):
        columns = ["count", "files", "bytes"] if table == "entries" else ["count"]
        for event in ("INSERT", "DELETE", "UPDATE"):
            name = f"{table}_counts_{event.lower()}"
            connection.execute(f"DROP TRIGGER IF EXISTS {name}")
            statements = []
            if event != "INSERT":
                subtract = ",".join(f"{column}={column}-({value.format(row='old')})"
                                    for column, value in zip(columns, values))
                statements.append(f"UPDATE {totals} SET {subtract} WHERE plan_id=old.plan_id AND {key}=old.{key};")
            if event != "DELETE":
                additions = ",".join(value.format(row="new") for value in values)
                update = ",".join(f"{column}={column}+excluded.{column}" for column in columns)
                statements.append(f"INSERT INTO {totals} VALUES(new.plan_id,new.{key},{additions}) "
                                  f"ON CONFLICT(plan_id,{key}) DO UPDATE SET {update};")
            if table == "entries":
                for row in (("new",) if event == "INSERT" else ("old",) if event == "DELETE" else ("old", "new")):
                    statements.append(f"DELETE FROM deletion_stats WHERE plan_id={row}.plan_id OR baseline_id={row}.plan_id;")
            connection.execute(f"CREATE TRIGGER {name} AFTER {event} ON {table} BEGIN " + " ".join(statements) + " END")
    connection.execute("DROP TRIGGER IF EXISTS lineage_stats_update")
    connection.execute("""CREATE TRIGGER lineage_stats_update AFTER UPDATE ON plan_lineage BEGIN
        DELETE FROM deletion_stats WHERE plan_id=old.plan_id OR plan_id=new.plan_id; END""")
    connection.execute("DROP TRIGGER IF EXISTS lineage_stats_delete")
    connection.execute("""CREATE TRIGGER lineage_stats_delete AFTER DELETE ON plan_lineage BEGIN
        DELETE FROM deletion_stats WHERE plan_id=old.plan_id; END""")
    for row in connection.execute("SELECT plan_id FROM plan_lineage WHERE scan_complete=1"):
        refresh_deletions(connection, row[0])
    connection.execute("PRAGMA user_version=6")
