"""Shared gettext setup for desktop-facing AmigaFS components."""

from __future__ import annotations

import gettext
import os
from pathlib import Path

DOMAIN = "amigafs"


def locale_directory() -> Path:
    """Return the packaged locale directory, or an explicit development override."""

    override = os.environ.get("AMIGAFS_LOCALE_DIR")
    return Path(override) if override else Path(__file__).with_name("locale")


def translation() -> gettext.NullTranslations:
    """Load the active catalogue while retaining English as a safe fallback."""

    return gettext.translation(DOMAIN, localedir=locale_directory(), fallback=True)


_catalogue = translation()
_ = _catalogue.gettext
ngettext = _catalogue.ngettext


def N_(message: str) -> str:
    """Mark a deferred message for extraction without translating it yet."""

    return message


__all__ = ["DOMAIN", "N_", "_", "locale_directory", "ngettext", "translation"]
