from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from amigafs._vendor.amiganut.file import FIBF_ARCHIVE, FIBF_DELETE, FIBF_WRITE, AmigaMeta
from amigafs.core.image import ROOT_INODE, VIRTUAL_VOLUME, AmigaImage
from amigafs.errors import AmigaFSError, DiscFullError
from amigafs.recovery import pending_recovery
from tests.image_fixture import (
    STANDARD_FILES,
    create_empty_floppy,
    create_floppy,
    create_hard_disc,
    create_hardfile,
    tree,
)

FLOPPY_FILESYSTEMS = (
    "OFS",
    "FFS",
    "OFS-INTL",
    "FFS-INTL",
    "OFS-DC",
    "FFS-DC",
    "OFS-LNFS",
    "FFS-LNFS",
)


def _expected(prefix: str) -> dict[str, int | None]:
    expected: dict[str, int | None] = {}
    for path, data, _protection, _comment in STANDARD_FILES:
        parts = path.split("/")
        for depth in range(1, len(parts)):
            expected[prefix + "/".join(parts[:depth])] = None
        expected[prefix + path] = len(data)
    return expected


@pytest.mark.parametrize("filesystem", FLOPPY_FILESYSTEMS)
def test_indexes_and_reads_every_floppy_filesystem(tmp_path: Path, filesystem: str) -> None:
    path = create_floppy(tmp_path, filesystem=filesystem)
    with AmigaImage.open(path) as image:
        assert image.layout == "single"
        assert tree(image) == _expected("Workbench:")
        for name, data, protection, comment in STANDARD_FILES:
            node = image.node_at_path(name)
            assert image.read(node.inode, 0, node.size + 10) == data
            assert node.protection == protection
            assert node.comment == comment
            assert node.mtime_ns is not None


def test_high_density_floppy_is_indexed(tmp_path: Path) -> None:
    path = create_floppy(tmp_path, density="hd", filesystem="FFS")
    assert path.stat().st_size == 1_802_240
    with AmigaImage.open(path) as image:
        assert image.read(image.node_at_path("Docs/Deep/Nested/Note").inode, 0, 99) == b"nested"


@pytest.mark.parametrize("filesystem", ["FFS-INTL", "PFS3", "SFS"])
def test_partitioned_hard_disc_presents_a_folder_per_partition(
    tmp_path: Path, filesystem: str
) -> None:
    path = create_hard_disc(tmp_path, filesystem=filesystem, capacity="12MB", partitions=2)
    with AmigaImage.open(path) as image:
        assert image.layout == "rdb"
        root = image.nodes[ROOT_INODE]
        assert root.volume == VIRTUAL_VOLUME
        names = [image.nodes[inode].name for inode in image.children[ROOT_INODE]]
        assert names == [b"DH0", b"DH1"]
        listing = tree(image)
        visible = {name: size for name, size in listing.items() if "/." not in name}
        expected = {"DH0:": None, "DH1:": None, **_expected("DH0:"), **_expected("DH1:")}
        # SFS keeps a hidden recycled drawer of its own in every volume.
        assert {key: value for key, value in visible.items() if ".recycled" not in key} == expected
        assert image.read(image.node_at_path("DH1:C/List").inode, 256, 4) == bytes(range(4))
        assert image.node_at_path("dh0:docs/readme").amiga_path == "DH0:Docs/ReadMe"


def test_hardfile_without_a_partition_table_is_one_volume(tmp_path: Path) -> None:
    path = create_hardfile(tmp_path)
    with AmigaImage.open(path) as image:
        assert image.layout == "single"
        assert tree(image) == _expected("Hardfile:")


