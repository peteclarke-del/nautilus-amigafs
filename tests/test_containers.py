from __future__ import annotations

import gzip
import os
import shutil
import stat
from pathlib import Path

import pytest

from amigafs.core import containers
from amigafs.core.containers import (
    DD_IMAGE_BYTES,
    decode_container,
    decode_extended_adf,
    floppy_format_for_size,
    sectors_complete,
)
from amigafs.core.image import ROOT_INODE, AmigaImage
from amigafs.errors import AmigaFSError, UnsupportedImageError
from amigafs.recovery import pending_recovery, recover_image, salvage_workspace
from tests.image_fixture import create_floppy, create_hard_disc, extended_adf, gzip_image, tree

# Greaseweazle takes between five and fifteen seconds to convert one image, so
# the conversions that really run it belong to the live suite.
requires_greaseweazle = pytest.mark.skipif(
    os.environ.get("AMIGAFS_RUN_LIVE_GREASEWEAZLE") != "1" or shutil.which("gw") is None,
    reason="set AMIGAFS_RUN_LIVE_GREASEWEAZLE=1 with the Greaseweazle host tools installed",
)


def test_compressed_floppy_mounts_read_only_without_touching_the_source(tmp_path: Path) -> None:
    source = create_floppy(tmp_path)
    packed = gzip_image(source, tmp_path / "disk.adz")
    before = packed.read_bytes()
    with AmigaImage.open(source) as plain:
        expected = tree(plain)
    with AmigaImage.open(packed) as image:
        assert tree(image) == expected
        assert image.source.kind == "compressed-image"
        with pytest.raises(PermissionError):
            image.create_file(ROOT_INODE, b"New")
    assert packed.read_bytes() == before
    assert pending_recovery(packed) is None


def test_compressed_image_is_rewritten_atomically_after_a_clean_session(tmp_path: Path) -> None:
    source = create_floppy(tmp_path)
    packed = gzip_image(source, tmp_path / "disk.adz")
    packed.chmod(0o640)
    before = packed.read_bytes()
    with AmigaImage.open(packed, writable=True) as image:
        assert pending_recovery(packed) is not None
        node = image.create_file(ROOT_INODE, b"Added")
        image.replace_file(node.inode, b"written through gzip")
        # The source is untouched until the session closes.
        assert packed.read_bytes() == before
    assert pending_recovery(packed) is None
    assert stat.S_IMODE(packed.stat().st_mode) == 0o640
    assert not [child for child in tmp_path.iterdir() if child.name.startswith(".disk")]
    with gzip.open(packed, "rb") as handle:
        assert len(handle.read()) == DD_IMAGE_BYTES
    with AmigaImage.open(packed) as image:
        assert image.read(image.node_at_path("Added").inode, 0, 99) == b"written through gzip"
        assert image.integrity_report().findings == ()


def test_unmodified_writable_session_leaves_the_compressed_source_identical(
    tmp_path: Path,
) -> None:
    packed = gzip_image(create_floppy(tmp_path), tmp_path / "disk.adz")
    before = (packed.read_bytes(), packed.stat().st_mtime_ns, packed.stat().st_ino)
    with AmigaImage.open(packed, writable=True):
        pass
    assert (packed.read_bytes(), packed.stat().st_mtime_ns, packed.stat().st_ino) == before
    assert pending_recovery(packed) is None


def test_compressed_hard_disc_round_trips(tmp_path: Path) -> None:
    source = create_hard_disc(tmp_path, capacity="4MB", partitions=2)
    packed = gzip_image(source, tmp_path / "disk.hdz")
    with AmigaImage.open(packed, writable=True) as image:
        image.make_directory(image.children[ROOT_INODE][1], b"Backup")
    with AmigaImage.open(packed) as image:
        assert image.layout == "rdb"
        assert image.node_at_path("DH1:Backup").is_dir


