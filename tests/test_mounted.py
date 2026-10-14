import hashlib
import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from migraflow.config import Config, ConfigError, Endpoint, RetryPolicy
from migraflow.planner import create_plan
from migraflow.preflight import mounts, preflight
from migraflow.rsync_adapter import RsyncError, check_rsync, transfer_batch
from migraflow.state import database
from migraflow.transfer import execute


@pytest.fixture
def mounted(tmp_path):
    source, target = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    target.mkdir()
    config = Config(Endpoint("mounted", source, "nas-source:/data"),
                    Endpoint("mounted", target, "nas-target:/data"), tmp_path / "state",
                    retry=RetryPolicy(3, 0, 0))
    records = {
        "source": {"fs": "nfs4", "source": "nas-source:/data", "options": ["ro"],
                   "point": str(source), "root": "/source", "mount_id": "101"},
        "destination": {"fs": "nfs4", "source": "nas-target:/data", "options": ["rw"],
                        "point": str(target), "root": "/destination", "mount_id": "102"},
    }
    local = {"fs": "ext4", "source": "/dev/test", "options": ["rw"],
             "point": str(tmp_path), "root": "/state-volume"}

    def filesystem(path):
        for label in ("source", "destination"):
            endpoint = getattr(config, label)
            if path == endpoint.path or endpoint.path in path.parents:
                return records[label].copy()
        return local.copy()

    with patch("migraflow.preflight.sys.platform", "linux"), patch(
            "migraflow.preflight.filesystem", side_effect=filesystem), patch(
            "migraflow.planner.filesystem", side_effect=filesystem), patch(
            "migraflow.transfer.filesystem", side_effect=filesystem):
        yield config, records, local


@pytest.fixture
def rsync():
    if os.name == "nt":
        pytest.skip("Mounted transfers run in Linux, including Python inside WSL.")
    return check_rsync()


@pytest.mark.parametrize("types", [("local", "mounted"), ("mounted", "local"), ("mounted", "mounted")])
@pytest.mark.parametrize("protocol", ["nfs4", "cifs"])
@pytest.mark.parametrize("algorithm", ["sha256", "md5"])
def test_mounted_real_transfer_resume_and_tamper_detection(mounted, rsync, types, protocol, algorithm):
    config, records, _ = mounted
    for label, kind in zip(("source", "destination"), types):
        endpoint = getattr(config, label)
        records[label]["fs"] = protocol if kind == "mounted" else "ext4"
        if protocol == "cifs" and kind == "mounted":
            records[label]["source"] = "//" + label + "/data"
        config = replace(config, **{label: Endpoint(kind, endpoint.path,
                         records[label]["source"] if kind == "mounted" else None)})
    config = replace(config, hash_algorithm=algorithm)
    (config.source.path / "empty-dir").mkdir()
    source = config.source.path / "한글 space.txt"
    source.write_bytes(b"original")
    before = (source.read_bytes(), source.stat().st_mtime_ns, source.stat().st_mode)
    planned = create_plan(config)
    assert planned["status"] == "PLANNED", planned

    def publish_then_interrupt(*args, **kwargs):
        transfer_batch(*args, **kwargs)
        raise KeyboardInterrupt

    with patch("migraflow.transfer.transfer_batch", side_effect=publish_then_interrupt):
        with pytest.raises(KeyboardInterrupt):
            execute(config, planned["id"])
    with patch("migraflow.transfer.transfer_batch") as copy:
        result = execute(config, planned["id"], resume=True)
    copy.assert_not_called()
    assert result["status"] == "VERIFIED", result
    assert result["reused_files"] == 1
    assert (config.destination.path / "empty-dir").is_dir()
    with database(config.state_dir, readonly=True) as db:
        row = db.execute("SELECT * FROM file_results WHERE status='VERIFIED'").fetchone()
        assert row["source_hash"] == row["destination_hash"] == hashlib.new(algorithm, b"original").hexdigest()
        assert row["algorithm"] == algorithm
    target = config.destination.path / source.name
    info = target.stat()
    target.write_bytes(b"Original")
    os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
    assert execute(config, planned["id"], verify_only=True)["status"] == "VERIFY_FAILED"
    assert execute(config, planned["id"], resume=True)["status"] == "VERIFIED"
    assert (source.read_bytes(), source.stat().st_mtime_ns, source.stat().st_mode) == before


@pytest.mark.parametrize("change", ["absent", "share", "remount", "readonly"])
@pytest.mark.parametrize("resume", [False, True])
def test_changed_mount_refuses_transfer_before_writes(mounted, change, resume):
    config, records, local = mounted
    (config.source.path / "file").write_bytes(b"data")
    planned = create_plan(config)
    if change == "absent":
        records["destination"] = local.copy()
    elif change == "share":
        records["destination"]["source"] = "different:/data"
    elif change == "remount":
        records["destination"]["mount_id"] = "999"
    else:
        records["destination"]["options"] = ["ro"]
    with patch("migraflow.transfer.transfer_batch") as copy, pytest.raises(ConfigError):
        execute(config, planned["id"], resume=resume)
    copy.assert_not_called()
    assert not list(config.destination.path.iterdir())


