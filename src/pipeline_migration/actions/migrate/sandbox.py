import logging
import os
import shutil

logger = logging.getLogger("migrate.sandbox")

BWRAP_BINARY = "bwrap"


def is_available() -> bool:
    """Check if bwrap sandbox is available."""
    return shutil.which(BWRAP_BINARY) is not None


def build_cmd(migration_file: str, file_path: str) -> list[str]:
    """Build bwrap command to run a migration script in an isolated sandbox.

    Allowlist model: only explicitly bound paths are visible inside the sandbox.
    All environment variables are cleared. Kubernetes secret volume mounts
    at /etc/renovate/ and docker config at ~/.docker/ are not bound,
    so they are invisible to the migration script.
    """
    file_path_abs = os.path.abspath(file_path)
    file_dir = os.path.dirname(file_path_abs)
    cwd = os.getcwd()
    home = os.path.expanduser("~")

    cmd: list[str] = [BWRAP_BINARY]

    for path in ("/usr", "/bin", "/sbin", "/lib", "/lib64"):
        if os.path.exists(path):
            cmd += ["--ro-bind", path, path]

    cmd += ["--proc", "/proc"]
    cmd += ["--dev-bind", "/dev", "/dev"]
    cmd += ["--tmpfs", "/tmp"]

    local_dir = os.path.join(home, ".local")
    if os.path.isdir(local_dir):
        cmd += ["--ro-bind", local_dir, local_dir]

    cmd += ["--ro-bind", migration_file, migration_file]

    # Pipeline file needs write access. If it's in /tmp, bind just the file
    # (binding /tmp would undo the --tmpfs overlay). Otherwise bind the directory.
    if file_dir.startswith("/tmp"):
        cmd += ["--bind", file_path_abs, file_path_abs]
    else:
        cmd += ["--bind", file_dir, file_dir]

    # Repo working directory (read-only so scripts can read other pipeline files)
    if not cwd.startswith("/tmp"):
        cmd += ["--ro-bind", cwd, cwd]

    # DNS — use --bind (not --ro-bind) because /etc/resolv.conf is a bind mount
    # in Kubernetes pods and the read-only remount fails in user namespaces
    for path in ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf"):
        if os.path.exists(path):
            cmd += ["--bind", path, path]

    for path in ("/etc/pki", "/etc/ssl", "/etc/alternatives"):
        if os.path.isdir(path):
            cmd += ["--ro-bind", path, path]

    cmd += ["--clearenv"]
    path_parts = ["/usr/bin", "/bin"]
    local_bin = os.path.join(home, ".local", "bin")
    if os.path.isdir(local_bin):
        path_parts.append(local_bin)
    cmd += ["--setenv", "PATH", ":".join(path_parts)]
    cmd += ["--setenv", "HOME", home]

    ca_bundle = "/etc/pki/tls/certs/ca-bundle.crt"
    if os.path.exists(ca_bundle):
        cmd += ["--setenv", "SSL_CERT_FILE", ca_bundle]
        cmd += ["--setenv", "REQUESTS_CA_BUNDLE", ca_bundle]

    cmd += ["--die-with-parent", "--new-session"]
    cmd += ["--chdir", cwd]
    cmd += ["--", "bash", migration_file, file_path_abs]

    return cmd