def test_interrupted_container_session_leaves_the_source_and_a_salvageable_copy(
    tmp_path: Path,
) -> None:
    packed = gzip_image(create_floppy(tmp_path), tmp_path / "disk.adz")
    before = packed.read_bytes()
    image = AmigaImage.open(packed, writable=True)
    node = image.create_file(ROOT_INODE, b"Unsaved")
    image.replace_file(node.inode, b"work in progress")
    image.close(clean=False)
    assert packed.read_bytes() == before
    info = pending_recovery(packed)
    assert info is not None and info.kind == "workspace"
    with pytest.raises(AmigaFSError, match="needs recovery"):
        AmigaImage.open(packed, writable=True)
    assert "--salvage" in recover_image(packed)
    saved = salvage_workspace(packed, tmp_path / "salvaged.adf")
    with AmigaImage.open(saved) as salvaged:
        assert salvaged.read(salvaged.node_at_path("Unsaved").inode, 0, 99) == b"work in progress"
    with pytest.raises(AmigaFSError, match="Refusing to overwrite"):
        salvage_workspace(packed, saved)
    assert "unchanged" in recover_image(packed, discard=True)
    assert pending_recovery(packed) is None
    assert packed.read_bytes() == before


def test_external_change_to_the_container_blocks_write_back(tmp_path: Path) -> None:
    packed = gzip_image(create_floppy(tmp_path), tmp_path / "disk.adz")
    image = AmigaImage.open(packed, writable=True)
    image.create_file(ROOT_INODE, b"First")
    replacement = packed.read_bytes() + b"\0"
    os.utime(packed, ns=(9, 9))
    with pytest.raises(AmigaFSError, match="changed outside AmigaFS"):
        image.create_file(ROOT_INODE, b"Second")
    image.close()
    assert pending_recovery(packed) is not None
    assert replacement


def test_damaged_and_hostile_compressed_images_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truncated = tmp_path / "truncated.adz"
    truncated.write_bytes(
        gzip_image(create_floppy(tmp_path), tmp_path / "ok.adz").read_bytes()[:500]
    )
    with pytest.raises(AmigaFSError, match="could not be unpacked"):
        AmigaImage.open(truncated)
    ragged = tmp_path / "ragged.adz"
    with gzip.open(ragged, "wb") as handle:
        handle.write(b"DOS\x00" + bytes(1000))
    with pytest.raises(AmigaFSError, match="whole number of 512-byte sectors"):
        AmigaImage.open(ragged)
    bomb = tmp_path / "bomb.adz"
    with gzip.open(bomb, "wb") as handle:
        handle.write(bytes(4 * 1024 * 1024))
    monkeypatch.setenv("AMIGAFS_MAX_WORKSPACE_BYTES", str(1024 * 1024))
    with pytest.raises(AmigaFSError, match="safety limit"):
        AmigaImage.open(bomb)
    assert not list((tmp_path / "state").rglob("workspace.img"))


def test_extended_adf_with_standard_tracks_mounts_read_only(tmp_path: Path) -> None:
    source = create_floppy(tmp_path)
    wrapped = extended_adf(source, tmp_path / "extended.adf")
    assert decode_extended_adf(wrapped.read_bytes()) == source.read_bytes()
    with AmigaImage.open(wrapped) as image:
        assert image.read(image.node_at_path("Docs/ReadMe").inode, 0, 4) == b"Read"
    with pytest.raises(AmigaFSError, match="not supported for this image format"):
        AmigaImage.open(wrapped, writable=True)


def test_extended_adf_with_raw_tracks_is_refused_rather_than_flattened(tmp_path: Path) -> None:
    source = create_floppy(tmp_path)
    protected = extended_adf(source, tmp_path / "protected.adf", raw_track=3)
    with pytest.raises(UnsupportedImageError, match="raw or copy-protected tracks"):
        AmigaImage.open(protected)
    with pytest.raises(UnsupportedImageError):
        decode_extended_adf(b"UAE-1ADF" + bytes(4))
    with pytest.raises(UnsupportedImageError):
        decode_extended_adf(b"UAE-1ADF\x00\x00\xff\xff" + bytes(64))
    with pytest.raises(UnsupportedImageError):
        decode_extended_adf(b"not an extended adf")


