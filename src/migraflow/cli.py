import argparse
import json
import sqlite3
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .config import ConfigError, load_config
from .planner import create_plan
from .preflight import preflight, state_path
from .state import database, summary
from .transfer import execute
from .verifier import VerificationError


def parser():
    root = argparse.ArgumentParser(description="MigraFlow migration planner, local transfer and verifier")
    commands = root.add_subparsers(dest="command", required=True)
    for command in ("preflight", "plan", "status", "run", "resume", "verify"):
        sub = commands.add_parser(command)
        if command == "preflight":
            sub.add_argument("--test-destination-write", action="store_true",
                             help="Create, write, sync and delete a temporary file in the destination")
        sub.add_argument("--config", help="YAML file; defaults to migration.local.yml when present")
        sub.add_argument("--state-dir", help="Local plan database directory")
        sub.add_argument("--json", action="store_true", help="Machine-readable result")
        if command == "status":
            sub.add_argument("--job", help="Plan ID; defaults to the latest plan")
        else:
            sub.add_argument("--source", help="Override source path")
            sub.add_argument("--destination", help="Override destination path")
            sub.add_argument("--source-type", choices=("local", "mounted"))
            sub.add_argument("--destination-type", choices=("local", "mounted"))
            if command in ("run", "resume", "verify"):
                sub.add_argument("--job", required=True, help="Saved plan ID")
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
    for key in ("id", "status", "hash_algorithm", "files", "verified_files", "retry_count", "reused_files", "bytes", "state_dir", "source_protection", "destination_free_bytes",
                "destination_write_permission", "destination_write_test"):
        if key in result:
            table.add_row(key, str(result[key]))
    for key, value in result.get("counts", {}).items():
        table.add_row(key, str(value))
    for key, value in result.get("file_statuses", {}).items():
        table.add_row(key, str(value))
    table.add_row("cutover_ready", "False")
    console.print(table)
    for warning in result.get("warnings", result.get("preflight", {}).get("warnings", [])):
        console.print(f"Warning: {warning}", markup=False)
    if result.get("error"):
        console.print(f"Error: {result['error']}", markup=False)
    for issue in result.get("issues", []):
        console.print(f"Blocked: {issue['path']!r}: {issue['detail']}", markup=False)


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
                                 state_dir=args.state_dir)
            if args.command == "status":
                state = state_path(config.state_dir)
        if args.command == "status":
            with database(state, readonly=True) as connection:
                result = summary(connection, args.job)
        elif args.command == "preflight":
            result = preflight(config, test_destination_write=args.test_destination_write)
        elif args.command in ("run", "resume", "verify"):
            result = execute(config, args.job, verify_only=args.command == "verify", resume=args.command == "resume",
                             overwrite=getattr(args, "overwrite", False),
                             allow_unprotected_source=getattr(args, "allow_unprotected_source", False))
        else:
            result = create_plan(config)
        display(result, args.json)
        if result.get("status") == "VERIFY_FAILED":
            return 3
        return 0 if result.get("status") in (None, "PLANNED", "VERIFIED") else 1
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
