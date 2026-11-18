"""Container sandbox for running migration scripts via crun.

Migration scripts are downloaded from registries and are untrusted. When the sandbox is enabled
(see ``enabled``), each script runs inside a rootless OCI container built from a dedicated runner
image (bash, pmt, yq, jq). The container sees no host files and no host environment. The network
is shared with the host so scripts can reach OCI registries.

The container is built only from operations that an unprivileged process is always allowed to do,
even under SELinux container_t, a masked /proc and the containers/common seccomp profile:
user/mount/pid/ipc/uts namespaces, tmpfs mounts and bind mounts of directories PMT creates itself.
Consequently there is no /proc inside the container, by design and in every environment.

Migration script contract inside the sandbox:

- The script is run as ``bash <script> <pipeline file>``. The pipeline file is a copy with the
  original file name in the private, writable work directory /run/migration, which is also the
  working directory. Any way of rewriting the file works (in place, rename, delete and recreate).
- When the script succeeds, the result is copied back to the original pipeline file. When it
  fails, the original file is left untouched. The result must be a regular file.
- Only PATH and HOME are set. There is no /proc, so bash process substitution ``<(...)`` and
  /dev/stdin do not work.

The runner image is flattened into a single filesystem tarball by ``crane export``
(go-containerregistry), which applies the layers and their whiteouts. The tarball is extracted into
a rootfs cached in a per-user directory under /tmp.
"""

import contextlib
import errno
import fcntl
import functools
import hashlib
import json
import logging
import os
import platform
import shutil
import stat
import subprocess as sp
import tarfile
import tempfile
import time
import uuid
from collections.abc import Iterator

from pipeline_migration import __version__

logger = logging.getLogger("migrate.sandbox")

SANDBOX_MODE_ENV = "PMT_SANDBOX"
RUNNER_IMAGE_ENV = "PMT_RUNNER_IMAGE"
_MODE_AUTO = "auto"
_MODE_ON = "on"
_MODE_OFF = "off"

_DEFAULT_RUNNER_REPO = "quay.io/konflux-ci/pipeline-migration-runner"
_PULL_TIMEOUT_SECONDS = 600
# platform.machine() values that differ from the OCI architecture names
_OCI_ARCHITECTURES = {"x86_64": "amd64", "aarch64": "arm64"}

_READY_MARKER = ".ready"
_HOST_RESOLV_CONF = "/etc/resolv.conf"
_WORK_DIR = "/run/migration"
_SCRIPT_DIR = "/run/migration-script"
_SCRIPT_NAME = "script.sh"
_MAX_PIPELINE_SIZE = 10 * 1024 * 1024


class SandboxError(Exception):
    """The sandbox is required or detected but cannot be used, e.g. the runner image pull failed.

    This is an infrastructure failure, unrelated to any particular migration script.
    """


@functools.cache
def enabled() -> bool:
    """Decide whether migration scripts run in the sandbox, and prepare it if so.

    The decision is made once per process. The environment variable PMT_SANDBOX selects the policy:

    - ``auto`` (default): use the sandbox when crun and crane are installed. Otherwise warn and
      run migration scripts directly on the host.
    - ``on``: require the sandbox.
    - ``off``: never use the sandbox.

    :raises SandboxError: the sandbox is required or crun and crane are installed, but the sandbox
        cannot be prepared. Migrations never fall back to the host silently in that case.
    """
    mode = os.environ.get(SANDBOX_MODE_ENV, _MODE_AUTO).lower()
    if mode not in (_MODE_AUTO, _MODE_ON, _MODE_OFF):
        raise SandboxError(f"Invalid {SANDBOX_MODE_ENV} value {mode!r}, expected auto, on or off")
    if mode == _MODE_OFF:
        logger.info("Container sandbox disabled by %s=off", SANDBOX_MODE_ENV)
        return False

    missing_tools = [tool for tool in ("crun", "crane") if not shutil.which(tool)]
    if missing_tools:
        message = f"Container sandbox unavailable, {' and '.join(missing_tools)} not found in PATH"
        if mode == _MODE_ON:
            raise SandboxError(message)
        logger.warning("%s. Migration scripts run directly on the host.", message)
        return False

    image = _get_runner_image()
    _ensure_rootfs(image)
    logger.info("Container sandbox enabled (image: %s)", image)
    return True


