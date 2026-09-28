import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from tools.release_artifacts import (
    _extract_sdist,
    _normalise_sdist,
    _resolved_requirements,
    verify_reproducible,
    write_checksums,
)


def test_reproducibility_requires_identical_named_content(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    for directory in (first, second):
        (directory / "package-1-py3-none-any.whl").write_bytes(b"wheel")
        (directory / "package-1.tar.gz").write_bytes(b"source")

    verified = verify_reproducible(first, second)

    assert sorted(verified) == ["package-1-py3-none-any.whl", "package-1.tar.gz"]


def test_reproducibility_reports_changed_artefact(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "package.whl").write_bytes(b"one")
    (second / "package.whl").write_bytes(b"two")

    with pytest.raises(RuntimeError, match="package.whl"):
        verify_reproducible(first, second)


def test_source_archive_normalisation_removes_host_metadata(tmp_path: Path) -> None:
    archives = [tmp_path / "one.tar.gz", tmp_path / "two.tar.gz"]
    payload = tmp_path / "payload.txt"
    payload.write_text("release content\n", encoding="utf-8")
    for index, archive in enumerate(archives, 1):
        with tarfile.open(archive, mode="w:gz") as output:
            info = output.gettarinfo(payload, arcname="package/payload.txt")
            info.mtime = index
            info.uid = index
            with payload.open("rb") as handle:
                output.addfile(info, handle)
        _normalise_sdist(archive, epoch=1_700_000_000)

    assert archives[0].read_bytes() == archives[1].read_bytes()
    with tarfile.open(archives[0], mode="r:gz") as source:
        member = source.getmember("package/payload.txt")
        assert member.mtime == 1_700_000_000
        assert member.uid == member.gid == 0


def test_source_archive_extraction_rejects_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive, mode="w:gz") as output:
        info = tarfile.TarInfo("../escaped")
        info.size = 1
        output.addfile(info, fileobj=io.BytesIO(b"x"))
    destination = tmp_path / "unpacked"
    destination.mkdir()

    with pytest.raises(RuntimeError, match="unsafe member"):
        _extract_sdist(archive, destination)

    assert not (tmp_path / "escaped").exists()


def test_resolved_requirements_exclude_build_tools_and_project() -> None:
    packages = [
        {"name": "pyfuse3", "version": "3.5.0"},
        {"name": "Nautilus_AmigaFS", "version": "0.1.0"},
        {"name": "pip", "version": "26.0"},
        {"name": "trio", "version": "0.31.0"},
    ]

    assert _resolved_requirements(packages) == ["pyfuse3==3.5.0", "trio==0.31.0"]


def test_checksum_manifest_is_sorted_and_sha256(tmp_path: Path) -> None:
    second = tmp_path / "z.tar.gz"
    first = tmp_path / "a.whl"
    second.write_bytes(b"source")
    first.write_bytes(b"wheel")

    destination = tmp_path / "SHA256SUMS"
    write_checksums([second, first], destination)

    assert destination.read_text(encoding="ascii").splitlines() == [
        f"{hashlib.sha256(b'wheel').hexdigest()}  a.whl",
        f"{hashlib.sha256(b'source').hexdigest()}  z.tar.gz",
    ]
