import json
import os
from pathlib import Path

import pytest

from tools.user_install import (
    _active_mounts,
    _bundled_source,
    _current_release,
    _ensure_launcher,
    _launcher_target,
    _prune_older_releases,
    _validate_install_root,
    _verify_marker,
    install,
)


def test_bundled_installer_finds_exactly_one_adjacent_wheel(tmp_path: Path) -> None:
    wheel = tmp_path / "nautilus_amigafs-0.1.0-py3-none-any.whl"
    wheel.touch()

    assert _bundled_source(tmp_path) == wheel

    (tmp_path / "nautilus_amigafs-0.2.0-py3-none-any.whl").touch()
    with pytest.raises(RuntimeError, match="exactly one"):
        _bundled_source(tmp_path)


def test_install_root_must_be_specific_absolute_directory(tmp_path: Path) -> None:
    assert _validate_install_root(tmp_path / "app") == tmp_path / "app"
    with pytest.raises(RuntimeError, match="specific absolute"):
        _validate_install_root(Path("relative"))
    with pytest.raises(RuntimeError, match="specific absolute"):
        _validate_install_root(Path("/"))


def test_current_release_cannot_escape_install_root(tmp_path: Path) -> None:
    prefix = tmp_path / "app"
    releases = prefix / "releases"
    releases.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (prefix / "current").symlink_to(outside)

    with pytest.raises(RuntimeError, match="escaped"):
        _current_release(prefix)


def test_launcher_never_replaces_an_unmanaged_command(tmp_path: Path) -> None:
    prefix = tmp_path / "app"
    launcher = tmp_path / "bin" / "amigafs"
    launcher.parent.mkdir()
    launcher.write_text("unmanaged", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unmanaged"):
        _ensure_launcher(launcher, _launcher_target(prefix))


def test_marker_must_match_before_uninstall(tmp_path: Path) -> None:
    prefix = tmp_path / "app"
    prefix.mkdir()
    (prefix / "installer.json").write_text(json.dumps({"installer_version": 999}), encoding="utf-8")

    with pytest.raises(RuntimeError, match="unrecognised"):
        _verify_marker(prefix)


def test_active_mount_inventory_must_be_a_list(monkeypatch: pytest.MonkeyPatch) -> None:
    class Result:
        stdout = "{}"

    monkeypatch.setattr("tools.user_install._run", lambda *_args, **_kwargs: Result())

    with pytest.raises(RuntimeError, match="invalid mount status"):
        _active_mounts(Path("/managed/amigafs"), environment=os.environ.copy())


def test_upgrade_refuses_to_replace_code_beneath_active_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "package.whl"
    source.touch()
    prefix = tmp_path / "app"
    release = prefix / "releases" / "release-one"
    release.mkdir(parents=True)
    (prefix / "current").symlink_to(release)
    (prefix / "installer.json").write_text(json.dumps({"installer_version": 1}), encoding="utf-8")
    monkeypatch.setattr("tools.user_install._active_mounts", lambda *_args, **_kwargs: [{}])

    with pytest.raises(RuntimeError, match="before upgrading"):
        install(
            source,
            prefix=prefix,
            bin_dir=tmp_path / "bin",
            upgrade=True,
            restart=False,
            environment=os.environ.copy(),
        )

    assert (prefix / "current").resolve() == release
    assert list((prefix / "releases").iterdir()) == [release]


def test_upgrade_pruning_retains_only_the_rollback_release(tmp_path: Path) -> None:
    releases = tmp_path / "releases"
    keep = releases / "current"
    stale = releases / "stale"
    keep.mkdir(parents=True)
    stale.mkdir()

    _prune_older_releases(releases, keep=keep)

    assert keep.is_dir()
    assert not stale.exists()