def test_readonly_destination_and_unprotected_source_are_blocked(mounted):
    config, records, _ = mounted
    records["destination"]["options"] = ["ro"]
    planned = create_plan(config)
    with pytest.raises(ConfigError, match="Destination mount is read-only"):
        execute(config, planned["id"])
    records["destination"]["options"] = ["rw"]
    records["source"]["options"] = ["rw"]
    planned = create_plan(config)
    with pytest.raises(ConfigError, match="read-only protection"):
        execute(config, planned["id"])


def test_same_share_overlapping_separate_mounts_are_blocked(mounted):
    config, records, _ = mounted
    records["destination"]["source"] = records["source"]["source"]
    records["source"]["root"] = "/data"
    records["destination"]["root"] = "/data/child"
    config = replace(config, destination=replace(config.destination, mount_source=config.source.mount_source))
    original_stat = Path.stat

    def separate_device(path, *args, **kwargs):
        info = original_stat(path, *args, **kwargs)
        if path == config.destination.path:
            values = list(info)
            values[2] += 1
            return os.stat_result(values)
        return info

    with patch.object(Path, "stat", separate_device), pytest.raises(ConfigError, match="overlap through bind mounts"):
        preflight(config)


def test_linux_mount_id_is_recorded_and_escaped_paths_decoded():
    data = "101 1 0:32 / /mnt/nas\\040data ro - nfs4 nas:/export rw\n"
    with patch.object(Path, "read_text", return_value=data):
        record = mounts()[0]
    assert record["mount_id"] == "101"
    assert record["point"] == "/mnt/nas data"
    assert record["options"] == ["ro"]


def test_mount_loss_during_retry_never_starts_another_writer(mounted):
    config, records, local = mounted
    (config.source.path / "file").write_bytes(b"data")
    planned = create_plan(config)

    def disconnected(*args, **kwargs):
        records["destination"] = local.copy()
        raise RsyncError(10, "connection reset")

    with patch("migraflow.transfer.check_rsync", return_value=["rsync"]), patch(
            "migraflow.transfer.transfer_batch", side_effect=disconnected) as copy:
        result = execute(config, planned["id"])
    assert result["status"] == "FAILED"
    assert result["verified_files"] == 0
    assert copy.call_count == 1
    assert not list(config.destination.path.iterdir())


def test_mount_change_during_hash_cannot_mark_verified(mounted, rsync):
    config, records, _ = mounted
    (config.source.path / "file").write_bytes(b"data")
    planned = create_plan(config)
    assert execute(config, planned["id"])["status"] == "VERIFIED"
    from migraflow.verifier import hash_file

    def replaced(*args):
        result = hash_file(*args)
        records["destination"]["mount_id"] = "999"
        return result

    with patch("migraflow.transfer.hash_file", side_effect=replaced):
        result = execute(config, planned["id"], verify_only=True)
    assert result["status"] == "FAILED"
    assert result["verified_files"] == 0


def test_guard_stops_running_rsync_and_releases_worker(mounted, rsync):
    config, records, local = mounted
    original_mount = records["destination"].copy()
    (config.source.path / "large").write_bytes(b"x" * (4 * 1024 * 1024))
    planned = create_plan(config)
    started = False

    def guarded_copy(prefix, source, destination, paths, manifest, log, **kwargs):
        original_guard = kwargs["guard"]

        def guard():
            nonlocal started
            for path in destination.iterdir():
                if path.is_file() and path.stat().st_size >= 65536:
                    started = True
                    records["destination"] = local.copy()
            original_guard()

        return transfer_batch(prefix + ["--bwlimit=512"], source, destination, paths, manifest, log,
                              **dict(kwargs, guard=guard))

    with patch("migraflow.transfer.transfer_batch", side_effect=guarded_copy):
        result = execute(config, planned["id"])
    assert started
    assert result["status"] == "FAILED"
    assert result["verified_files"] == 0
    from migraflow.locking import file_lock
    with file_lock(config.state_dir / "worker.lock"):
        pass
    assert not (config.destination.path / "large").exists()
    records["destination"] = original_mount
    result = execute(config, planned["id"], resume=True)
    assert result["status"] == "VERIFIED", result
    assert (config.destination.path / "large").read_bytes() == (config.source.path / "large").read_bytes()


def test_pinned_rsync_root_never_writes_replacement_directory(mounted, rsync):
    config, _, _ = mounted
    (config.source.path / "file").write_bytes(b"data")
    scratch = config.state_dir / "transfer-test"
    scratch.mkdir(parents=True)
    previous = config.destination.path.with_name("previous-destination")
    replaced = False

    def replace_after_worker_opens_roots():
        nonlocal replaced
        if not replaced:
            config.destination.path.rename(previous)
            config.destination.path.mkdir()
            replaced = True

    transfer_batch(rsync, config.source.path, config.destination.path, [b"file"],
                   scratch / "manifest.bin", scratch / "rsync.log",
                   guard=replace_after_worker_opens_roots, pin_roots=True)
    assert (previous / "file").read_bytes() == b"data"
    assert not list(config.destination.path.iterdir())
