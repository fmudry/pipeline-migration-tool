import io
import random
import string
import tarfile
import time
from dataclasses import dataclass
from itertools import count
from pathlib import Path


class RepoPath(Path):
    @property
    def tekton_dir(self) -> Path:
        return self / ".tekton"


def select_random_chars(n=64):
    return (random.choice(string.hexdigits) for _ in range(n))


def generate_digest() -> str:
    random_choices = select_random_chars()
    return "sha256:" + "".join(random_choices).lower()


def generate_git_sha() -> str:
    return "".join(select_random_chars(40)).lower()


def generate_sha256sum() -> str:
    random_choices = select_random_chars()
    return "".join(random_choices).lower()


def generate_timestamp():
    counter = count()

    def _inner() -> int:
        return int(time.time()) - next(counter)

    return _inner


@dataclass
class TarEntry:
    """An entry of a test image tarball. ``content`` is file data or the link target."""

    name: str
    type: bytes = tarfile.REGTYPE
    content: bytes | str = b""
    mode: int = 0o644


def file_entry(name: str, content: bytes = b"", mode: int = 0o644) -> TarEntry:
    return TarEntry(name, tarfile.REGTYPE, content, mode)


def dir_entry(name: str, mode: int = 0o755) -> TarEntry:
    return TarEntry(name, tarfile.DIRTYPE, mode=mode)


def symlink_entry(name: str, target: str) -> TarEntry:
    return TarEntry(name, tarfile.SYMTYPE, target)


def hardlink_entry(name: str, target: str) -> TarEntry:
    return TarEntry(name, tarfile.LNKTYPE, target)


def write_tarball(path: Path, entries: list[TarEntry]) -> None:
    """Write an uncompressed tarball with the given entries, as crane export would."""
    with tarfile.open(path, mode="w") as tar:
        for entry in entries:
            info = tarfile.TarInfo(entry.name)
            info.type = entry.type
            info.mode = entry.mode
            if entry.type == tarfile.REGTYPE:
                data = entry.content if isinstance(entry.content, bytes) else entry.content.encode()
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                if isinstance(entry.content, str):
                    info.linkname = entry.content
                tar.addfile(info)
