from __future__ import annotations

import json
import stat
import xml.dom.minidom
import zipfile
from pathlib import Path

import pytest

from tools import debian_package
from tools.debian_package import stage_package, vendored_inventory

POLKIT_POLICY = Path(debian_package.POLKIT_SOURCE)


def _wheel(
    path: Path,
    *,
    name: str = "nautilus-amigafs",
    version: str = "0.1.0",
    members: dict[str, bytes] | None = None,
) -> Path:
    distribution = name.replace("-", "_")
    metadata_root = f"{distribution}-{version}.dist-info"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"{metadata_root}/METADATA",
            f"Name: {name}\nVersion: {version}\nLicense-Expression: MIT\n",
        )
        archive.writestr(
            f"{metadata_root}/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(f"{metadata_root}/licenses/LICENSE", b"MIT licence")
        for member, content in (members or {}).items():
            archive.writestr(member, content)
    return path


def _project(tmp_path: Path) -> Path:
    return _wheel(
        tmp_path / "project.whl",
        members={
            "amigafs/__init__.py": b'__version__ = "0.1.0"\n',
            "amigafs/cli.py": b"def main(): return 0\n",
            "amigafs/_vendor/amiganut/__init__.py": b"ENGINE = True\n",
            "amigafs_nautilus/__init__.py": b"NAUTILUS = True\n",
        },
    )


def test_vendored_engine_is_verified_against_its_manifest() -> None:
    inventory = vendored_inventory()
    engine = inventory["amiganut"]
    assert engine["license"] == "MIT"
    assert len(engine["source_commit"]) == 40
    assert engine["upstream"].startswith("https://github.com/")
    assert engine["patches"] == [
        "0001-amiganut-reader-reopen.patch",
        "0002-amiganut-linear-allocation.patch",
    ]


def test_a_modified_vendored_engine_cannot_be_packaged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = json.loads(debian_package.VENDOR_MANIFEST.read_text(encoding="utf-8"))
    name = next(iter(manifest["files"]))
    manifest["files"][name] = "0" * 64
    tampered = tmp_path / "VENDORED.json"
    tampered.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(debian_package, "VENDOR_MANIFEST", tampered)
    with pytest.raises(RuntimeError, match="does not match its manifest"):
        vendored_inventory()
    tampered.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="manifest is incomplete"):
        vendored_inventory()
    monkeypatch.setattr(debian_package, "VENDOR_MANIFEST", tmp_path / "missing.json")
    with pytest.raises(RuntimeError, match="no readable manifest"):
        vendored_inventory()


def test_staging_builds_one_system_package_with_desktop_assets(tmp_path: Path) -> None:
    root = tmp_path / "root"

    files, inventory = stage_package(
        root,
        project_wheel=_project(tmp_path),
        version="0.1.0",
        epoch=1_700_000_000,
    )

    assert inventory["amiganut"]["version"]
    assert "/usr/bin/amigafs" in files
    assert "/usr/lib/python3/dist-packages/amigafs/_vendor/amiganut/__init__.py" in files
    assert "/usr/share/nautilus-python/extensions/nautilus_amigafs.py" in files
    assert "/usr/share/mime/packages/amigafs.xml" in files
    assert "/usr/share/doc/nautilus-amigafs/copyright" in files
    control = (root / "DEBIAN/control").read_text(encoding="utf-8")
    assert "Package: nautilus-amigafs" in control
    assert "python3-pyfuse3" in control
    assert "Recommends: pkexec, polkitd" in control
    assert f"X-Amiganut-Version: {inventory['amiganut']['version']}" in control
    assert "oaknut" not in control.casefold() and "acorn" not in control.casefold()
    assert (root / "DEBIAN/postinst").stat().st_mode & 0o111
    assert all(path.stat().st_mode & 0o777 == 0o755 for path in root.rglob("*") if path.is_dir())
    assert all(int(path.stat().st_mtime) == 1_700_000_000 for path in root.rglob("*"))


