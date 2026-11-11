import json
import logging
import os
import stat
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

from pipeline_migration import __version__
from pipeline_migration.actions.migrate import sandbox
from pipeline_migration.actions.migrate.sandbox import SandboxError
from tests.utils import file_entry, write_oci_layout

RUNNER_IMAGE = "quay.io/konflux-ci/pipeline-migration-runner:v1.0"


@pytest.fixture(autouse=True)
def temp_dir(tmp_path, monkeypatch) -> Path:
    """Private temporary directory, also used for the runner cache."""
    path = tmp_path / "tmp"
    path.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(path))
    monkeypatch.setenv(sandbox.RUNNER_IMAGE_ENV, RUNNER_IMAGE)
    return path


@pytest.fixture
def tools_installed(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda tool: f"/usr/bin/{tool}")


class TestEnabled:

    def test_off(self, monkeypatch):
        monkeypatch.setenv(sandbox.SANDBOX_MODE_ENV, "off")
        assert not sandbox.enabled()

    def test_invalid_mode(self, monkeypatch):
        monkeypatch.setenv(sandbox.SANDBOX_MODE_ENV, "yes")
        with pytest.raises(SandboxError, match="Invalid PMT_SANDBOX"):
            sandbox.enabled()

    def test_auto_warns_when_tools_are_missing(self, monkeypatch, caplog):
        monkeypatch.setenv(sandbox.SANDBOX_MODE_ENV, "auto")
        monkeypatch.setattr("shutil.which", lambda tool: None if tool == "crun" else "/bin/x")
        with caplog.at_level(logging.WARNING):
            assert not sandbox.enabled()
        assert "crun not found in PATH" in caplog.text
        assert "run directly on the host" in caplog.text

    def test_on_requires_tools(self, monkeypatch):
        monkeypatch.setenv(sandbox.SANDBOX_MODE_ENV, "on")
        monkeypatch.setattr("shutil.which", lambda tool: None)
        with pytest.raises(SandboxError, match="crun and skopeo not found"):
            sandbox.enabled()

    @pytest.mark.parametrize("mode", ["auto", "on"])
    def test_prepares_rootfs_once(self, monkeypatch, tools_installed, mode):
        monkeypatch.setenv(sandbox.SANDBOX_MODE_ENV, mode)
        prepared = []
        monkeypatch.setattr(sandbox, "_ensure_rootfs", prepared.append)
        assert sandbox.enabled()
        assert sandbox.enabled()
        assert prepared == [RUNNER_IMAGE]

    def test_preparation_failure_does_not_fall_back(self, monkeypatch, tools_installed):
        monkeypatch.setenv(sandbox.SANDBOX_MODE_ENV, "auto")

        def fail(image):
            raise SandboxError("pull failed")

        monkeypatch.setattr(sandbox, "_ensure_rootfs", fail)
        with pytest.raises(SandboxError, match="pull failed"):
            sandbox.enabled()


class TestRunnerImage:

    def test_default_follows_pmt_version(self, monkeypatch):
        monkeypatch.delenv(sandbox.RUNNER_IMAGE_ENV)
        assert sandbox._get_runner_image().endswith(f":v{__version__}")

    def test_override(self):
        assert sandbox._get_runner_image() == RUNNER_IMAGE

    @pytest.mark.parametrize(
        "image,source",
        [
            ("quay.io/org/runner:v1", "docker://quay.io/org/runner:v1"),
            ("localhost:5000/runner:v1", "docker://localhost:5000/runner:v1"),
            ("oci:/path/layout:tag", "oci:/path/layout:tag"),
            ("docker://quay.io/org/runner:v1", "docker://quay.io/org/runner:v1"),
        ],
    )
    def test_skopeo_source(self, image, source):
        assert sandbox._skopeo_source(image) == source


class TestCache:

    def test_cache_root_is_private(self, temp_dir):
        root = Path(sandbox._cache_root())
        assert root.parent == temp_dir
        assert stat.S_IMODE(root.stat().st_mode) == 0o700

    def test_cache_root_accessible_by_others_is_refused(self, temp_dir):
        root = temp_dir / f"pmt-runner-{os.getuid()}"
        root.mkdir(mode=0o777)
        root.chmod(0o777)
        with pytest.raises(SandboxError, match="Refusing to use the runner cache"):
            sandbox._cache_root()

    def test_cache_root_symlink_is_refused(self, temp_dir, tmp_path):
        (tmp_path / "elsewhere").mkdir(mode=0o700)
        (temp_dir / f"pmt-runner-{os.getuid()}").symlink_to(tmp_path / "elsewhere")
        with pytest.raises(SandboxError, match="Refusing to use the runner cache"):
            sandbox._cache_root()

    def test_cache_dir_is_keyed_by_image(self):
        assert sandbox._image_cache_dir("a:v1") != sandbox._image_cache_dir("a:v2")