def test_lookup_is_case_insensitive(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    with AmigaImage.open(path) as image:
        docs = image.lookup(ROOT_INODE, b"DOCS")
        assert docs is not None and docs.name == b"Docs"
        assert image.lookup(docs.inode, b"readme") is not None
        assert image.lookup(docs.inode, b"missing") is None
        assert image.lookup(docs.inode, b"\xff\xfe") is None
        with pytest.raises(FileNotFoundError):
            image.node_at_path("Docs/Nothing")


def test_reports_filesystem_capacity(tmp_path: Path) -> None:
    # OFS keeps a 24-byte header in every data block; FFS uses the whole block.
    for filesystem, usable in (("OFS", 488), ("FFS", 512)):
        path = create_empty_floppy(tmp_path, name=filesystem, filesystem=filesystem)
        with AmigaImage.open(path) as image:
            assert image.total_bytes == 1758 * usable
            assert 0 < image.free_bytes < image.total_bytes


def test_open_is_read_only(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    before = path.read_bytes()
    with AmigaImage.open(path) as image:
        assert not image.writable
        with pytest.raises(PermissionError):
            image.create_file(ROOT_INODE, b"New")
        with pytest.raises(PermissionError):
            image.replace_file(image.node_at_path("C/List").inode, b"x")
    assert path.read_bytes() == before
    assert pending_recovery(path) is None


def test_read_only_storage_browses_safely_and_refuses_writable_open(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    path.chmod(0o444)
    with AmigaImage.open(path) as image:
        assert image.lookup(ROOT_INODE, b"C") is not None
    if os.geteuid() != 0:
        with pytest.raises(AmigaFSError, match="read-only storage"):
            AmigaImage.open(path, writable=True)
    assert pending_recovery(path) is None


def test_writable_open_persists_replacement_and_locks_the_image(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    with AmigaImage.open(path, writable=True) as image:
        assert pending_recovery(path) is not None
        with pytest.raises(AmigaFSError, match="another AmigaFS process"):
            AmigaImage.open(path)
        node = image.node_at_path("Docs/ReadMe")
        image.replace_file(node.inode, b"replaced")
        assert image.read(node.inode, 0, 99) == b"replaced"
    assert pending_recovery(path) is None
    with AmigaImage.open(path) as image:
        node = image.node_at_path("Docs/ReadMe")
        assert image.read(node.inode, 0, 99) == b"replaced"
        assert node.comment == "Introduction"


@pytest.mark.parametrize("filesystem", ["FFS", "OFS-DC", "FFS-LNFS", "PFS3", "SFS"])
def test_writable_namespace_operations_persist(tmp_path: Path, filesystem: str) -> None:
    if filesystem in {"PFS3", "SFS"}:
        path = create_hard_disc(tmp_path, filesystem=filesystem, capacity="8MB", partitions=1)
        prefix = "DH0:"
    else:
        path = create_floppy(tmp_path, filesystem=filesystem)
        prefix = ""
    with AmigaImage.open(path, writable=True) as image:
        top = image.node_at_path(prefix).inode
        drawer = image.make_directory(top, b"Projects")
        created = image.create_file(drawer.inode, b"Draft")
        image.replace_file(created.inode, b"draft text")
        image.rename(drawer.inode, b"Draft", top, b"Final")
        image.remove(top, b"Empty", directory=False)
        image.remove(top, b"Projects", directory=True)
        with pytest.raises(FileExistsError):
            image.create_file(top, b"final")
        with pytest.raises(OSError) as refused:
            image.remove(top, b"Docs", directory=True)
        assert refused.value.errno == errno.ENOTEMPTY
        with pytest.raises(IsADirectoryError):
            image.remove(top, b"Docs", directory=False)
        with pytest.raises(NotADirectoryError):
            image.remove(top, b"Final", directory=True)
    with AmigaImage.open(path) as image:
        listing = tree(image)
        assert listing[f"{'DH0:' if prefix else 'Workbench:'}Final"] == 10
        assert not any(name.endswith(("Projects", ":Empty")) for name in listing)
        assert image.integrity_report().findings == ()


def test_rename_replaces_existing_file_like_atomic_editor_save(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    with AmigaImage.open(path, writable=True) as image:
        docs = image.node_at_path("Docs").inode
        temporary = image.create_file(docs, b"ReadMe.tmp")
        image.replace_file(temporary.inode, b"saved by an editor")
        renamed = image.rename(docs, b"ReadMe.tmp", docs, b"ReadMe")
        assert renamed.inode == temporary.inode
        assert image.read(renamed.inode, 0, 99) == b"saved by an editor"
        assert image.lookup(docs, b"ReadMe.tmp") is None
    with AmigaImage.open(path) as image:
        assert image.read(image.node_at_path("Docs/ReadMe").inode, 0, 99) == b"saved by an editor"


def test_rename_can_change_only_the_case_of_a_name(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    with AmigaImage.open(path, writable=True) as image:
        docs = image.node_at_path("Docs").inode
        image.rename(docs, b"ReadMe", docs, b"README")
    with AmigaImage.open(path) as image:
        assert image.node_at_path("Docs/readme").name == b"README"


def test_directory_move_carries_its_descendants(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    with AmigaImage.open(path, writable=True) as image:
        docs = image.node_at_path("Docs").inode
        image.rename(docs, b"Deep", ROOT_INODE, b"Moved")
        assert image.node_at_path("Moved/Nested/Note").amiga_path == "Workbench:Moved/Nested/Note"
        with pytest.raises(ValueError, match="inside itself"):
            image.rename(ROOT_INODE, b"Moved", image.node_at_path("Moved/Nested").inode, b"Loop")
    with AmigaImage.open(path) as image:
        assert image.read(image.node_at_path("Moved/Nested/Note").inode, 0, 9) == b"nested"
        assert image.integrity_report().findings == ()


def test_partition_folders_cannot_be_changed_and_renames_stay_within_one(tmp_path: Path) -> None:
    path = create_hard_disc(tmp_path, capacity="8MB")
    with AmigaImage.open(path, writable=True) as image:
        first, second = image.children[ROOT_INODE]
        with pytest.raises(PermissionError):
            image.create_file(ROOT_INODE, b"Loose")
        with pytest.raises(PermissionError):
            image.make_directory(ROOT_INODE, b"DH9")
        with pytest.raises(PermissionError):
            image.remove(ROOT_INODE, b"DH0", directory=True)
        with pytest.raises(PermissionError):
            image.rename(ROOT_INODE, b"DH0", ROOT_INODE, b"Renamed")
        with pytest.raises(OSError) as crossed:
            image.rename(first, b"Empty", second, b"Moved")
        assert crossed.value.errno == errno.EXDEV
        # None of the refusals poisoned the session.
        image.create_file(first, b"StillWorks")
    with AmigaImage.open(path) as image:
        assert image.node_at_path("DH0:StillWorks").size == 0


def test_external_change_blocks_further_mutations(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    image = AmigaImage.open(path, writable=True)
    try:
        image.create_file(ROOT_INODE, b"Before")
        with path.open("r+b") as other:
            other.seek(0)
            other.write(b"DOS\x01")
        os.utime(path, ns=(5, 5))
        with pytest.raises(AmigaFSError, match="changed outside AmigaFS"):
            image.create_file(ROOT_INODE, b"After")
        with pytest.raises(AmigaFSError, match="session has failed"):
            image.create_file(ROOT_INODE, b"Again")
    finally:
        image.close()
    # A failed session never discards the means of undoing it.
    assert pending_recovery(path) is not None


def test_long_filename_volume_accepts_107_characters(tmp_path: Path) -> None:
    ordinary = create_empty_floppy(tmp_path, name="ordinary", filesystem="FFS")
    long_names = create_empty_floppy(tmp_path, name="long", filesystem="FFS-LNFS")
    with AmigaImage.open(ordinary, writable=True) as image:
        assert image.name_limit(0) == 30
        image.create_file(ROOT_INODE, b"a" * 30)
        with pytest.raises(ValueError):
            image.create_file(ROOT_INODE, b"b" * 31)
    with AmigaImage.open(long_names, writable=True) as image:
        assert image.name_limit(0) == 107
        image.create_file(ROOT_INODE, b"c" * 107)
        with pytest.raises(ValueError):
            image.create_file(ROOT_INODE, b"d" * 108)
    with AmigaImage.open(long_names) as image:
        assert image.lookup(ROOT_INODE, b"c" * 107) is not None


def test_replacing_contents_keeps_metadata_stamps_the_file_and_clears_archive(
    tmp_path: Path,
) -> None:
    path = create_empty_floppy(tmp_path, filesystem="FFS")
    with AmigaImage.open(path, writable=True) as image:
        node = image.import_file(
            ROOT_INODE,
            b"Letter",
            b"old",
            AmigaMeta(protection=FIBF_ARCHIVE | 0x40, comment="Keep me"),
        )
        image.set_metadata(node.inode, mtime_ns=400_000_000 * 1_000_000_000)
        before = image.nodes[node.inode]
        assert before.mtime_ns == 400_000_000 * 1_000_000_000
        image.replace_file(node.inode, b"new contents")
        after = image.nodes[node.inode]
        assert after.comment == "Keep me"
        assert after.protection == 0x40
        assert after.mtime_ns is not None and after.mtime_ns > before.mtime_ns
    with AmigaImage.open(path) as image:
        stored = image.node_at_path("Letter")
        assert (stored.protection, stored.comment, stored.size) == (0x40, "Keep me", 12)


def test_protection_bits_govern_writing_and_deleting(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    with AmigaImage.open(path, writable=True) as image:
        node = image.node_at_path("C/List")
        parent = node.parent_inode
        image.set_metadata(node.inode, protection=FIBF_DELETE)
        # Delete-protected content can still be rewritten, and keeps its protection.
        image.replace_file(node.inode, b"rewritten")
        assert image.nodes[node.inode].protection == FIBF_DELETE
        with pytest.raises(PermissionError, match="delete-protected"):
            image.remove(parent, b"List", directory=False)
        image.set_metadata(node.inode, protection=FIBF_WRITE)
        with pytest.raises(PermissionError, match="write-protected"):
            image.replace_file(node.inode, b"refused")
        image.remove(parent, b"List", directory=False)
    with AmigaImage.open(path) as image:
        assert image.lookup(image.node_at_path("C").inode, b"List") is None
        assert image.integrity_report().findings == ()


def test_metadata_is_validated_before_anything_is_written(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    before = path.read_bytes()
    with AmigaImage.open(path, writable=True) as image:
        node = image.node_at_path("C/List")
        with pytest.raises(ValueError):
            image.set_metadata(node.inode, comment="x" * 80)
        with pytest.raises(ValueError):
            image.set_metadata(node.inode, comment="snowman ☃")
        with pytest.raises(ValueError):
            image.set_metadata(node.inode, comment="line\nbreak")
        with pytest.raises(ValueError):
            image.set_metadata(node.inode, protection=1 << 32)
        with pytest.raises(PermissionError):
            image.set_metadata(ROOT_INODE, comment="root")
    assert path.read_bytes() == before


def test_oversized_overwrite_is_rejected_before_freeing_original_file(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    with AmigaImage.open(path, writable=True) as image:
        node = image.node_at_path("C/List")
        original = image.read(node.inode, 0, node.size)
        with pytest.raises(DiscFullError):
            image.preflight_file_size(node.inode, 2_000_000)
        with pytest.raises(DiscFullError):
            image.replace_file(node.inode, bytes(2_000_000))
        with pytest.raises(OSError) as too_big:
            image.preflight_file_size(node.inode, 1 << 32)
        assert too_big.value.errno == errno.EFBIG
        assert image.read(node.inode, 0, node.size) == original
        image.replace_file(node.inode, b"still writable")
    with AmigaImage.open(path) as image:
        assert image.integrity_report().findings == ()


def test_filling_the_volume_reports_no_space_and_leaves_it_valid(tmp_path: Path) -> None:
    path = create_empty_floppy(tmp_path, filesystem="FFS")
    with AmigaImage.open(path, writable=True) as image:
        node = image.create_file(ROOT_INODE, b"Filler")
        # The free-byte figure ignores the file's own header and extension blocks.
        with pytest.raises(DiscFullError):
            image.replace_file(node.inode, bytes(image.free_bytes))
        assert image.read(node.inode, 0, 1) == b""
        image.replace_file(node.inode, bytes(image.free_bytes - 40 * 512))
        assert image.free_bytes < 40 * 512
        with pytest.raises(DiscFullError):
            image.import_file(ROOT_INODE, b"Overflow", bytes(41 * 512), AmigaMeta())
    with AmigaImage.open(path) as image:
        assert image.integrity_report().findings == ()


def test_large_uncached_reads_fetch_only_the_requested_range(tmp_path: Path) -> None:
    payload = bytes(range(256)) * 600
    for filesystem in ("FFS-INTL", "PFS3", "SFS", "OFS"):
        directory = tmp_path / filesystem
        directory.mkdir()
        path = create_hard_disc(
            directory,
            filesystem=filesystem,
            capacity="8MB",
            partitions=1,
            files=(("Big", payload, 0, ""),),
        )
        with AmigaImage.open(path, cache_bytes=4096) as image:
            node = image.node_at_path("DH0:Big")
            assert image.uses_ranged_reads(node.inode)
            calls: list[str] = []
            mount = image.mount_for(0)

            def counted(
                name: str, *, whole: Any = mount.read_bytes, seen: list[str] = calls
            ) -> bytes:
                seen.append(name)
                return bytes(whole(name))

            mount.read_bytes = counted
            for offset, size in ((0, 10), (511, 3), (70_000, 9_000), (len(payload) - 5, 50)):
                assert image.read(node.inode, offset, size) == payload[offset : offset + size]
            assert image.read(node.inode, len(payload), 10) == b""
            # OFS data blocks carry their own headers, so OFS falls back to a whole read.
            assert bool(calls) == (filesystem == "OFS")


def test_small_files_are_served_from_the_bounded_cache(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    with AmigaImage.open(path, cache_bytes=3500) as image:
        first = image.node_at_path("C/List")
        second = image.node_at_path("Docs/ReadMe")
        assert not image.uses_ranged_reads(first.inode)
        image.read(first.inode, 0, 1)
        image.read(second.inode, 0, 1)
        assert image._cache_size <= 3500
        assert first.inode not in image._cache
        assert image.read(first.inode, 5, 3) == bytes((5, 6, 7))


def test_timestamps_are_presented_as_local_wall_clock_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time
    from datetime import UTC, datetime

    from amigafs.core.image import datestamp_from_ns, datestamp_ns

    noon = datetime(1993, 7, 1, 12, 0, tzinfo=UTC)
    assert datestamp_ns(noon) == int(noon.timestamp()) * 1_000_000_000
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    shifted = datestamp_ns(noon)
    assert shifted is not None
    # Noon on the Amiga is shown as noon in Berlin, two hours before noon UTC.
    assert shifted == (int(noon.timestamp()) - 7200) * 1_000_000_000
    assert datestamp_from_ns(shifted) == noon
    assert datestamp_from_ns(0).year == 1978
    assert datestamp_ns(None) is None


def test_mode_bits_are_not_part_of_the_index(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    with AmigaImage.open(path) as image:
        script = image.node_at_path("S/Startup-Sequence")
        assert script.protection == 0x40
        assert not script.locked
        assert stat.S_ISREG(stat.S_IFREG)


def test_volume_can_be_relabelled(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    with AmigaImage.open(path, writable=True) as image:
        image.set_volume_title(0, "Renamed")
        assert image.node_at_path("C/List").amiga_path == "Renamed:C/List"
        with pytest.raises(ValueError):
            image.set_volume_title(0, "bad:name")
        with pytest.raises(ValueError):
            image.set_volume_title(0, "x" * 31)
    with AmigaImage.open(path) as image:
        assert image.volume_title(0) == "Renamed"


def test_hostile_tree_limits_are_enforced(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    with pytest.raises(AmigaFSError, match="more than 3 entries"):
        AmigaImage.open(path, max_nodes=3)
    with pytest.raises(AmigaFSError, match="exceeds 1 levels"):
        AmigaImage.open(path, max_depth=1)
    assert pending_recovery(path) is None
