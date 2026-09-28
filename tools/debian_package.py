#!/usr/bin/env python3
"""Build a reproducible Ubuntu 24.04 amd64 package without install-time downloads."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from amigafs.nautilus_install import (
    DESKTOP_FILE_NAME,
    EXTENSION_NAME,
    MIME_PACKAGE_NAME,
    desktop_file_content,
    extension_loader_content,
    mime_package_content,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "nautilus-amigafs"
ARCHITECTURE = "amd64"
PYTHON_ROOT = PurePosixPath("usr/lib/python3/dist-packages")
VENDOR_MANIFEST = PROJECT_ROOT / "src/amigafs/_vendor/VENDORED.json"
VENDOR_ROOT = PROJECT_ROOT / "src/amigafs/_vendor"
HELPER_SOURCE = PROJECT_ROOT / "src/amigafs/core/device_policy.py"
HELPER_PATH = PurePosixPath("usr/libexec/amigafs/amigafs-device-helper")
POLKIT_SOURCE = PROJECT_ROOT / "packaging/polkit/org.amigafs.device-helper.policy"
POLKIT_PATH = PurePosixPath("usr/share/polkit-1/actions/org.amigafs.device-helper.policy")
RUNTIME_DEPENDENCIES = (
    "python3 (>= 3.11~)",
    "python3 (<< 3.13)",
    "fuse3",
    "python3-pyfuse3 (>= 3.3)",
    "python3-trio (>= 0.24)",
    "python3-nautilus",
    "gir1.2-nautilus-4.0",
    "shared-mime-info",
    "desktop-file-utils",
    "libnotify-bin",
    "zenity",
)
RECOMMENDED_PACKAGES = ("pkexec", "polkitd")


def _run(
    command: list[str],
    *,
    environment: dict[str, str],
    cwd: Path | None = None,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        cwd=cwd,
        env=environment,
        text=True,
        capture_output=capture_output,
    )


def _normalise_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def vendored_inventory() -> dict[str, Any]:
    """Verify the in-tree engine snapshot and return what the package will carry."""

    try:
        manifest: dict[str, Any] = json.loads(VENDOR_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"the vendored engine has no readable manifest: {exc}") from exc
    files: Any = manifest.get("files")
    if manifest.get("license") != "MIT" or not isinstance(files, dict) or not files:
        raise RuntimeError("the vendored engine manifest is incomplete")
    actual = {
        path.relative_to(VENDOR_ROOT).as_posix(): _sha256(path)
        for path in sorted(VENDOR_ROOT.rglob("*"))
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
        and path != VENDOR_MANIFEST
    }
    if actual != files:
        changed = sorted(
            name for name in set(actual) | set(files) if actual.get(name) != files.get(name)
        )
        raise RuntimeError(
            "the vendored engine does not match its manifest: " + ", ".join(changed[:5])
        )
    return {
        "amiganut": {
            "license": "MIT",
            "patches": list(manifest.get("patches", [])),
            "source_commit": manifest["source_commit"],
            "upstream": manifest["upstream"],
            "version": manifest["amiganut_version"],
        }
    }


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
            cwd=PROJECT_ROOT,
            capture_output=True,
        )
        epoch = int(result.stdout.strip())
    if epoch < 0:
        raise RuntimeError("SOURCE_DATE_EPOCH must be a non-negative integer")
    return epoch


def _project_version() -> str:
    configuration = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version: Any = configuration["project"]["version"]
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise RuntimeError("the project version is not a release-compatible semantic version")
    return version


def _debian_version(project_version: str) -> str:
    exact = subprocess.run(
        ["git", "describe", "--exact-match", "--tags", "--match", f"v{project_version}"],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if exact.returncode == 0 and exact.stdout.strip() == f"v{project_version}":
        return project_version
    result = _run(
        ["git", "show", "-s", "--format=%cd:%h", "--date=format:%Y%m%d", "HEAD"],
        environment=os.environ.copy(),
        cwd=PROJECT_ROOT,
        capture_output=True,
    )
    date, revision = result.stdout.strip().split(":", 1)
    return f"{project_version}+git{date}.{revision}"


def _build_environment(epoch: int) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "LC_ALL": "C.UTF-8",
            "PYTHONHASHSEED": "0",
            "SOURCE_DATE_EPOCH": str(epoch),
            "TZ": "UTC",
        }
    )
    return environment


def _build_project_wheel(destination: Path, *, epoch: int) -> Path:
    _run(
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--wheel",
            "--outdir",
            str(destination),
            str(PROJECT_ROOT),
        ],
        environment=_build_environment(epoch),
    )
    wheels = list(destination.glob("nautilus_amigafs-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected exactly one AmigaFS wheel, found {len(wheels)}")
    return wheels[0]


def _safe_wheel_member(name: str) -> PurePosixPath:
    member = PurePosixPath(name)
    if member.is_absolute() or ".." in member.parts or not member.parts:
        raise RuntimeError(f"wheel contains an unsafe member: {name}")
    if any(part.endswith(".data") for part in member.parts):
        raise RuntimeError(f"wheel contains an unsupported data-layout member: {name}")
    return member


def _write(
    root: Path,
    installed: PurePosixPath,
    content: bytes,
    owned: set[str],
    *,
    mode: int = 0o644,
) -> None:
    rendered = "/" + installed.as_posix()
    if rendered in owned:
        raise RuntimeError(f"package payload path overlaps: {rendered}")
    target = root / installed.as_posix()
    target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    target.write_bytes(content)
    target.chmod(mode)
    owned.add(rendered)


def _extract_wheel(path: Path, root: Path, owned: set[str]) -> None:
    with zipfile.ZipFile(path) as archive:
        for info in sorted(archive.infolist(), key=lambda item: item.filename):
            member = _safe_wheel_member(info.filename)
            if info.is_dir():
                continue
            file_type = (info.external_attr >> 16) & 0o170000
            if file_type not in (0, stat.S_IFREG):
                raise RuntimeError(f"wheel contains a non-regular member: {info.filename}")
            _write(root, PYTHON_ROOT / member, archive.read(info), owned)


def _stage_documentation(root: Path, owned: set[str]) -> None:
    destination = PurePosixPath("usr/share/doc") / PACKAGE_NAME
    for name in ("README.md", "CHANGELOG.md", "LICENSE"):
        _write(root, destination / name, (PROJECT_ROOT / name).read_bytes(), owned)
    for source in sorted((PROJECT_ROOT / "docs").glob("*.md")):
        _write(root, destination / "manual" / source.name, source.read_bytes(), owned)
    _write(
        root,
        destination / "copyright",
        (PROJECT_ROOT / "packaging/debian/copyright").read_bytes(),
        owned,
    )


def _stage_device_helper(root: Path, owned: set[str]) -> None:
    """Install the privileged helper and the polkit action that authorises it."""

    helper = HELPER_SOURCE.read_bytes()
    if not helper.startswith(b"#!/usr/bin/python3 -I\n"):
        raise RuntimeError("the device helper must run the system Python in isolated mode")
    forbidden = re.search(rb"^\s*(?:from|import)\s+amigafs\b", helper, flags=re.MULTILINE)
    if forbidden is not None:
        raise RuntimeError("the device helper must not import the amigafs package")
    _write(root, HELPER_PATH, helper, owned, mode=0o755)
    _write(root, POLKIT_PATH, POLKIT_SOURCE.read_bytes(), owned)


def _stage_desktop(root: Path, owned: set[str]) -> None:
    files = {
        PurePosixPath("usr/share/nautilus-python/extensions") / EXTENSION_NAME: (
            extension_loader_content(["/usr/bin/amigafs"]).encode()
        ),
        PurePosixPath("usr/share/mime/packages") / MIME_PACKAGE_NAME: (
            mime_package_content().encode()
        ),
        PurePosixPath("usr/share/applications") / DESKTOP_FILE_NAME: (
            desktop_file_content(["/usr/bin/amigafs"]).encode()
        ),
    }
    for installed, content in files.items():
        _write(root, installed, content, owned)


def _installed_size(root: Path) -> int:
    total = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
    return max(1, (total + 1023) // 1024)


def _stage_control(root: Path, *, version: str, installed_size: int, engine_version: str) -> None:
    control = f"""Package: {PACKAGE_NAME}
