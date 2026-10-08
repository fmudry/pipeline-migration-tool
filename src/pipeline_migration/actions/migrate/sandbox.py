"""Namespace-based sandbox for migration script isolation.

Uses Linux user + mount namespaces (Python 3.12 os.unshare) to hide secret
paths from migration scripts. MintMaker passes paths to mask via
PMT_DENY_PATHS env var; this module reads them and bind-mounts /dev/null
(files) or empty tmpfs (directories) over each one before exec.
"""

import ctypes
import logging
import os
import signal
import time

logger = logging.getLogger("migrate.sandbox")

# Colon-separated list of filesystem paths to hide from migration scripts
ENV_DENY_PATHS = "PMT_DENY_PATHS"
MS_BIND = 4096  # linux/mount.h
_PROBE_TIMEOUT_SECONDS = 15.0

# Paths relative to cwd that are always denied, regardless of PMT_DENY_PATHS
_BUILTIN_DENY_PATHS = [".git/config"]


def _get_deny_paths() -> list[str]:
    paths = [os.path.join(os.getcwd(), p) for p in _BUILTIN_DENY_PATHS]
    raw = os.environ.get(ENV_DENY_PATHS, "")
    if raw:
        paths.extend(p.strip() for p in raw.split(":") if p.strip())
    return paths


def is_available() -> bool:
    """Probe whether the kernel allows unprivileged user+mount namespaces.

    Forks a throwaway child that attempts unshare; the parent checks exit status
    with a timeout to avoid hanging if the child deadlocks post-fork.
    """
    try:
        pid = os.fork()
        if pid == 0:
            try:
                os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNS)
                os._exit(0)
            except OSError:
                os._exit(1)
        else:
            deadline = time.monotonic() + _PROBE_TIMEOUT_SECONDS
            while True:
                waited, status = os.waitpid(pid, os.WNOHANG)
                if waited != 0:
                    available = os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
                    if not available:
                        logger.warning("Namespace sandbox probe: unshare failed in child")
                    return available
                if time.monotonic() >= deadline:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                    logger.warning("Namespace sandbox probe: child timed out")
                    return False
                time.sleep(0.01)
    except (OSError, AttributeError) as e:
        logger.warning("Namespace sandbox probe error: %s", e)
        return False
    return False


def _load_libc_mount():  # type: ignore[no-untyped-def]
    """Load libc and configure mount(2) signature.

    Must be called in the parent process before fork — ctypes.CDLL calls
    dlopen which takes the dynamic linker lock and can deadlock in a forked
    child if another thread held that lock at fork time.
    """
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    libc.mount.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_ulong,
        ctypes.c_char_p,
    ]
    libc.mount.restype = ctypes.c_int
    return libc


def _write_proc(path: bytes, content: bytes) -> None:
    """Write to a /proc file using raw POSIX I/O.

    Runs post-fork — avoids Python buffered I/O and string encoding.
    """
    fd = os.open(path, os.O_WRONLY)
    try:
        os.write(fd, content)
    finally:
        os.close(fd)


def _make_preexec_fn(deny_paths: list[str]):  # type: ignore[no-untyped-def]
    """Build a preexec_fn that runs between fork and exec in the child.

    All Python object allocation (string encoding, path resolution) happens
    here in the parent before fork. The returned closure uses only pre-computed
    bytes and raw syscalls to minimize async-signal-safety violations.
    """
    uid = os.getuid()
    gid = os.getgid()

    uid_map = f"{uid} {uid} 1\n".encode()
    gid_map = f"{gid} {gid} 1\n".encode()

    # Pre-resolve: check existence/type and encode paths before fork
    mount_targets: list[tuple[bytes, bool]] = []
    for path in deny_paths:
        if os.path.exists(path):
            mount_targets.append((os.fsencode(path), os.path.isdir(path)))

    libc = _load_libc_mount() if mount_targets else None

    def _setup_namespace() -> None:
        os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNS)

        _write_proc(b"/proc/self/uid_map", uid_map)
        _write_proc(b"/proc/self/setgroups", b"deny\n")
        _write_proc(b"/proc/self/gid_map", gid_map)

        if not libc:
            return

        for encoded_path, is_dir in mount_targets:
            if is_dir:
                ret = libc.mount(b"tmpfs", encoded_path, b"tmpfs", ctypes.c_ulong(0), b"size=0")
            else:
                ret = libc.mount(b"/dev/null", encoded_path, None, ctypes.c_ulong(MS_BIND), None)
            if ret != 0:
                errno = ctypes.get_errno()
                raise OSError(errno, os.strerror(errno))

    return _setup_namespace


# Env vars safe to forward, everything else (RENOVATE_TOKEN, etc.) is dropped
ALLOWED_ENV_VARS = ("PATH", "HOME")


def build_env() -> dict[str, str]:
    """Forward only non-secret env vars so migration scripts can't read e.g. RENOVATE_TOKEN."""
    return {k: v for k, v in os.environ.items() if k in ALLOWED_ENV_VARS}


def build_preexec_fn():  # type: ignore[no-untyped-def]
    """Build preexec_fn that sets up namespace isolation and masks deny paths."""
    deny_paths = _get_deny_paths()
    if deny_paths:
        logger.info("Deny paths from %s: %s", ENV_DENY_PATHS, deny_paths)
    return _make_preexec_fn(deny_paths)
