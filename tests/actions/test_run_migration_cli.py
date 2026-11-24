import logging
import subprocess
from pathlib import Path

import pytest

from pipeline_migration.actions import run_migration
from pipeline_migration.actions.migrate import sandbox
from pipeline_migration.actions.migrate.constants import MIGRATION_TIMEOUT_SECONDS
from pipeline_migration.cli import entry_point


@pytest.fixture
def files(tmp_path) -> tuple[Path, Path]:
    migration_file = tmp_path / "0.2.sh"
    migration_file.write_text("pmt modify -f $1 task init add-param x y\n")
    pipeline_file = tmp_path / "pipeline.yaml"
    pipeline_file.write_text("kind: Pipeline\n")
    return migration_file, pipeline_file


@pytest.fixture
def fake_sandbox(monkeypatch):
    """Replace the sandbox. Tests set ``result`` to the outcome of the migration script."""

    class FakeSandbox:
        prepare_error: Exception | None = None
        result: subprocess.CompletedProcess | Exception = subprocess.CompletedProcess(
            [], 0, b"migrated\n"
        )
        calls: list[tuple[str, str, float]] = []

        def prepare(self):
            if self.prepare_error:
                raise self.prepare_error

        def run_migration(self, migration_file, pipeline_file, timeout):
            self.calls.append((migration_file, pipeline_file, timeout))
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    fake = FakeSandbox()
    monkeypatch.setattr(sandbox, "prepare", fake.prepare)
    monkeypatch.setattr(sandbox, "run_migration", fake.run_migration)
    return fake


def run_cli(monkeypatch, *args) -> int | None:
    monkeypatch.setattr("sys.argv", ["pmt", "run-migration", *map(str, args)])
    return entry_point()


def test_success(monkeypatch, capsys, files, fake_sandbox):
    migration_file, pipeline_file = files
    assert run_cli(monkeypatch, migration_file, pipeline_file) == 0
    assert fake_sandbox.calls == [
        (str(migration_file), str(pipeline_file), MIGRATION_TIMEOUT_SECONDS)
    ]
    assert capsys.readouterr().out == "migrated\n"


def test_timeout_option(monkeypatch, files, fake_sandbox):
    assert run_cli(monkeypatch, *files, "--timeout", "30") == 0
    assert fake_sandbox.calls[0][2] == 30


def test_script_exit_status_is_returned(monkeypatch, capsys, caplog, files, fake_sandbox):
    fake_sandbox.result = subprocess.CompletedProcess([], 3, b"cat: /dev/stdin: No such file\n")
    with caplog.at_level(logging.ERROR):
        assert run_cli(monkeypatch, *files) == 3
    assert capsys.readouterr().out == "cat: /dev/stdin: No such file\n"
    assert "Migration script exited with code 3" in caplog.text


@pytest.mark.parametrize("status", [run_migration.EXIT_TIMEOUT, run_migration.EXIT_SANDBOX_ERROR])
def test_script_cannot_fake_a_reserved_status(monkeypatch, caplog, files, fake_sandbox, status):
    fake_sandbox.result = subprocess.CompletedProcess([], status, b"")
    with caplog.at_level(logging.ERROR):
        assert run_cli(monkeypatch, *files) == run_migration.EXIT_SCRIPT_FAILED
    assert f"exited with code {status}, reported as 1" in caplog.text


def test_sandbox_error(monkeypatch, caplog, files, fake_sandbox):
    fake_sandbox.prepare_error = sandbox.SandboxError("crun not found in PATH")
    with caplog.at_level(logging.ERROR):
        assert run_cli(monkeypatch, *files) == run_migration.EXIT_SANDBOX_ERROR
    assert "cannot be used on this machine: crun not found in PATH" in caplog.text
    assert fake_sandbox.calls == []


def test_timeout(monkeypatch, files, fake_sandbox):
    fake_sandbox.result = TimeoutError("Migration script did not finish within 600 seconds")
    assert run_cli(monkeypatch, *files) == run_migration.EXIT_TIMEOUT


def test_unusable_result(monkeypatch, caplog, files, fake_sandbox):
    fake_sandbox.result = RuntimeError("Migration script replaced the pipeline file by a symlink")
    with caplog.at_level(logging.ERROR):
        assert run_cli(monkeypatch, *files) == run_migration.EXIT_SCRIPT_FAILED
    assert "by a symlink" in caplog.text


@pytest.mark.parametrize("missing", ["migration", "pipeline"])
def test_missing_file(monkeypatch, capsys, files, fake_sandbox, tmp_path, missing):
    migration_file, pipeline_file = files
    if missing == "migration":
        migration_file = tmp_path / "missing.sh"
    else:
        pipeline_file = tmp_path / "missing.yaml"
    with pytest.raises(SystemExit) as exc_info:
        run_cli(monkeypatch, migration_file, pipeline_file)
    assert exc_info.value.code == 2
    assert "is not an existing file" in capsys.readouterr().err
    assert fake_sandbox.calls == []
