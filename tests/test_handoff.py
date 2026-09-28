from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

from amigafs import handoff
from amigafs.handoff import hand_off, sibling_claiming


def _sibling(directory: Path, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    executable = directory / "acornfs"
    executable.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


def _environment(directory: Path) -> dict[str, str]:
    return {"PATH": f"{directory}:/usr/bin:/bin"}


def test_a_sibling_that_recognises_the_content_claims_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "asked"
    executable = _sibling(tmp_path / "bin", f'printf "%s\\n" "$@" > "{log}"; exit 0')
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    image = tmp_path / "odd name; $(reboot).adf"
    assert sibling_claiming(image, _environment(tmp_path / "bin")) == str(executable)
    assert log.read_text(encoding="utf-8").splitlines() == ["desktop-claims", str(image)]


@pytest.mark.parametrize(
    "body",
    [
        "exit 1",
        # An older sibling does not know the question.
        "exit 2",
        "sleep 5",
    ],
)
def test_a_sibling_that_refuses_fails_or_stalls_claims_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    _sibling(tmp_path / "bin", body)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setattr(handoff, "CLAIM_TIMEOUT", 0.2)
    assert sibling_claiming(tmp_path / "disc.adf", _environment(tmp_path / "bin")) is None


def test_a_missing_sibling_claims_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert sibling_claiming(tmp_path / "disc.adf", {}) is None


def test_a_sibling_installed_for_the_user_alone_is_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _sibling(tmp_path / ".local" / "bin", "exit 0")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert sibling_claiming(tmp_path / "disc.adf", _environment(tmp_path)) == str(executable)


def test_the_image_is_handed_over_once_and_without_a_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(
        subprocess, "Popen", lambda command, **keywords: calls.append((command, keywords))
    )
    image = tmp_path / "odd name; $(reboot).adf"
    hand_off("/usr/bin/acornfs", image, {"HOME": "/home/alice"})
    ((command, keywords),) = calls
    assert command == ["/usr/bin/acornfs", "desktop-open", "--handed-off", str(image)]
    assert keywords["env"] == {"HOME": "/home/alice"}
    assert keywords["start_new_session"] is True
    assert "shell" not in keywords
