"""Unpack an image from an OCI image layout into a rootfs directory, without privileges.

An image is a list of layers. Each layer is a tar archive describing the changes to the layers below
it: added or replaced entries, and deletions expressed as "whiteout" marker files (see the OCI image
spec, "Image Layer Filesystem Changeset").

Paths in a layer are paths inside the container, so they are always resolved inside the rootfs: a
symlink is followed with the rootfs as "/" and ".." never goes above it (see ``_resolve``). Nothing
is ever written or deleted outside the rootfs, whatever the layers contain.

No privileges are needed: files are owned by the current user, device nodes are skipped and the
setuid/setgid/sticky bits are cleared.
"""

import json
import os
import shutil
import stat
import tarfile

from pipeline_migration.registry import (
    MEDIA_TYPE_DOCKER_IMAGE_LAYER_TAR,
    MEDIA_TYPE_DOCKER_IMAGE_LAYER_TAR_GZ,
    MEDIA_TYPE_DOCKER_IMAGE_MANIFEST_V2,
    MEDIA_TYPE_OCI_IMAGE_LAYER_V1_TAR,
    MEDIA_TYPE_OCI_IMAGE_LAYER_V1_TAR_GZ,
    MEDIA_TYPE_OCI_IMAGE_MANIFEST_V1,
)

IMAGE_MANIFEST_MEDIA_TYPES = {
    MEDIA_TYPE_OCI_IMAGE_MANIFEST_V1,
    MEDIA_TYPE_DOCKER_IMAGE_MANIFEST_V2,
}
# Compressions tarfile can read. zstd is not supported by Python 3.12, so the image is converted to
# gzip when it is pulled.
LAYER_MEDIA_TYPES = {
    MEDIA_TYPE_OCI_IMAGE_LAYER_V1_TAR,
    MEDIA_TYPE_OCI_IMAGE_LAYER_V1_TAR_GZ,
    MEDIA_TYPE_DOCKER_IMAGE_LAYER_TAR,
    MEDIA_TYPE_DOCKER_IMAGE_LAYER_TAR_GZ,
}

# ".wh.<name>" deletes <name> from the lower layers.
_WHITEOUT_PREFIX = ".wh."
# A directory containing this entry hides everything the lower layers put into it.
_OPAQUE_WHITEOUT = ".wh..wh..opq"
# Same limit as filepath-securejoin, which umoci and runc use for the same purpose.
_MAX_SYMLINKS = 255


def unpack_image(oci_dir: str, ref_name: str, rootfs: str) -> None:
    """Unpack the image tagged ``ref_name`` in the OCI layout ``oci_dir`` into ``rootfs``.

    ``rootfs`` must be an existing empty directory. Raises ``ValueError`` for unsupported or
    malformed images.
    """
    rootfs = os.path.realpath(rootfs)
    manifest = _read_manifest(oci_dir, ref_name)
    for layer in manifest["layers"]:
        if layer.get("mediaType") not in LAYER_MEDIA_TYPES:
            raise ValueError(f"Unsupported layer media type: {layer.get('mediaType')}")
        _apply_layer(_blob_path(oci_dir, layer["digest"]), rootfs)


def _read_manifest(oci_dir: str, ref_name: str) -> dict:
    # index.json lists the manifests of the layout. skopeo stores the tag of the copied image in
    # the org.opencontainers.image.ref.name annotation of its manifest descriptor.
    with open(os.path.join(oci_dir, "index.json")) as f:
        index = json.load(f)
    descriptors = [
        d
        for d in index.get("manifests", [])
        if d.get("annotations", {}).get("org.opencontainers.image.ref.name") == ref_name
    ]
    if len(descriptors) != 1:
        raise ValueError(f"Expected one manifest tagged {ref_name} in {oci_dir}")
    media_type = descriptors[0].get("mediaType")
    if media_type not in IMAGE_MANIFEST_MEDIA_TYPES:
        raise ValueError(f"Unsupported manifest media type: {media_type}")
    with open(_blob_path(oci_dir, descriptors[0]["digest"])) as f:
        return json.load(f)


