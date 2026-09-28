#!/usr/bin/env python3
"""Exercise wheel install, forced upgrade and uninstall without losing user state."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from user_install import install, uninstall


def _run(command: list[str], *, environment: dict[str, str]) -> None:
    subprocess.run(command, check=True, env=environment)


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _seed_preserved_state(config: Path, state: Path) -> dict[str, dict[str, bytes]]:
    files = {
        config / "amigafs" / "preferences.json": (b'{"mount_location": "sidebar", "version": 1}\n'),
        state / "amigafs" / "recovery" / ("a" * 64) / "manifest.json": (
            b'{"state": "ready", "fixture": true}\n'
        ),
        state / "amigafs" / "recovery" / ("a" * 64) / "image.dat": b"checkpoint",
        state / "amigafs" / "repair-audits" / "fixture.json": (
            b'{"status": "retained", "fixture": true}\n'
        ),
    }
    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return {"config": _snapshot(config), "state": _snapshot(state)}


def _assert_preserved(config: Path, state: Path, expected: dict[str, dict[str, bytes]]) -> None:
    actual = {"config": _snapshot(config), "state": _snapshot(state)}
    if actual != expected:
        raise RuntimeError("package lifecycle changed preserved preferences or recovery state")


def smoke(wheel: Path) -> None:
    wheel = wheel.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="amigafs-package-smoke-") as temporary:
        root = Path(temporary)
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(root / "home"),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_DATA_HOME": str(root / "data"),
                "XDG_RUNTIME_DIR": str(root / "runtime"),
                "XDG_STATE_HOME": str(root / "state"),
                "AMIGAFS_NO_SYSTEMD": "1",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            }
        )
        for directory in (root / "home", root / "runtime"):
            directory.mkdir(mode=0o700, parents=True)

        prefix = root / "managed-install"
        bin_dir = root / "bin"
        preserved = _seed_preserved_state(root / "config", root / "state")
        launcher = install(
            wheel,
            prefix=prefix,
            bin_dir=bin_dir,
            upgrade=False,
            restart=False,
            environment=environment,
        )
        amigafs = prefix / "current" / "venv" / "bin" / "amigafs"
        python = prefix / "current" / "venv" / "bin" / "python"
        assert launcher == bin_dir / "amigafs"
        _run([str(amigafs), "--help"], environment=environment)
        _run(
            [
                str(python),
                "-c",
                (
                    "from amigafs._vendor.amiganut import __version__; "
                    "from amigafs._vendor.dms import to_adf; "
                    "assert __version__, 'the vendored engine was not installed'"
                ),
            ],
            environment=environment,
        )
        _assert_preserved(root / "config", root / "state", preserved)
        install(
            wheel,
            prefix=prefix,
            bin_dir=bin_dir,
            upgrade=True,
            restart=False,
            environment=environment,
        )
        _assert_preserved(root / "config", root / "state", preserved)
        uninstall(
            prefix=prefix,
            bin_dir=bin_dir,
            restart=False,
            environment=environment,
        )
        _assert_preserved(root / "config", root / "state", preserved)
        if prefix.exists() or launcher.exists() or launcher.is_symlink():
            raise RuntimeError("managed uninstall left AmigaFS code or its command installed")


def build_and_smoke() -> None:
    """Build exactly one fresh wheel, then exercise it."""

    with tempfile.TemporaryDirectory(prefix="amigafs-package-artifact-") as temporary:
        output = Path(temporary)
        _run(
            [sys.executable, "-m", "build", "--wheel", "--outdir", str(output)],
            environment=os.environ.copy(),
        )
        wheels = list(output.glob("*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"expected exactly one built wheel, found {len(wheels)}")
        smoke(wheels[0])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--wheel", type=Path)
    source.add_argument("--build", action="store_true", help="build and test a fresh wheel")
    arguments = parser.parse_args()
    if arguments.build:
        build_and_smoke()
    else:
        smoke(arguments.wheel)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
