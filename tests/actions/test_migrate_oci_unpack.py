import json
import os
import stat
import tarfile
from pathlib import Path

import pytest

from pipeline_migration.actions.migrate.oci_unpack import unpack_image
from tests.utils import (
    TarEntry,
    dir_entry,
    file_entry,
    hardlink_entry,
    symlink_entry,
    write_oci_layout,
)


@pytest.fixture
def unpack(tmp_path):
    """Unpack the given layers into a fresh rootfs and return the rootfs path."""

    def _unpack(*layers: list[TarEntry]) -> Path:
        oci_dir = tmp_path / "oci"
        rootfs = tmp_path / "rootfs"
        rootfs.mkdir()
        write_oci_layout(oci_dir, list(layers))
        unpack_image(str(oci_dir), "latest", str(rootfs))
        return rootfs

    return _unpack


@pytest.fixture
def host_dir(tmp_path) -> Path:
    """A directory outside the rootfs that layers must never touch."""
    path = tmp_path / "host"
    path.mkdir()
    (path / "secret").write_text("host secret")
    return path


class TestLayers:

    def test_files_directories_and_links(self, unpack):
        rootfs = unpack(
            [
                dir_entry("usr"),
                dir_entry("usr/bin"),
                file_entry("usr/bin/tool", b"binary", mode=0o755),
                symlink_entry("bin", "usr/bin"),
                hardlink_entry("usr/bin/tool-alias", "usr/bin/tool"),
            ]
        )
        assert (rootfs / "usr/bin/tool").read_bytes() == b"binary"
        assert os.readlink(rootfs / "bin") == "usr/bin"
        assert (rootfs / "usr/bin/tool-alias").stat().st_ino == (
            rootfs / "usr/bin/tool"
        ).stat().st_ino

    def test_upper_layer_replaces_entries(self, unpack):
        rootfs = unpack(
            [file_entry("etc/conf", b"old"), dir_entry("data"), file_entry("data/a")],
            [file_entry("etc/conf", b"new"), file_entry("data", b"now a file")],
        )
        assert (rootfs / "etc/conf").read_bytes() == b"new"
        assert (rootfs / "data").read_bytes() == b"now a file"

    def test_whiteout_deletes_lower_entry(self, unpack):
        rootfs = unpack(
            [
                file_entry("etc/keep"),
                file_entry("etc/gone"),
                dir_entry("cache"),
                file_entry("cache/x"),
            ],
            [file_entry("etc/.wh.gone"), file_entry(".wh.cache")],
        )
        assert sorted(os.listdir(rootfs)) == ["etc"]
        assert os.listdir(rootfs / "etc") == ["keep"]

    def test_whiteout_and_readd_in_same_layer(self, unpack):
        rootfs = unpack(
            [file_entry("a", b"lower")], [file_entry("a", b"upper"), file_entry(".wh.a")]
        )
        assert (rootfs / "a").read_bytes() == b"upper"

    def test_opaque_whiteout_hides_lower_directory_content(self, unpack):
        rootfs = unpack(
            [dir_entry("d"), file_entry("d/lower")],
            [dir_entry("d"), file_entry("d/upper"), file_entry("d/.wh..wh..opq")],
        )
        assert os.listdir(rootfs / "d") == ["upper"]

    def test_directory_entry_after_its_children(self, unpack):
        """A directory replacing a lower symlink must not redirect children listed before it."""
        rootfs = unpack(
            [
                dir_entry("usr"),
                dir_entry("usr/lib"),
                file_entry("usr/lib/foo", b"keep"),
                symlink_entry("lib", "usr/lib"),
            ],
            [file_entry("lib/foo", b"new"), dir_entry("lib")],
        )
        assert (rootfs / "usr/lib/foo").read_bytes() == b"keep"
        assert not (rootfs / "lib").is_symlink()
        assert (rootfs / "lib/foo").read_bytes() == b"new"

    def test_path_through_absolute_directory_symlink(self, unpack):
        rootfs = unpack(
            [dir_entry("run"), dir_entry("var"), symlink_entry("var/run", "/run")],
            [file_entry("var/run/pid", b"1"), file_entry("var/run/.wh.missing")],
        )
        assert (rootfs / "run/pid").read_bytes() == b"1"
        assert os.readlink(rootfs / "var/run") == "/run"

    def test_absolute_symlink_entry_is_kept(self, unpack):
        rootfs = unpack([symlink_entry("usr/bin/sh", "/bin/bash")])
        assert os.readlink(rootfs / "usr/bin/sh") == "/bin/bash"

    @pytest.mark.parametrize(
        "first",
        [dir_entry("x"), symlink_entry("x", "target"), file_entry("x", b"old", mode=0o444)],
        ids=["dir", "symlink", "read-only-file"],
    )
    def test_duplicate_entry_in_same_layer(self, unpack, first):
        rootfs = unpack([file_entry("target", b"untouched")], [first, file_entry("x", b"last")])
        assert not (rootfs / "x").is_symlink()
        assert (rootfs / "x").read_bytes() == b"last"
        assert (rootfs / "target").read_bytes() == b"untouched"

    def test_child_of_lower_file(self, unpack):
        rootfs = unpack([file_entry("x")], [file_entry("x/y", b"child")])
        assert (rootfs / "x/y").read_bytes() == b"child"

    def test_hard_link_with_absolute_link_name(self, unpack):
        rootfs = unpack([file_entry("a", b"data"), hardlink_entry("b", "/a")])
        assert (rootfs / "b").stat().st_ino == (rootfs / "a").stat().st_ino

    def test_hard_link_to_symlink_links_the_symlink(self, unpack):
        rootfs = unpack([symlink_entry("s", "/etc/passwd"), hardlink_entry("h", "s")])
        assert os.readlink(rootfs / "h") == "/etc/passwd"

    def test_device_nodes_and_links_to_them_are_skipped(self, unpack):
        rootfs = unpack(
            [
                TarEntry("dev/null", tarfile.CHRTYPE),
                TarEntry("fifo", tarfile.FIFOTYPE),
                hardlink_entry("null-link", "dev/null"),
                file_entry("ok"),
            ]
        )
        assert os.listdir(rootfs) == ["ok"]

    def test_privileged_mode_bits_are_cleared(self, unpack):
        rootfs = unpack([file_entry("suid", mode=0o4755), dir_entry("ro", mode=0o555)])
        assert stat.S_IMODE((rootfs / "suid").stat().st_mode) == 0o755
        assert stat.S_IMODE((rootfs / "ro").stat().st_mode) & stat.S_IRWXU == stat.S_IRWXU


