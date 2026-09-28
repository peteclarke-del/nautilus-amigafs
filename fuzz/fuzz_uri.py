#!/usr/bin/env python3
"""Coverage-guided target for desktop URI and local-path handling."""

import sys
from contextlib import suppress

import atheris

with atheris.instrument_imports():
    from amigafs.desktop import local_image_reference
    from amigafs.errors import AmigaFSError


def test_one_input(data: bytes) -> None:
    reference = data.decode("utf-8", errors="surrogateescape")
    with suppress(AmigaFSError):
        local_image_reference(reference)


def main() -> None:
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
