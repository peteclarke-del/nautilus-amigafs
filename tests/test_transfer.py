from __future__ import annotations

import os
from pathlib import Path

import pytest

from amigafs._vendor.amiganut.file import AmigaMeta
from amigafs.core.image import AmigaImage
from amigafs.core.transfer import (
    export_file,
    format_inf_record,
    import_file,
    parse_inf_record,
    parse_protection,
)
from amigafs.errors import AmigaFSError
from amigafs.recovery import pending_recovery
from tests.image_fixture import create_empty_floppy, create_floppy, create_hard_disc


def test_inf_record_round_trips_path_protection_length_and_comment() -> None:
    record = format_inf_record(
        "Games/My Program", 7, AmigaMeta(protection=0x45, comment="The  game\tloader")
    )
    assert record == '"Games/My Program" -s--r-e- 00000007 "The game loader"\n'
    parsed = parse_inf_record(record)
    assert parsed.name == "Games/My Program"
    assert parsed.length == 7
    assert parsed.metadata.protection == 0x45
    assert parsed.metadata.comment == "The game loader"
    assert format_inf_record("C/List", 0x1234, AmigaMeta()) == "C/List ----rwed 00001234\n"


def test_inf_record_matches_the_amiga_file_forge_documentation() -> None:
    parsed = parse_inf_record(b'Games/Program ----r-e- 00000007 "The game loader"\n')
    assert parsed.name == "Games/Program"
    # r and e are granted; w and d are denied, which sets their inverted bits.
    assert parsed.metadata.protection == 0b0101
    assert parsed.length == 7
    assert parsed.metadata.comment == "The game loader"


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("----rwed", 0x00),
        ("--------", 0x0F),
        ("hsparwed", 0xF0),
        ("-s--rwed", 0x40),
        ("HSPARWED", 0xF0),
    ],
)
def test_protection_text_uses_the_inverted_low_bits(text: str, value: int) -> None:
    assert parse_protection(text) == value


@pytest.mark.parametrize("text", ["", "rwed", "----rwxd", "----rwedx", "&0000FF00"])
def test_malformed_protection_text_is_rejected(text: str) -> None:
    assert parse_protection(text) is None


def test_inf_record_accepts_records_without_length_or_comment() -> None:
    bare = parse_inf_record("File ----rwed")
    assert (bare.name, bare.length, bare.metadata.comment) == ("File", None, "")
    commented = parse_inf_record('File ----rwed "only a comment"')
    assert (commented.length, commented.metadata.comment) == (None, "only a comment")
    prefixed = parse_inf_record("File ----rwed &0000002A")
    assert prefixed.length == 42


@pytest.mark.parametrize(
    "record",
    [
        "",
        "OnlyAName",
        "$.FILE FFFF1900 FFFF8023 00000010",
        "File ----rwed 1FFFFFFFF",
        'File ----rwed 00000001 "' + "c" * 80 + '"',
    ],
)
def test_foreign_and_malformed_inf_records_are_rejected(record: str) -> None:
    with pytest.raises(AmigaFSError):
        parse_inf_record(record)


def test_names_that_cannot_be_quoted_are_refused() -> None:
    with pytest.raises(AmigaFSError, match="quote"):
        format_inf_record('say "hi"', 1, AmigaMeta())
    with pytest.raises(AmigaFSError, match="quote"):
        format_inf_record("File", 1, AmigaMeta(comment='a "quoted" comment'))


def test_export_then_import_preserves_content_metadata_and_date(tmp_path: Path) -> None:
    source = create_floppy(tmp_path)
    with AmigaImage.open(source, writable=True) as image:
        node = image.node_at_path("Docs/ReadMe")
        image.set_metadata(
            node.inode, protection=0x45, comment="Carried across", mtime_ns=500_000_000 * 10**9
        )
    exported = export_file(source, "docs/readme", tmp_path / "ReadMe")
    assert exported.amiga_path == "Workbench:Docs/ReadMe"
    assert exported.data_path.read_bytes() == b"Read me first.\n" * 40
    assert exported.sidecar_path.read_text(encoding="latin-1") == (
        'Docs/ReadMe -s--r-e- 00000258 "Carried across"\n'
    )
    assert exported.data_path.stat().st_mtime_ns == 500_000_000 * 10**9
    assert pending_recovery(source) is None

    target = create_hard_disc(tmp_path, capacity="4MB", files=())
    imported = import_file(target, exported.data_path, directory="DH1:")
    assert imported.node.amiga_path == "DH1:ReadMe"
    assert imported.metadata_source == "INF sidecar ReadMe.inf"
    assert pending_recovery(target) is None
    with AmigaImage.open(target) as image:
        node = image.node_at_path("DH1:ReadMe")
        assert image.read(node.inode, 0, node.size) == b"Read me first.\n" * 40
        assert (node.protection, node.comment) == (0x45, "Carried across")
        assert node.mtime_ns == 500_000_000 * 10**9
        assert image.integrity_report().findings == ()


def test_export_refuses_either_collision_without_partial_output(tmp_path: Path) -> None:
    source = create_floppy(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "LIST.INF").write_text("in the way", encoding="utf-8")
    with pytest.raises(AmigaFSError, match="would overwrite"):
        export_file(source, "C/List", out / "List")
    (out / "LIST.INF").unlink()
    (out / "list").write_text("in the way", encoding="utf-8")
    with pytest.raises(AmigaFSError, match="would overwrite"):
        export_file(source, "C/List", out / "List")
    assert sorted(child.name for child in out.iterdir()) == ["list"]


