from __future__ import annotations

import os
from pathlib import Path

import pytest

from amigafs._vendor.amiganut.errors import DataError
from amigafs.core.blockio import (
    CHUNK_BYTES,
    ImageStore,
    UndoJournal,
    apply_journal,
    read_journal,
)
from amigafs.errors import AmigaFSError


def _image(tmp_path: Path, size: int = 3 * CHUNK_BYTES + 512) -> Path:
    path = tmp_path / "medium.img"
    path.write_bytes(bytes(index % 251 for index in range(size)))
    return path


def test_read_only_store_shares_its_lock_and_refuses_writes(tmp_path: Path) -> None:
    path = _image(tmp_path)
    first = ImageStore.open(path, writable=False)
    second = ImageStore.open(path, writable=False)
    try:
        assert first.read(0, 8) == path.read_bytes()[:8]
        with pytest.raises(PermissionError):
            first.begin()
        with pytest.raises(AmigaFSError, match="another AmigaFS process"):
            ImageStore.open(path, writable=True)
    finally:
        first.close()
        second.close()


def test_writable_store_is_exclusive_and_refuses_hard_links(tmp_path: Path) -> None:
    path = _image(tmp_path)
    store = ImageStore.open(path, writable=True)
    try:
        with pytest.raises(AmigaFSError, match="another AmigaFS process"):
            ImageStore.open(path, writable=False)
    finally:
        store.close()
    os.link(path, tmp_path / "second-name.img")
    with pytest.raises(AmigaFSError, match="hard links"):
        ImageStore.open(path, writable=True)


