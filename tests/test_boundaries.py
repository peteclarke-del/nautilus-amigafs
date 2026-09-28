from __future__ import annotations

from pathlib import Path

import pytest

from amigafs.core.image import ROOT_INODE, AmigaImage, amiga_name, display_name
from amigafs.errors import AmigaFSError, FilenameTooLongError
from tests.image_fixture import create_empty_floppy, create_hard_disc


def test_deep_tree_indexes_at_configured_depth_boundary(tmp_path: Path) -> None:
    path = create_hard_disc(tmp_path, capacity="4MB", partitions=1, files=())
    with AmigaImage.open(path, writable=True, commit_validation_bytes=0) as image:
        parent = image.children[ROOT_INODE][0]
        for level in range(64):
            parent = image.make_directory(parent, f"L{level:02d}".encode()).inode
    with AmigaImage.open(path, max_depth=64) as image:
        assert len(image.nodes) == 66
    with pytest.raises(AmigaFSError, match="exceeds 63 levels"):
        AmigaImage.open(path, max_depth=63)


def test_a_directory_holds_far_more_entries_than_one_hash_table(tmp_path: Path) -> None:
    path = create_empty_floppy(tmp_path, filesystem="FFS")
    with AmigaImage.open(path, writable=True, commit_validation_bytes=0) as image:
        for number in range(150):
            image.create_file(ROOT_INODE, f"Entry{number:03d}".encode())
    with AmigaImage.open(path) as image:
        assert len(image.children[ROOT_INODE]) == 150
        names = [image.nodes[inode].name for inode in image.children[ROOT_INODE]]
        assert names == sorted(names)
        assert image.integrity_report().findings == ()


def test_boundary_names_and_latin_1_round_trip(tmp_path: Path) -> None:
    path = create_empty_floppy(tmp_path, filesystem="FFS-INTL")
    names = ["x", "Ärger über Größe", "a.b.c", "trailing.", "A" * 30, "café ©1992", "in\xa0side"]
    with AmigaImage.open(path, writable=True) as image:
        for name in names:
            image.create_file(ROOT_INODE, name.encode("utf-8"))
        with pytest.raises(FileExistsError):
            image.create_file(ROOT_INODE, "ÄRGER ÜBER GRÖSSE".replace("SS", "ß").encode())
    with AmigaImage.open(path) as image:
        stored = {image.nodes[inode].name.decode("utf-8") for inode in image.children[ROOT_INODE]}
        assert stored == set(names)


@pytest.mark.parametrize(
    "name",
    ["", "a/b", "a:b", "back\\slash", "tab\there", "nul\x00", "snow☃", "B" * 31],
)
def test_invalid_names_are_rejected_without_changing_the_volume(tmp_path: Path, name: str) -> None:
    path = create_empty_floppy(tmp_path)
    before = path.read_bytes()
    with AmigaImage.open(path, writable=True) as image:
        with pytest.raises((ValueError, FilenameTooLongError)):
            image.create_file(ROOT_INODE, name.encode("utf-8"))
        with pytest.raises((ValueError, FilenameTooLongError)):
            image.make_directory(ROOT_INODE, name.encode("utf-8"))
        assert image.children[ROOT_INODE] == ()
    assert path.read_bytes() == before


def test_names_that_are_not_utf8_are_rejected(tmp_path: Path) -> None:
    path = create_empty_floppy(tmp_path)
    with AmigaImage.open(path, writable=True) as image, pytest.raises(ValueError):
        image.create_file(ROOT_INODE, b"\xff\xfe")


def test_every_non_posix_display_mapping_is_unambiguous_and_reversible() -> None:
    hostile = ["a/b", ".", "..", "bell\x07", "del\x7f", "nul\x00x", "plain", "café"]
    shown = [display_name(name) for name in hostile]
    assert len(set(shown)) == len(hostile)
    for original, encoded in zip(hostile, shown, strict=True):
        assert b"/" not in encoded
        assert encoded not in {b".", b".."}
        assert amiga_name(encoded) == original
    assert display_name("a/b") == "a∕b".encode()
    assert display_name("bell\x07") == "bell␇".encode()