def run_migration(
    migration_file: str, pipeline_file: str, timeout: float
) -> sp.CompletedProcess[bytes]:
    """Run a migration script on a pipeline file inside an isolated crun container.

    :raises TimeoutError: the script did not finish within ``timeout`` seconds.
    :raises RuntimeError: the script succeeded but left no usable pipeline file.
    """
    started = time.monotonic()
    rootfs = _ensure_rootfs(_get_runner_image())
    container_id = f"pmt-{uuid.uuid4().hex[:12]}"
    pipeline_name = os.path.basename(pipeline_file)

    with tempfile.TemporaryDirectory(prefix="pmt-sandbox-", ignore_cleanup_errors=True) as tmp:
        bundle_dir, state_dir, script_dir, work_dir = (
            _make_subdir(tmp, name) for name in ("bundle", "state", "script", "work")
        )
        shutil.copyfile(migration_file, os.path.join(script_dir, _SCRIPT_NAME))
        work_pipeline = os.path.join(work_dir, pipeline_name)
        shutil.copyfile(pipeline_file, work_pipeline)

        resolv_conf = None
        if os.path.isfile(_HOST_RESOLV_CONF):
            # Copied rather than bind-mounted: the host file may carry an SELinux label that a
            # container_t process is not allowed to mount.
            resolv_conf = os.path.join(bundle_dir, "resolv.conf")
            shutil.copyfile(_HOST_RESOLV_CONF, resolv_conf)

        config = _build_config(rootfs, script_dir, work_dir, resolv_conf, pipeline_name)
        with open(os.path.join(bundle_dir, "config.json"), "w") as f:
            json.dump(config, f)

        result = _run_container(container_id, bundle_dir, state_dir, timeout)
        logger.debug(
            "Container %s finished in %.0fms (rc=%d)",
            container_id,
            (time.monotonic() - started) * 1000,
            result.returncode,
        )
        if result.returncode == 0:
            _copy_result_back(work_pipeline, pipeline_file)
        return result


def _run_container(
    container_id: str, bundle_dir: str, state_dir: str, timeout: float
) -> sp.CompletedProcess[bytes]:
    crun = ["crun", "--root", state_dir]
    try:
        return sp.run(
            [*crun, "run", "--bundle", bundle_dir, container_id],
            stderr=sp.STDOUT,
            stdout=sp.PIPE,
            timeout=timeout,
        )
    except sp.TimeoutExpired:
        # sp.run kills crun, but the container keeps running until crun deletes it
        sp.run([*crun, "delete", "--force", container_id], capture_output=True)
        raise TimeoutError(f"Migration script did not finish within {timeout:.0f} seconds")


def _copy_result_back(work_pipeline: str, pipeline_file: str) -> None:
    """Copy the migrated pipeline file from the work directory to the original file.

    The work directory is writable by the container, so its content is untrusted: the script could
    have replaced the pipeline file with a symlink to a host file (e.g. /proc/self/environ), whose
    content would otherwise end up in the pipeline file. Only a regular file is accepted, opened
    without following symlinks. A hard link to a host file cannot exist: nothing else on the work
    directory's file system is visible in the container.
    """
    try:
        # O_NONBLOCK: opening a FIFO must not block
        fd = os.open(work_pipeline, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise RuntimeError(f"Migration script removed the pipeline file {pipeline_file}")
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise RuntimeError("Migration script replaced the pipeline file by a symlink") from e
        raise RuntimeError(f"Migration script left an unusable pipeline file: {e}") from e

    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError("Migration script replaced the pipeline file by a non-regular file")
        content = _read_up_to(fd, _MAX_PIPELINE_SIZE + 1)
    finally:
        os.close(fd)
    if len(content) > _MAX_PIPELINE_SIZE:
        raise RuntimeError(f"Migrated pipeline file exceeds {_MAX_PIPELINE_SIZE} bytes")

    # Writing into the existing file keeps its inode, ownership and permissions
    with open(pipeline_file, "wb") as f:
        f.write(content)


def _read_up_to(fd: int, limit: int) -> bytes:
    chunks = []
    size = 0
    while size < limit and (chunk := os.read(fd, limit - size)):
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def _get_runner_image() -> str:
    """Determine the runner image reference."""
    return os.environ.get(RUNNER_IMAGE_ENV) or f"{_DEFAULT_RUNNER_REPO}:v{__version__}"


def _ensure_rootfs(image: str) -> str:
    """Return the cached rootfs of the runner image, pulling and unpacking it if needed."""
    cache_dir = _image_cache_dir(image)
    rootfs = os.path.join(cache_dir, "rootfs")
    if _is_ready(cache_dir):
        return rootfs

    # Another PMT process may be building the same rootfs
    with _locked(os.path.join(cache_dir, ".lock")):
        if not _is_ready(cache_dir):
            _build_rootfs(image, cache_dir)
    return rootfs


def _is_ready(cache_dir: str) -> bool:
    marker = os.path.join(cache_dir, _READY_MARKER)
    return os.path.isfile(marker) and os.path.isdir(os.path.join(cache_dir, "rootfs"))


def _build_rootfs(image: str, cache_dir: str) -> None:
    """Pull and extract the runner image. The caller holds the cache lock."""
    tarball = os.path.join(cache_dir, "rootfs.tar")
    unpack_dir = os.path.join(cache_dir, "rootfs.tmp")
    rootfs = os.path.join(cache_dir, "rootfs")

    # Leftovers of an interrupted build, or a rootfs whose marker or content is missing
    for leftover in (unpack_dir, rootfs):
        shutil.rmtree(leftover, ignore_errors=True)
    for leftover in (tarball, os.path.join(cache_dir, _READY_MARKER)):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(leftover)

    started = time.monotonic()
    logger.info("Pulling runner image %s", image)
    try:
        _export_image(image, tarball)
        pulled = time.monotonic()

        logger.info("Extracting runner image")
        os.mkdir(unpack_dir)
        try:
            with tarfile.open(tarball) as tar:
                tar.extractall(unpack_dir, filter=_rootfs_filter)
        except (OSError, tarfile.TarError) as e:
            raise SandboxError(f"Cannot extract runner image {image}: {e}") from e
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tarball)

    # Renamed into place only when complete, so an interrupted extraction is never used as a rootfs
    os.rename(unpack_dir, rootfs)
    open(os.path.join(cache_dir, _READY_MARKER), "w").close()
    logger.info(
        "Runner rootfs ready (pull: %.0fms, extract: %.0fms)",
        (pulled - started) * 1000,
        (time.monotonic() - pulled) * 1000,
    )


