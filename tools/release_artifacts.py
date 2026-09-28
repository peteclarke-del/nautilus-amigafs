#!/usr/bin/env python3
"""Build and verify reproducible release artefacts, an SBOM and checksums."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_SUFFIXES = (".whl", ".tar.gz")
SBOM_NAME = "nautilus-amigafs.cdx.json"
CHECKSUM_NAME = "SHA256SUMS"
EXCLUDED_ENVIRONMENT_PACKAGES = frozenset({"nautilus-amigafs", "pip", "setuptools", "wheel"})


def _run(
    command: list[str],
    *,
    environment: dict[str, str],
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        env=environment,
        text=True,
        capture_output=capture_output,
    )


def _source_date_epoch(explicit: int | None) -> int:
    if explicit is not None:
        epoch = explicit
    elif configured := os.environ.get("SOURCE_DATE_EPOCH"):
        try:
            epoch = int(configured)
        except ValueError as exc:
            raise RuntimeError("SOURCE_DATE_EPOCH must be a non-negative integer") from exc
    else:
        result = _run(
            ["git", "log", "-1", "--format=%ct"],
            environment=os.environ.copy(),
            capture_output=True,
        )
        try:
            epoch = int(result.stdout.strip())
        except ValueError as exc:
            raise RuntimeError("could not derive SOURCE_DATE_EPOCH from the Git commit") from exc
    if epoch < 0:
        raise RuntimeError("SOURCE_DATE_EPOCH must be a non-negative integer")
    return epoch


def _build_once(destination: Path, *, epoch: int) -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "LC_ALL": "C.UTF-8",
            "PYTHONHASHSEED": "0",
            "SOURCE_DATE_EPOCH": str(epoch),
            "TZ": "UTC",
        }
    )
    _run(
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--sdist",
            "--outdir",
            str(destination),
            str(PROJECT_ROOT),
        ],
        environment=environment,
    )
    source_archives = list(destination.glob("*.tar.gz"))
    if len(source_archives) != 1:
        raise RuntimeError(f"expected exactly one source archive, found {len(source_archives)}")
    _normalise_sdist(source_archives[0], epoch=epoch)
    with tempfile.TemporaryDirectory(prefix="amigafs-wheel-source-") as temporary:
        unpacked = Path(temporary)
        source_root = _extract_sdist(source_archives[0], unpacked)
        _run(
            [
                sys.executable,
                "-m",
                "build",
                "--no-isolation",
                "--wheel",
                "--outdir",
                str(destination),
                str(source_root),
            ],
            environment=environment,
        )


def _extract_sdist(path: Path, destination: Path) -> Path:
    """Extract the generated regular-file tree and reject unsafe archive members."""

    root = destination.resolve(strict=True)
    with tarfile.open(path, mode="r:gz") as source:
        members = source.getmembers()
        for member in members:
            target = (root / member.name).resolve(strict=False)
            if not target.is_relative_to(root) or not (member.isfile() or member.isdir()):
                raise RuntimeError(f"source archive contains an unsafe member: {member.name}")
        source.extractall(destination, members=members)
    candidates = [candidate for candidate in destination.iterdir() if candidate.is_dir()]
    if len(candidates) != 1:
        raise RuntimeError(f"expected one source root in the archive, found {len(candidates)}")
    return candidates[0]


def _normalise_sdist(path: Path, *, epoch: int) -> None:
    """Remove build-host and wall-clock metadata from a setuptools sdist."""

    entries: list[tuple[tarfile.TarInfo, bytes | None]] = []
    with tarfile.open(path, mode="r:gz") as source:
        for member in source.getmembers():
            extracted = source.extractfile(member) if member.isfile() else None
            entries.append((member, extracted.read() if extracted is not None else None))

    temporary = path.with_name(f".{path.name}.normalising")
    try:
        with (
            temporary.open("wb") as raw,
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch) as compressed,
            tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as output,
        ):
            for member, content in sorted(entries, key=lambda entry: entry[0].name):
                member.mtime = epoch
                member.uid = 0
                member.gid = 0
                member.uname = ""
                member.gname = ""
                member.pax_headers = {}
                output.addfile(
                    member,
                    io.BytesIO(content) if content is not None else None,
                )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _artefacts(directory: Path) -> dict[str, Path]:
    return {
        path.name: path
        for path in directory.iterdir()
        if path.is_file() and path.name.endswith(ARTIFACT_SUFFIXES)
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify_reproducible(first: Path, second: Path) -> dict[str, Path]:
    """Return verified artefacts or raise with the differing file names."""

    first_files = _artefacts(first)
    second_files = _artefacts(second)
    if not first_files or set(first_files) != set(second_files):
        raise RuntimeError("the two builds produced different release artefact sets")
    differing = [
        name
        for name in sorted(first_files)
        if _sha256(first_files[name]) != _sha256(second_files[name])
    ]
    if differing:
        raise RuntimeError(f"release artefacts are not reproducible: {', '.join(differing)}")
    return first_files


def _resolved_requirements(packages: list[dict[str, Any]]) -> list[str]:
    requirements: list[str] = []
    for package in packages:
        name = package.get("name")
        version = package.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            raise RuntimeError("pip returned an invalid installed-package record")
        if name.lower().replace("_", "-") not in EXCLUDED_ENVIRONMENT_PACKAGES:
            requirements.append(f"{name}=={version}")
    return sorted(requirements, key=str.casefold)


def _build_sbom(wheel: Path, destination: Path) -> None:
    environment = os.environ.copy()
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    with tempfile.TemporaryDirectory(prefix="amigafs-sbom-") as temporary:
        root = Path(temporary)
        virtual_environment = root / "venv"
        _run(
            [sys.executable, "-m", "venv", str(virtual_environment)],
            environment=environment,
        )
        python = virtual_environment / "bin" / "python"
        requirement = f"nautilus-amigafs[fuse] @ {wheel.resolve().as_uri()}"
        _run(
            [str(python), "-m", "pip", "install", "--quiet", requirement],
            environment=environment,
        )
        installed = _run(
            [str(python), "-m", "pip", "list", "--format", "json"],
            environment=environment,
            capture_output=True,
        )
        packages = json.loads(installed.stdout)
        if not isinstance(packages, list):
            raise RuntimeError("pip returned an invalid installed-package inventory")
        requirements = root / "resolved-requirements.txt"
        requirements.write_text(
            "\n".join(_resolved_requirements(packages)) + "\n",
            encoding="utf-8",
        )
        _run(
            [
                sys.executable,
                "-m",
                "cyclonedx_py",
                "requirements",
                str(requirements),
                "--pyproject",
                str(PROJECT_ROOT / "pyproject.toml"),
                "--mc-type",
                "application",
                "--spec-version",
                "1.6",
                "--output-reproducible",
                "--output-format",
                "JSON",
                "--output-file",
                str(destination),
            ],
            environment=environment,
        )


def write_checksums(paths: list[Path], destination: Path) -> None:
    content = "".join(f"{_sha256(path)}  {path.name}\n" for path in sorted(paths))
    destination.write_text(content, encoding="ascii")


def build_release(output: Path, *, epoch: int | None = None) -> list[Path]:
    """Build the complete release set into an empty destination."""

    if output.is_symlink():
        raise RuntimeError(f"release output directory must not be a symbolic link: {output}")
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"release output directory is not empty: {output}")
    output.mkdir(mode=0o755, parents=True, exist_ok=True)
    resolved_epoch = _source_date_epoch(epoch)
    with tempfile.TemporaryDirectory(prefix="amigafs-release-build-") as temporary:
        root = Path(temporary)
        first = root / "first"
        second = root / "second"
        staged = root / "release"
        first.mkdir()
        second.mkdir()
        staged.mkdir()
        _build_once(first, epoch=resolved_epoch)
        _build_once(second, epoch=resolved_epoch)
        verified = verify_reproducible(first, second)
        staged_artefacts: list[Path] = []
        for name, source in sorted(verified.items()):
            target = staged / name
            shutil.copyfile(source, target)
            staged_artefacts.append(target)

        wheels = [path for path in staged_artefacts if path.suffix == ".whl"]
        if len(wheels) != 1:
            raise RuntimeError(f"expected exactly one wheel, found {len(wheels)}")
        _run(
            [
                sys.executable,
                str(PROJECT_ROOT / "tools/build_addon.py"),
                "--output",
                str(staged),
                "--wheel",
                str(wheels[0]),
                "--source-date-epoch",
                str(resolved_epoch),
            ],
            environment=os.environ.copy(),
        )
        add_ons = list(staged.glob("nautilus-amigafs-addon-*.zip"))
        if len(add_ons) != 1:
            raise RuntimeError(f"expected exactly one add-on archive, found {len(add_ons)}")
        staged_artefacts.extend(add_ons)
        debian_output = root / "debian"
        _run(
            [
                sys.executable,
                str(PROJECT_ROOT / "tools/debian_package.py"),
                "--output",
                str(debian_output),
                "--source-date-epoch",
                str(resolved_epoch),
            ],
            environment=os.environ.copy(),
        )
        debian_files = sorted(debian_output.iterdir())
        if len(list(debian_output.glob("*.deb"))) != 1 or len(debian_files) != 2:
            raise RuntimeError("expected one Debian package and one Debian manifest")
        for source in debian_files:
            target = staged / source.name
            shutil.copyfile(source, target)
            staged_artefacts.append(target)
        sbom = staged / SBOM_NAME
        _build_sbom(wheels[0], sbom)
        staged_artefacts.append(sbom)
        write_checksums(staged_artefacts, staged / CHECKSUM_NAME)

        for source in sorted(staged.iterdir()):
            shutil.copyfile(source, output / source.name)
    return [output / path.name for path in staged_artefacts]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("build/release"))
    parser.add_argument("--source-date-epoch", type=int)
    arguments = parser.parse_args()
    artefacts = build_release(arguments.output, epoch=arguments.source_date_epoch)
    print(f"Verified {len(artefacts)} reproducible release files in {arguments.output}")
    print(f"Checksums: {arguments.output / CHECKSUM_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
