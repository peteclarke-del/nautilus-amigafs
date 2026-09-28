"""Hand a double-clicked image to the sibling mounter that recognises it.

Files chooses the application for a double-click from the name of a file, and
several retro platforms share suffixes such as ``.adf`` and ``.hdf``. Whichever
mounter Files chose therefore looks at the content, and passes an image that
belongs to a sibling on to it. Nothing is handed on unless the sibling says,
from the content, that the image is its own.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

SIBLINGS = ("acornfs",)
CLAIM_COMMAND = "desktop-claims"
OPEN_COMMAND = "desktop-open"
HANDED_OFF_OPTION = "--handed-off"
CLAIM_TIMEOUT = 10


def _installed(name: str) -> str | None:
    resolved = shutil.which(name)
    if resolved is not None:
        return resolved
    launcher = Path.home() / ".local" / "bin" / name
    if launcher.is_file() and os.access(launcher, os.X_OK):
        return str(launcher)
    return None


def sibling_claiming(path: Path, environment: Mapping[str, str]) -> str | None:
    """Return the installed sibling that recognises the content, if one does.

    A sibling that is missing, too old to be asked, slow or failing claims
    nothing.
    """

    for name in SIBLINGS:
        executable = _installed(name)
        if executable is None:
            continue
        try:
            answer = subprocess.run(
                [executable, CLAIM_COMMAND, str(path)],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=dict(environment),
                timeout=CLAIM_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if answer.returncode == 0:
            return executable
    return None


def hand_off(executable: str, path: Path, environment: Mapping[str, str]) -> None:
    """Start the sibling on the image, telling it not to pass the image back."""

    subprocess.Popen(
        [executable, OPEN_COMMAND, HANDED_OFF_OPTION, str(path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=dict(environment),
        start_new_session=True,
    )


__all__ = ["CLAIM_COMMAND", "HANDED_OFF_OPTION", "SIBLINGS", "hand_off", "sibling_claiming"]