class TestStaysInsideRootfs:

    def test_hard_link_through_absolute_symlink(self, unpack, host_dir):
        with pytest.raises(ValueError, match="points to a missing file"):
            unpack([symlink_entry("x", str(host_dir)), hardlink_entry("y", "x/secret")])
        assert (host_dir / "secret").stat().st_nlink == 1

    def test_hard_link_through_escaping_relative_symlink(self, unpack, host_dir):
        escape = "../" * 20 + str(host_dir).lstrip("/")
        with pytest.raises(ValueError, match="points to a missing file"):
            unpack(
                [
                    symlink_entry("s", escape),
                    hardlink_entry("h", "s/secret"),
                    file_entry("h", b"overwritten"),
                ]
            )
        assert (host_dir / "secret").read_text() == "host secret"

    def test_file_through_escaping_symlink(self, unpack, host_dir):
        rootfs = unpack(
            [symlink_entry("s", "../" * 20 + str(host_dir).lstrip("/"))],
            [file_entry("s/secret", b"overwritten")],
        )
        assert (host_dir / "secret").read_text() == "host secret"
        assert (rootfs / str(host_dir).lstrip("/") / "secret").read_bytes() == b"overwritten"

    def test_dot_dot_entry_names_stay_inside(self, unpack, tmp_path):
        rootfs = unpack([file_entry("../../outside", b"x")])
        assert (rootfs / "outside").read_bytes() == b"x"
        assert not (tmp_path / "outside").exists()

    @pytest.mark.parametrize("name", [".wh...", ".wh..", "d/.wh.."])
    def test_invalid_whiteout_is_rejected(self, unpack, tmp_path, name):
        (tmp_path / "sibling").write_text("keep")
        with pytest.raises(ValueError, match="Invalid whiteout"):
            unpack([dir_entry("d")], [file_entry(name)])
        assert (tmp_path / "sibling").read_text() == "keep"

    def test_symlink_loop(self, unpack):
        with pytest.raises(ValueError, match="Too many levels"):
            unpack([symlink_entry("a", "b"), symlink_entry("b", "a")], [file_entry("a/x")])


class TestImageLayout:

    def test_unsupported_layer_media_type(self, tmp_path):
        write_oci_layout(
            tmp_path / "oci",
            [[file_entry("a")]],
            layer_media_type="application/vnd.oci.image.layer.v1.tar+zstd",
        )
        with pytest.raises(ValueError, match="Unsupported layer media type"):
            unpack_image(str(tmp_path / "oci"), "latest", str(tmp_path))

    def test_missing_ref_name(self, tmp_path):
        write_oci_layout(tmp_path / "oci", [[file_entry("a")]], ref_name="other")
        with pytest.raises(ValueError, match="Expected one manifest tagged latest"):
            unpack_image(str(tmp_path / "oci"), "latest", str(tmp_path))

    def test_invalid_digest(self, tmp_path):
        oci_dir = tmp_path / "oci"
        write_oci_layout(oci_dir, [[file_entry("a")]])
        index = json.loads((oci_dir / "index.json").read_text())
        index["manifests"][0]["digest"] = "sha256:../../index"
        (oci_dir / "index.json").write_text(json.dumps(index))
        with pytest.raises(ValueError, match="Invalid blob digest"):
            unpack_image(str(oci_dir), "latest", str(tmp_path))