class FakeSkopeo:
    """Stands in for subprocess.run: "pulls" an image by writing an OCI layout."""

    def __init__(self, returncode: int = 0, stderr: bytes = b""):
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        assert cmd[:2] == ["skopeo", "copy"]
        assert kwargs["timeout"] > 0
        self.calls.append(cmd)
        if self.returncode:
            raise subprocess.CalledProcessError(self.returncode, cmd, b"", self.stderr)
        oci_dir = cmd[-1].removeprefix("oci:").removesuffix(":latest")
        write_oci_layout(Path(oci_dir), [[file_entry("usr/bin/yq", b"yq")]])
        return subprocess.CompletedProcess(cmd, 0, b"", b"")


class TestEnsureRootfs:

    def test_pull_and_unpack(self, monkeypatch):
        skopeo = FakeSkopeo()
        monkeypatch.setattr(sandbox.sp, "run", skopeo)
        rootfs = Path(sandbox._ensure_rootfs(RUNNER_IMAGE))

        assert (rootfs / "usr/bin/yq").read_bytes() == b"yq"
        assert sorted(os.listdir(rootfs.parent)) == [".lock", ".ready", "rootfs"]
        cmd = skopeo.calls[0]
        assert f"docker://{RUNNER_IMAGE}" in cmd
        assert cmd[cmd.index("--dest-compress-format") + 1] == "gzip"

    def test_cached_rootfs_is_reused(self, monkeypatch):
        skopeo = FakeSkopeo()
        monkeypatch.setattr(sandbox.sp, "run", skopeo)
        sandbox._ensure_rootfs(RUNNER_IMAGE)
        sandbox._ensure_rootfs(RUNNER_IMAGE)
        assert len(skopeo.calls) == 1

    def test_deleted_rootfs_is_rebuilt(self, monkeypatch):
        skopeo = FakeSkopeo()
        monkeypatch.setattr(sandbox.sp, "run", skopeo)
        rootfs = Path(sandbox._ensure_rootfs(RUNNER_IMAGE))
        sandbox.shutil.rmtree(rootfs)
        sandbox._ensure_rootfs(RUNNER_IMAGE)
        assert len(skopeo.calls) == 2
        assert (rootfs / "usr/bin/yq").exists()

    def test_pull_failure_reports_skopeo_error(self, monkeypatch):
        monkeypatch.setattr(sandbox.sp, "run", FakeSkopeo(1, b"manifest unknown"))
        with pytest.raises(SandboxError, match="Cannot pull runner image .+: manifest unknown"):
            sandbox._ensure_rootfs(RUNNER_IMAGE)
        assert not sandbox._is_ready(sandbox._image_cache_dir(RUNNER_IMAGE))

    def test_pull_timeout(self, monkeypatch):
        def hang(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

        monkeypatch.setattr(sandbox.sp, "run", hang)
        with pytest.raises(SandboxError, match="did not finish"):
            sandbox._ensure_rootfs(RUNNER_IMAGE)


Script = Callable[[Path], int]


class FakeCrun:
    """Stands in for subprocess.run: runs ``script`` on the pipeline file in the work directory."""

    def __init__(self, script: Script):
        self.script = script
        self.config: dict = {}
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        assert cmd[0] == "crun"
        self.calls.append(cmd)
        if "run" not in cmd:
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        bundle_dir = cmd[cmd.index("--bundle") + 1]
        with open(os.path.join(bundle_dir, "config.json")) as f:
            self.config = json.load(f)
        work_dir = self.mount_source(sandbox._WORK_DIR)
        pipeline_name = os.path.basename(self.config["process"]["args"][2])
        returncode = self.script(Path(work_dir) / pipeline_name)
        return subprocess.CompletedProcess(cmd, returncode, b"script output")

    def mount_source(self, destination: str) -> str:
        return next(m["source"] for m in self.config["mounts"] if m["destination"] == destination)


class TestRunMigration:

    @pytest.fixture
    def pipeline_file(self, tmp_path) -> Path:
        path = tmp_path / "repo" / "component-pull-request.yaml"
        path.parent.mkdir()
        path.write_text("original")
        return path

    @pytest.fixture
    def run(self, tmp_path, monkeypatch, pipeline_file):
        rootfs = tmp_path / "rootfs"
        rootfs.mkdir()
        monkeypatch.setattr(sandbox, "_ensure_rootfs", lambda image: str(rootfs))
        script_file = tmp_path / "migration.sh"
        script_file.write_text("echo migrate")

        def _run(script: Script, crun=None) -> tuple[subprocess.CompletedProcess, FakeCrun]:
            fake_crun = FakeCrun(script)
            monkeypatch.setattr(sandbox.sp, "run", crun or fake_crun)
            result = sandbox.run_migration(str(script_file), str(pipeline_file), timeout=10)
            return result, fake_crun

        return _run

    def test_result_is_copied_back(self, run, pipeline_file, temp_dir):
        inode = pipeline_file.stat().st_ino

        def migrate(path: Path) -> int:
            path.unlink()  # Recreating the file must work
            path.write_text("migrated")
            return 0

        result, crun = run(migrate)
        assert result.returncode == 0
        assert pipeline_file.read_text() == "migrated"
        assert pipeline_file.stat().st_ino == inode
        assert os.listdir(temp_dir) == []  # Temporary directories are removed

    def test_script_and_original_file_name_are_passed(self, run):
        _, crun = run(lambda path: 0)
        assert crun.config["process"]["args"] == [
            "bash",
            f"{sandbox._SCRIPT_DIR}/{sandbox._SCRIPT_NAME}",
            f"{sandbox._WORK_DIR}/component-pull-request.yaml",
        ]
        assert crun.config["process"]["cwd"] == sandbox._WORK_DIR

    def test_failed_migration_leaves_file_untouched(self, run, pipeline_file):
        def fail(path: Path) -> int:
            path.write_text("half migrated")
            return 1

        result, _ = run(fail)
        assert result.returncode == 1
        assert pipeline_file.read_text() == "original"

    def test_symlink_to_host_file_is_refused(self, run, pipeline_file, tmp_path):
        secret = tmp_path / "secret"
        secret.write_text("RENOVATE_TOKEN=abc")

        def leak(path: Path) -> int:
            path.unlink()
            path.symlink_to(secret)
            return 0

        with pytest.raises(RuntimeError, match="by a symlink"):
            run(leak)
        assert pipeline_file.read_text() == "original"

    @pytest.mark.parametrize("make", [os.mkfifo, os.mkdir], ids=["fifo", "directory"])
    def test_non_regular_file_is_refused(self, run, pipeline_file, make):
        def replace(path: Path) -> int:
            path.unlink()
            make(path)
            return 0

        with pytest.raises(RuntimeError, match="non-regular file"):
            run(replace)
        assert pipeline_file.read_text() == "original"

    def test_removed_file_is_reported(self, run, pipeline_file):
        def remove(path: Path) -> int:
            path.unlink()
            return 0

        with pytest.raises(RuntimeError, match="removed the pipeline file"):
            run(remove)
        assert pipeline_file.read_text() == "original"

    def test_oversized_file_is_refused(self, run, pipeline_file, monkeypatch):
        monkeypatch.setattr(sandbox, "_MAX_PIPELINE_SIZE", 10)

        def grow(path: Path) -> int:
            path.write_text("x" * 11)
            return 0

        with pytest.raises(RuntimeError, match="exceeds 10 bytes"):
            run(grow)
        assert pipeline_file.read_text() == "original"

    def test_timeout_deletes_container(self, run, monkeypatch):
        crun_calls: list[list[str]] = []

        def crun(cmd, **kwargs):
            crun_calls.append(cmd)
            if "run" in cmd:
                raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
            return subprocess.CompletedProcess(cmd, 0, b"", b"")

        with pytest.raises(TimeoutError, match="did not finish within 10 seconds"):
            run(lambda path: 0, crun=crun)
        container_id = crun_calls[0][-1]
        assert crun_calls[1][-3:] == ["delete", "--force", container_id]


class TestConfig:

    def test_config(self, tmp_path):
        config = sandbox._build_config("/rootfs", "/script", "/work", None, "pipeline.yaml")
        assert config["ociVersion"] == "1.0.0"
        assert config["root"] == {"path": "/rootfs", "readonly": True}
        assert config["process"]["noNewPrivileges"]
        assert all(caps == [] for caps in config["process"]["capabilities"].values())
        assert [ns["type"] for ns in config["linux"]["namespaces"]] == [
            "user",
            "mount",
            "pid",
            "ipc",
            "uts",
        ]
        binds = {m["destination"]: m for m in config["mounts"] if m["type"] == "bind"}
        assert binds[sandbox._SCRIPT_DIR]["options"] == ["rbind", "ro"]
        assert binds[sandbox._WORK_DIR]["options"] == ["rbind", "rw"]
        assert "/etc/resolv.conf" not in binds
        assert "/proc" not in {m["destination"] for m in config["mounts"]}

    def test_resolv_conf_mount(self):
        config = sandbox._build_config("/rootfs", "/script", "/work", "/b/resolv.conf", "p.yaml")
        resolv = [m for m in config["mounts"] if m["destination"] == "/etc/resolv.conf"]
        assert resolv == [
            {
                "destination": "/etc/resolv.conf",
                "type": "bind",
                "source": "/b/resolv.conf",
                "options": ["rbind", "ro"],
            }
        ]