def test_block_devices_and_special_files_need_explicit_permission(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(AmigaFSError, match="neither a regular file nor a block device"):
        ImageStore.open(fifo, writable=False)


def test_writes_outside_a_transaction_are_refused(tmp_path: Path) -> None:
    store = ImageStore.open(_image(tmp_path), writable=True)
    try:
        with pytest.raises(AmigaFSError, match="outside an image transaction"):
            store.write(0, b"x")
    finally:
        store.close()


def test_staged_writes_are_visible_but_never_reach_the_medium_until_commit(
    tmp_path: Path,
) -> None:
    path = _image(tmp_path)
    original = path.read_bytes()
    store = ImageStore.open(path, writable=True)
    try:
        store.begin()
        # One write inside a chunk, one spanning a chunk boundary, one in the short tail.
        store.write(10, b"AAAA")
        store.write(CHUNK_BYTES - 2, b"BBBB")
        store.write(len(original) - 3, b"CCC")
        assert store.read(8, 8) == original[8:10] + b"AAAA" + original[14:16]
        assert store.read(CHUNK_BYTES - 4, 8) == (
            original[CHUNK_BYTES - 4 : CHUNK_BYTES - 2] + b"BBBB" + original[CHUNK_BYTES + 2 :][:2]
        )
        assert store.read(len(original) - 3, 3) == b"CCC"
        assert path.read_bytes() == original
        store.rollback()
        assert store.read(10, 4) == original[10:14]
        assert path.read_bytes() == original
    finally:
        store.close()


def test_commit_applies_every_staged_write_and_refreshes_the_signature(tmp_path: Path) -> None:
    path = _image(tmp_path)
    original = path.read_bytes()
    store = ImageStore.open(path, writable=True)
    try:
        store.begin()
        store.write(CHUNK_BYTES - 2, b"BBBB")
        store.commit()
        expected = bytearray(original)
        expected[CHUNK_BYTES - 2 : CHUNK_BYTES + 2] = b"BBBB"
        assert path.read_bytes() == bytes(expected)
        store.verify_unchanged()
        assert not store.in_transaction
    finally:
        store.close()


def test_ranges_outside_the_medium_are_refused(tmp_path: Path) -> None:
    path = _image(tmp_path)
    store = ImageStore.open(path, writable=True)
    try:
        with pytest.raises(DataError):
            store.read(store.size - 1, 2)
        store.begin()
        with pytest.raises(DataError):
            store.write(store.size - 1, b"xy")
        with pytest.raises(DataError):
            store.write(-1, b"x")
    finally:
        store.close()


def test_transaction_size_is_bounded(tmp_path: Path) -> None:
    path = _image(tmp_path, size=8 * CHUNK_BYTES)
    handle = path.open("r+b", buffering=0)
    store = ImageStore(
        path, handle, writable=True, is_device=False, max_transaction_bytes=2 * CHUNK_BYTES
    )
    try:
        store.begin()
        store.write(0, b"a")
        store.write(CHUNK_BYTES, b"b")
        with pytest.raises(AmigaFSError, match="more data than AmigaFS can stage"):
            store.write(2 * CHUNK_BYTES, b"c")
    finally:
        store.close()


def test_external_change_is_detected(tmp_path: Path) -> None:
    path = _image(tmp_path)
    store = ImageStore.open(path, writable=True)
    try:
        store.verify_unchanged()
        with path.open("r+b") as other:
            other.write(b"external")
        os.utime(path, ns=(1, 1))
        with pytest.raises(AmigaFSError, match="changed outside AmigaFS"):
            store.verify_unchanged()
    finally:
        store.close()


def test_journal_records_each_chunk_once_and_restores_the_pre_session_state(
    tmp_path: Path,
) -> None:
    path = _image(tmp_path)
    original = path.read_bytes()
    journal_path = tmp_path / "undo.journal"
    store = ImageStore.open(path, writable=True)
    store.journal = UndoJournal.create(journal_path)
    try:
        store.begin()
        store.write(5, b"first")
        store.commit()
        store.begin()
        store.write(7, b"second")
        store.write(2 * CHUNK_BYTES + 1, b"third")
        store.commit()
    finally:
        store.close()
    records = list(read_journal(journal_path))
    assert sorted(offset for offset, _data in records) == [0, 2 * CHUNK_BYTES]
    assert dict(records)[0] == original[:CHUNK_BYTES]
    assert path.read_bytes() != original
    with path.open("r+b") as handle:
        restored = apply_journal(journal_path, handle.fileno(), size=len(original))
    assert restored == 2 * CHUNK_BYTES
    assert path.read_bytes() == original


def test_unchanged_chunks_are_neither_journalled_nor_rewritten(tmp_path: Path) -> None:
    path = _image(tmp_path)
    original = path.read_bytes()
    journal_path = tmp_path / "undo.journal"
    store = ImageStore.open(path, writable=True)
    store.journal = UndoJournal.create(journal_path)
    try:
        store.begin()
        store.write(0, original[:64])
        store.commit()
    finally:
        store.close()
    assert list(read_journal(journal_path)) == []
    assert path.read_bytes() == original


def test_torn_final_journal_record_is_ignored(tmp_path: Path) -> None:
    path = _image(tmp_path)
    original = path.read_bytes()
    journal_path = tmp_path / "undo.journal"
    store = ImageStore.open(path, writable=True)
    store.journal = UndoJournal.create(journal_path)
    try:
        store.begin()
        store.write(0, b"one")
        store.commit()
        store.begin()
        store.write(CHUNK_BYTES, b"two")
        store.commit()
    finally:
        store.close()
    complete = journal_path.read_bytes()
    journal_path.write_bytes(complete[:-100])
    assert [offset for offset, _data in read_journal(journal_path)] == [0]
    damaged = bytearray(complete)
    damaged[-1] ^= 0xFF
    journal_path.write_bytes(bytes(damaged))
    assert [offset for offset, _data in read_journal(journal_path)] == [0]
    journal_path.write_bytes(b"not a journal at all")
    with pytest.raises(AmigaFSError, match="unrecognised header"):
        list(read_journal(journal_path))
    assert original


def test_a_fault_between_journal_and_write_leaves_a_restorable_medium(tmp_path: Path) -> None:
    path = _image(tmp_path)
    original = path.read_bytes()
    journal_path = tmp_path / "undo.journal"
    store = ImageStore.open(path, writable=True)
    store.journal = UndoJournal.create(journal_path)

    def fault(stage: str) -> None:
        if stage == "commit.journalled":
            raise OSError("power lost")

    try:
        store.begin()
        store.write(100, b"doomed")
        with pytest.raises(OSError, match="power lost"):
            store.commit(fault=fault)
    finally:
        store.close()
    assert path.read_bytes() == original
    with path.open("r+b") as handle:
        apply_journal(journal_path, handle.fileno(), size=len(original))
    assert path.read_bytes() == original


def test_reader_views_share_one_store_and_survive_being_closed(tmp_path: Path) -> None:
    path = _image(tmp_path, size=16 * 512)
    store = ImageStore.open(path, writable=True)
    try:
        whole = store.reader()
        window = whole.window(4, 8)
        wide = window.reopen(block_size=1024)
        assert whole.total_blocks == 16
        assert window.total_blocks == 8
        assert wide.total_blocks == 4
        assert window.read_block(0) == whole.read_block(4)
        assert wide.read_block(1) == whole.read_block(6) + whole.read_block(7)
        assert window.read_all() == path.read_bytes()[4 * 512 : 12 * 512]
        window.close()
        store.begin()
        window.write_block(0, b"\xaa" * 512)
        assert whole.read_block(4) == b"\xaa" * 512
        store.rollback()
        with pytest.raises(DataError):
            window.read_block(8)
        with pytest.raises(DataError):
            window.write_block(0, b"short")
        read_only = whole.reopen(writable=False)
        with pytest.raises(DataError, match="read-only"):
            read_only.write_block(0, bytes(512))
    finally:
        store.close()