def test_device_helper_is_installed_root_runnable_and_self_contained(tmp_path: Path) -> None:
    root = tmp_path / "root"
    files, _inventory = stage_package(
        root, project_wheel=_project(tmp_path), version="0.1.0", epoch=1_700_000_000
    )
    assert "/usr/libexec/amigafs/amigafs-device-helper" in files
    assert "/usr/share/polkit-1/actions/org.amigafs.device-helper.policy" in files
    helper = root / "usr/libexec/amigafs/amigafs-device-helper"
    mode = stat.S_IMODE(helper.stat().st_mode)
    assert mode == 0o755
    # Nothing in the package is setuid; privilege comes only from polkit.
    assert not any(
        path.stat().st_mode & (stat.S_ISUID | stat.S_ISGID)
        for path in root.rglob("*")
        if path.is_file()
    )
    source = helper.read_text(encoding="utf-8")
    assert source.startswith("#!/usr/bin/python3 -I\n")
    assert "import amigafs" not in source and "from amigafs" not in source
    assert source == debian_package.HELPER_SOURCE.read_text(encoding="utf-8")


def test_polkit_policy_authorises_exactly_the_installed_helper() -> None:
    document = xml.dom.minidom.parseString(POLKIT_POLICY.read_bytes())
    (action,) = document.getElementsByTagName("action")
    assert action.getAttribute("id") == "org.amigafs.open-physical-disc"
    annotations = {
        node.getAttribute("key"): node.firstChild.data
        for node in action.getElementsByTagName("annotate")
    }
    assert annotations["org.freedesktop.policykit.exec.path"] == (
        "/" + debian_package.HELPER_PATH.as_posix()
    )
    defaults = {
        node.tagName: node.firstChild.data
        for node in action.getElementsByTagName("defaults")[0].childNodes
        if node.nodeType == node.ELEMENT_NODE
    }
    # Nobody opens a disc without administrator authentication.
    assert defaults == {
        "allow_any": "auth_admin",
        "allow_inactive": "auth_admin",
        "allow_active": "auth_admin_keep",
    }


def test_a_helper_that_could_be_influenced_by_its_caller_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unsafe = tmp_path / "helper.py"
    unsafe.write_text("#!/usr/bin/env python3\nprint('hello')\n", encoding="utf-8")
    monkeypatch.setattr(debian_package, "HELPER_SOURCE", unsafe)
    with pytest.raises(RuntimeError, match="isolated mode"):
        stage_package(tmp_path / "root", project_wheel=_project(tmp_path), version="0.1.0", epoch=1)
    unsafe.write_text("#!/usr/bin/python3 -I\nfrom amigafs.core import devices\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="must not import the amigafs package"):
        stage_package(
            tmp_path / "second", project_wheel=_project(tmp_path), version="0.1.0", epoch=1
        )


def test_staging_refuses_unsafe_or_overlapping_members(tmp_path: Path) -> None:
    overlapping = _wheel(
        tmp_path / "overlap.whl",
        members={"amigafs/__init__.py": b"one", "amigafs/../amigafs/__init__.py": b"two"},
    )
    with pytest.raises(RuntimeError, match="unsafe member"):
        stage_package(tmp_path / "root", project_wheel=overlapping, version="0.1.0", epoch=1)
    escaping = _wheel(tmp_path / "escape.whl", members={"/etc/passwd": b"root"})
    with pytest.raises(RuntimeError, match="unsafe member"):
        stage_package(tmp_path / "second", project_wheel=escaping, version="0.1.0", epoch=1)
    data_layout = _wheel(
        tmp_path / "data.whl", members={"nautilus_amigafs-0.1.0.data/scripts/run": b"#!/bin/sh"}
    )
    with pytest.raises(RuntimeError, match="data-layout"):
        stage_package(tmp_path / "third", project_wheel=data_layout, version="0.1.0", epoch=1)
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "file").write_text("x", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not empty"):
        stage_package(occupied, project_wheel=_project(tmp_path), version="0.1.0", epoch=1)
