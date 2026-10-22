import argparse
import json
import sqlite3
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .config import ConfigError, load_config
from .planner import create_plan
from .monitor import watch
from .preflight import preflight, state_path
from .progress import Dashboard
from .reporting import cutover_report
from .state import database, summary
from .transfer import execute
from .verifier import VerificationError


def parser():
    root = argparse.ArgumentParser(description="MigraFlow migration planner, local transfer and verifier")
    commands = root.add_subparsers(dest="command", required=True)
    for command in ("preflight", "plan", "status", "run", "resume", "verify", "cutover-check"):
        sub = commands.add_parser(command)
        if command == "preflight":
            sub.add_argument("--test-destination-write", action="store_true",
                             help="Create, write, sync and delete a temporary file in the destination")
        if command == "plan":
            planning = sub.add_mutually_exclusive_group()
            planning.add_argument("--baseline", help="VERIFIED plan ID for a new incremental round")
            planning.add_argument("--resume", dest="resume_scan", help="Resume a saved interrupted scan ID")
        sub.add_argument("--config", help="YAML file; defaults to migration.local.yml when present")
        sub.add_argument("--state-dir", help="Local plan database directory")
        sub.add_argument("--json", action="store_true", help="Machine-readable result")
        if command == "status":
            sub.add_argument("--job", help="Plan ID; defaults to the latest plan")
            sub.add_argument("--watch", action="store_true", help="Read-only live monitor; Ctrl+C stops monitoring only")
            sub.add_argument("--interval", type=float, default=2, help="Watch polling seconds (1..60)")
        else:
            sub.add_argument("--source", help="Override source path")
            sub.add_argument("--destination", help="Override destination path")
            sub.add_argument("--source-type", choices=("local", "mounted", "rsync_ssh"))
            sub.add_argument("--destination-type", choices=("local", "mounted", "rsync_ssh"))
            for label in ("source", "destination"):
                sub.add_argument("--" + label + "-host", help="OpenSSH host alias for rsync_ssh")
                sub.add_argument("--" + label + "-remote-state-dir", help="Remote local directory for the worker lease")
            sub.add_argument("--metadata-policy", choices=("basic", "posix"), help="Override YAML metadata.policy")
            if command in ("run", "resume", "verify", "cutover-check"):
                sub.add_argument("--job", required=True, help="Saved plan ID")
                live = sub.add_mutually_exclusive_group()
                live.add_argument("--live", dest="live", action="store_true", default=None,
                                  help="Show live progress (automatic in interactive terminals)")
                live.add_argument("--no-live", dest="live", action="store_false", help="Disable live progress")
            if command == "cutover-check":
                for reference in ("source-freeze", "destination-freeze", "backup", "approval", "metadata-review", "extras-review"):
                    sub.add_argument("--" + reference, help="Operator-provided evidence reference; does not execute a cutover")
            if command in ("run", "resume"):
                sub.add_argument("--overwrite", action="store_true", help="Allow replacements after reviewing the plan and securing backups")
                sub.add_argument("--allow-unprotected-source", action="store_true", help="Development tests only: source read-only protection is not enforced")
    return root


