from pathlib import Path
from unittest.mock import patch

from amigafs.i18n import DOMAIN, locale_directory, translation


def test_packaged_locale_directory_is_the_default(monkeypatch: object) -> None:
    monkeypatch.delenv("AMIGAFS_LOCALE_DIR", raising=False)  # type: ignore[attr-defined]

    assert locale_directory().name == "locale"
    assert locale_directory().parent.name == "amigafs"


def test_locale_directory_can_be_overridden_for_development(
    tmp_path: Path, monkeypatch: object
) -> None:
    monkeypatch.setenv("AMIGAFS_LOCALE_DIR", str(tmp_path))  # type: ignore[attr-defined]

    with patch("amigafs.i18n.gettext.translation") as load:
        translation()

    load.assert_called_once_with(DOMAIN, localedir=tmp_path, fallback=True)
