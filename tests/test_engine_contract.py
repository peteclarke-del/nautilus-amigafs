"""The behaviour AmigaFS relies on from its vendored engine.

These tests pin the public calls, the private internals that ranged reads and
bitmap repair use, and the two local patches. They are the gate for refreshing
the vendored snapshot: if one fails after a refresh, the adapter named in the
test has to be reviewed before the new engine can ship.
"""

from __future__ import annotations

import json
import random
import subprocess
import sys
from pathlib import Path

import pytest

from amigafs._vendor import amiganut
from amigafs._vendor.amiganut.errors import AmiganutError, DataError
from amigafs._vendor.amiganut.file import (
    FIBF_DELETE,
    FIBF_WRITE,
    Access,
    AmigaMeta,
    format_access_text,
    parse_access_text,
)
from amigafs._vendor.amiganut.filesystem import AmigaDOSMount, PFS3Mount, SFSMount
from amigafs._vendor.amiganut.filesystem.blocks import BlockReader
from amigafs.core.blockio import ImageStore, StoreReader
from amigafs.core.image import AmigaImage
from amigafs.core.media import open_media, open_volume
from tests.image_fixture import create_floppy, create_hard_disc

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VENDOR_ROOT = PROJECT_ROOT / "src" / "amigafs" / "_vendor"


def test_vendored_snapshot_matches_its_manifest() -> None:
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "tools" / "vendor_amiganut.py"), "--check"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    manifest = json.loads((VENDOR_ROOT / "VENDORED.json").read_text(encoding="utf-8"))
    assert manifest["amiganut_version"] == amiganut.__version__
    assert manifest["license"] == "MIT"
    assert len(manifest["source_commit"]) == 40
    assert manifest["patches"] == sorted(manifest["patches"])
    for patch in manifest["patches"]:
        assert (VENDOR_ROOT / "patches" / patch).is_file()


def test_engine_never_opens_the_medium_by_name() -> None:
    """Patch 0001: every second view of a medium goes through ``reader.reopen``."""

    offenders = []
    for source in sorted((VENDOR_ROOT / "amiganut").rglob("*.py")):
        relative = source.relative_to(VENDOR_ROOT).as_posix()
        for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if "reader.path" in stripped or ".path.read_bytes()" in stripped:
                offenders.append(f"{relative}:{number}: {stripped}")
            if "BlockReader(" in stripped and relative not in {
                "amiganut/filesystem/blocks.py",
                "amiganut/filesystem/__init__.py",
                # Creates and copies drives by name; AmigaFS never calls it.
                "amiganut/filesystem/drive.py",
            }:
                offenders.append(f"{relative}:{number}: {stripped}")
    assert offenders == []


