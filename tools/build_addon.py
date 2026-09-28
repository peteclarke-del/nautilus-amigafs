#!/usr/bin/env python3
"""Build a self-contained per-user Nautilus AmigaFS add-on bundle."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
import tomllib
import zipfile
from pathlib import Path


def _archive_info(name: str, *, epoch: int) -> zipfile.ZipInfo:
    timestamp = time.gmtime(max(epoch, 315_532_800))[:6]
    info = zipfile.ZipInfo(name, date_time=timestamp)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def create_bundle(
    wheel: Path,
    installer: Path,
    destination: Path,
    *,
    version: str,
    epoch: int,
) -> Path:
    """Package one wheel and its standalone lifecycle installer."""

    destination.mkdir(parents=True, exist_ok=True)
    target = destination / f"nautilus-amigafs-addon-{version}.zip"
    instructions = f"""Nautilus AmigaFS add-on {version}

Supported host: Ubuntu 24.04 LTS amd64 with GNOME Files 46 or later.

Install prerequisites:
    sudo apt update
    sudo apt install --no-install-recommends python3-venv python3-dev \\
        build-essential pkg-config fuse3 libfuse3-dev python3-nautilus \\
        gir1.2-nautilus-4.0 shared-mime-info desktop-file-utils \\
        libnotify-bin zenity

Installation downloads pinned Python dependencies into a private environment.

Track-level images (HFE, SCP, IPF) and physical floppies require the
separately installed Greaseweazle host tools, with gw available in the
graphical PATH.

Physical hard discs and memory cards: this per-user add-on cannot install the
root-owned polkit helper, so it opens a disc only when your account already
has access to it. Install the Debian package instead if you want AmigaFS to
ask for authorisation.

Install for the current user:
    python3 install.py --restart install

Verify:
    ~/.local/bin/amigafs --help
    ~/.local/bin/amigafs status

Upgrade an existing managed installation:
    Unmount every AmigaFS image first, then run:
    python3 install.py --restart upgrade

Uninstall while retaining preferences and recovery data:
    Unmount every AmigaFS image first, then run:
    python3 install.py --restart uninstall

Installed locations:
    Managed environment: ~/.local/share/nautilus-amigafs
    Command:             ~/.local/bin/amigafs
    Desktop integration: ~/.local/share

If the command is not found, add ~/.local/bin to PATH. If Files did not restart,
run "nautilus --quit" and reopen Files. Full instructions and troubleshooting:
https://github.com/peteclarke-del/nautilus-amigafs#installation
"""
    with tempfile.NamedTemporaryFile(
        prefix=f".{target.name}.", suffix=".tmp", dir=destination, delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with zipfile.ZipFile(temporary_path, "w") as archive:
            for name, content in (
                (wheel.name, wheel.read_bytes()),
                ("install.py", installer.read_bytes()),
                ("INSTALL.txt", instructions.encode()),
            ):
                archive.writestr(
                    _archive_info(name, epoch=epoch),
                    content,
                    compresslevel=9,
                )
        temporary_path.replace(target)
    finally:
        temporary_path.unlink(missing_ok=True)
    return target


def _source_date_epoch(project: Path, explicit: int | None) -> int:
    if explicit is not None:
        epoch = explicit
    elif configured := os.environ.get("SOURCE_DATE_EPOCH"):
        try:
            epoch = int(configured)
        except ValueError as exc:
            raise RuntimeError("SOURCE_DATE_EPOCH must be an integer") from exc
    else:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%ct"],
            check=True,
            cwd=project,
            capture_output=True,
            text=True,
        )
        epoch = int(result.stdout.strip())
    if epoch < 0:
        raise RuntimeError("SOURCE_DATE_EPOCH must be a non-negative integer")
    return epoch


def build_addon(
    project: Path,
    destination: Path,
    *,
    wheel: Path | None = None,
    epoch: int | None = None,
) -> Path:
    """Build a wheel and wrap it in the standalone add-on archive."""

    metadata = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
    version = metadata["project"]["version"]
    resolved_epoch = _source_date_epoch(project, epoch)
    if wheel is not None:
        return create_bundle(
            wheel.resolve(strict=True),
            project / "tools" / "user_install.py",
            destination,
            version=version,
            epoch=resolved_epoch,
        )
    environment = os.environ.copy()
    environment.update(
        {
            "LC_ALL": "C.UTF-8",
            "PYTHONHASHSEED": "0",
            "SOURCE_DATE_EPOCH": str(resolved_epoch),
            "TZ": "UTC",
        }
    )
    with tempfile.TemporaryDirectory(prefix="amigafs-addon-wheel-") as temporary:
        wheel_directory = Path(temporary)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--no-isolation",
                "--outdir",
                str(wheel_directory),
            ],
            check=True,
            cwd=project,
            env=environment,
        )
        wheels = list(wheel_directory.glob("nautilus_amigafs-*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"expected exactly one AmigaFS wheel, found {len(wheels)}")
        return create_bundle(
            wheels[0],
            project / "tools" / "user_install.py",
            destination,
            version=version,
            epoch=resolved_epoch,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("dist"))
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--source-date-epoch", type=int)
    arguments = parser.parse_args()
    project = Path(__file__).resolve().parent.parent
    print(
        build_addon(
            project,
            arguments.output.resolve(),
            wheel=arguments.wheel,
            epoch=arguments.source_date_epoch,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
