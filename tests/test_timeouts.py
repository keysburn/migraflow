import errno
import subprocess
import sys
import tempfile
from dataclasses import replace
from unittest.mock import patch

import pytest
import yaml

from migraflow.config import Config, ConfigError, Endpoint, RetryPolicy, TimeoutPolicy, load_config
from migraflow.locking import file_lock
from migraflow.planner import create_plan
from migraflow.preflight import config_data
from migraflow.progress import observing
from migraflow.rsync_adapter import RsyncError, check_rsync, transfer_batch
from migraflow.ssh import Session
from migraflow.transfer import execute


@pytest.fixture
def config(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    return Config(Endpoint("local", source), Endpoint("local", target), tmp_path / "state")


@pytest.mark.parametrize("settings", [None, [], {"unknown": 1}, {"hash_idle_seconds": None},
    {"transfer_idle_seconds": 0}, {"transfer_idle_seconds": 1.5}, {"hash_total_seconds": True},
    {"hash_idle_seconds": float("nan")}, {"transfer_total_seconds": -1}, {"hash_idle_seconds": 10**400}])
def test_invalid_timeout_policy(config, tmp_path, settings):
    file = tmp_path / "settings.yml"
    file.write_text(yaml.safe_dump({"source": {"path": str(config.source.path)},
                                  "destination": {"path": str(config.destination.path)}, "timeouts": settings}))
    with pytest.raises(ConfigError, match="timeouts"):
        load_config(file)


def test_timeout_policy_yaml_and_existing_fingerprint(config, tmp_path):
    assert "timeouts" not in config_data(config)
    file = tmp_path / "settings.yml"
    file.write_text(yaml.safe_dump({"source": {"path": str(config.source.path)},
        "destination": {"path": str(config.destination.path)}, "timeouts": {
            "transfer_idle_seconds": 90, "transfer_total_seconds": 600, "hash_idle_seconds": 2.5, "hash_total_seconds": None}}))
    assert load_config(file).timeouts == TimeoutPolicy(90, 600, 2.5, None)


def test_runtime_timeout_changes_reuse_plan_and_baseline(config):
    try:
        check_rsync()
    except ConfigError as exc:
        pytest.skip(str(exc))
    (config.source.path / "file").write_bytes(b"content")
    plan = create_plan(config)
    changed = replace(config, timeouts=TimeoutPolicy(120, 60, 120, 60))
    result = execute(changed, plan["id"], allow_unprotected_source=True)
    assert result["status"] == "VERIFIED", result
    assert result["preflight"]["execution_options"]["timeouts"]["transfer_total_seconds"] == 60
    assert execute(config, plan["id"], resume=True, allow_unprotected_source=True)["reused_files"] == 1
    assert create_plan(changed, baseline_id=plan["id"])["status"] == "PLANNED"


@pytest.mark.skipif(sys.platform != "linux", reason="SSH pipe readiness uses Linux select.")
@pytest.mark.parametrize("mode", ["progress", "stall", "total", "duplicate", "partial-line"])
def test_remote_hash_progress_idle_and_total_limits(mode):
    # A real pipe helper, with short limits instead of a 30-minute wall-clock test.
    program = """import json,sys,time
json.loads(sys.stdin.buffer.readline())
for index in range(8):
    time.sleep(0.1)
    if MODE == 'stall':
        continue
    if MODE == 'partial-line':
        sys.stdout.write(' '); sys.stdout.flush(); continue
    print(json.dumps({'progress_bytes': 10 if MODE == 'duplicate' else (index+1)*10}),flush=True)
print(json.dumps({'result': ['digest',[1,2,100,3,4]]}),flush=True)
""".replace("MODE", repr(mode))
    session = Session.__new__(Session)
    session.header = {}
    session.timeouts = TimeoutPolicy(hash_idle_seconds=0.5, hash_total_seconds=0.3 if mode == "total" else None)
    session.error = tempfile.TemporaryFile()
    session.process = subprocess.Popen([sys.executable, "-u", "-c", program], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=session.error, bufsize=0)
    session.buffer, session.closed = bytearray(), False
    events = []
    try:
        with observing(lambda kind, **values: events.append((kind, values))):
            if mode == "progress":
                assert session.call("hash", algorithm="sha256")[1][2] == 100
                assert sum(value["bytes"] for kind, value in events if kind == "hash_chunk") == 100
            elif mode == "duplicate":
                with pytest.raises(ConfigError, match="progress"):
                    session.call("hash", algorithm="sha256")
            else:
                with pytest.raises(OSError) as error:
                    session.call("hash", algorithm="sha256")
                assert error.value.errno == errno.ETIMEDOUT
                assert session.closed
    finally:
        session.close()
    assert session.process.poll() is not None


@pytest.mark.skipif(sys.platform != "linux", reason="Standalone supervisor test uses native Linux child groups.")
def test_transfer_total_limit_reaps_child_and_releases_lease(config, tmp_path):
    manifest = tmp_path / "state" / "batch" / "manifest"
    manifest.parent.mkdir(parents=True)
    completed = config.destination.path / "completed"
    program = "import time,pathlib;time.sleep(0.5);pathlib.Path(" + repr(str(completed)) + ").touch()"
    prefix = [sys.executable, "-c", program]
    with pytest.raises(RsyncError) as error:
        transfer_batch(prefix, config.source.path, config.destination.path, [], manifest, tmp_path / "log",
                       timeouts=TimeoutPolicy(transfer_total_seconds=0.05))
    assert error.value.code == 30
    assert not completed.exists()
    with file_lock(manifest.parent.parent / "worker.lock"):
        pass
    # Default has no total deadline; actual child completion is awaited.
    transfer_batch(prefix, config.source.path, config.destination.path, [], manifest, tmp_path / "log")
    assert completed.exists()


def test_rsync_progress_can_exceed_idle_window(config):
    try:
        prefix = check_rsync()
    except ConfigError as exc:
        pytest.skip(str(exc))
    (config.source.path / "file").write_bytes(b"x" * (3 * 1024 * 1024))
    plan = create_plan(config)
    changed = replace(config, timeouts=TimeoutPolicy(transfer_idle_seconds=1))
    with patch("migraflow.transfer.check_rsync", return_value=prefix + ["--bwlimit=1024"]):
        result = execute(changed, plan["id"], allow_unprotected_source=True)
    assert result["status"] == "VERIFIED", result


def test_real_rsync_total_timeout_can_resume(config):
    try:
        prefix = check_rsync()
    except ConfigError as exc:
        pytest.skip(str(exc))
    (config.source.path / "file").write_bytes(b"x" * (3 * 1024 * 1024))
    plan = create_plan(config)
    limited = replace(config, retry=RetryPolicy(1), timeouts=TimeoutPolicy(transfer_total_seconds=0.1))
    with patch("migraflow.transfer.check_rsync", return_value=prefix + ["--bwlimit=1024"]):
        failed = execute(limited, plan["id"], allow_unprotected_source=True)
    assert failed["status"] == "FAILED", failed
    assert failed["verified_files"] == 0
    result = execute(config, plan["id"], resume=True, allow_unprotected_source=True)
    assert result["status"] == "VERIFIED", result
