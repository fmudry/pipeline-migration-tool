"""Container sandbox for running migration scripts via crun.

When crun and skopeo are available, migration scripts execute inside an isolated rootless OCI
container built from a dedicated runner image (bash, pmt, yq, jq). The container has no
access to host secrets. Network is shared so scripts can reach OCI registries.

The container is built only from operations that an unprivileged process is always allowed to do,
even under SELinux container_t, a masked /proc and the containers/common seccomp profile:
user/mount/pid/ipc/uts namespaces, tmpfs mounts and bind mounts of directories PMT creates itself.
Consequently there is no /proc inside the container, by design and in every environment.

Migration scripts get a private, writable work directory /run/migration with script.sh and
pipeline.yaml copies, so any way of rewriting the file (in place, rename, delete and recreate)
works. The result is copied back to the original pipeline file afterwards.

The runner image is pulled once with skopeo into an OCI image layout, unpacked into a rootfs by
this module and cached under /tmp.
"""

import json
import logging
import os
import shutil
import subprocess as sp
import tarfile
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

_IMAGE_MANIFEST_MEDIA_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
# Compressions tarfile can read. zstd is not supported by Python 3.12.
_LAYER_MEDIA_TYPES = {
    "application/vnd.oci.image.layer.v1.tar",
    "application/vnd.oci.image.layer.v1.tar+gzip",
    "application/vnd.docker.image.rootfs.diff.tar",
    "application/vnd.docker.image.rootfs.diff.tar.gzip",
}
_WHITEOUT_PREFIX = ".wh."
_OPAQUE_WHITEOUT = ".wh..wh..opq"


def _get_runner_image() -> str:
    """Determine the runner image reference."""
    override = os.environ.get("PMT_RUNNER_IMAGE")
    if override:
        return override
    return f"{_DEFAULT_RUNNER_REPO}:v{__version__}"


def is_available() -> bool:
    """Check whether containerized execution is available."""
    for tool in ("crun", "skopeo"):
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
    unpack_dir = os.path.join(_CACHE_DIR, "rootfs.tmp")
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
    for path in (unpack_dir, rootfs):
        if os.path.isdir(path):
            shutil.rmtree(path)
    os.makedirs(unpack_dir)
    unpack_oci_image(oci_dir, "latest", unpack_dir)
    unpack_ms = (time.monotonic() - t0) * 1000

    # Renamed into place only when complete, so an interrupted unpack is never used as a rootfs
    os.rename(unpack_dir, rootfs)
    shutil.rmtree(oci_dir, ignore_errors=True)

    open(marker, "w").close()
    logger.info("Runner rootfs ready (pull: %.0fms, unpack: %.0fms)", pull_ms, unpack_ms)
    return rootfs


def _read_blob(oci_dir: str, digest: str) -> str:
    algorithm, _, encoded = digest.partition(":")
    if not algorithm or not encoded or "/" in algorithm or "/" in encoded:
        raise ValueError(f"Invalid blob digest: {digest}")
    return os.path.join(oci_dir, "blobs", algorithm, encoded)


def unpack_oci_image(oci_dir: str, ref_name: str, rootfs: str) -> None:
    """Unpack an image from an OCI image layout into an existing empty rootfs directory.

    Layers are applied in order, with OCI whiteouts handled as described in the OCI image spec.
    No privileges are needed: files are owned by the current user, device nodes are skipped and
    setuid/setgid bits are cleared.
    """
    with open(os.path.join(oci_dir, "index.json")) as f:
        index = json.load(f)
    descriptors = [
        d
        for d in index.get("manifests", [])
        if d.get("annotations", {}).get("org.opencontainers.image.ref.name") == ref_name
    ]
    if len(descriptors) != 1:
        raise ValueError(f"Expected one manifest tagged {ref_name} in {oci_dir}")
    if descriptors[0].get("mediaType") not in _IMAGE_MANIFEST_MEDIA_TYPES:
        raise ValueError(f"Unsupported manifest media type: {descriptors[0].get('mediaType')}")

    with open(_read_blob(oci_dir, descriptors[0]["digest"])) as f:
        manifest = json.load(f)
    for layer in manifest["layers"]:
        if layer.get("mediaType") not in _LAYER_MEDIA_TYPES:
            raise ValueError(f"Unsupported layer media type: {layer.get('mediaType')}")
        _apply_layer(_read_blob(oci_dir, layer["digest"]), rootfs)


def _apply_layer(layer_path: str, rootfs: str) -> None:
    rootfs = os.path.realpath(rootfs)
    with tarfile.open(layer_path, "r:*") as tar:
        members = tar.getmembers()
        contents = []

        # Whiteouts and replaced entries refer to lower layers only, so they are removed before
        # anything from this layer is extracted, regardless of the order of entries in the tar.
        for member in members:
            dirname, basename = os.path.split(_member_path(member))
            if basename == _OPAQUE_WHITEOUT:
                directory = _rootfs_path(rootfs, dirname)
                if os.path.isdir(directory) and not os.path.islink(directory):
                    for entry in os.listdir(directory):
                        _remove(os.path.join(directory, entry))
            elif basename.startswith(_WHITEOUT_PREFIX):
                # Other .wh..wh.* names are aufs metadata and have no meaning in the rootfs
                if not basename.startswith(_WHITEOUT_PREFIX * 2):
                    name = basename.removeprefix(_WHITEOUT_PREFIX)
                    _remove(_rootfs_path(rootfs, os.path.join(dirname, name)))
            else:
                contents.append(member)
                path = _rootfs_path(rootfs, _member_path(member))
                # A directory merges with an existing directory, anything else replaces the entry
                if not (member.isdir() and os.path.isdir(path) and not os.path.islink(path)):
                    _remove(path)

        tar.extractall(rootfs, members=contents, filter=_layer_filter)


def _layer_filter(member: tarfile.TarInfo, dest_path: str) -> tarfile.TarInfo | None:
    if member.ischr() or member.isblk() or member.isfifo():
        # Creating device nodes needs CAP_MKNOD. crun creates the devices the container needs.
        return None
    if member.islnk():
        # The "tar" filter does not check hard link targets
        target = os.path.normpath(member.linkname.lstrip("/"))
        if target == ".." or target.startswith("../"):
            raise tarfile.LinkOutsideDestinationError(member, member.linkname)
    # Strips leading slashes, refuses paths escaping dest_path, clears setuid/setgid and go-w
    filtered = tarfile.tar_filter(member, dest_path)
    if filtered.isdir():
        # Owner write access lets later layers and cache cleanup modify the directory
        filtered = filtered.replace(mode=filtered.mode | 0o700, deep=False)
    return filtered


def _member_path(member: tarfile.TarInfo) -> str:
    path = os.path.normpath(member.name.lstrip("/"))
    if path == ".." or path.startswith("../"):
        raise tarfile.OutsideDestinationError(member, member.name)
    return path


def _rootfs_path(rootfs: str, path: str) -> str:
    """Return the host path of a rootfs entry, refusing paths resolving outside the rootfs.

    The parent directory is resolved because it may contain symlinks from lower layers. The entry
    itself is not, so a symlink is removed rather than its target.
    """
    target = os.path.join(rootfs, path)
    parent = os.path.realpath(os.path.dirname(target))
    if os.path.commonpath([parent, rootfs]) != rootfs:
        raise ValueError(f"Path {path} resolves outside of the rootfs")
    return os.path.join(parent, os.path.basename(target))


def _remove(path: str) -> None:
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    elif os.path.lexists(path):
        os.unlink(path)


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
