"""Container sandbox for running migration scripts via crun.

When PMT_RUNNER_IMAGE is set (or crun/skopeo/umoci are available and
PMT_SANDBOX is enabled), migration scripts execute inside an isolated OCI
container built from a dedicated runner image. The container has its own
filesystem (bash, pmt, yq, jq) and no access to host secrets. Network is
shared so scripts can reach OCI registries.

The runner image is pulled once per process lifetime and cached under /tmp.
"""

import json
import logging
import os
import shutil
import subprocess as sp
import tempfile
import time
import uuid

from pipeline_migration import __version__

logger = logging.getLogger("migrate.sandbox")

_CACHE_DIR = os.path.join(tempfile.gettempdir(), "pmt-runner")
_READY_MARKER = ".ready"
_DEFAULT_RUNNER_REPO = "quay.io/konflux-ci/pipeline-migration-runner"


def _get_runner_image() -> str | None:
    """Determine the runner image reference."""
    override = os.environ.get("PMT_RUNNER_IMAGE")
    if override:
        return override
    return f"{_DEFAULT_RUNNER_REPO}:v{__version__}"


def is_available() -> bool:
    """Check whether containerized execution is available."""
    for tool in ("crun", "skopeo", "umoci"):
        if not shutil.which(tool):
            logger.debug("%s not found in PATH", tool)
            return False
    image = _get_runner_image()
    logger.info("Container sandbox enabled (image: %s)", image)
    return True


def run_migration(
    migration_file: str,
    pipeline_file: str,
) -> "sp.CompletedProcess[bytes]":
    """Run a migration script inside an isolated crun container."""
    rootfs = _ensure_rootfs()
    container_id = f"pmt-{uuid.uuid4().hex[:12]}"
    bundle_dir = tempfile.mkdtemp(prefix="pmt-bundle-")
    state_dir = tempfile.mkdtemp(prefix="pmt-crun-")

    try:
        config = _build_config(
            rootfs=rootfs,
            migration_file=os.path.abspath(migration_file),
            pipeline_file=os.path.abspath(pipeline_file),
        )
        with open(os.path.join(bundle_dir, "config.json"), "w") as f:
            json.dump(config, f)

        logger.debug("Running migration in container %s", container_id)
        t0 = time.monotonic()
        result = sp.run(
            ["crun", "--root", state_dir, "run", "--bundle", bundle_dir, container_id],
            stderr=sp.STDOUT,
            stdout=sp.PIPE,
        )
        run_ms = (time.monotonic() - t0) * 1000
        logger.info(
            "Container %s finished in %.0fms (rc=%d)", container_id, run_ms, result.returncode
        )
        return result
    finally:
        try:
            sp.run(
                ["crun", "--root", state_dir, "delete", "--force", container_id],
                capture_output=True,
            )
        except Exception:
            pass
        shutil.rmtree(bundle_dir, ignore_errors=True)
        shutil.rmtree(state_dir, ignore_errors=True)


def _ensure_rootfs() -> str:
    """Pull and unpack the runner image if not already cached."""
    rootfs = os.path.join(_CACHE_DIR, "rootfs")
    marker = os.path.join(_CACHE_DIR, _READY_MARKER)

    if os.path.exists(marker):
        logger.debug("Runner rootfs cached at %s", rootfs)
        return rootfs

    image = _get_runner_image()
    oci_dir = os.path.join(_CACHE_DIR, "oci")
    bundle_dir = os.path.join(_CACHE_DIR, "bundle")
    os.makedirs(_CACHE_DIR, exist_ok=True)

    t0 = time.monotonic()
    logger.info("Pulling runner image %s", image)
    sp.run(
        [
            "skopeo",
            "copy",
            "--remove-signatures",
            f"docker://{image}",
            f"oci:{oci_dir}:latest",
        ],
        check=True,
        capture_output=True,
    )
    pull_ms = (time.monotonic() - t0) * 1000

    t0 = time.monotonic()
    logger.info("Unpacking runner image")
    sp.run(
        ["umoci", "unpack", "--rootless", "--image", f"{oci_dir}:latest", bundle_dir],
        check=True,
        capture_output=True,
    )
    unpack_ms = (time.monotonic() - t0) * 1000

    unpacked_rootfs = os.path.join(bundle_dir, "rootfs")
    if os.path.isdir(rootfs):
        shutil.rmtree(rootfs)
    os.rename(unpacked_rootfs, rootfs)

    shutil.rmtree(oci_dir, ignore_errors=True)
    shutil.rmtree(bundle_dir, ignore_errors=True)

    open(marker, "w").close()
    logger.info("Runner rootfs ready (pull: %.0fms, unpack: %.0fms)", pull_ms, unpack_ms)
    return rootfs


def _build_config(rootfs: str, migration_file: str, pipeline_file: str) -> dict:
    uid = os.getuid()
    gid = os.getgid()

    return {
        "ociVersion": "1.2.1",
        "process": {
            "terminal": False,
            "user": {"uid": 0, "gid": 0},
            "args": ["bash", "/run/migration/script.sh", "/run/migration/pipeline.yaml"],
            "env": [
                "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "HOME=/root",
            ],
            "cwd": "/",
            "capabilities": {
                t: [] for t in ["bounding", "effective", "inheritable", "permitted", "ambient"]
            },
            "noNewPrivileges": True,
        },
        "root": {"path": rootfs},
        "hostname": "migration-runner",
        "mounts": _build_mounts(migration_file, pipeline_file),
        "linux": {
            "uidMappings": [{"containerID": 0, "hostID": uid, "size": 1}],
            "gidMappings": [{"containerID": 0, "hostID": gid, "size": 1}],
            "namespaces": [
                {"type": "user"},
                {"type": "mount"},
                {"type": "pid"},
                {"type": "ipc"},
                {"type": "uts"},
            ],
            "maskedPaths": [
                "/proc/kcore",
                "/proc/latency_stats",
                "/proc/timer_list",
                "/proc/timer_stats",
                "/proc/sched_debug",
                "/sys/firmware",
                "/proc/scsi",
            ],
            "readonlyPaths": [
                "/proc/asound",
                "/proc/bus",
                "/proc/fs",
                "/proc/irq",
                "/proc/sys",
                "/proc/sysrq-trigger",
            ],
        },
    }


def _build_mounts(migration_file: str, pipeline_file: str) -> list[dict]:
    mounts: list[dict] = [
        {"destination": "/proc", "type": "proc", "source": "proc"},
        {
            "destination": "/dev",
            "type": "tmpfs",
            "source": "tmpfs",
            "options": ["nosuid", "strictatime", "mode=755", "size=65536k"],
        },
        {
            "destination": "/tmp",
            "type": "tmpfs",
            "source": "tmpfs",
            "options": ["nosuid", "nodev", "mode=1777"],
        },
        {
            "destination": "/run/migration/script.sh",
            "type": "bind",
            "source": migration_file,
            "options": ["rbind", "ro"],
        },
        {
            "destination": "/run/migration/pipeline.yaml",
            "type": "bind",
            "source": pipeline_file,
            "options": ["rbind", "rw"],
        },
    ]

    resolv = "/etc/resolv.conf"
    if os.path.exists(resolv):
        mounts.append(
            {
                "destination": resolv,
                "type": "bind",
                "source": resolv,
                "options": ["rbind", "ro"],
            }
        )

    for ca_path in ("/etc/pki/tls/certs", "/etc/ssl/certs"):
        if os.path.isdir(ca_path):
            mounts.append(
                {
                    "destination": ca_path,
                    "type": "bind",
                    "source": ca_path,
                    "options": ["rbind", "ro"],
                }
            )

    return mounts