def test_diskmasher_archive_is_decoded_by_the_vendored_decoder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = create_floppy(tmp_path)
    archive = tmp_path / "disk.dms"
    archive.write_bytes(b"DMS!" + bytes(64))
    # DiskMasher omits trailing empty cylinders; the decoder restores the full size.
    monkeypatch.setattr(
        "amigafs._vendor.dms.to_adf", lambda data: source.read_bytes()[: 70 * 2 * 11 * 512]
    )
    with AmigaImage.open(archive) as image:
        assert image.store.size == DD_IMAGE_BYTES
        assert image.source.kind == "dms-archive"
    with pytest.raises(AmigaFSError, match="not supported for this image format"):
        AmigaImage.open(archive, writable=True)


def test_damaged_diskmasher_archive_is_reported_clearly(tmp_path: Path) -> None:
    archive = tmp_path / "bad.dms"
    archive.write_bytes(b"DMS!" + bytes(200))
    with pytest.raises(AmigaFSError, match="DiskMasher"):
        AmigaImage.open(archive)


def test_decode_failure_removes_every_partial_file(tmp_path: Path) -> None:
    archive = tmp_path / "bad.dms"
    archive.write_bytes(b"DMS!" + bytes(200))
    directory = tmp_path / "work"
    directory.mkdir()
    with pytest.raises(UnsupportedImageError):
        decode_container(archive, kind="dms", directory=directory)
    assert list(directory.iterdir()) == []
    with pytest.raises(UnsupportedImageError, match="Unknown image container"):
        decode_container(archive, kind="tar", directory=directory)


def test_sector_result_parsing_requires_a_complete_disk() -> None:
    assert sectors_complete("T0.0: ok\nFound 1760 sectors of 1760 (100%)\n")
    assert not sectors_complete("Found 1700 sectors of 1760 (96%)")
    assert not sectors_complete("Found 880 sectors of 880 (100%)\nFound 1 sectors of 1760 (0%)")
    assert not sectors_complete("no summary")
    assert floppy_format_for_size(901120).sectors_per_track == 11  # type: ignore[union-attr]
    assert floppy_format_for_size(1802240).sectors_per_track == 22  # type: ignore[union-attr]
    assert floppy_format_for_size(800 * 1024) is None


def test_flux_conversion_uses_explicit_amiga_formats_and_no_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = create_floppy(tmp_path)
    flux = tmp_path / "disk.scp"
    flux.write_bytes(b"SCP" + bytes(100))
    calls: list[list[str]] = []

    def convert(arguments: list[str], **_kwargs: object) -> str:
        calls.append(arguments)
        if "--format=amiga.amigados_hd" in arguments:
            return "Found 0 sectors of 22 (0%)"
        shutil.copyfile(source, arguments[-1])
        if "--tracks=c=0:h=0" in arguments:
            return "Found 11 sectors of 11 (100%)"
        return "Found 1760 sectors of 1760 (100%)"

    monkeypatch.setattr(containers, "run_greaseweazle", convert)
    with AmigaImage.open(flux) as image:
        assert image.source.kind == "flux-image"
        assert image.read(image.node_at_path("Docs/ReadMe").inode, 0, 4) == b"Read"
    # One track is probed at each density; the whole image is decoded only once.
    assert [call[:-2] for call in calls] == [
        ["convert", "--tracks=c=0:h=0", "--format=amiga.amigados_hd"],
        ["convert", "--tracks=c=0:h=0", "--format=amiga.amigados"],
        ["convert", "--format=amiga.amigados"],
    ]
    with pytest.raises(AmigaFSError, match="not supported for this image format"):
        AmigaImage.open(flux, writable=True)


