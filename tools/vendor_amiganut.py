#!/usr/bin/env python3
"""Refresh or verify the pinned Amiga File Forge engine snapshot.

AmigaFS vendors the ``amiganut`` filesystem engine and the DiskMasher decoder
from Amiga File Forge rather than depending on a published package. This tool
is the only supported way to change that snapshot:

``--check``
    Verify that every vendored file still matches the recorded manifest. CI and
    the release build run this, so an unreviewed edit cannot ship.

``--update CHECKOUT``
    Extract the engine from one commit of an Amiga File Forge checkout, apply
    the local patches in order and record the new manifest. The commit is read
    with ``git archive``, so uncommitted work in that checkout is never vendored.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VENDOR_ROOT = PROJECT_ROOT / "src" / "amigafs" / "_vendor"
MANIFEST = VENDOR_ROOT / "VENDORED.json"
PATCHES = VENDOR_ROOT / "patches"
UPSTREAM = "https://github.com/peteclarke-del/AmigaFileForge"
DMS_SOURCES = {"app/dms.py": "dms/dms.py", "app/dms_codec.py": "dms/dms_codec.py"}
LOCAL_FILES = ("__init__.py", "dms/__init__.py", "dms/errors.py", "dms/checksum.py")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tracked_files() -> list[Path]:
    files = [
        path
        for path in VENDOR_ROOT.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.name != MANIFEST.name
        and path.suffix != ".pyc"
    ]
    return sorted(files)


def _inventory() -> dict[str, str]:
    return {path.relative_to(VENDOR_ROOT).as_posix(): _sha256(path) for path in _tracked_files()}


def check() -> int:
    if not MANIFEST.is_file():
        print("The vendored snapshot has no manifest.", file=sys.stderr)
        return 1
    recorded = json.loads(MANIFEST.read_text(encoding="utf-8"))
    expected: dict[str, str] = recorded["files"]
    actual = _inventory()
    problems = []
    for name in sorted(set(expected) | set(actual)):
        if name not in actual:
            problems.append(f"missing: {name}")
        elif name not in expected:
            problems.append(f"unrecorded: {name}")
        elif expected[name] != actual[name]:
            problems.append(f"modified: {name}")
    if problems:
        print("The vendored snapshot does not match its manifest:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print(
        f"Vendored amiganut {recorded['amiganut_version']} from "
        f"{recorded['source_commit'][:12]} matches its manifest ({len(actual)} files)."
    )
    return 0


def _git(checkout: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(checkout), *arguments], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _extract(checkout: Path, commit: str, destination: Path) -> None:
    archive = subprocess.run(
        ["git", "-C", str(checkout), "archive", commit, "amiganut", *DMS_SOURCES, "LICENSE"],
        check=True,
        capture_output=True,
    )
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as bundle:
        bundle.extractall(destination, filter="data")


def update(checkout: Path, *, revision: str) -> int:
    checkout = checkout.resolve()
    try:
        commit = _git(checkout, "rev-parse", "--verify", f"{revision}^{{commit}}")
    except subprocess.CalledProcessError:
        print(f"{revision} is not a commit in {checkout}.", file=sys.stderr)
        return 1
    preserved = {name: (VENDOR_ROOT / name).read_bytes() for name in LOCAL_FILES}
    staging = VENDOR_ROOT.parent / "_vendor.staging"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    try:
        _extract(checkout, commit, staging)
        if not (staging / "amiganut" / "version.py").is_file():
            print(f"{commit[:12]} does not contain the amiganut engine.", file=sys.stderr)
            return 1
        shutil.rmtree(VENDOR_ROOT / "amiganut", ignore_errors=True)
        shutil.copytree(staging / "amiganut", VENDOR_ROOT / "amiganut")
        (VENDOR_ROOT / "dms").mkdir(exist_ok=True)
        for source, target in DMS_SOURCES.items():
            shutil.copyfile(staging / source, VENDOR_ROOT / target)
        shutil.copyfile(staging / "LICENSE", VENDOR_ROOT / "LICENSE.amiga-file-forge")
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    for name, content in preserved.items():
        (VENDOR_ROOT / name).write_bytes(content)
    for patch in sorted(PATCHES.glob("*.patch")):
        subprocess.run(
            ["patch", "--strip=1", "--directory", str(VENDOR_ROOT), "--input", str(patch)],
            check=True,
        )
    for leftover in VENDOR_ROOT.rglob("*.orig"):
        leftover.unlink()
    namespace: dict[str, str] = {}
    exec((VENDOR_ROOT / "amiganut" / "version.py").read_text(encoding="utf-8"), namespace)
    MANIFEST.write_text(
        json.dumps(
            {
                "schema": 1,
                "upstream": UPSTREAM,
                "source_commit": commit,
                "amiganut_version": namespace["__version__"],
                "license": "MIT",
                "patches": [patch.name for patch in sorted(PATCHES.glob("*.patch"))],
                "files": _inventory(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Vendored amiganut {namespace['__version__']} from {commit[:12]}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="verify the snapshot")
    action.add_argument("--update", type=Path, metavar="CHECKOUT", help="refresh the snapshot")
    parser.add_argument(
        "--revision", default="HEAD", help="commit, tag or branch to vendor (default: HEAD)"
    )
    arguments = parser.parse_args()
    if arguments.check:
        return check()
    return update(arguments.update, revision=arguments.revision)


if __name__ == "__main__":
    raise SystemExit(main())
