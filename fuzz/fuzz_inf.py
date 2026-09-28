#!/usr/bin/env python3
"""Coverage-guided target for the untrusted INF metadata sidecar parser."""

import sys
from contextlib import suppress

import atheris

with atheris.instrument_imports():
    from amigafs.core.transfer import format_inf_record, parse_inf_record
    from amigafs.errors import AmigaFSError


def test_one_input(data: bytes) -> None:
    with suppress(AmigaFSError):
        record = parse_inf_record(data)
        # Whatever parses must be expressible again, or be refused cleanly.
        format_inf_record(record.name or "File", record.length or 0, record.metadata)


def main() -> None:
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
