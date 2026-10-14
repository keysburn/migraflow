import io
import json
from dataclasses import replace
from unittest.mock import patch

import pytest
from rich.console import Console

from migraflow.cli import main
from migraflow.config import Config, ConfigError, Endpoint, RetryPolicy
from migraflow.planner import create_plan
from migraflow.progress import Dashboard, emit, observing
from migraflow.rsync_adapter import ProgressLog, RsyncError, check_rsync, transfer_batch
from migraflow.state import database
from migraflow.transfer import execute
from migraflow.verifier import hash_file


@pytest.fixture
def config(tmp_path):
    source, target = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    target.mkdir()
    (source / "file").write_bytes(b"x" * (2 * 1024 * 1024 + 3))
    return Config(Endpoint("local", source), Endpoint("local", target), tmp_path / "state",
                  retry=RetryPolicy(3, 0, 0))


@pytest.fixture
def rsync():
    try:
        return check_rsync()
    except ConfigError as exc:
        pytest.skip(str(exc))


def arguments(config):
    return ["--source", str(config.source.path), "--destination", str(config.destination.path),
            "--state-dir", str(config.state_dir)]


@pytest.mark.parametrize("algorithm", ["sha256", "md5"])
def test_live_events_match_persisted_results_and_resume(config, rsync, algorithm):
    config = replace(config, hash_algorithm=algorithm)
    planned = create_plan(config)
    dashboard = Dashboard(Console(file=io.StringIO()), False)
    result = execute(config, planned["id"], allow_unprotected_source=True, progress=dashboard.update)
    assert result["status"] == dashboard.data["status"] == "VERIFIED"
    assert result["verified_files"] == dashboard.data["verified"] == 1
    assert dashboard.data["files"] == dashboard.data["total_files"] == 1
    assert dashboard.data["bytes"] == dashboard.data["total_bytes"] == result["bytes"]
    assert dashboard.data["algorithm"] == algorithm
    with patch("migraflow.transfer.transfer_batch") as copy:
        result = execute(config, planned["id"], resume=True, allow_unprotected_source=True, progress=dashboard.update)
    copy.assert_not_called()
    assert dashboard.data["reused"] == result["reused_files"] == 1
    assert dashboard.data["verified"] == result["verified_files"] == 1


def test_display_error_does_not_interrupt_transfer(config, rsync):
    planned = create_plan(config)
    events = []

    def broken(kind, **values):
        events.append(kind)
        raise OSError("terminal disappeared")

    result = execute(config, planned["id"], allow_unprotected_source=True, progress=broken)
    assert result["status"] == "VERIFIED"
    assert len(events) == 1
    assert (config.destination.path / "file").read_bytes() == (config.source.path / "file").read_bytes()


