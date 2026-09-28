#!/usr/bin/env python3
"""Install, upgrade or uninstall the supported per-user AmigaFS environment."""

from __future__ import annotations

import argparse
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

INSTALLER_VERSION = 1
MARKER_NAME = "installer.json"


def _data_home(environment: dict[str, str]) -> Path:
    configured = environment.get("XDG_DATA_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local" / "share"


def _default_bin_dir() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local" / "bin"


def _bundled_source(directory: Path) -> Path:
    """Find the single wheel shipped beside a standalone add-on installer."""

    wheels = sorted(directory.glob("nautilus_amigafs-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(
            "no source was supplied and the installer directory does not contain exactly one "
            "nautilus_amigafs wheel"
        )
    return wheels[0]


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


def _validate_install_root(prefix: Path) -> Path:
    prefix = prefix.expanduser()
    if not prefix.is_absolute() or prefix == Path("/") or prefix == prefix.parent:
        raise RuntimeError("the AmigaFS install prefix must be a specific absolute directory")
    if prefix.is_symlink():
        raise RuntimeError(f"refusing a symbolic-link AmigaFS install prefix: {prefix}")
    return prefix


def _replace_symlink(link: Path, target: Path) -> None:
    temporary = link.with_name(f".{link.name}.{uuid.uuid4().hex}.tmp")
    temporary.symlink_to(target)
    try:
        os.replace(temporary, link)
    finally:
        temporary.unlink(missing_ok=True)


def _current_release(prefix: Path) -> Path | None:
    current = prefix / "current"
    if not current.exists() and not current.is_symlink():
        return None
    if not current.is_symlink():
        raise RuntimeError(f"refusing a non-symlink current release pointer: {current}")
    release = current.resolve(strict=True)
    releases = (prefix / "releases").resolve(strict=True)
    if release.parent != releases:
        raise RuntimeError("the current AmigaFS release pointer escaped its install root")
    return release


def _launcher_target(prefix: Path) -> Path:
    return prefix / "current" / "venv" / "bin" / "amigafs"


def _ensure_launcher(link: Path, target: Path) -> None:
    if link.exists() or link.is_symlink():
        if not link.is_symlink() or Path(os.readlink(link)) != target:
            raise RuntimeError(f"refusing to replace an unmanaged command: {link}")
        return
    _replace_symlink(link, target)


def _write_marker(prefix: Path) -> None:
    marker = prefix / MARKER_NAME
    temporary = marker.with_name(f".{marker.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps({"installer_version": INSTALLER_VERSION}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, marker)


def _verify_marker(prefix: Path) -> None:
    marker = prefix / MARKER_NAME
    if marker.is_symlink():
        raise RuntimeError(f"refusing to remove an unrecognised install root: {prefix}")
    try:
        payload: Any = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"refusing to remove an unrecognised install root: {prefix}") from exc
    if payload != {"installer_version": INSTALLER_VERSION}:
        raise RuntimeError(f"refusing to remove an unrecognised install root: {prefix}")


def _active_mounts(amigafs: Path, *, environment: dict[str, str]) -> list[dict[str, Any]]:
    result = _run(
        [str(amigafs), "status", "--json"],
        environment=environment,
        capture_output=True,
    )
    payload: Any = json.loads(result.stdout)
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise RuntimeError("the installed amigafs command returned invalid mount status")
    return payload


def _prune_older_releases(releases: Path, *, keep: Path) -> None:
    """Remove releases older than the rollback candidate after a no-mount gate."""

    for candidate in releases.iterdir():
        if candidate == keep:
            continue
        if candidate.is_symlink() or not candidate.is_dir():
            raise RuntimeError(f"refusing an unmanaged release entry: {candidate}")
        shutil.rmtree(candidate)


def install(
    source: Path,
    *,
    prefix: Path,
    bin_dir: Path,
    upgrade: bool,
    restart: bool,
    environment: dict[str, str],
) -> Path:
    """Install one staged release and atomically make it current."""

    source = source.expanduser().resolve(strict=True)
    prefix = _validate_install_root(prefix)
    prefix_was_empty = not prefix.exists() or (prefix.is_dir() and not any(prefix.iterdir()))
    prefix.mkdir(mode=0o755, parents=True, exist_ok=True)
    releases = prefix / "releases"
    if releases.is_symlink():
        raise RuntimeError(f"refusing a symbolic-link release directory: {releases}")
    releases.mkdir(mode=0o755, exist_ok=True)
    old_release = _current_release(prefix)
    if upgrade and old_release is None:
        raise RuntimeError("AmigaFS is not installed; use the install action first")
    if not upgrade and old_release is not None:
        raise RuntimeError("AmigaFS is already installed; use the upgrade action")
    if old_release is not None:
        _verify_marker(prefix)
        old_amigafs = old_release / "venv" / "bin" / "amigafs"
        if _active_mounts(old_amigafs, environment=environment):
            raise RuntimeError("unmount every AmigaFS image before upgrading")
        _prune_older_releases(releases, keep=old_release)
    elif not prefix_was_empty:
        raise RuntimeError(f"refusing to install over a non-empty unmanaged directory: {prefix}")

    release = Path(tempfile.mkdtemp(prefix="release-", dir=releases))
    current = prefix / "current"
    launcher = bin_dir.expanduser() / "amigafs"
    launcher_created = False
    activated = False
    try:
        virtual_environment = release / "venv"
        _run(
            [sys.executable, "-m", "venv", str(virtual_environment)],
            environment=environment,
        )
        python = virtual_environment / "bin" / "python"
        amigafs = virtual_environment / "bin" / "amigafs"
        requirement = f"nautilus-amigafs[fuse] @ {source.as_uri()}"
        _run(
            [str(python), "-m", "pip", "install", "--quiet", requirement],
            environment=environment,
        )
        _run([str(amigafs), "--help"], environment=environment)
        _replace_symlink(current, release)
        activated = True
        bin_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
        launcher_created = not launcher.exists() and not launcher.is_symlink()
        _ensure_launcher(launcher, _launcher_target(prefix))
        _run(
            [str(amigafs), "install-nautilus", *(["--restart"] if restart else [])],
            environment=environment,
        )
        _write_marker(prefix)
    except BaseException:
        if activated:
            if old_release is None:
                amigafs = release / "venv" / "bin" / "amigafs"
                with suppress(OSError, subprocess.SubprocessError):
                    _run([str(amigafs), "uninstall-nautilus"], environment=environment)
                current.unlink(missing_ok=True)
            else:
                _replace_symlink(current, old_release)
                old_amigafs = old_release / "venv" / "bin" / "amigafs"
                with suppress(OSError, subprocess.SubprocessError):
                    _run([str(old_amigafs), "install-nautilus"], environment=environment)
        if launcher_created:
            launcher.unlink(missing_ok=True)
        shutil.rmtree(release, ignore_errors=True)
        raise
    return launcher


def uninstall(
    *,
    prefix: Path,
    bin_dir: Path,
    restart: bool,
    environment: dict[str, str],
) -> None:
    """Remove managed code and desktop integration while retaining all user state."""

    prefix = _validate_install_root(prefix)
    _verify_marker(prefix)
    release = _current_release(prefix)
    if release is None:
        raise RuntimeError("the managed AmigaFS installation has no current release")
    amigafs = release / "venv" / "bin" / "amigafs"
    if _active_mounts(amigafs, environment=environment):
        raise RuntimeError("unmount every AmigaFS image before uninstalling")
    launcher = bin_dir.expanduser() / "amigafs"
    expected = _launcher_target(prefix)
    if (launcher.exists() or launcher.is_symlink()) and (
        not launcher.is_symlink() or Path(os.readlink(launcher)) != expected
    ):
        raise RuntimeError(f"refusing to remove an unmanaged command: {launcher}")
    _run(
        [str(amigafs), "uninstall-nautilus", *(["--restart"] if restart else [])],
        environment=environment,
    )
    if launcher.is_symlink() and Path(os.readlink(launcher)) == expected:
        launcher.unlink()
    shutil.rmtree(prefix)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path)
    parser.add_argument("--bin-dir", type=Path)
    parser.add_argument("--restart", action="store_true", help="restart Nautilus after the action")
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("install", "upgrade"):
        action_parser = subparsers.add_parser(action)
        action_parser.add_argument(
            "source",
            type=Path,
            nargs="?",
            help="source tree, archive or wheel; defaults to the wheel beside this installer",
        )
    subparsers.add_parser("uninstall")
    arguments = parser.parse_args()
    environment = os.environ.copy()
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    prefix = arguments.prefix or _data_home(environment) / "nautilus-amigafs"
    bin_dir = arguments.bin_dir or _default_bin_dir()
    if arguments.action == "uninstall":
        uninstall(
            prefix=prefix,
            bin_dir=bin_dir,
            restart=arguments.restart,
            environment=environment,
        )
        print("Removed AmigaFS code and desktop integration; user data was retained.")
    else:
        source = arguments.source or _bundled_source(Path(__file__).resolve().parent)
        launcher = install(
            source,
            prefix=prefix,
            bin_dir=bin_dir,
            upgrade=arguments.action == "upgrade",
            restart=arguments.restart,
            environment=environment,
        )
        print(f"AmigaFS {arguments.action} complete: {launcher}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
