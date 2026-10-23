"""Container sandbox for running migration scripts via crun.

When crun, skopeo and umoci are available, migration scripts execute inside an isolated rootless
OCI container built from a dedicated runner image (bash, pmt, yq, jq). The container has no
access to host secrets. Network is shared so scripts can reach OCI registries.

The container is built only from operations that an unprivileged process is always allowed to do,
even under SELinux container_t, a masked /proc and the containers/common seccomp profile:
user/mount/pid/ipc/uts namespaces, tmpfs mounts and bind mounts of directories PMT creates itself.
Consequently there is no /proc inside the container, by design and in every environment.

Migration scripts get a private, writable work directory /run/migration with script.sh and
pipeline.yaml copies, so any way of rewriting the file (in place, rename, delete and recreate)
works. The result is copied back to the original pipeline file afterwards.

The runner image is pulled once and cached under /tmp.
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
_HOST_RESOLV_CONF = "/etc/resolv.conf"
_WORK_DIR = "/run/migration"


def _get_runner_image() -> str:
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
    t_total = time.monotonic()
    rootfs = _ensure_rootfs()
    container_id = f"pmt-{uuid.uuid4().hex[:12]}"
    bundle_dir = tempfile.mkdtemp(prefix="pmt-bundle-")
    state_dir = tempfile.mkdtemp(prefix="pmt-crun-")
    work_dir = tempfile.mkdtemp(prefix="pmt-work-")

    try:
        shutil.copyfile(migration_file, os.path.join(work_dir, "script.sh"))
        work_pipeline = os.path.join(work_dir, "pipeline.yaml")
        shutil.copyfile(pipeline_file, work_pipeline)

        resolv_conf = None
        if os.path.isfile(_HOST_RESOLV_CONF):
            # Copied rather than bind-mounted: the host file may carry an SELinux label that a
            # container_t process is not allowed to mount.
            resolv_conf = os.path.join(bundle_dir, "resolv.conf")
            shutil.copyfile(_HOST_RESOLV_CONF, resolv_conf)

        config = _build_config(rootfs=rootfs, work_dir=work_dir, resolv_conf=resolv_conf)
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
        total_ms = (time.monotonic() - t_total) * 1000
        logger.info(
            "Container %s finished in %.0fms (total overhead: %.0fms, rc=%d)",
            container_id,
            run_ms,
            total_ms - run_ms,
            result.returncode,
        )
        if result.returncode != 0:
            logger.error(
                "Migration in container %s failed with output:\n%s",
                container_id,
                result.stdout.decode(errors="replace"),
            )

        if not os.path.isfile(work_pipeline):
            raise RuntimeError(f"Migration script removed the pipeline file {pipeline_file}")
        # Copy into the existing file to keep its inode, ownership and permissions
        shutil.copyfile(work_pipeline, pipeline_file)
        return result
    finally:
        shutil.rmtree(bundle_dir, ignore_errors=True)
        shutil.rmtree(state_dir, ignore_errors=True)
        shutil.rmtree(work_dir, ignore_errors=True)


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


def _build_config(rootfs: str, work_dir: str, resolv_conf: str | None) -> dict:
    uid = os.getuid()
    gid = os.getgid()

    return {
        "ociVersion": "1.2.1",
        "process": {
            "terminal": False,
            "user": {"uid": 0, "gid": 0},
            "args": ["bash", f"{_WORK_DIR}/script.sh", f"{_WORK_DIR}/pipeline.yaml"],
            "env": [
                "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "HOME=/root",
            ],
            "cwd": _WORK_DIR,
            "capabilities": {
                t: [] for t in ["bounding", "effective", "inheritable", "permitted", "ambient"]
            },
            "noNewPrivileges": True,
        },
        # Read-only, so a migration cannot tamper with the cached rootfs used by later migrations
        "root": {"path": rootfs, "readonly": True},
        # No "hostname": crun would call sethostname(), which seccomp profiles such as the
        # containers/common default deny without CAP_SYS_ADMIN. The uts namespace stays private.
        "mounts": _build_mounts(work_dir, resolv_conf),
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
        },
    }


def _tmpfs(destination: str, *options: str) -> dict:
    return {
        "destination": destination,
        "type": "tmpfs",
        "source": "tmpfs",
        "options": list(options),
    }


def _build_mounts(work_dir: str, resolv_conf: str | None) -> list[dict]:
    # No /proc: a fresh procfs mount is refused under SELinux container_t and when the parent
    # /proc has masked paths, and bind-mounting the parent /proc would expose the environment
    # (RENOVATE_TOKEN) of the Renovate process, which runs as the same UID.
    #
    # Device nodes (/dev/null, /dev/urandom, ...) are not bind-mounted here: crun creates the
    # default OCI devices itself, while binding host device nodes is denied by SELinux when they
    # carry host labels (e.g. null_device_t) and the caller runs as container_t.
    mounts = [
        _tmpfs("/dev", "nosuid", "strictatime", "mode=755", "size=65536k"),
        _tmpfs("/dev/shm", "nosuid", "nodev", "noexec", "mode=1777", "size=65536k"),
        _tmpfs("/tmp", "nosuid", "nodev", "mode=1777"),
        _tmpfs("/root", "nosuid", "nodev", "mode=700"),
        # A directory, not single-file bind mounts, so scripts can rename or recreate files
        {"destination": _WORK_DIR, "type": "bind", "source": work_dir, "options": ["rbind", "rw"]},
    ]
    if resolv_conf:
        mounts.append(
            {
                "destination": _HOST_RESOLV_CONF,
                "type": "bind",
                "source": resolv_conf,
                "options": ["rbind", "ro"],
            }
        )
    return mounts
