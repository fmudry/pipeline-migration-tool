"""Bubblewrap-based sandbox for migration script isolation.

Uses bwrap with a deny-list model: the full filesystem is visible by default
(--bind / /), then specific secret paths are masked with tmpfs or /dev/null
bind-mounts. MintMaker passes paths to mask via PMT_DENY_PATHS env var.

Environment variables are sanitized via --clearenv, forwarding only PATH and HOME.
"""

import logging
import os
import shutil
import subprocess

logger = logging.getLogger("migrate.sandbox")

BWRAP_BINARY = "bwrap"

ENV_DENY_PATHS = "PMT_DENY_PATHS"
ALLOWED_ENV_VARS = ("PATH", "HOME")
_BUILTIN_DENY_PATHS = [".git/config"]


def _get_deny_paths() -> list[str]:
    paths = [os.path.join(os.getcwd(), p) for p in _BUILTIN_DENY_PATHS]
    raw = os.environ.get(ENV_DENY_PATHS, "")
    if raw:
        paths.extend(p.strip() for p in raw.split(":") if p.strip())
    return paths


def is_available() -> bool:
    """Check if bwrap is installed and can create user+mount namespaces."""
    if shutil.which(BWRAP_BINARY) is None:
        return False
    try:
        proc = subprocess.run(
            [BWRAP_BINARY, "--bind", "/", "/", "--", "/usr/bin/true"],
            capture_output=True,
            timeout=5,
        )
        if proc.returncode != 0:
            logger.warning(
                "bwrap probe failed (exit %d): %s",
                proc.returncode,
                proc.stderr.decode(errors="replace").strip(),
            )
            return False
        return True
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("bwrap probe error: %s", e)
        return False


def build_env() -> dict[str, str]:
    """Sanitized env for the fallback path (no bwrap)."""
    return {k: v for k, v in os.environ.items() if k in ALLOWED_ENV_VARS}


def build_cmd(migration_file: str, file_path: str) -> list[str]:
    """Build bwrap command using the deny-list model.

    Starts from full filesystem visibility (--bind / /) then masks each deny
    path: directories get an empty tmpfs overlay, files get /dev/null.
    Environment is cleared and only PATH + HOME are forwarded.
    """
    cmd: list[str] = [BWRAP_BINARY]

    cmd += ["--bind", "/", "/"]

    deny_paths = _get_deny_paths()
    for path in deny_paths:
        if not os.path.exists(path):
            continue
        if os.path.isdir(path):
            cmd += ["--tmpfs", path]
        else:
            cmd += ["--bind", "/dev/null", path]

    cmd += ["--clearenv"]
    for var in ALLOWED_ENV_VARS:
        val = os.environ.get(var)
        if val:
            cmd += ["--setenv", var, val]

    cmd += ["--die-with-parent", "--new-session"]
    cmd += ["--chdir", os.getcwd()]
    cmd += ["--", "bash", migration_file, os.path.abspath(file_path)]

    return cmd
