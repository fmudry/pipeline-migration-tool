import hashlib
import io
import json
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
    """An entry of a test image layer. ``content`` is file data or the link target."""

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


def write_oci_layout(
    oci_dir: Path,
    layers: list[list[TarEntry]],
    ref_name: str = "latest",
    layer_media_type: str = "application/vnd.oci.image.layer.v1.tar+gzip",
) -> None:
    """Write a minimal OCI image layout with the given layers, as skopeo copy would."""
    blobs = oci_dir / "blobs" / "sha256"
    blobs.mkdir(parents=True)

    def write_blob(data: bytes) -> dict:
        digest = hashlib.sha256(data).hexdigest()
        (blobs / digest).write_bytes(data)
        return {"digest": f"sha256:{digest}", "size": len(data)}

    layer_descriptors = [
        {"mediaType": layer_media_type, **write_blob(_build_layer(entries))} for entries in layers
    ]
    manifest = {"schemaVersion": 2, "layers": layer_descriptors}
    index = {
        "schemaVersion": 2,
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                **write_blob(json.dumps(manifest).encode()),
                "annotations": {"org.opencontainers.image.ref.name": ref_name},
            }
        ],
    }
    (oci_dir / "index.json").write_text(json.dumps(index))


def _build_layer(entries: list[TarEntry]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
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
    return buf.getvalue()
