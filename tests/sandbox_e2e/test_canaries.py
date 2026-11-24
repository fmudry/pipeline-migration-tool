"""End-to-end tests of the container sandbox with real crun, crane and runner image.

Each script in scripts/ is run on pipeline.yaml by ``pmt run-migration`` and directly on the host.
The outcome must match the script name prefix:

- ``pass-``: succeeds in the sandbox (and on the host) and changes the pipeline file.
- ``sandbox-fail-``: succeeds on the host, fails in the sandbox. These scripts break a rule of the
  sandbox contract (see pipeline_migration.actions.migrate.sandbox).
- ``fail-``: fails in both.

The tests run only when PMT_SANDBOX_E2E=1, as they need crun and crane in PATH, unprivileged user
namespaces and network access to the runner image (PMT_RUNNER_IMAGE, or the image of this PMT
version), quay.io and the host tools the scripts use (yq, kubectl). For example::

    PMT_SANDBOX_E2E=1 PMT_RUNNER_IMAGE=quay.io/... tox -e py312 -- tests/sandbox_e2e
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline_migration.actions import run_migration
from pipeline_migration.api_version import MIGRATION_SCRIPT_ENV

pytestmark = pytest.mark.skipif(
    os.environ.get("PMT_SANDBOX_E2E") != "1",
    reason="set PMT_SANDBOX_E2E=1 to run sandbox e2e tests",
)

HERE = Path(__file__).parent
SCRIPTS = sorted((HERE / "scripts").glob("*.sh"))
# Stands for the tokens in the environment of Renovate. Must never reach the sandbox.
SECRET = "canary-secret-value"
BIN_DIR = Path(sys.executable).parent


def run_in_sandbox(script: Path, pipeline_file: Path, *options: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [BIN_DIR / "pmt", "run-migration", *options, script, pipeline_file],
        env={**os.environ, "CANARY_SECRET": SECRET},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    if proc.returncode == run_migration.EXIT_SANDBOX_ERROR:
        pytest.fail(f"The sandbox cannot be used on this machine:\n{proc.stderr}", pytrace=False)
    return proc


def run_on_host(script: Path, pipeline_file: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", script, pipeline_file.name],
        cwd=pipeline_file.parent,
        env={
            **os.environ,
            **MIGRATION_SCRIPT_ENV,
            "CANARY_SECRET": SECRET,
            # The pmt of this test environment, for scripts that call pmt modify
            "PATH": f"{BIN_DIR}:{os.environ['PATH']}",
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )


def copy_pipeline(directory: Path) -> Path:
    directory.mkdir()
    return Path(shutil.copy(HERE / "pipeline.yaml", directory / "pipeline.yaml"))


@pytest.mark.parametrize("script", SCRIPTS, ids=[script.stem for script in SCRIPTS])
def test_canary(script: Path, tmp_path: Path) -> None:
    sandbox_pipeline = copy_pipeline(tmp_path / "sandbox")
    host_pipeline = copy_pipeline(tmp_path / "host")

    sandbox = run_in_sandbox(script, sandbox_pipeline)
    host = run_on_host(script, host_pipeline)
    report = (
        f"sandbox exit status {sandbox.returncode}, output:\n{sandbox.stdout}{sandbox.stderr}\n"
        f"host exit status {host.returncode}, output:\n{host.stdout}{host.stderr}"
    )
    original = (HERE / "pipeline.yaml").read_text()
    result = sandbox_pipeline.read_text()

    if script.name.startswith("pass-"):
        assert sandbox.returncode == 0, report
        assert result != original, "the migration did not change the pipeline file"
        # The sandbox does not change what a compatible script produces
        assert host.returncode == 0, report
        assert result == host_pipeline.read_text()
    elif script.name.startswith("sandbox-fail-"):
        assert host.returncode == 0, f"the script must work on the host\n{report}"
        assert sandbox.returncode != 0, report
        assert result == original, "a failed migration must leave the pipeline file untouched"
    else:
        assert script.name.startswith("fail-"), f"unknown script name prefix: {script.name}"
        assert host.returncode != 0, report
        assert sandbox.returncode == host.returncode, report
        assert result == original

    assert SECRET not in result + sandbox.stdout


def test_script_cannot_fake_a_sandbox_error(tmp_path: Path) -> None:
    script = tmp_path / "exit-125.sh"
    script.write_text(f"exit {run_migration.EXIT_SANDBOX_ERROR}\n")
    proc = run_in_sandbox(script, copy_pipeline(tmp_path / "work"))
    assert proc.returncode == run_migration.EXIT_SCRIPT_FAILED


def test_timeout(tmp_path: Path) -> None:
    script = tmp_path / "sleep.sh"
    script.write_text("sleep 60\n")
    proc = run_in_sandbox(script, copy_pipeline(tmp_path / "work"), "--timeout", "2")
    assert proc.returncode == run_migration.EXIT_TIMEOUT, proc.stderr
    assert "did not finish within 2 seconds" in proc.stderr
