import zipfile
from pathlib import Path

from tools.build_addon import create_bundle


def test_addon_bundle_contains_wheel_installer_and_instructions(tmp_path: Path) -> None:
    wheel = tmp_path / "nautilus_amigafs-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    installer = tmp_path / "user_install.py"
    installer.write_text("# installer\n", encoding="utf-8")
    output = tmp_path / "output"

    bundle = create_bundle(wheel, installer, output, version="0.1.0", epoch=1_700_000_000)
    first = bundle.read_bytes()

    with zipfile.ZipFile(bundle) as archive:
        assert set(archive.namelist()) == {wheel.name, "install.py", "INSTALL.txt"}
        instructions = archive.read("INSTALL.txt").decode()
        assert "python3 install.py --restart install" in instructions
        assert "python3-dev" in instructions
        assert "Unmount every AmigaFS image first" in instructions
        assert "~/.local/share/nautilus-amigafs" in instructions

    assert create_bundle(wheel, installer, output, version="0.1.0", epoch=1_700_000_000) == bundle
    assert bundle.read_bytes() == first