def _export_image(image: str, tarball: str) -> None:
    """Write the flattened filesystem of the runner image to ``tarball`` with crane."""
    machine = platform.machine()
    cmd = [
        "crane",
        "export",
        "--platform",
        f"linux/{_OCI_ARCHITECTURES.get(machine, machine)}",
        image,
        tarball,
    ]
    try:
        sp.run(cmd, check=True, capture_output=True, timeout=_PULL_TIMEOUT_SECONDS)
    except sp.CalledProcessError as e:
        stderr = e.stderr.decode(errors="replace").strip()
        raise SandboxError(f"Cannot pull runner image {image}: {stderr}") from e
    except sp.TimeoutExpired as e:
        raise SandboxError(
            f"Pulling runner image {image} did not finish within {_PULL_TIMEOUT_SECONDS} seconds"
        ) from e


def _rootfs_filter(member: tarfile.TarInfo, dest_path: str) -> tarfile.TarInfo | None:
    """Extract the runner image without privileges and never outside the rootfs.

    The "tar" filter refuses entries resolving outside ``dest_path`` (also through symlinks
    extracted before) and clears setuid/setgid/sticky bits. The stricter "data" filter cannot be
    used: it refuses absolute symlinks, which every distribution rootfs has.
    """
    if member.ischr() or member.isblk() or member.isfifo():
        # Creating device nodes needs CAP_MKNOD. crun creates the devices the container needs.
        return None
    if member.islnk():
        # The "tar" filter does not check hard link targets. A target reached through a symlink
        # to a host file would link that file into the rootfs.
        dest_path = os.path.realpath(dest_path)
        linkname = member.linkname.lstrip("/")
        target = os.path.realpath(os.path.join(dest_path, linkname))
        if os.path.commonpath([target, dest_path]) != dest_path:
            raise tarfile.LinkOutsideDestinationError(member, target)
        member = member.replace(linkname=linkname, deep=False)

    member = tarfile.tar_filter(member, dest_path)
    # Owned by the current user, as if extracted with tar --no-same-owner
    attributes: dict = {"uid": None, "gid": None, "uname": None, "gname": None}
    if member.isdir() and member.mode is not None:
        # Owner access lets cache cleanup remove the directory
        attributes["mode"] = member.mode | stat.S_IRWXU
    return member.replace(**attributes, deep=False)