def test_live_retry_and_failure_counts_match_database(config, rsync):
    planned = create_plan(config)
    dashboard = Dashboard(Console(file=io.StringIO()), False)
    attempts = 0

    def transient(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RsyncError(10, "temporary")
        return transfer_batch(*args, **kwargs)

    with patch("migraflow.transfer.transfer_batch", side_effect=transient):
        result = execute(config, planned["id"], allow_unprotected_source=True, progress=dashboard.update)
    assert result["status"] == "VERIFIED"
    assert dashboard.data["retries"] == result["retry_count"] == 1
    assert dashboard.data["errors"] == 1
    (config.destination.path / "file").write_bytes(b"wrong")
    result = execute(config, planned["id"], verify_only=True, progress=dashboard.update)
    assert result["status"] == dashboard.data["status"] == "VERIFY_FAILED"
    assert dashboard.data["verified"] == result["verified_files"] == 0
    assert dashboard.data["failures"] == 1


def test_keyboard_interrupt_keeps_checkpoint_and_reports_interrupted(config, rsync):
    planned = create_plan(config)
    dashboard = Dashboard(Console(file=io.StringIO()), False)
    with patch("migraflow.transfer.transfer_batch", side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            execute(config, planned["id"], allow_unprotected_source=True, progress=dashboard.update)
    assert dashboard.data["status"] == "INTERRUPTED"
    assert dashboard.data["verified"] == 0
    with database(config.state_dir, readonly=True) as db:
        assert db.execute("SELECT status FROM plans WHERE id=?", (planned["id"],)).fetchone()[0] == "INTERRUPTED"


def test_hash_bytes_are_live_and_bounded(config):
    events = []
    with observing(lambda kind, **values: events.append((kind, values))):
        hash_file(config.source.path / "file", "sha256")
    assert events[0][0] == "hash_start"
    chunks = [value["bytes"] for kind, value in events if kind == "hash_chunk"]
    assert chunks == [1024 * 1024, 1024 * 1024, 3]
    assert events[-1][0] == "hash_end"


@pytest.mark.parametrize("width", [48, 100])
def test_dashboard_renders_literal_paths_and_unknown_eta(width):
    output = io.StringIO()
    console = Console(file=output, width=width, force_terminal=False)
    dashboard = Dashboard(console, False)
    dashboard.update("init", source="[red]source[/red]", planned_files=10)
    dashboard.update("hash_start", path="bad\nname\x1b[31m", size=100)
    console.print(dashboard)
    rendered = output.getvalue()
    assert "MigraFlow" in rendered
    assert "[red]source[/red]" in rendered
    assert "\\n" in rendered
    assert "\x1b" not in rendered
    assert "미확정" in rendered


def test_json_and_redirected_output_never_start_live_screen(config, monkeypatch, capsys):
    monkeypatch.chdir(config.source.path.parent)
    result = {"status": "VERIFIED", "id": "job", "verified_files": 1}
    with patch("migraflow.cli.execute", return_value=result) as run, patch("migraflow.progress.Live") as live:
        assert main(["run", "--job", "job", "--json", *arguments(config)]) == 0
        assert json.loads(capsys.readouterr().out) == result
        assert run.call_args.kwargs["progress"] is None
        assert main(["run", "--job", "job", *arguments(config)]) == 0
        assert run.call_args.kwargs["progress"] is None
    live.assert_not_called()


def test_live_json_conflict_and_non_terminal_refused(config, monkeypatch, capsys):
    monkeypatch.chdir(config.source.path.parent)
    with patch("migraflow.cli.execute") as run:
        assert main(["run", "--job", "job", "--live", "--json", *arguments(config)]) == 2
        assert "cannot be combined" in json.loads(capsys.readouterr().out)["error"]
        assert main(["verify", "--job", "job", "--live", *arguments(config)]) == 2
        assert "interactive terminal" in capsys.readouterr().err
    run.assert_not_called()


@pytest.mark.parametrize("option", [[], ["--live"], ["--no-live"]])
def test_terminal_auto_live_and_explicit_disable(config, monkeypatch, option):
    monkeypatch.chdir(config.source.path.parent)
    console = Console(file=io.StringIO(), force_terminal=True)
    with patch("migraflow.cli.Console", return_value=console), patch(
            "migraflow.cli.execute", return_value={"status": "VERIFIED"}) as run:
        assert main(["run", "--job", "job", *option, *arguments(config)]) == 0
    assert (run.call_args.kwargs["progress"] is not None) == (option != ["--no-live"])


def test_observer_scope_does_not_leak():
    seen = []
    with observing(lambda kind, **values: seen.append(kind)):
        emit("inside")
    emit("outside")
    assert seen == ["inside"]


def test_progress_log_reads_only_new_bounded_data_and_ignores_errors(tmp_path):
    log = tmp_path / "rsync.log"
    log.write_bytes(b"123 100% 1.00MB/s 0:00:00\n")
    reader = ProgressLog(log)
    events = []
    with observing(lambda kind, **values: events.append((kind, values))):
        reader.poll()
        assert not events
        with log.open("ab") as output:
            output.write(b"\r1,234 50% 1.00MB/s 0:00")
        reader.poll()
        assert not events
        with log.open("ab") as output:
            output.write(b":01\rerror: file vanished\n,,, 10% 1.00MB/s 0:00:00\n")
        reader.poll()
        assert events == [("rsync", {"bytes": 1234, "percent": 50, "speed": "1.00MB/s", "time": "0:00:01"})]
        with log.open("ab") as output:
            output.write(b"x" * 20000)
        offset = reader.offset
        reader.poll()
        assert reader.offset - offset == 4096
        assert len(reader.buffer) <= 4096
        with patch.object(type(log), "open", side_effect=OSError("log unavailable")):
            reader.poll()


def test_real_rsync_stream_progress_precedes_checkpoint(config, rsync):
    (config.source.path / "file").write_bytes(b"x" * (4 * 1024 * 1024))
    planned = create_plan(config)
    samples = []

    def observer(kind, **values):
        if kind == "rsync":
            with database(config.state_dir, readonly=True) as db:
                verified = db.execute("SELECT COUNT(*) FROM file_results WHERE status='VERIFIED'").fetchone()[0]
            samples.append((values, verified))

    with patch("migraflow.transfer.check_rsync", return_value=rsync + ["--bwlimit=1024"]):
        result = execute(config, planned["id"], allow_unprotected_source=True, progress=observer)
    assert result["status"] == "VERIFIED"
    assert samples
    assert any(0 < values["percent"] < 100 for values, _ in samples)
    assert all(verified == 0 for _, verified in samples)


def test_cp949_console_renders_unknown_values_and_unicode_filenames():
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="cp949", errors="strict", write_through=True)
    console = Console(file=stream, width=100, legacy_windows=False)
    dashboard = Dashboard(console, False)
    dashboard.update("hash_start", path="data-😀.bin", size=10)
    console.print(dashboard)
    rendered = buffer.getvalue().decode("cp949")
    assert "MigraFlow" in rendered
    assert "\\U0001f600" in rendered


@pytest.mark.parametrize("failure", [OSError("closed terminal"), UnicodeError("encoding")])
def test_live_output_failure_falls_back_to_no_observer(failure):
    dashboard = Dashboard(Console(file=io.StringIO()), True)
    with patch("migraflow.progress.Live") as live:
        live.return_value.start.side_effect = failure
        live.return_value.stop.side_effect = failure
        with dashboard as observer:
            assert observer is None


def test_cli_live_runs_real_transfer_and_verify(config, rsync, monkeypatch):
    monkeypatch.chdir(config.source.path.parent)
    planned = create_plan(config)
    output = io.StringIO()
    console = Console(file=output, width=80, force_terminal=True)
    with patch("migraflow.cli.Console", return_value=console):
        assert main(["run", "--job", planned["id"], "--live", "--allow-unprotected-source",
                     *arguments(config)]) == 0
        assert main(["verify", "--job", planned["id"], "--live", *arguments(config)]) == 0
    assert "MigraFlow" in output.getvalue()
    assert "VERIFIED" in output.getvalue()
    assert (config.destination.path / "file").read_bytes() == (config.source.path / "file").read_bytes()