@pytest.mark.parametrize("filesystem", ["FFS-INTL", "PFS3", "SFS"])
def test_every_driver_stays_inside_the_store(
    tmp_path: Path, filesystem: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = create_hard_disc(tmp_path, filesystem=filesystem, capacity="8MB", partitions=1)

    def forbidden(self: BlockReader, *_args: object, **_kwargs: object) -> None:
        if not isinstance(self, StoreReader):
            raise AssertionError("the engine opened the medium behind the store's back")

    monkeypatch.setattr(BlockReader, "__init__", forbidden)
    media = open_media(path, writable=True)
    try:
        mount = open_volume(media.store, media.volumes[0], writable=True)
        assert isinstance(
            mount,
            {"FFS-INTL": AmigaDOSMount, "PFS3": PFS3Mount, "SFS": SFSMount}[filesystem],
        )
        before = path.read_bytes()
        media.store.begin()
        mount.write_bytes("Staged", b"inside the transaction", AmigaMeta())
        mount.mkdir("Drawer")
        mount.rename("Staged", "Drawer/Moved")
        mount.flush()
        assert mount.read_bytes("Drawer/Moved") == b"inside the transaction"
        assert list(mount.validate()) == []
        # Nothing reaches the medium until the store commits.
        assert path.read_bytes() == before
        media.store.rollback()
        assert path.read_bytes() == before
    finally:
        media.close()


def test_closing_a_mount_does_not_close_the_store(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    store = ImageStore.open(path, writable=False)
    try:
        media_reader = store.reader()
        from amigafs._vendor.amiganut.filesystem.amigados import AmigaDOSVolume

        mount = AmigaDOSMount(AmigaDOSVolume(media_reader))
        mount.close()
        assert store.read(0, 4) == b"DOS\x01"
    finally:
        store.close()


def test_allocator_chooses_the_same_block_as_the_original_search() -> None:
    """Patch 0002: two bitmap searches replace a block-by-block scan."""

    def original(bits: bytearray, start: int) -> int | None:
        for distance in range(len(bits)):
            for candidate in (start + distance, start - distance):
                if 0 <= candidate < len(bits) and bits[candidate]:
                    return candidate
        return None

    def patched(bits: bytearray, start: int) -> int | None:
        forward = bits.find(1, start)
        backward = bits.rfind(1, 0, start)
        if forward < 0 and backward < 0:
            return None
        if backward < 0 or (forward >= 0 and forward - start <= start - backward):
            return forward
        return backward

    generator = random.Random(1985)
    for _ in range(5000):
        length = generator.randint(1, 80)
        bits = bytearray(generator.choice((0, 0, 0, 1)) for _ in range(length))
        start = generator.randint(0, length - 1)
        assert patched(bits, start) == original(bits, start)


def test_allocator_is_the_patched_implementation(tmp_path: Path) -> None:
    import inspect

    from amigafs._vendor.amiganut.filesystem.amigados import AmigaDOSVolume

    source = inspect.getsource(AmigaDOSVolume._allocate)
    assert "bits.find(1, start)" in source
    assert "for distance in range" not in source


def test_public_mount_interface(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    with AmigaImage.open(path) as image:
        mount = image.mount_for(0)
        for name in (
            "title",
            "set_title",
            "exists",
            "stat",
            "iter_entries",
            "read_bytes",
            "write_bytes",
            "mkdir",
            "remove",
            "rename",
            "amiga_meta",
            "set_amiga_meta",
            "set_access",
            "size_bytes",
            "free_bytes",
            "validate",
            "flush",
            "close",
        ):
            assert hasattr(mount, name), name
        entries = {entry.name: entry for entry in mount.iter_entries("Docs")}
        assert set(entries) == {"ReadMe", "Deep"}
        assert entries["ReadMe"].path == "Docs/ReadMe"
        assert (entries["ReadMe"].is_dir, entries["Deep"].is_dir) == (False, True)
        assert entries["ReadMe"].length == 600
        assert not entries["ReadMe"].is_link
        stat = mount.stat("Docs/ReadMe")
        assert (stat.length, stat.is_dir, stat.block) == (600, False, entries["ReadMe"].block)
        meta = mount.amiga_meta("Docs/ReadMe")
        assert isinstance(meta, AmigaMeta)
        assert (meta.protection, meta.comment) == (0, "Introduction")
        assert meta.datestamp is not None and meta.datestamp.tzinfo is not None
        with pytest.raises(DataError):
            mount.read_bytes("Docs/Missing")
        assert issubclass(DataError, AmiganutError)


def test_internals_used_for_ranged_reads_and_bitmap_repair(tmp_path: Path) -> None:
    floppy = create_floppy(tmp_path)
    with AmigaImage.open(floppy) as image:
        volume = image.mount_for(0).volume
        for name in (
            "ffs",
            "dircache",
            "long_names",
            "name_limit",
            "block_size",
            "total_blocks",
            "reserved",
            "root_block",
            "reader",
            "_data_blocks",
            "_chain_blocks",
            "_read_header",
            "_header_name",
            "_comment_block_of",
            "_load_cache",
            "_store_bitmap",
            "_require_writable",
            "_bitmap",
            "_bitmap_blocks",
            "_dirty_bitmap",
            "_dirty_pages",
        ):
            assert hasattr(volume, name), name
        assert volume.root_block == 880
        blocks = volume._data_blocks(image.mount_for(0).stat("C/List").block)
        assert len(blocks) == 6
    for filesystem in ("PFS3", "SFS"):
        directory = tmp_path / filesystem
        directory.mkdir()
        disc = create_hard_disc(directory, filesystem=filesystem, capacity="8MB", partitions=1)
        with AmigaImage.open(disc) as image:
            volume = image.mount_for(0).volume
            found, _parts = volume.resolve("C/List")
            key = found.first if filesystem == "SFS" else found.anode
            runs = volume.extents(key)
            assert sum(count for _first, count in runs) * volume.block_size >= 3072
            assert hasattr(volume, "blocks")
            assert hasattr(volume, "read_run") == (filesystem == "SFS")


def test_protection_helpers_keep_the_inverted_low_bits() -> None:
    assert format_access_text(0) == "----rwed"
    assert format_access_text(0x0F) == "--------"
    assert format_access_text(0xF0) == "hsparwed"
    assert int(parse_access_text("----r-e-")) == FIBF_WRITE | FIBF_DELETE
    assert Access(FIBF_DELETE).locked
    assert Access(FIBF_WRITE).locked
    assert not Access(0).locked
    with pytest.raises(DataError):
        parse_access_text("rwx")


def test_engine_messages_the_adapter_classifies(tmp_path: Path) -> None:
    from amigafs.core.image import translate_engine_error
    from amigafs.errors import DiscFullError

    assert isinstance(translate_engine_error(DataError("The volume is full.")), DiscFullError)
    assert isinstance(
        translate_engine_error(DataError("9 bytes need 3 blocks but only 1 are free.")),
        DiscFullError,
    )
    assert isinstance(
        translate_engine_error(DataError("There is not enough free space on this SFS volume.")),
        DiscFullError,
    )
    assert isinstance(translate_engine_error(DataError("Docs already exists.")), FileExistsError)
    assert isinstance(
        translate_engine_error(DataError("C/List is protected against deletion.")),
        PermissionError,
    )
    assert isinstance(
        translate_engine_error(DataError("C/List is protected from deletion.")), PermissionError
    )
    not_empty = translate_engine_error(DataError("Docs is not empty."))
    assert isinstance(not_empty, OSError) and not_empty.errno == 39
    untouched = ValueError("not an engine error")
    assert translate_engine_error(untouched) is untouched

    path = create_floppy(tmp_path)
    with AmigaImage.open(path) as image:
        mount = image.mount_for(0)
        for call, fragment in (
            (lambda: mount.volume._require_writable(), "cannot be modified"),
            (lambda: mount.read_bytes("Docs"), "is not a file"),
        ):
            with pytest.raises(DataError, match=fragment):
                call()