def _blob_path(oci_dir: str, digest: str) -> str:
    """Return the path of a blob, which an OCI layout stores at blobs/<algorithm>/<encoded>."""
    algorithm, _, encoded = digest.partition(":")
    if not algorithm or not encoded or "/" in algorithm or "/" in encoded:
        raise ValueError(f"Invalid blob digest: {digest}")
    return os.path.join(oci_dir, "blobs", algorithm, encoded)


def _apply_layer(layer_path: str, rootfs: str) -> None:
    with tarfile.open(layer_path, "r:*") as tar:
        members = tar.getmembers()
        whiteouts = [m for m in members if _is_whiteout(m)]
        entries = [m for m in members if not _is_whiteout(m)]

        # 1. Whiteouts delete entries of the lower layers, so they are applied before anything of
        #    this layer is extracted, wherever they are in the tar.
        for member in whiteouts:
            _apply_whiteout(rootfs, _member_path(member))

        # 2. Directories of this layer replace lower non-directories (e.g. a symlink) before any
        #    entry is extracted. Otherwise an entry listed before its parent directory would be
        #    resolved through the old symlink and land in the wrong place. Parents go first.
        directories = sorted((m for m in entries if m.isdir()), key=_depth)
        for member in directories:
            _make_directory(rootfs, _member_path(member))

        # 3. Everything else, in tar order so the archive is read sequentially. A later entry for
        #    the same path replaces an earlier one.
        skipped_devices: set[str] = set()
        for member in entries:
            _extract(tar, member, rootfs, skipped_devices)


def _is_whiteout(member: tarfile.TarInfo) -> bool:
    return os.path.basename(_member_path(member)).startswith(_WHITEOUT_PREFIX)


def _member_path(member: tarfile.TarInfo) -> str:
    """Return the path of an entry relative to the rootfs, e.g. "usr/bin/bash"."""
    return os.path.normpath(member.name.lstrip("/"))


def _depth(member: tarfile.TarInfo) -> int:
    return len(_member_path(member).split("/"))


def _apply_whiteout(rootfs: str, path: str) -> None:
    parent_path, name = os.path.split(path)
    parent = _resolve(rootfs, parent_path)

    if name == _OPAQUE_WHITEOUT:
        if _is_real_dir(parent):
            for child in os.listdir(parent):
                _remove(os.path.join(parent, child))
        return

    if name.startswith(_WHITEOUT_PREFIX * 2):
        return  # Other .wh..wh.* names are aufs metadata and have no meaning in the rootfs

    deleted = name.removeprefix(_WHITEOUT_PREFIX)
    if deleted in ("", ".", ".."):
        raise ValueError(f"Invalid whiteout entry: {path}")
    _remove(os.path.join(parent, deleted))


def _make_directory(rootfs: str, path: str) -> None:
    target = _entry_path(rootfs, path)
    if target == rootfs:
        return
    _make_parent_dirs(rootfs, target)
    if not _is_real_dir(target):
        _remove(target)
        os.mkdir(target, 0o700)  # The final mode is set when the entry is extracted


def _extract(
    tar: tarfile.TarFile, member: tarfile.TarInfo, rootfs: str, skipped_devices: set[str]
) -> None:
    target = _entry_path(rootfs, _member_path(member))
    if target == rootfs:
        return  # The "./" entry: the rootfs directory itself
    relative_target = os.path.relpath(target, rootfs)

    if member.ischr() or member.isblk() or member.isfifo():
        # Creating device nodes needs CAP_MKNOD. crun creates the devices the container needs.
        skipped_devices.add(relative_target)
        return

    _make_parent_dirs(rootfs, target)
    # A directory merges with an existing directory, anything else replaces the existing entry
    if not (member.isdir() and _is_real_dir(target)):
        _remove(target)

    if member.islnk():
        _make_hard_link(rootfs, member, target, skipped_devices)
    else:
        # The member is renamed to the resolved path, so tarfile never follows a symlink itself
        resolved_member = member.replace(name=relative_target, deep=False)
        tar.extract(resolved_member, rootfs, filter=_extraction_filter)


