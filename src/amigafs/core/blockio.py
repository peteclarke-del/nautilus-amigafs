"""Locked, transactional access to one Amiga medium.

An :class:`ImageStore` owns the single descriptor through which an image file,
decoded workspace or physical disc is read and written. Every filesystem driver
reaches the medium through a :class:`StoreReader`, so locking, external-change
detection, per-operation rollback and crash recovery are applied in one place.

A mutation runs inside a transaction. Its writes are held in memory and the
medium is untouched until :meth:`ImageStore.commit`. Abandoning the transaction
is therefore a complete rollback. At commit the previous content of every chunk
about to change is appended to the session's undo journal and synchronised
before the first byte reaches the medium, so an interrupted session can always
be returned to its pre-mount state.
"""

from __future__ import annotations

import errno
import fcntl
import os
import stat
import struct
import zlib
from collections.abc import Callable, Iterator
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO

from amigafs._vendor.amiganut.errors import DataError
from amigafs._vendor.amiganut.filesystem.blocks import BLOCK_SIZE, BlockReader
from amigafs.errors import AmigaFSError
from amigafs.i18n import _

CHUNK_BYTES = 4096
JOURNAL_MAGIC = b"AMIGAFS-UNDO\x00\x01\x00\x00"
_RECORD_HEADER = struct.Struct(">QII")
_IO_BYTES = 8 * 1024 * 1024
DEFAULT_TRANSACTION_BYTES = 1024 * 1024 * 1024

Signature = tuple[int, int, int, int, int]


def media_size(descriptor: int) -> int:
    """Return the byte length of an open regular file or block device."""

    details = os.fstat(descriptor)
    if stat.S_ISBLK(details.st_mode):
        return os.lseek(descriptor, 0, os.SEEK_END)
    return details.st_size


def _pread_exact(descriptor: int, length: int, offset: int) -> bytes:
    parts: list[bytes] = []
    remaining = length
    position = offset
    while remaining:
        chunk = os.pread(descriptor, min(remaining, _IO_BYTES), position)
        if not chunk:
            break
        parts.append(chunk)
        remaining -= len(chunk)
        position += len(chunk)
    data = b"".join(parts)
    if len(data) < length:
        data = data.ljust(length, b"\0")
    return data


def _pwrite_exact(descriptor: int, data: bytes | bytearray | memoryview, offset: int) -> None:
    view = memoryview(data)
    position = offset
    while view:
        written = os.pwrite(descriptor, view[:_IO_BYTES], position)
        if written <= 0:
            raise OSError(errno.EIO, "short write to the image")
        view = view[written:]
        position += written


class UndoJournal:
    """Append-only before-images for one writable session."""

    def __init__(self, path: Path, handle: BinaryIO, recorded: set[int]) -> None:
        self.path = path
        self._handle = handle
        self._recorded = recorded

    @classmethod
    def create(cls, path: Path) -> UndoJournal:
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
        )
        handle = os.fdopen(descriptor, "wb")
        try:
            handle.write(JOURNAL_MAGIC)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            handle.close()
            with suppress(OSError):
                path.unlink()
            raise
        return cls(path, handle, set())

    def __contains__(self, offset: int) -> bool:
        return offset in self._recorded

    @property
    def recorded_bytes(self) -> int:
        return len(self._recorded) * CHUNK_BYTES

    def record(self, before_images: list[tuple[int, bytes]]) -> None:
        """Durably append before-images that are not yet in the journal."""

        pending = [(offset, data) for offset, data in before_images if offset not in self._recorded]
        if not pending:
            return
        for offset, data in pending:
            self._handle.write(_RECORD_HEADER.pack(offset, len(data), zlib.crc32(data)))
            self._handle.write(data)
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._recorded.update(offset for offset, _data in pending)

    def close(self) -> None:
        with suppress(OSError, ValueError):
            self._handle.close()


def read_journal(path: Path) -> Iterator[tuple[int, bytes]]:
    """Yield every complete before-image, stopping at a torn final record.

    A record is synchronised before the write it protects begins, so a record
    that is short or fails its checksum describes a write that never started.
    """

    with path.open("rb") as handle:
        if handle.read(len(JOURNAL_MAGIC)) != JOURNAL_MAGIC:
            raise AmigaFSError(_("The recovery journal has an unrecognised header."))
        while True:
            header = handle.read(_RECORD_HEADER.size)
            if len(header) < _RECORD_HEADER.size:
                return
            offset, length, checksum = _RECORD_HEADER.unpack(header)
            if length == 0 or length > CHUNK_BYTES:
                return
            data = handle.read(length)
            if len(data) < length or zlib.crc32(data) != checksum:
                return
            yield offset, data


