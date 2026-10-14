import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from migraflow.cli import main
from migraflow.config import Config, ConfigError, Endpoint, load_config
from migraflow.planner import create_plan
from migraflow.preflight import endpoint_identity, preflight, _unescape
from migraflow.state import database, summary, writer_lock


@pytest.fixture
def config(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    return Config(Endpoint("local", source), Endpoint("local", destination), tmp_path / "state")


def settings(config):
    return {"source": {"type": "local", "path": str(config.source.path)},
            "destination": {"type": "local", "path": str(config.destination.path)},
            "state_dir": str(config.state_dir)}


def snapshot(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
            for p in root.rglob("*") if p.is_file()}


def test_config_file_relative_paths_and_cli_priority(tmp_path, monkeypatch):
    folder = tmp_path / "settings"
    folder.mkdir()
    file = folder / "migration.yml"
    file.write_text("source:\n  path: relative-source\ndestination:\n  path: relative-destination\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    loaded = load_config(file, source="override-source")
    assert loaded.source.path == tmp_path / "override-source"
    assert loaded.destination.path == folder / "relative-destination"
    assert loaded.state_dir == folder / "state"


@pytest.mark.parametrize("content", [
    "source: {}\nsource: {}", "unknown: true", "source: [a]", "source: {path: 123}",
    "source: {type: rsync_ssh, path: /tmp}\ndestination: {path: /tmp2}",
    "source: {type: mounted, path: /tmp}\ndestination: {path: /tmp2}",
    "source: {path: /tmp, typo: x}\ndestination: {path: /tmp2}",
    "!!python/object/apply:os.system ['echo unsafe']", "source: [", "[]",
])
def test_invalid_yaml_fails_closed(tmp_path, content):
    file = tmp_path / "migration.yml"
    file.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(file)


def test_missing_explicit_config_does_not_fall_back(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.yml", source="a", destination="b")


def test_preflight_writes_nothing(config):
    with patch("migraflow.preflight.tempfile.mkstemp", side_effect=AssertionError("Unexpected write")):
        result = preflight(config)
    assert result["destination_write_permission"] is True
    assert result["destination_write_test"] == "not_requested"
    assert not config.state_dir.exists()
    assert result["transfer_ready"] is False
    assert not list(config.source.path.iterdir())
    assert not list(config.destination.path.iterdir())


def test_destination_permission_failure_keeps_readonly_planning(config):
    access = os.access

    def denied(path, mode):
        return False if mode & os.W_OK else access(path, mode)

    with patch("migraflow.preflight.os.access", side_effect=denied):
        result = create_plan(config)
    assert result["status"] == "PLANNED"
    assert result["preflight"]["destination_write_permission"] is False
    assert any("write permission check failed" in warning for warning in result["preflight"]["warnings"])
    assert not list(config.destination.path.iterdir())


def test_cli_destination_write_test_preserves_existing_files(config, tmp_path, capsys):
    file = tmp_path / "migration.yml"
    file.write_text(yaml.safe_dump(settings(config)), encoding="utf-8")
    (config.source.path / "source.txt").write_bytes(b"source")
    (config.destination.path / ".migraflow-write-test-existing").write_bytes(b"keep")
    before_source, before_destination = snapshot(config.source.path), snapshot(config.destination.path)
    source_mtime = config.source.path.stat().st_mtime_ns
    fsync = os.fsync

    def check_write(fd):
        probes = list(config.destination.path.glob(".migraflow-write-test-*"))
        probe = next(path for path in probes if path.name != ".migraflow-write-test-existing")
        assert probe.read_bytes() == b"MigraFlow destination write test\n"
        fsync(fd)

    with patch("migraflow.preflight.os.fsync", side_effect=check_write):
        assert main(["preflight", "--config", str(file), "--test-destination-write", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["destination_write_test"] == "passed"
    assert result["transfer_ready"] is False
    assert snapshot(config.source.path) == before_source
    assert config.source.path.stat().st_mtime_ns == source_mtime
    assert snapshot(config.destination.path) == before_destination
    assert not config.state_dir.exists()
    assert main(["preflight", "--config", str(file)]) == 0
    output = capsys.readouterr().out
    assert "destination_write_permission" in output
    assert "not_requested" in output


@pytest.mark.parametrize("operation", ["create", "write", "flush", "sync", "interrupt"])
def test_destination_write_failure_cleans_up(config, operation):
    target = {"create": "tempfile.mkstemp", "write": "os.fdopen", "flush": "os.fdopen",
              "sync": "os.fsync", "interrupt": "os.fsync"}[operation]
    error = KeyboardInterrupt() if operation == "interrupt" else PermissionError("denied")
    if operation in ("write", "flush"):
        fdopen = os.fdopen

        def failing_file(fd, mode):
            file = fdopen(fd, mode)

            def fail(*args):
                raise error

            setattr(file, operation, fail)
            return file

        options = {"side_effect": failing_file}
    else:
        options = {"side_effect": error}
    with patch(f"migraflow.preflight.{target}", **options):
        with pytest.raises(KeyboardInterrupt if operation == "interrupt" else ConfigError):
            preflight(config, test_destination_write=True)
    assert not list(config.destination.path.iterdir())
    assert not list(config.source.path.iterdir())


def test_write_probe_cleanup_failure_reports_path_and_cli_error(config, tmp_path, capsys):
    file = tmp_path / "migration.yml"
    file.write_text(yaml.safe_dump(settings(config)), encoding="utf-8")
    with patch("migraflow.preflight.os.unlink", side_effect=PermissionError("delete denied")):
        assert main(["preflight", "--config", str(file), "--test-destination-write", "--json"]) == 2
    result = json.loads(capsys.readouterr().out)
    remaining = list(config.destination.path.iterdir())
    assert len(remaining) == 1
    assert str(remaining[0]) in result["error"]
    assert "cleanup failed" in result["error"]
    remaining[0].unlink()


@pytest.mark.parametrize("unsafe", ["overlap", "identity", "readonly", "missing-mount"])
def test_write_probe_blocked_before_creation(config, unsafe):
    identity = endpoint_identity

    def changed(endpoint):
        result = identity(endpoint)
        if endpoint == config.destination:
            if unsafe == "readonly":
                result["read_only_mount"] = True
            elif unsafe == "identity":
                result["inode"] += calls[0]
                calls[0] += 1
            elif unsafe == "missing-mount":
                raise ConfigError("Expected NAS mount is absent or different")
        return result

    calls = [0]
    if unsafe == "overlap":
        config = Config(config.source, config.source, config.state_dir)
    with patch("migraflow.preflight.endpoint_identity", side_effect=changed), patch("migraflow.preflight.tempfile.mkstemp") as create:
        with pytest.raises(ConfigError):
            preflight(config, test_destination_write=True)
    create.assert_not_called()


@pytest.mark.parametrize("command", ["plan", "status", "run", "verify"])
def test_write_probe_option_is_preflight_only(command):
    with pytest.raises(SystemExit) as exc:
        main([command, "--test-destination-write"])
    assert exc.value.code == 2


@pytest.mark.parametrize("placement", ["same", "nested", "state-source", "state-destination", "state-parent"])
def test_overlapping_roots_rejected(config, placement):
    if placement == "same":
        config = Config(config.source, config.source, config.state_dir)
    elif placement == "nested":
        nested = config.source.path / "nested"
        nested.mkdir()
        config = Config(config.source, Endpoint("local", nested), config.state_dir)
    else:
        state = config.source.path / "state" if placement == "state-source" else config.destination.path / "state" if placement == "state-destination" else config.source.path.parent
        config = Config(config.source, config.destination, state)
    with pytest.raises(ConfigError):
        create_plan(config)


def test_plan_actions_preserves_both_trees(config):
    src, dst = config.source.path, config.destination.path
    (src / "한글 space.txt").write_text("new", encoding="utf-8")
    (src / "empty.txt").touch()
    (src / "empty-dir").mkdir()
    (src / "replace.txt").write_text("new longer content")
    (dst / "replace.txt").write_text("old")
    (src / "equal.txt").write_text("same")
    (dst / "equal.txt").write_text("same")
    info = (src / "equal.txt").stat()
    os.utime(dst / "equal.txt", ns=(info.st_atime_ns, info.st_mtime_ns))
    (dst / "destination-only.txt").write_text("keep")
    before_source, before_destination = snapshot(src), snapshot(dst)
    result = create_plan(config)
    assert result["status"] == "PLANNED"
    assert result["counts"] == {"COPY_NEW": 2, "COPY_REPLACE": 1, "MKDIR": 1, "QUICK_MATCH_UNVERIFIED": 1}
    assert result["files"] == 4
    assert result["transfer_ready"] is False
    assert snapshot(src) == before_source
    assert snapshot(dst) == before_destination
    with database(config.state_dir, readonly=True) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert summary(db, result["id"])["counts"] == result["counts"]


def test_file_directory_conflict_blocks(config):
    (config.source.path / "entry").write_text("file")
    (config.destination.path / "entry").mkdir()
    result = create_plan(config)
    assert result["status"] == "BLOCKED"
    assert result["counts"]["BLOCKED"] == 1


def test_destination_symlink_blocks_without_following(config, tmp_path):
    (config.source.path / "link").write_text("source")
    outside = tmp_path / "outside"
    outside.write_text("keep")
    try:
        (config.destination.path / "link").symlink_to(outside)
    except OSError:
        pytest.skip("OS permission does not allow symlink creation")
    result = create_plan(config)
    assert result["status"] == "BLOCKED"
    assert outside.read_text() == "keep"


def test_failed_scan_never_becomes_planned(config):
    with patch("migraflow.planner.os.scandir", side_effect=PermissionError("read denied")):
        result = create_plan(config)
    assert result["status"] == "FAILED"
    assert "read denied" in result["error"]


def test_mount_missing_or_different_rejected(config):
    mounted = Endpoint("mounted", config.source.path, "nas:/data")
    local_mount = {"fs": "ext4", "source": "/dev/sda", "options": [], "point": "/", "root": "/"}
    with patch("migraflow.preflight.sys.platform", "linux"), patch("migraflow.preflight.filesystem", return_value=local_mount):
        with pytest.raises(ConfigError, match="absent or different"):
            endpoint_identity(mounted)
        wrong_mount = dict(local_mount, fs="nfs4", source="other-nas:/data")
        with patch("migraflow.preflight.filesystem", return_value=wrong_mount):
            with pytest.raises(ConfigError):
                endpoint_identity(mounted)


def test_local_cannot_bypass_network_mount(config):
    mount = {"fs": "nfs4", "source": "nas:/data", "options": ["ro"], "point": "/", "root": "/"}
    with patch("migraflow.preflight.filesystem", return_value=mount):
        with pytest.raises(ConfigError, match="requires mounted"):
            endpoint_identity(config.source)


def test_endpoint_identity_change_marks_failed(config):
    identity = endpoint_identity(config.source)
    with patch("migraflow.planner.endpoint_identity", return_value=dict(identity, inode=-1)):
        result = create_plan(config)
    assert result["status"] == "FAILED"


def test_writer_lock_excludes_second_planner_and_releases(config):
    with writer_lock(config.state_dir):
        with pytest.raises(ConfigError, match="Another planner"):
            with writer_lock(config.state_dir):
                pass
    with writer_lock(config.state_dir):
        pass


def test_interrupted_plan_is_not_success(config):
    with patch("migraflow.planner.os.scandir", side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            create_plan(config)
    with database(config.state_dir, readonly=True) as db:
        assert summary(db)["status"] == "INTERRUPTED"


def test_cli_default_yaml_and_status(config, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    file = tmp_path / "migration.local.yml"
    file.write_text(yaml.safe_dump(settings(config)), encoding="utf-8")
    assert main(["plan", "--json"]) == 0
    planned = json.loads(capsys.readouterr().out)
    assert main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == planned["id"]
    assert main(["status", "--job", "missing", "--json"]) == 2
    assert "error" in json.loads(capsys.readouterr().out)


def test_cli_only_configuration_and_failed_status(config, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["plan", "--source", str(config.source.path), "--destination", str(config.destination.path), "--json"]) == 0
    capsys.readouterr()
    assert main(["status", "--state-dir", str(config.state_dir), "--json"]) == 0
    capsys.readouterr()
    with patch("migraflow.planner.os.scandir", side_effect=PermissionError("denied")):
        assert main(["plan", "--source", str(config.source.path), "--destination", str(config.destination.path), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "FAILED"


def test_mount_escape_decoding():
    assert _unescape(r"/mnt/my\040nas\134share") == "/mnt/my nas\\share"


def test_status_missing_database_does_not_create_one(tmp_path):
    with pytest.raises(ConfigError):
        with database(tmp_path, readonly=True):
            pass
    assert not list(tmp_path.iterdir())


def test_multiple_batches_and_empty_directories(config):
    source = config.source.path
    (source / "nested").mkdir()
    (source / "nested" / "empty").mkdir()
    for index in range(1001):
        (source / f"file-{index}").touch()
    result = create_plan(config)
    assert result["status"] == "PLANNED"
    assert result["files"] == 1001
    assert result["counts"]["MKDIR"] == 2
    with database(config.state_dir, readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM directories WHERE done=0").fetchone()[0] == 0


def test_unicode_normalization_collision_blocks_both_entries(config):
    source = config.source.path
    (source / "é.txt").write_text("first")
    (source / "e\u0301.txt").write_text("second")
    if len(list(source.iterdir())) != 2:
        pytest.skip("Filesystem normalizes Unicode names")
    result = create_plan(config)
    assert result["status"] == "BLOCKED"
    assert result["counts"]["BLOCKED"] == 2


def test_symlink_detection_blocks_entry_without_os_symlink_permission(config):
    (config.source.path / "file").touch()

    def reject_source(path):
        if path == config.source.path:
            raise ConfigError("Symlink path")

    with patch("migraflow.planner._no_links", side_effect=reject_source):
        result = create_plan(config)
    assert result["status"] == "FAILED"


def test_state_db_hardlink_cannot_modify_source(config):
    config.state_dir.mkdir()
    original = config.source.path / "important"
    original.write_bytes(b"original")
    try:
        os.link(original, config.state_dir / "migraflow.sqlite3")
    except PermissionError:
        pytest.skip("OS permission does not allow hardlink creation")
    with pytest.raises(ConfigError, match="hardlinks"):
        create_plan(config)
    assert original.read_bytes() == b"original"


@pytest.mark.parametrize("source_type,destination_type", [
    ("local", "local"), ("local", "mounted"), ("mounted", "local"), ("mounted", "mounted")])
def test_endpoint_combinations_with_simulated_linux_mounts(config, source_type, destination_type):
    config = Config(Endpoint(source_type, config.source.path, "src-nas:/data" if source_type == "mounted" else None),
                    Endpoint(destination_type, config.destination.path, "dst-nas:/data" if destination_type == "mounted" else None),
                    config.state_dir)

    def mount(path):
        for label in ("source", "destination"):
            endpoint = getattr(config, label)
            if path == endpoint.path or endpoint.path in path.parents:
                return {"fs": "nfs4" if endpoint.type == "mounted" else "ext4",
                        "source": endpoint.mount_source or "/dev/test", "options": ["ro"] if label == "source" else ["rw"],
                        "point": str(endpoint.path), "root": "/" + label}
        return {"fs": "ext4", "source": "/dev/test", "options": ["rw"],
                "point": str(config.state_dir.parent), "root": "/state-volume"}

    with patch("migraflow.preflight.sys.platform", "linux"), patch("migraflow.preflight.filesystem", side_effect=mount), patch("migraflow.planner.filesystem", side_effect=mount):
        result = create_plan(config)
    assert result["status"] == "PLANNED"


def test_status_when_endpoints_disappear(config, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "migration.local.yml").write_text(yaml.safe_dump(settings(config)), encoding="utf-8")
    assert main(["plan", "--json"]) == 0
    capsys.readouterr()
    config.source.path.rmdir()
    assert main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "PLANNED"


def test_hash_algorithm_defaults_to_sha256(config, tmp_path):
    file = tmp_path / "migration.yml"
    file.write_text(yaml.safe_dump(settings(config)), encoding="utf-8")
    loaded = load_config(file)
    assert loaded.hash_algorithm == "sha256"
    assert preflight(loaded)["hash_algorithm"] == "sha256"


@pytest.mark.parametrize("algorithm", ["sha256", "md5"])
def test_selected_hash_algorithm_persisted_and_displayed(config, tmp_path, capsys, algorithm):
    data = settings(config)
    data["verification"] = {"algorithm": algorithm}
    file = tmp_path / "migration.yml"
    file.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert main(["plan", "--config", str(file), "--json"]) == 0
    planned = json.loads(capsys.readouterr().out)
    assert planned["config"]["hash_algorithm"] == algorithm
    assert planned["preflight"]["hash_algorithm"] == algorithm
    assert planned["hash_algorithm"] == algorithm
    assert planned["transfer_ready"] is False
    data["verification"]["algorithm"] = "sha256" if algorithm == "md5" else "md5"
    file.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert main(["status", "--config", str(file), "--job", planned["id"], "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["hash_algorithm"] == algorithm
    assert main(["status", "--state-dir", str(config.state_dir), "--job", planned["id"]]) == 0
    assert algorithm in capsys.readouterr().out


@pytest.mark.parametrize("verification", [None, [], "md5", {"algorithm": "sha1"},
                                        {"algorithm": None}, {"algorithm": 256},
                                        {"algorithm": False}, {"algoritm": "md5"}])
def test_invalid_hash_settings_rejected_before_state_writes(config, tmp_path, verification):
    data = settings(config)
    data["verification"] = verification
    file = tmp_path / "migration.yml"
    file.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(file)
    assert not config.state_dir.exists()


def test_hash_policy_changes_config_fingerprint(config, tmp_path):
    sha_plan = create_plan(config)
    md5_plan = create_plan(Config(config.source, config.destination, config.state_dir, "md5"))
    assert sha_plan["fingerprint"] != md5_plan["fingerprint"]
    assert len(sha_plan["fingerprint"]) == len(md5_plan["fingerprint"]) == 64
    assert any("MD5 selected" in warning for warning in md5_plan["preflight"]["warnings"])
    assert not any("MD5 selected" in warning for warning in sha_plan["preflight"]["warnings"])


def test_old_plan_without_algorithm_reports_original_sha256_default(config):
    planned = create_plan(config)
    with database(config.state_dir) as db:
        data = planned["config"].copy()
        data.pop("hash_algorithm")
        db.execute("UPDATE plans SET config=? WHERE id=?", (json.dumps(data), planned["id"]))
        db.commit()
    with database(config.state_dir, readonly=True) as db:
        assert summary(db, planned["id"])["hash_algorithm"] == "sha256"