def _make_hard_link(
    rootfs: str, member: tarfile.TarInfo, target: str, skipped_devices: set[str]
) -> None:
    # The link name is a path inside the container, resolved like any other entry path
    source = _entry_path(rootfs, os.path.normpath(member.linkname.lstrip("/")))
    if os.path.relpath(source, rootfs) in skipped_devices:
        skipped_devices.add(os.path.relpath(target, rootfs))
        return
    if not os.path.lexists(source) or _is_real_dir(source):
        raise ValueError(f"Hard link {member.name} points to a missing file {member.linkname}")
    # follow_symlinks=False: a hard link to a symlink links the symlink itself, never its target
    os.link(source, target, follow_symlinks=False)


def _extraction_filter(member: tarfile.TarInfo, dest_path: str) -> tarfile.TarInfo:
    # Refuses paths outside dest_path and clears setuid/setgid/sticky and group/other write bits
    member = tarfile.tar_filter(member, dest_path)
    # Owned by the current user, as if extracted with tar --no-same-owner
    attributes: dict = {"uid": None, "gid": None, "uname": None, "gname": None}
    if member.isdir() and member.mode is not None:
        # Owner access lets later layers and cache cleanup modify the directory
        attributes["mode"] = member.mode | stat.S_IRWXU
    return member.replace(**attributes, deep=False)


def _resolve(rootfs: str, path: str) -> str:
    """Return the host path of ``path`` resolved inside the rootfs, following every symlink.

    Resolution works like the kernel does inside the container: a symlink with an absolute target
    is resolved from the rootfs, and ".." stops at the rootfs. Components that do not exist are
    kept as they are. The result is therefore always inside the rootfs.
    """
    pending = path.split("/")
    resolved: list[str] = []
    followed_symlinks = 0

    while pending:
        part = pending.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            if resolved:
                resolved.pop()
            continue

        candidate = os.path.join(rootfs, *resolved, part)
        if not os.path.islink(candidate):
            resolved.append(part)
            continue

        followed_symlinks += 1
        if followed_symlinks > _MAX_SYMLINKS:
            raise ValueError(f"Too many levels of symbolic links in {path}")
        link_target = os.readlink(candidate)
        if link_target.startswith("/"):
            resolved = []
        pending = link_target.split("/") + pending

    return os.path.join(rootfs, *resolved)


def _entry_path(rootfs: str, path: str) -> str:
    """Return the host path of the entry ``path``.

    Only the parent directory is resolved. The entry itself is not followed, so replacing or
    removing a symlink affects the symlink and never its target.
    """
    parent, name = os.path.split(path)
    if name in ("", ".", ".."):
        return _resolve(rootfs, path)  # The path names a directory, such as "." or "a/.."
    return os.path.join(_resolve(rootfs, parent), name)


def _make_parent_dirs(rootfs: str, path: str) -> None:
    """Create the missing parent directories of ``path``, replacing parents that are files."""
    current = rootfs
    for part in os.path.relpath(os.path.dirname(path), rootfs).split(os.sep):
        if part == ".":
            continue
        current = os.path.join(current, part)
        if not _is_real_dir(current):
            _remove(current)
            os.mkdir(current, 0o755)


def _is_real_dir(path: str) -> bool:
    return os.path.isdir(path) and not os.path.islink(path)


def _remove(path: str) -> None:
    """Remove a file, symlink or directory tree. Symlinks are removed, never followed."""
    if _is_real_dir(path):
        shutil.rmtree(path)
    elif os.path.lexists(path):
        os.unlink(path)
