"""Persistent checkpoints for recoverable writable image sessions.

A writable session on an image file or physical disc keeps an undo journal: the
previous content of every chunk the session changes, made durable before the
change is written. Restoring the journal returns the medium to its pre-mount
state, and the cost is proportional to what changed rather than to the size of
the disc.

A session on a container (a compressed, track-level or physical-floppy source)
edits a private decoded working copy instead. The source is replaced only when
the session closes cleanly, so an interrupted session leaves the source
untouched and the working copy available to salvage.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pwd
import shutil
import stat
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from amigafs.core.blockio import (
    CHUNK_BYTES,
    ImageStore,
    UndoJournal,
    apply_journal,
    media_size,
    read_journal,
)
from amigafs.errors import AmigaFSError
from amigafs.i18n import _
from amigafs.operations import CancellationCheck, cancellation_point
from amigafs.safe_paths import atomic_write_private_text, ensure_private_directory

MANIFEST_VERSION = 1
FINGERPRINT_BYTES = 64 * 1024
KIND_JOURNAL = "journal"
KIND_WORKSPACE = "workspace"
JOURNAL_NAME = "undo.journal"
WORKSPACE_NAME = "workspace.img"


def state_root() -> Path:
    configured = os.environ.get("XDG_STATE_HOME")
    if configured:
        return Path(configured).expanduser() / "amigafs" / "recovery"
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    return home / ".local" / "state" / "amigafs" / "recovery"


def _identity(path: Path) -> str:
    raw = str(path).encode("utf-8", "surrogateescape")
    return hashlib.sha256(raw).hexdigest()


def canonical_source(selected: str | Path) -> Path:
    """Return the name a source is recorded under.

    A stable ``/dev/disk/by-id`` name is kept as given, because the kernel name
    it points to can belong to a different disc after the next replug.
    """

    path = Path(selected).expanduser()
    absolute = Path(os.path.abspath(path))
    if absolute.parts[:4] == ("/", "dev", "disk", "by-id"):
        return absolute
    return path.resolve()


@dataclass(frozen=True, slots=True)
class RecoveryInfo:
    version: int
    identity: str
    image_path: str
    kind: str
    created_at: str
    state: str
    size: int
    is_device: bool
    fingerprint: str | None = None
    detail: str = ""


def checkpoint_directory(selected: str | Path) -> Path:
    return state_root() / _identity(canonical_source(selected))


def _manifest_path(selected: str | Path) -> Path:
    return checkpoint_directory(selected) / "manifest.json"


def _ensure_checkpoint_directory(directory: Path) -> None:
    """Create AmigaFS-owned state without following symlinked descendants."""

    root = state_root()
    ensure_private_directory(directory, anchor=root.parent.parent)


def _write_manifest(path: Path, info: RecoveryInfo) -> None:
    content = json.dumps(asdict(info), indent=2) + "\n"
    root = state_root()
    atomic_write_private_text(path, content, anchor=root.parent.parent)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def pending_recovery(selected: str | Path) -> RecoveryInfo | None:
    path = _manifest_path(selected)
    if not path.exists():
        return None
    try:
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("version") != MANIFEST_VERSION:
            raise ValueError("unsupported manifest version")
        return RecoveryInfo(**payload)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AmigaFSError(
            _("The recovery manifest is unreadable: {path}: {error}").format(path=path, error=exc)
        ) from exc


def pending_physical_recoveries() -> tuple[RecoveryInfo, ...]:
    """Return interrupted sessions on sources that have no file to right-click.

    A physical disc or floppy drive cannot be selected in a file manager, so the
    desktop lists their pending recoveries from the background menu instead.
    """

    root = state_root()
    try:
        directories = sorted(root.iterdir())
    except OSError:
        return ()
    found: list[RecoveryInfo] = []
    for directory in directories:
        manifest = directory / "manifest.json"
        try:
            if directory.is_symlink() or not manifest.is_file():
                continue
            payload: dict[str, Any] = json.loads(manifest.read_text(encoding="utf-8"))
            if payload.get("version") != MANIFEST_VERSION:
                continue
            info = RecoveryInfo(**payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if info.is_device or info.detail == "greaseweazle":
            found.append(info)
    return tuple(found)


def _fingerprint_regions(size: int) -> tuple[tuple[int, int], ...]:
    """Return the start, middle and end of a medium, where its identity lives.

    A hard disc names itself in its first blocks. A floppy or a bare volume
    keeps its root block, with the volume name and dates, in the middle.
    """

    length = min(size, FINGERPRINT_BYTES)
    if size <= 3 * FINGERPRINT_BYTES:
        return ((0, size),)
    middle = (size // 2) - (size // 2) % CHUNK_BYTES
    return ((0, length), (middle, length), (size - length, length))


def _fingerprint(descriptor: int, size: int, before_images: Iterable[tuple[int, bytes]]) -> str:
    """Hash the identifying regions of a medium as they were before the session."""

    regions = [
        (start, bytearray(os.pread(descriptor, length, start).ljust(length, b"\0")))
        for start, length in _fingerprint_regions(size)
    ]
    for offset, data in before_images:
        for start, content in regions:
            low = max(start, offset)
            high = min(start + len(content), offset + len(data))
            if low < high:
                content[low - start : high - start] = data[low - offset : high - offset]
    digest = hashlib.sha256()
    for start, content in regions:
        digest.update(start.to_bytes(8, "big"))
        digest.update(bytes(content))
    return digest.hexdigest()


def _remove_directory(directory: Path, names: tuple[str, ...]) -> None:
    # The manifest is the authority for pending recovery. Remove and sync it
    # first so a crash cannot advertise already-deleted recovery data.
    (directory / "manifest.json").unlink(missing_ok=True)
    if directory.exists():
        _fsync_directory(directory)
    for name in names:
        (directory / name).unlink(missing_ok=True)
    if directory.exists():
        for candidate in directory.iterdir():
            if candidate.name.startswith("workspace") or candidate.name.startswith("source."):
                candidate.unlink(missing_ok=True)
        _fsync_directory(directory)
    with suppress(OSError):
        directory.rmdir()
    with suppress(OSError):
        directory.parent.rmdir()


class SessionCheckpoint:
    """The undo journal protecting one writable image or physical disc."""

    def __init__(self, source: Path, directory: Path, info: RecoveryInfo) -> None:
        self.source = source
        self.directory = directory
        self.info = info

    @classmethod
    def create(cls, source: str | Path, store: ImageStore) -> SessionCheckpoint:
        path = canonical_source(source)
        directory = checkpoint_directory(path)
        manifest = directory / "manifest.json"
        if manifest.exists():
            raise AmigaFSError(
                _(
                    "An interrupted writable session needs recovery. Run "
                    "'amigafs recover {path}' before mounting read-write."
                ).format(path=path)
            )
        _ensure_checkpoint_directory(directory)
        journal_path = directory / JOURNAL_NAME
        # A crash after a clean session removed its manifest may leave a
        # harmless orphan journal. It is not authoritative.
        journal_path.unlink(missing_ok=True)
        _fsync_directory(directory)
        info = RecoveryInfo(
            version=MANIFEST_VERSION,
            identity=_identity(path),
            image_path=str(path),
            kind=KIND_JOURNAL,
            created_at=datetime.now(UTC).isoformat(),
            state="ready",
            size=store.size,
            is_device=store.is_device,
            fingerprint=_fingerprint(store.handle.fileno(), store.size, ()),
        )
        try:
            store.journal = UndoJournal.create(journal_path)
            _fsync_directory(directory)
            _write_manifest(manifest, info)
        except Exception as exc:
            if store.journal is not None:
                store.journal.close()
                store.journal = None
            with suppress(OSError):
                _remove_directory(directory, (JOURNAL_NAME,))
            raise AmigaFSError(
                _("Could not create the writable recovery checkpoint: {error}").format(error=exc)
            ) from exc
        return cls(path, directory, info)

    def complete(self) -> None:
        _remove_directory(self.directory, (JOURNAL_NAME,))


class WorkspaceCheckpoint:
    """The retained working copy of one container or physical-floppy session."""

    def __init__(self, source: Path, directory: Path, info: RecoveryInfo) -> None:
        self.source = source
        self.directory = directory
        self.info = info

    @property
    def workspace_path(self) -> Path:
        return self.directory / WORKSPACE_NAME

    @classmethod
    def create(
        cls, source: str | Path, *, size: int, detail: str = "", suffix: str = ".img"
    ) -> WorkspaceCheckpoint:
        path = canonical_source(source)
        directory = checkpoint_directory(path)
        if (directory / "manifest.json").exists():
            raise AmigaFSError(
                _(
                    "An interrupted writable session needs recovery. Run "
                    "'amigafs recover {path}' before mounting read-write."
                ).format(path=path)
            )
        _ensure_checkpoint_directory(directory)
        info = RecoveryInfo(
            version=MANIFEST_VERSION,
            identity=_identity(path),
            image_path=str(path),
            kind=KIND_WORKSPACE,
            created_at=datetime.now(UTC).isoformat(),
            state="ready",
            size=size,
            is_device=False,
            detail=detail or suffix,
        )
        try:
            _write_manifest(directory / "manifest.json", info)
        except Exception as exc:
            raise AmigaFSError(
                _("Could not create the writable recovery checkpoint: {error}").format(error=exc)
            ) from exc
        return cls(path, directory, info)

    def complete(self) -> None:
        _remove_directory(self.directory, (WORKSPACE_NAME,))


def _open_for_recovery(path: Path, info: RecoveryInfo) -> int:
    if info.is_device:
        from amigafs.core.devices import open_device

        return open_device(path, writable=True)
    return os.open(path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)


def _restore_journal(
    image: Path, directory: Path, info: RecoveryInfo, cancelled: CancellationCheck | None
) -> None:
    journal = directory / JOURNAL_NAME
    if not journal.is_file():
        raise AmigaFSError(_("The recovery journal is missing and cannot be restored."))
    try:
        descriptor = _open_for_recovery(image, info)
    except OSError as exc:
        raise AmigaFSError(
            _("Could not open {path} to restore it: {error}").format(path=image, error=exc)
        ) from exc
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AmigaFSError(
                _("The image is mounted or open in another AmigaFS process.")
            ) from exc
        details = os.fstat(descriptor)
        if info.is_device != stat.S_ISBLK(details.st_mode):
            raise AmigaFSError(
                _("The recovery checkpoint was made for a different kind of medium.")
            )
        size = media_size(descriptor)
        if size != info.size:
            raise AmigaFSError(
                _(
                    "The medium is {actual} bytes but the checkpoint was made for {expected} "
                    "bytes. It is not the image this checkpoint belongs to."
                ).format(actual=size, expected=info.size)
            )
        if info.fingerprint is not None and (
            _fingerprint(descriptor, size, read_journal(journal)) != info.fingerprint
        ):
            raise AmigaFSError(
                _(
                    "The medium does not match the recovery checkpoint. A different disc or "
                    "image may be in its place; nothing was restored."
                )
            )
        # Every before-image is staged and verified readable above. Restoring
        # is idempotent, so an interruption here is resolved by running it again.
        cancellation_point(cancelled)
        apply_journal(journal, descriptor, size=size)
    finally:
        os.close(descriptor)


def salvage_workspace(selected: str | Path, destination: str | Path) -> Path:
    """Copy an interrupted session's decoded working copy to a new image file."""

    info = pending_recovery(selected)
    if info is None or info.kind != KIND_WORKSPACE:
        raise AmigaFSError(_("No interrupted working copy is available for this image."))
    workspace = checkpoint_directory(selected) / WORKSPACE_NAME
    if not workspace.is_file():
        raise AmigaFSError(_("The interrupted working copy no longer exists."))
    target = Path(destination).expanduser()
    try:
        with workspace.open("rb") as source, target.open("xb") as output:
            shutil.copyfileobj(source, output, length=8 * 1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
    except FileExistsError as exc:
        raise AmigaFSError(
            _("Refusing to overwrite an existing file: {path}").format(path=target)
        ) from exc
    except OSError as exc:
        target.unlink(missing_ok=True)
        raise AmigaFSError(_("Could not save the working copy: {error}").format(error=exc)) from exc
    return target


def recover_image(
    selected: str | Path,
    *,
    restore: bool = False,
    discard: bool = False,
    cancelled: CancellationCheck | None = None,
    progress: Callable[[int, str], None] | None = None,
) -> str:
    cancellation_point(cancelled)
    del progress
    image = canonical_source(selected)
    info = pending_recovery(image)
    directory = checkpoint_directory(image)
    if info is None:
        return _("No recovery checkpoint is pending.")
    if restore and discard:
        raise AmigaFSError(_("Choose either --restore or --discard, not both."))
    if not restore and not discard:
        if info.kind == KIND_WORKSPACE:
            return _(
                "A working copy from {created_at} was interrupted before it was written back. "
                "The source is unchanged. Use --salvage FILE to keep the working copy, then "
                "--restore or --discard to remove it."
            ).format(created_at=info.created_at)
        return _(
            "Recovery checkpoint from {created_at} is ready. Use --restore to restore it "
            "or --discard to accept the current image."
        ).format(created_at=info.created_at)
    if info.kind == KIND_WORKSPACE:
        WorkspaceCheckpoint(image, directory, info).complete()
        return _("The interrupted working copy was removed. The source image is unchanged.")
    if restore:
        _restore_journal(image, directory, info, cancelled)
        result = _("Recovery checkpoint restored.")
    else:
        if not info.is_device:
            try:
                with image.open("r+b") as handle:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AmigaFSError(
                    _("The image is mounted or open in another AmigaFS process.")
                ) from exc
            except OSError:
                pass
        result = _("Recovery checkpoint discarded.")
    SessionCheckpoint(image, directory, info).complete()
    return result


__all__ = [
    "KIND_JOURNAL",
    "KIND_WORKSPACE",
    "RecoveryInfo",
    "SessionCheckpoint",
    "WorkspaceCheckpoint",
    "canonical_source",
    "checkpoint_directory",
    "pending_physical_recoveries",
    "pending_recovery",
    "recover_image",
    "salvage_workspace",
    "state_root",
]