def apply_journal(
    path: Path,
    descriptor: int,
    *,
    size: int,
    checkpoint: Callable[[], None] | None = None,
) -> int:
    """Restore every journalled before-image to an open medium."""

    restored = 0
    for offset, data in read_journal(path):
        if checkpoint is not None:
            checkpoint()
        if offset + len(data) > size:
            raise AmigaFSError(
                _("The recovery journal describes data beyond the end of the image.")
            )
        _pwrite_exact(descriptor, data, offset)
        restored += len(data)
    os.fsync(descriptor)
    return restored


class ImageStore:
    """One locked medium with in-memory transactions and an optional undo journal."""

    def __init__(
        self,
        path: Path,
        handle: BinaryIO,
        *,
        writable: bool,
        is_device: bool,
        display_name: str | None = None,
        max_transaction_bytes: int = DEFAULT_TRANSACTION_BYTES,
    ) -> None:
        self.path = path
        self.handle = handle
        self.writable = writable
        self.is_device = is_device
        self.display_name = display_name or path.name
        self.size = media_size(handle.fileno())
        self.journal: UndoJournal | None = None
        self._overlay: dict[int, bytearray] | None = None
        self._max_transaction_bytes = max_transaction_bytes
        self._closed = False
        self.expected_signature: Signature | None = self.signature() if writable else None

    # ---- opening -----------------------------------------------------
    @classmethod
    def open(
        cls,
        selected: str | Path,
        *,
        writable: bool,
        allow_device: bool = False,
        check_links: bool = True,
    ) -> ImageStore:
        """Open, lock and identity-check one regular image or block device."""

        path = Path(selected).expanduser().resolve(strict=True)
        flags = (os.O_RDWR if writable else os.O_RDONLY) | os.O_CLOEXEC | os.O_NOCTTY
        try:
            descriptor = os.open(path, flags | os.O_NONBLOCK)
        except OSError as exc:
            if writable and exc.errno in {errno.EACCES, errno.EROFS, errno.EPERM}:
                raise AmigaFSError(
                    _(
                        "The image is on read-only storage or is not writable by this user. "
                        "Open it read-only, or copy it to writable local storage."
                    )
                ) from exc
            raise
        return cls.adopt(
            path,
            descriptor,
            writable=writable,
            allow_device=allow_device,
            check_links=check_links,
        )

    @classmethod
    def adopt(
        cls,
        path: Path,
        descriptor: int,
        *,
        writable: bool,
        allow_device: bool = False,
        check_links: bool = True,
        display_name: str | None = None,
    ) -> ImageStore:
        """Take ownership of an already open descriptor, then lock and verify it."""

        try:
            opened = os.fstat(descriptor)
            is_device = stat.S_ISBLK(opened.st_mode)
            if is_device and not allow_device:
                raise AmigaFSError(
                    _("{path} is a block device; physical discs must be opened explicitly.").format(
                        path=path
                    )
                )
            if not is_device and not stat.S_ISREG(opened.st_mode):
                raise AmigaFSError(
                    _("{path} is neither a regular file nor a block device.").format(path=path)
                )
            # O_NONBLOCK protected the open itself; ordinary I/O must block.
            fcntl.fcntl(
                descriptor,
                fcntl.F_SETFL,
                fcntl.fcntl(descriptor, fcntl.F_GETFL) & ~os.O_NONBLOCK,
            )
            try:
                fcntl.flock(
                    descriptor, (fcntl.LOCK_EX if writable else fcntl.LOCK_SH) | fcntl.LOCK_NB
                )
            except BlockingIOError as exc:
                raise AmigaFSError(
                    _("The image is mounted or open in another AmigaFS process.")
                ) from exc
            current = path.stat()
            if is_device:
                if not stat.S_ISBLK(current.st_mode) or current.st_rdev != opened.st_rdev:
                    raise AmigaFSError(
                        _("The device changed while AmigaFS was opening it: {path}").format(
                            path=path
                        )
                    )
            else:
                unfollowed = path.stat(follow_symlinks=False)
                if (opened.st_dev, opened.st_ino) != (unfollowed.st_dev, unfollowed.st_ino):
                    raise AmigaFSError(
                        _("The image changed while AmigaFS was opening it: {path}").format(
                            path=path
                        )
                    )
                if writable and check_links and opened.st_nlink != 1:
                    raise AmigaFSError(
                        _(
                            "Writable mounting refuses an image with hard links: {path}. "
                            "Copy it to a uniquely owned file first."
                        ).format(path=path)
                    )
            handle = os.fdopen(descriptor, "r+b" if writable else "rb", buffering=0)
        except BaseException:
            with suppress(OSError):
                os.close(descriptor)
            raise
        return cls(
            path,
            handle,
            writable=writable,
            is_device=is_device,
            display_name=display_name,
        )

    # ---- identity ----------------------------------------------------
    def signature(self) -> Signature:
        """Return the identity and modification state of the open medium."""

        opened = os.fstat(self.handle.fileno())
        current = self.path.stat()
        if self.is_device:
            return (current.st_rdev, opened.st_rdev, media_size(self.handle.fileno()), 0, 0)
        return (
            current.st_dev,
            current.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        )

    def verify_unchanged(self) -> None:
        """Fail closed when something other than this session altered the medium."""

        if self.expected_signature is None:
            raise AmigaFSError(_("This image has no writable identity signature."))
        try:
            current = self.signature()
        except OSError as exc:
            raise AmigaFSError(
                _("The image can no longer be identified: {error}").format(error=exc)
            ) from exc
        if current != self.expected_signature:
            raise AmigaFSError(_("The image changed outside AmigaFS; further writes are blocked."))

    # ---- reading and writing ----------------------------------------
    def read(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0 or offset + length > self.size:
            raise DataError("The requested range is outside this medium.")
        if length == 0:
            return b""
        data = _pread_exact(self.handle.fileno(), length, offset)
        overlay = self._overlay
        if not overlay:
            return data
        first = offset - offset % CHUNK_BYTES
        last = offset + length
        patched: bytearray | None = None
        for chunk_offset in range(first, last, CHUNK_BYTES):
            chunk = overlay.get(chunk_offset)
            if chunk is None:
                continue
            if patched is None:
                patched = bytearray(data)
            start = max(offset, chunk_offset)
            end = min(last, chunk_offset + len(chunk))
            patched[start - offset : end - offset] = chunk[
                start - chunk_offset : end - chunk_offset
            ]
        return data if patched is None else bytes(patched)

    def write(self, offset: int, data: bytes) -> None:
        if not self.writable:
            raise DataError("This medium is open read-only.")
        overlay = self._overlay
        if overlay is None:
            raise AmigaFSError(_("A write was attempted outside an image transaction."))
        if offset < 0 or offset + len(data) > self.size:
            raise DataError("The requested range is outside this medium.")
        view = memoryview(data)
        position = offset
        while view:
            chunk_offset = position - position % CHUNK_BYTES
            within = position - chunk_offset
            chunk = overlay.get(chunk_offset)
            if chunk is None:
                if (len(overlay) + 1) * CHUNK_BYTES > self._max_transaction_bytes:
                    raise AmigaFSError(
                        _("One operation would change more data than AmigaFS can stage safely.")
                    )
                chunk_length = min(CHUNK_BYTES, self.size - chunk_offset)
                if within == 0 and len(view) >= chunk_length:
                    chunk = bytearray(chunk_length)
                else:
                    chunk = bytearray(
                        _pread_exact(self.handle.fileno(), chunk_length, chunk_offset)
                    )
                overlay[chunk_offset] = chunk
            take = min(len(view), len(chunk) - within)
            chunk[within : within + take] = view[:take]
            view = view[take:]
            position += take

    # ---- transactions ------------------------------------------------
    @property
    def in_transaction(self) -> bool:
        return self._overlay is not None

    @property
    def staged_bytes(self) -> int:
        return sum(len(chunk) for chunk in (self._overlay or {}).values())

    def begin(self) -> None:
        if not self.writable:
            raise PermissionError(_("image is read-only"))
        if self._overlay is not None:
            raise AmigaFSError(_("An image transaction is already in progress."))
        self._overlay = {}

    def rollback(self) -> None:
        """Discard every staged write. The medium was never touched."""

        self._overlay = None

    def commit(self, *, fault: Callable[[str], None] | None = None) -> None:
        """Journal the before-images, apply the staged writes and synchronise."""

        overlay = self._overlay
        if overlay is None:
            raise AmigaFSError(_("No image transaction is in progress."))
        descriptor = self.handle.fileno()
        offsets = sorted(overlay)
        changed: list[int] = []
        before_images: list[tuple[int, bytes]] = []
        for offset in offsets:
            current = _pread_exact(descriptor, len(overlay[offset]), offset)
            if current == overlay[offset]:
                continue
            changed.append(offset)
            if self.journal is not None and offset not in self.journal:
                before_images.append((offset, current))
        if self.journal is not None:
            self.journal.record(before_images)
        if fault is not None:
            fault("commit.journalled")
        for offset in changed:
            _pwrite_exact(descriptor, overlay[offset], offset)
        if changed:
            os.fsync(descriptor)
        self._overlay = None
        self.expected_signature = self.signature()

    def sync(self) -> None:
        if self.writable and not self._closed:
            os.fsync(self.handle.fileno())
            self.expected_signature = self.signature()

    def reader(
        self,
        *,
        offset: int = 0,
        length: int | None = None,
        block_size: int = BLOCK_SIZE,
        writable: bool | None = None,
    ) -> StoreReader:
        return StoreReader(
            self,
            writable=self.writable if writable is None else writable and self.writable,
            offset=offset,
            length=length,
            block_size=block_size,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._overlay = None
        if self.journal is not None:
            self.journal.close()
        with suppress(OSError, ValueError):
            self.handle.close()


class StoreReader(BlockReader):
    """The vendored block-reader interface over an :class:`ImageStore`.

    Closing a reader never closes the store: filesystem drivers open and close
    several views of one medium, while the store's lock and descriptor must
    outlive all of them.
    """

    def __init__(
        self,
        store: ImageStore,
        *,
        writable: bool = False,
        offset: int = 0,
        length: int | None = None,
        block_size: int = BLOCK_SIZE,
    ) -> None:
        # The base initialiser opens its own handle by path, so it is
        # deliberately not called.
        self.store = store
        self.path = store.path
        self.writable = bool(writable) and store.writable
        self.block_size = int(block_size)
        if self.block_size <= 0:
            raise DataError("A block size must be positive.")
        self.offset = int(offset)
        if self.offset < 0 or self.offset > store.size:
            raise DataError("The partition starts beyond the end of the image.")
        available = store.size - self.offset
        self.length = available if length is None else min(int(length), available)
        if self.length < 0:
            raise DataError("A partition cannot have a negative length.")
        self.total_blocks = self.length // self.block_size

    def close(self) -> None:
        return None

    def read_block(self, number: int) -> bytes:
        if not 0 <= number < self.total_blocks:
            raise DataError(f"Block {number} is outside this volume.")
        return self.store.read(self.offset + number * self.block_size, self.block_size)

    def read_range(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0 or offset + length > self.length:
            raise DataError("The requested range is outside this volume.")
        return self.store.read(self.offset + offset, length)

    def write_block(self, number: int, data: bytes) -> None:
        if not self.writable:
            raise DataError("This volume is open read-only.")
        if not 0 <= number < self.total_blocks:
            raise DataError(f"Block {number} is outside this volume.")
        if len(data) != self.block_size:
            raise DataError("A block write must supply exactly one block.")
        self.store.write(self.offset + number * self.block_size, bytes(data))

    def write_range(self, offset: int, data: bytes) -> None:
        if not self.writable:
            raise DataError("This volume is open read-only.")
        if offset < 0 or offset + len(data) > self.length:
            raise DataError("The requested range is outside this volume.")
        self.store.write(self.offset + offset, bytes(data))

    def flush(self) -> None:
        return None

    def sync(self) -> None:
        # Ordering within one operation is provided by the transaction: nothing
        # reaches the medium until the whole operation commits.
        return None

    def reopen(
        self,
        *,
        writable: bool | None = None,
        offset: int | None = None,
        length: int | None = None,
        block_size: int | None = None,
    ) -> StoreReader:
        return StoreReader(
            self.store,
            writable=self.writable if writable is None else writable,
            offset=self.offset if offset is None else offset,
            length=self.length if length is None else length,
            block_size=self.block_size if block_size is None else block_size,
        )


__all__ = [
    "CHUNK_BYTES",
    "ImageStore",
    "StoreReader",
    "UndoJournal",
    "apply_journal",
    "media_size",
    "read_journal",
]