def test_export_rejects_drawers_and_missing_paths(tmp_path: Path) -> None:
    source = create_floppy(tmp_path)
    with pytest.raises(AmigaFSError, match="files, not directories"):
        export_file(source, "Docs", tmp_path / "Docs.out")
    with pytest.raises(AmigaFSError, match="No such file"):
        export_file(source, "Docs/Missing", tmp_path / "Missing.out")
    with pytest.raises(AmigaFSError, match="destination directory does not exist"):
        export_file(source, "C/List", tmp_path / "nowhere" / "List")
    assert not [child for child in tmp_path.iterdir() if child.name.startswith(".")]


def test_export_removes_temporary_files_after_a_short_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = create_floppy(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(AmigaImage, "read", lambda *_args: b"")
    with pytest.raises(AmigaFSError, match="Image read ended"):
        export_file(source, "C/List", out / "List")
    assert list(out.iterdir()) == []


def test_import_without_a_sidecar_uses_neutral_defaults_and_the_host_date(
    tmp_path: Path,
) -> None:
    target = create_empty_floppy(tmp_path, filesystem="FFS")
    host = tmp_path / "Notes.txt"
    host.write_text("plain host file", encoding="utf-8")
    os.utime(host, ns=(600_000_000 * 10**9, 600_000_000 * 10**9))
    result = import_file(target, host, name="Notes")
    assert result.metadata_source == "neutral defaults"
    with AmigaImage.open(target) as image:
        node = image.node_at_path("Notes")
        assert (node.protection, node.comment) == (0, "")
        assert node.mtime_ns == 600_000_000 * 10**9


def test_import_clamps_impossible_host_dates(tmp_path: Path) -> None:
    target = create_empty_floppy(tmp_path, filesystem="FFS")
    ancient = tmp_path / "Ancient"
    ancient.write_bytes(b"old")
    os.utime(ancient, ns=(0, 0))
    import_file(target, ancient)
    with AmigaImage.open(target) as image:
        # 1 January 1978 is the first date an Amiga can record.
        assert image.node_at_path("Ancient").mtime_ns == 252_460_800 * 10**9


def test_import_rejects_mismatched_inf_length_before_image_write(tmp_path: Path) -> None:
    target = create_empty_floppy(tmp_path)
    before = target.read_bytes()
    host = tmp_path / "Data"
    host.write_bytes(b"twelve bytes")
    host.with_name("Data.inf").write_text("Data ----rwed 00000099\n", encoding="latin-1")
    with pytest.raises(AmigaFSError, match="does not match host file length"):
        import_file(target, host)
    assert target.read_bytes() == before
    assert pending_recovery(target) is None
    import_file(target, host, ignore_sidecar=True)


def test_import_sidecar_selection(tmp_path: Path) -> None:
    target = create_empty_floppy(tmp_path)
    host = tmp_path / "Data"
    host.write_bytes(b"x")
    explicit = tmp_path / "elsewhere.inf"
    explicit.write_text('Tools/Renamed -s--rwed 00000001 "explicit"\n', encoding="latin-1")
    result = import_file(target, host, sidecar=explicit)
    assert result.node.amiga_path == "Empty:Renamed"
    assert result.node.comment == "explicit"
    with pytest.raises(AmigaFSError, match="not both"):
        import_file(target, host, sidecar=explicit, ignore_sidecar=True)
    with pytest.raises(AmigaFSError, match="does not exist"):
        import_file(target, host, sidecar=tmp_path / "missing.inf")
    (tmp_path / "Data.inf").write_text("Data ----rwed\n", encoding="latin-1")
    (tmp_path / "DATA.INF").write_text("Data ----rwed\n", encoding="latin-1")
    with pytest.raises(AmigaFSError, match="More than one"):
        import_file(target, host, name="Other")
    huge = tmp_path / "huge.inf"
    huge.write_bytes(b"x" * 5000)
    with pytest.raises(AmigaFSError, match="safety limit"):
        import_file(target, host, sidecar=huge, name="Other")


def test_import_refuses_collisions_bad_destinations_and_oversized_files(tmp_path: Path) -> None:
    target = create_floppy(tmp_path)
    before = target.read_bytes()
    host = tmp_path / "list"
    host.write_bytes(b"replacement")
    with pytest.raises(AmigaFSError, match="already exists"):
        import_file(target, host, directory="C")
    with pytest.raises(AmigaFSError, match="does not exist"):
        import_file(target, host, directory="Nowhere")
    with pytest.raises(AmigaFSError, match="not a directory"):
        import_file(target, host, directory="C/List")
    with pytest.raises(AmigaFSError, match="does not exist or is not a file"):
        import_file(target, tmp_path / "missing")
    big = tmp_path / "Big"
    big.write_bytes(bytes(1_000_000))
    with pytest.raises(AmigaFSError, match="bytes free"):
        import_file(target, big)
    assert target.read_bytes() == before
    assert pending_recovery(target) is None
    disc = create_hard_disc(tmp_path, capacity="4MB", files=())
    with pytest.raises(AmigaFSError, match="Name a partition"):
        import_file(disc, host)


def test_import_rolls_back_content_and_metadata_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = create_floppy(tmp_path)
    before = target.read_bytes()
    host = tmp_path / "Data"
    host.write_bytes(b"payload")
    opened = AmigaImage.open

    def faulty(*args: object, **kwargs: object) -> AmigaImage:
        def fault(stage: str) -> None:
            if stage == "import.after":
                raise RuntimeError("interrupted")

        return opened(*args, **kwargs, fault_injector=fault)  # type: ignore[arg-type]

    monkeypatch.setattr(AmigaImage, "open", faulty)
    with pytest.raises(AmigaFSError, match="interrupted"):
        import_file(target, host)
    monkeypatch.setattr(AmigaImage, "open", opened)
    assert target.read_bytes() == before
    assert pending_recovery(target) is None