Version: {version}
Section: utils
Priority: optional
Architecture: {ARCHITECTURE}
Maintainer: Pete Clarke <249926147+peteclarke-del@users.noreply.github.com>
Homepage: https://github.com/peteclarke-del/nautilus-amigafs
Depends: {", ".join(RUNTIME_DEPENDENCIES)}
Recommends: {", ".join(RECOMMENDED_PACKAGES)}
Installed-Size: {installed_size}
X-Amiganut-Version: {engine_version}
Description: Nautilus integration for Commodore Amiga discs and disk images
 Mount validated Amiga OFS, FFS, PFS3 and SFS volumes from floppy and hard-disc
 images, physical discs and Greaseweazle floppy drives through FUSE 3, with
 journalled writes, recovery and GNOME Files integration.
"""
    maintainer_script = """#!/bin/sh
set -e
if command -v update-mime-database >/dev/null 2>&1; then
    update-mime-database /usr/share/mime
fi
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database /usr/share/applications
fi
exit 0
"""
    debian = root / "DEBIAN"
    debian.mkdir(mode=0o755)
    control_path = debian / "control"
    control_path.write_text(control, encoding="utf-8")
    control_path.chmod(0o644)
    for name in ("postinst", "postrm"):
        target = debian / name
        target.write_text(maintainer_script, encoding="utf-8")
        target.chmod(0o755)


def _normalise_timestamps(root: Path, *, epoch: int) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_dir():
            path.chmod(0o755)
        os.utime(path, (epoch, epoch), follow_symlinks=False)
    root.chmod(0o755)
    os.utime(root, (epoch, epoch), follow_symlinks=False)


def stage_package(
    root: Path,
    *,
    project_wheel: Path,
    version: str,
    epoch: int,
) -> tuple[list[str], dict[str, Any]]:
    """Create one policy-bounded package root and return its payload and vendor inventory."""

    if root.is_symlink():
        raise RuntimeError(f"package root must not be a symbolic link: {root}")
    if root.exists() and any(root.iterdir()):
        raise RuntimeError(f"package root is not empty: {root}")
    root.mkdir(mode=0o755, parents=True, exist_ok=True)
    inventory = vendored_inventory()
    owned: set[str] = set()
    _extract_wheel(project_wheel, root, owned)
    _write(
        root,
        PurePosixPath("usr/bin/amigafs"),
        b"#!/usr/bin/python3\nfrom amigafs.cli import main\nraise SystemExit(main())\n",
        owned,
        mode=0o755,
    )
    _stage_documentation(root, owned)
    _stage_desktop(root, owned)
    _stage_device_helper(root, owned)
    _stage_control(
        root,
        version=version,
        installed_size=_installed_size(root),
        engine_version=str(inventory["amiganut"]["version"]),
    )
    _normalise_timestamps(root, epoch=epoch)
    return sorted(owned), inventory


def _build_one(
    destination: Path,
    *,
    project_wheel: Path,
    version: str,
    epoch: int,
) -> tuple[Path, list[str], dict[str, Any]]:
    root = destination / "root"
    owned, inventory = stage_package(
        root,
        project_wheel=project_wheel,
        version=version,
        epoch=epoch,
    )
    package = destination / f"{PACKAGE_NAME}_{version}_{ARCHITECTURE}.deb"
    _run(
        ["dpkg-deb", "--root-owner-group", "--build", str(root), str(package)],
        environment=_build_environment(epoch),
    )
    return package, owned, inventory


def build_deb(output: Path, *, epoch: int | None = None) -> tuple[Path, Path]:
    """Build twice, verify reproducibility and publish the package plus its manifest."""

    if platform.machine() not in {"amd64", "x86_64"}:
        raise RuntimeError("Debian package production is currently limited to amd64")
    if output.is_symlink():
        raise RuntimeError(f"Debian output directory must not be a symbolic link: {output}")
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"Debian output directory is not empty: {output}")
    output.mkdir(mode=0o755, parents=True, exist_ok=True)
    resolved_epoch = _source_date_epoch(epoch)
    project_version = _project_version()
    version = _debian_version(project_version)
    with tempfile.TemporaryDirectory(prefix="amigafs-deb-build-") as temporary:
        workspace = Path(temporary)
        wheels = workspace / "wheels"
        wheels.mkdir()
        project_wheel = _build_project_wheel(wheels, epoch=resolved_epoch)
        vendored_inventory()
        builds: list[tuple[Path, list[str], dict[str, Any]]] = []
        for name in ("first", "second"):
            destination = workspace / name
            destination.mkdir()
            builds.append(
                _build_one(
                    destination,
                    project_wheel=project_wheel,
                    version=version,
                    epoch=resolved_epoch,
                )
            )
        first, second = builds
        if _sha256(first[0]) != _sha256(second[0]):
            raise RuntimeError("Debian package is not reproducible")
        if first[1:] != second[1:]:
            raise RuntimeError("Debian package builds produced different manifests")
        package = output / first[0].name
        shutil.copyfile(first[0], package)
        manifest = output / f"{PACKAGE_NAME}-deb-manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "architecture": ARCHITECTURE,
                    "artifact": {
                        "filename": package.name,
                        "sha256": _sha256(package),
                    },
                    "depends": list(RUNTIME_DEPENDENCIES),
                    "recommends": list(RECOMMENDED_PACKAGES),
                    "files": first[1],
                    "package": PACKAGE_NAME,
                    "publishable": True,
                    "schema": 1,
                    "source_version": project_version,
                    "vendor": first[2],
                    "version": version,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return package, manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("build/debian"))
    parser.add_argument("--source-date-epoch", type=int)
    arguments = parser.parse_args()
    package, manifest = build_deb(arguments.output, epoch=arguments.source_date_epoch)
    print(f"Verified reproducible Debian package: {package}")
    print(f"Package manifest: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