def display(result, json_output):
    if json_output:
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return
    console = Console()
    table = Table(title="MigraFlow")
    table.add_column("Item")
    table.add_column("Value")
    for key in ("id", "job", "baseline_id", "status", "hash_algorithm", "metadata_policy", "files", "verified_files", "retry_count", "reused_files", "deletion_candidates_complete", "deletion_candidates_count", "target_extras_count", "unsupported_entries", "bytes", "state_dir", "source_protection", "destination_free_bytes",
                "destination_write_permission", "destination_write_test"):
        if key in result:
            table.add_row(key, str(result[key]))
    for key, value in result.get("counts", {}).items():
        table.add_row(key, str(value))
    for key, value in result.get("file_statuses", {}).items():
        table.add_row(key, str(value))
    table.add_row("cutover_ready", str(result.get("cutover_ready", False)))
    console.print(table)
    for warning in result.get("warnings", result.get("preflight", {}).get("warnings", [])):
        console.print(f"Warning: {warning}", markup=False)
    if result.get("error"):
        console.print(f"Error: {result['error']}", markup=False)
    for issue in result.get("issues", []):
        console.print(f"Blocked: {issue['path']!r}: {issue['detail']}", markup=False)
    for candidate in result.get("deletion_candidates", []):
        console.print(f"Retained deletion candidate: {candidate['path']!r} ({candidate['kind']})", markup=False)
    for check, passed in result.get("checks", {}).items():
        console.print(f"{'PASS' if passed else 'BLOCKED'}: {check}", markup=False)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "status" and args.config is None and not Path("migration.local.yml").exists():
            # A status query does not depend on source/destination still being available.
            state = state_path(Path(args.state_dir or "state").absolute())
        else:
            config = load_config(args.config, source=getattr(args, "source", None),
                                 destination=getattr(args, "destination", None),
                                 source_type=getattr(args, "source_type", None),
                                 destination_type=getattr(args, "destination_type", None),
                                 state_dir=args.state_dir, metadata_policy=getattr(args, "metadata_policy", None),
                                 source_host=getattr(args, "source_host", None), destination_host=getattr(args, "destination_host", None),
                                 source_remote_state_dir=getattr(args, "source_remote_state_dir", None),
                                 destination_remote_state_dir=getattr(args, "destination_remote_state_dir", None))
            if args.command == "status":
                state = state_path(config.state_dir)
        if args.command == "status":
            if args.watch:
                if args.json:
                    raise ConfigError("--watch and --json cannot be combined.")
                try:
                    watch(state, args.job, Console(stderr=True), interval=args.interval)
                except KeyboardInterrupt:
                    return 0
            with database(state, readonly=True) as connection:
                result = summary(connection, args.job)
        elif args.command == "preflight":
            result = preflight(config, test_destination_write=args.test_destination_write)
        elif args.command in ("run", "resume", "verify", "cutover-check"):
            console = Console(stderr=True)
            if args.live and args.json:
                raise ConfigError("--live and --json cannot be combined.")
            if args.live and not console.is_terminal:
                raise ConfigError("--live requires an interactive terminal; use --no-live for redirected output.")
            enabled = not args.json and console.is_terminal and args.live is not False
            with Dashboard(console, enabled) as observer:
                if args.command == "cutover-check":
                    result = cutover_report(config, args.job, source_freeze=args.source_freeze,
                                            destination_freeze=args.destination_freeze, backup=args.backup,
                                            approval=args.approval, metadata_review=args.metadata_review,
                                            extras_review=args.extras_review, progress=observer)
                else:
                    result = execute(config, args.job, verify_only=args.command == "verify", resume=args.command == "resume",
                                 overwrite=getattr(args, "overwrite", False),
                                 allow_unprotected_source=getattr(args, "allow_unprotected_source", False), progress=observer)
        else:
            result = create_plan(config, baseline_id=args.baseline, resume_id=args.resume_scan)
        display(result, args.json)
        if result.get("status") == "VERIFY_FAILED":
            return 3
        return 0 if result.get("status") in (None, "PLANNED", "VERIFIED", "READY") else 1
    except VerificationError as exc:
        if args.json:
            print(json.dumps({"error": str(exc), "cutover_ready": False}))
        else:
            Console(stderr=True).print(f"Error: {exc}", markup=False)
        return 3
    except ConfigError as exc:
        if args.json:
            print(json.dumps({"error": str(exc), "transfer_ready": False}))
        else:
            Console(stderr=True).print(f"Error: {exc}", markup=False)
        return 2
    except (OSError, sqlite3.Error) as exc:
        if args.json:
            print(json.dumps({"error": str(exc), "transfer_ready": False}))
        else:
            Console(stderr=True).print(f"Error: {exc}", markup=False)
        return 1
    except KeyboardInterrupt:
        return 130