def _image_cache_dir(image: str) -> str:
    """Return the cache directory of a runner image, creating it if needed.

    Keyed by the image reference, so a new PMT version or a PMT_RUNNER_IMAGE override never
    reuses a rootfs unpacked from a different image.
    """
    key = hashlib.sha256(image.encode()).hexdigest()[:16]
    cache_dir = os.path.join(_cache_root(), key)
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


def _cache_root() -> str:
    """Return the per-user cache root, refusing one another user could have tampered with.

    /tmp is shared, so another user could create the directory first and plant a rootfs that every
    migration would then run in. Everything below a directory only the current user can write is
    safe.
    """
    path = os.path.join(tempfile.gettempdir(), f"pmt-runner-{os.getuid()}")
    with contextlib.suppress(FileExistsError):
        os.mkdir(path, 0o700)

    st = os.lstat(path)
    accessible_by_others = st.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or accessible_by_others:
        raise SandboxError(
            f"Refusing to use the runner cache {path}: it must be a directory owned by the current "
            "user and not accessible by others"
        )
    return path


@contextlib.contextmanager
def _locked(lock_file: str) -> Iterator[None]:
    with open(lock_file, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def _make_subdir(parent: str, name: str) -> str:
    path = os.path.join(parent, name)
    os.mkdir(path)
    return path


def _build_config(
    rootfs: str, script_dir: str, work_dir: str, resolv_conf: str | None, pipeline_name: str
) -> dict:
    """Build the OCI runtime config (config.json) of the container."""
    no_capabilities: list[str] = []
    return {
        # Every field used here exists in runtime-spec 1.0.0, which every crun release supports
        "ociVersion": "1.0.0",
        "process": {
            # No pseudo-terminal: the output is captured through the inherited stdout pipe
            "terminal": False,
            "user": {"uid": 0, "gid": 0},
            "args": ["bash", f"{_SCRIPT_DIR}/{_SCRIPT_NAME}", f"{_WORK_DIR}/{pipeline_name}"],
            "env": [
                "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "HOME=/root",
            ],
            "cwd": _WORK_DIR,
            # Root inside the container has no capabilities at all, and cannot gain any
            "capabilities": {
                capability_set: no_capabilities
                for capability_set in (
                    "bounding",
                    "effective",
                    "inheritable",
                    "permitted",
                    "ambient",
                )
            },
            "noNewPrivileges": True,
        },
        # Read-only, so a migration cannot tamper with the cached rootfs used by later migrations
        "root": {"path": rootfs, "readonly": True},
        # No "hostname": crun would call sethostname(), which seccomp profiles such as the
        # containers/common default deny without CAP_SYS_ADMIN. The uts namespace stays private.
        "mounts": _build_mounts(script_dir, work_dir, resolv_conf),
        "linux": {
            "uidMappings": [{"containerID": 0, "hostID": os.getuid(), "size": 1}],
            "gidMappings": [{"containerID": 0, "hostID": os.getgid(), "size": 1}],
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


def _bind(destination: str, source: str, *options: str) -> dict:
    return {
        "destination": destination,
        "type": "bind",
        "source": source,
        "options": ["rbind", *options],
    }


def _build_mounts(script_dir: str, work_dir: str, resolv_conf: str | None) -> list[dict]:
    # No /proc: a fresh procfs mount is refused under SELinux container_t and when the parent
    # /proc has masked paths and bind-mounting the parent /proc would expose the environment
    # (RENOVATE_TOKEN) of the Renovate process, which runs as the same UID.
    #
    # Device nodes (/dev/null, /dev/urandom, ...) are not bind-mounted here: crun creates the
    # default OCI devices itself, while binding host device nodes is denied by SELinux when they
    # carry host labels (e.g. null_device_t) and the caller runs as container_t.
    mounts = [
        _tmpfs("/dev", "nosuid", "strictatime", "mode=755", "size=65536k"),
        # POSIX shared memory (shm_open, Python multiprocessing), part of the default OCI mounts
        _tmpfs("/dev/shm", "nosuid", "nodev", "noexec", "mode=1777", "size=65536k"),
        # Private scratch space, as the rootfs is read-only. Not the host /tmp.
        _tmpfs("/tmp", "nosuid", "nodev", "mode=1777"),
        _tmpfs("/root", "nosuid", "nodev", "mode=700"),
        _bind(_SCRIPT_DIR, script_dir, "ro"),
        # A directory, not single-file bind mounts, so scripts can rename or recreate files
        _bind(_WORK_DIR, work_dir, "rw"),
    ]
    if resolv_conf:
        mounts.append(_bind(_HOST_RESOLV_CONF, resolv_conf, "ro"))
    return mounts