def test_incomplete_flux_image_is_refused_but_remains_writable_to_a_floppy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flux = tmp_path / "protected.ipf"
    flux.write_bytes(b"CAPS" + bytes(100))

    def convert(arguments: list[str], **_kwargs: object) -> str:
        if "--tracks=c=0:h=0" in arguments:
            return "Found 11 sectors of 11 (100%)"
        return "Found 1700 sectors of 1760 (96%)"

    monkeypatch.setattr(containers, "run_greaseweazle", convert)
    with pytest.raises(UnsupportedImageError, match="written directly to a physical floppy"):
        AmigaImage.open(flux)
    monkeypatch.setattr(
        containers, "run_greaseweazle", lambda *_a, **_k: "Found 3 sectors of 11 (27%)"
    )
    with pytest.raises(UnsupportedImageError, match="no complete standard AmigaDOS layout"):
        AmigaImage.open(flux)


def test_hfe_density_comes_from_the_declared_bit_rate(tmp_path: Path) -> None:
    def header(signature: bytes, rate: int) -> Path:
        path = tmp_path / f"{signature.decode()}-{rate}.hfe"
        path.write_bytes(signature + bytes((0, 80, 2, 0xFF)) + rate.to_bytes(2, "little"))
        return path

    assert containers.hfe_density(header(b"HXCPICFE", 250)).sectors_per_track == 11  # type: ignore[union-attr]
    assert containers.hfe_density(header(b"HXCHFEV3", 253)).sectors_per_track == 11  # type: ignore[union-attr]
    assert containers.hfe_density(header(b"HXCPICFE", 500)).sectors_per_track == 22  # type: ignore[union-attr]
    assert containers.hfe_density(header(b"HXCPICFE", 0)) is None
    assert containers.hfe_density(header(b"NOTANHFE", 250)) is None


def test_missing_greaseweazle_tools_are_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flux = tmp_path / "disk.hfe"
    flux.write_bytes(b"HXCPICFE" + bytes(600))
    monkeypatch.setattr("amigafs.core.containers.shutil.which", lambda _name: None)
    with pytest.raises(UnsupportedImageError, match="Greaseweazle host tools"):
        AmigaImage.open(flux)


@requires_greaseweazle
@pytest.mark.parametrize("version", [1, 3])
def test_hfe_round_trip_preserves_its_container_version(tmp_path: Path, version: int) -> None:
    source = create_floppy(tmp_path)
    flux = tmp_path / "disk.hfe"
    target = f"{flux}::version=3" if version == 3 else str(flux)
    containers.run_greaseweazle(["convert", "--format=amiga.amigados", str(source), target])
    assert containers.hfe_version(flux) == version
    with AmigaImage.open(flux, writable=True) as image:
        node = image.create_file(ROOT_INODE, b"OnFlux")
        image.replace_file(node.inode, b"sector data inside track data")
    assert containers.hfe_version(flux) == version
    assert pending_recovery(flux) is None
    with AmigaImage.open(flux) as image:
        assert image.read(image.node_at_path("OnFlux").inode, 0, 99) == (
            b"sector data inside track data"
        )
        assert image.integrity_report().findings == ()


@requires_greaseweazle
def test_high_density_hfe_is_detected_from_its_first_track(tmp_path: Path) -> None:
    source = create_floppy(tmp_path, density="hd")
    flux = tmp_path / "hd.hfe"
    containers.run_greaseweazle(["convert", "--format=amiga.amigados_hd", str(source), str(flux)])
    with AmigaImage.open(flux) as image:
        assert image.store.size == 1_802_240
        assert image.read(image.node_at_path("Docs/ReadMe").inode, 0, 4) == b"Read"


@requires_greaseweazle
def test_supercard_pro_image_mounts_read_only(tmp_path: Path) -> None:
    source = create_floppy(tmp_path)
    flux = tmp_path / "disk.scp"
    containers.run_greaseweazle(["convert", "--format=amiga.amigados", str(source), str(flux)])
    with AmigaImage.open(flux) as image:
        assert image.read(image.node_at_path("C/List").inode, 0, 4) == bytes(range(4))
