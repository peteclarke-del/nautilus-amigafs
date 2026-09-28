"""Cached view of an Amiga medium, read-only unless explicitly writable."""

from __future__ import annotations

import errno
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any, TypeVar

from amigafs._vendor.amiganut.errors import AmiganutError
from amigafs._vendor.amiganut.file import (
    AMIGA_EPOCH,
    FIBF_ARCHIVE,
    FIBF_DELETE,
    FIBF_READ,
    FIBF_WRITE,
    AmigaMeta,
)
from amigafs._vendor.amiganut.filesystem.amigados import validate_name
from amigafs._vendor.amiganut.filesystem.blocks import (
    MAX_COMMENT,
    ST_LINKDIR,
    ST_LINKFILE,
    ST_SOFTLINK,
)
from amigafs.core.formats import ResolvedImage
from amigafs.core.media import OpenedMedia, VolumeInfo, open_media, open_volume
from amigafs.core.ranged import RangedReader
from amigafs.errors import AmigaFSError, DiscFullError, FilenameTooLongError
from amigafs.i18n import _, ngettext
from amigafs.recovery import SessionCheckpoint

if TYPE_CHECKING:
    from amigafs.core.validation import IntegrityReport
    from amigafs.operations import OperationBudget

ROOT_INODE = 1
VIRTUAL_VOLUME = -1
DEFAULT_CACHE_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_NODES = 100_000
DEFAULT_MAX_DEPTH = 256
DEFAULT_COMMIT_VALIDATION_BYTES = 64 * 1024 * 1024
PROTECTION_MASK = 0xFF
T = TypeVar("T")

_FULL = re.compile(
    r"volume is full|not enough free space|no room left|are free\.|free\.$|index is full",
    re.IGNORECASE,
)
_NOT_EMPTY = re.compile(r"is not empty", re.IGNORECASE)
_EXISTS = re.compile(r"already exists", re.IGNORECASE)
_PROTECTED = re.compile(r"protected (?:from|against|by)", re.IGNORECASE)
_MISSING = re.compile(r"not found|does not exist|no such", re.IGNORECASE)


class _MutationRolledBack(Exception):
    """Carry the original failure through the mutation guard after rollback."""

    def __init__(self, original: Exception) -> None:
        super().__init__(str(original))
        self.original = original


@dataclass(frozen=True, slots=True)
class ImageNode:
    """One immutable entry in the mounted directory index."""

    inode: int
    parent_inode: int
    name: bytes
    volume: int
    inner_path: str
    amiga_path: str
    is_dir: bool
    size: int
    protection: int = 0
    comment: str = ""
    mtime_ns: int | None = None
    link: str = ""
    block: int = 0

    @property
    def readable(self) -> bool:
        return not self.protection & FIBF_READ

    @property
    def write_protected(self) -> bool:
        return bool(self.protection & FIBF_WRITE)

    @property
    def delete_protected(self) -> bool:
        return bool(self.protection & FIBF_DELETE)

    @property
    def locked(self) -> bool:
        """True when the entry may not be written or may not be deleted."""

        return self.write_protected or self.delete_protected

    @property
    def is_volume_root(self) -> bool:
        return self.is_dir and self.inner_path == "" and self.volume != VIRTUAL_VOLUME


def display_name(name: str) -> bytes:
    """Map characters POSIX cannot represent to unambiguous Unicode glyphs."""

    mapped: list[str] = []
    for character in name:
        codepoint = ord(character)
        if character == "/":
            mapped.append("∕")
        elif codepoint < 32:
            mapped.append(chr(0x2400 + codepoint))
        elif codepoint == 127:
            mapped.append("␡")
        else:
            mapped.append(character)
    result = "".join(mapped)
    if result == ".":
        result = "．"
    elif result == "..":
        result = "．．"
    return result.encode("utf-8")


def amiga_name(name: bytes) -> str:
    """Reverse :func:`display_name` and require a Latin-1 Amiga name."""

    try:
        displayed = name.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(_("Amiga filenames must be valid UTF-8 on the Linux side")) from exc
    if displayed == "．":
        displayed = "."
    elif displayed == "．．":
        displayed = ".."
    decoded = "".join(
        "/"
        if character == "∕"
        else chr(ord(character) - 0x2400)
        if 0x2400 <= ord(character) <= 0x241F
        else chr(127)
        if character == "␡"
        else character
        for character in displayed
    )
    try:
        decoded.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise ValueError(
            _("Amiga filenames can contain only ISO 8859-1 (Latin-1) characters")
        ) from exc
    return decoded


def datestamp_ns(moment: datetime | None) -> int | None:
    """Return the Linux timestamp that shows the same wall-clock time as the Amiga.

    An AmigaDOS datestamp is local time with no zone. It is presented as the
    same local time on this host, which is also how the kernel's own Amiga
    filesystem driver reads it. The engine labels datestamps as UTC, so the
    label is dropped rather than converted.
    """

    if moment is None:
        return None
    wall = moment.astimezone(UTC).replace(tzinfo=None) if moment.tzinfo else moment
    try:
        local = wall.astimezone()
    except (OverflowError, OSError, ValueError):
        return None
    delta = local - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000


def datestamp_from_ns(value: int) -> datetime:
    """Return the engine datestamp holding this instant's local wall-clock time."""

    try:
        wall = datetime.fromtimestamp(value / 1_000_000_000)
    except (OverflowError, OSError, ValueError):
        return AMIGA_EPOCH
    return max(wall.replace(tzinfo=UTC), AMIGA_EPOCH)


def amiga_now() -> datetime:
    """Return the current local wall-clock time as an engine datestamp."""

    return datetime.now().replace(tzinfo=UTC)


def translate_engine_error(exc: Exception) -> Exception:
    """Give an engine failure the type the POSIX layer reports it as."""

    if not isinstance(exc, AmiganutError):
        return exc
    text = str(exc)
    if _FULL.search(text):
        return DiscFullError(text)
    if _NOT_EMPTY.search(text):
        return OSError(errno.ENOTEMPTY, text)
    if _EXISTS.search(text):
        return FileExistsError(text)
    if _PROTECTED.search(text):
        return PermissionError(text)
    if _MISSING.search(text):
        return FileNotFoundError(text)
    return exc


class AmigaImage:
    """An eagerly indexed Amiga directory tree backed by long-lived volume mounts."""

    def __init__(
        self,
        media: OpenedMedia,
        *,
        cache_bytes: int = DEFAULT_CACHE_BYTES,
        max_nodes: int = DEFAULT_MAX_NODES,
        max_depth: int = DEFAULT_MAX_DEPTH,
        commit_validation_bytes: int = DEFAULT_COMMIT_VALIDATION_BYTES,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self.media = media
        self.source: ResolvedImage = media.source
        self.store = media.store
        self.layout = media.layout
        self.volumes: dict[int, VolumeInfo] = {volume.index: volume for volume in media.volumes}
        self.writable = media.writable
        self._mounts: dict[int, Any] = {}
        self._cache_limit = cache_bytes
        self._max_nodes = max_nodes
        self.max_nodes = max_nodes
        self._max_depth = max_depth
        self._commit_validation_bytes = commit_validation_bytes
        self._fault_injector = fault_injector
        self._mutation_lock = RLock()
        self._failed = False
        self._closed = False
        self._modified = False
        self._cache: OrderedDict[int, bytes] = OrderedDict()
        self._cache_size = 0
        self._ranged = RangedReader()
        self.unmounted: list[tuple[VolumeInfo, str]] = []
        try:
            self.timestamp_ns = self.source.primary_path.stat().st_mtime_ns
        except OSError:
            self.timestamp_ns = time.time_ns()
        self.nodes: dict[int, ImageNode] = {}
        self.children: dict[int, tuple[int, ...]] = {}
        self.children_by_name: dict[int, dict[bytes, int]] = {}
        self._open_volumes()
        self._index_tree()
        self._next_inode = max(self.nodes) + 1
        self.total_bytes = 0
        self.free_bytes = 0
        self._refresh_space()

    # ---- opening -----------------------------------------------------
    @classmethod
    def open(
        cls,
        selected: str | Path | ResolvedImage,
        *,
        cache_bytes: int = DEFAULT_CACHE_BYTES,
        max_nodes: int = DEFAULT_MAX_NODES,
        max_depth: int = DEFAULT_MAX_DEPTH,
        writable: bool = False,
        commit_validation_bytes: int = DEFAULT_COMMIT_VALIDATION_BYTES,
        fault_injector: Callable[[str], None] | None = None,
        progress: Callable[[int, str], None] | None = None,
        operation_budget: OperationBudget | None = None,
        repairable_codes: frozenset[str] = frozenset(),
    ) -> AmigaImage:
        """Detect and open a supported source, read-only unless explicitly writable."""

        del operation_budget
        media: OpenedMedia | None = None
        image: AmigaImage | None = None
        try:
            media = open_media(selected, writable=writable, progress=progress)
            image = cls(
                media,
                cache_bytes=cache_bytes,
                max_nodes=max_nodes,
                max_depth=max_depth,
                commit_validation_bytes=commit_validation_bytes,
                fault_injector=fault_injector,
            )
            if writable:
                image._require_integrity(repairable_codes)
                if media.checkpoint is None:
                    media.checkpoint = SessionCheckpoint.create(
                        media.source.primary_path, media.store
                    )
                image._prepare_mutation()
            return image
        except Exception as exc:
            if image is not None:
                with suppress(Exception):
                    image.close(clean=False)
            elif media is not None:
                media.close()
            if (
                media is not None
                and media.checkpoint is not None
                and not (image is not None and image._modified)
            ):
                # Nothing was written, so the checkpoint protects nothing.
                with suppress(Exception):
                    media.checkpoint.complete()
            if isinstance(exc, AmigaFSError):
                raise
            raise AmigaFSError(
                _("The Amiga image could not be opened safely: {error}").format(error=exc)
            ) from exc

    def _open_volumes(self) -> None:
        for volume in self.media.volumes:
            if not volume.mountable:
                self.unmounted.append((volume, volume.problem or ""))
                continue
            try:
                self._mounts[volume.index] = open_volume(self.store, volume, writable=self.writable)
            except AmigaFSError as exc:
                if self.layout != "rdb":
                    raise
                self.unmounted.append((volume, str(exc)))
        if not self._mounts:
            reason = self.unmounted[0][1] if self.unmounted else ""
            raise AmigaFSError(
                _("No volume on this medium could be opened: {reason}").format(reason=reason)
            )

    def _reopen_volume(self, index: int) -> None:
        """Discard a driver's cached state after its staged writes were abandoned."""

        self._mounts[index] = open_volume(self.store, self.volumes[index], writable=self.writable)
        self._ranged.clear()

    @property
    def needs_write_back(self) -> bool:
        """Whether a clean close will re-encode or physically rewrite the source."""

        return (
            self.writable
            and self._modified
            and not self._failed
            and self.media.workspace is not None
        )

    @property
    def mounted_volumes(self) -> tuple[int, ...]:
        return tuple(sorted(self._mounts))

    def mount_for(self, index: int) -> Any:
        try:
            return self._mounts[index]
        except KeyError as exc:
            raise AmigaFSError(_("That volume is not mounted.")) from exc

    def volume_writable(self, index: int) -> bool:
        if not self.writable or index not in self._mounts:
            return False
        volume = self.volumes[index]
        if not volume.writable:
            return False
        engine = getattr(self._mounts[index], "volume", None)
        return not bool(getattr(engine, "read_only", False))

    def volume_title(self, index: int) -> str:
        try:
            return str(self.mount_for(index).title)
        except Exception:
            return ""

    def name_limit(self, index: int) -> int:
        engine = getattr(self.mount_for(index), "volume", None)
        return int(getattr(engine, "name_limit", 30))

    def case_sensitive(self, index: int) -> bool:
        if index == VIRTUAL_VOLUME:
            return False
        if self.source.case_sensitive_names:
            return True
        engine = getattr(self._mounts.get(index), "volume", None)
        return bool(getattr(engine, "case_sensitive", False))

    def _prefix(self, index: int) -> str:
        volume = self.volumes[index]
        if self.layout == "rdb":
            return f"{volume.device_name or volume.directory}:"
        title = self.volume_title(index)
        return f"{title}:" if title else ":"

    # ---- indexing ----------------------------------------------------
    def _metadata_for(self, mount: Any, path: str) -> AmigaMeta:
        try:
            meta = mount.amiga_meta(path)
        except Exception:
            return AmigaMeta()
        return meta if isinstance(meta, AmigaMeta) else AmigaMeta()

    def _index_tree(self) -> None:
        if self.layout == "rdb":
            self.nodes[ROOT_INODE] = ImageNode(
                inode=ROOT_INODE,
                parent_inode=ROOT_INODE,
                name=b"",
                volume=VIRTUAL_VOLUME,
                inner_path="",
                amiga_path="",
                is_dir=True,
                size=0,
            )
            roots: list[int] = []
            names: dict[bytes, int] = {}
            for index in sorted(self._mounts):
                volume = self.volumes[index]
                inode = len(self.nodes) + 1
                encoded = display_name(volume.directory)
                self.nodes[inode] = ImageNode(
                    inode=inode,
                    parent_inode=ROOT_INODE,
                    name=encoded,
                    volume=index,
                    inner_path="",
                    amiga_path=self._prefix(index),
                    is_dir=True,
                    size=0,
                    mtime_ns=self._root_time(index),
                )
                roots.append(inode)
                names[encoded] = inode
            self.children[ROOT_INODE] = tuple(roots)
            self.children_by_name[ROOT_INODE] = names
            for inode in roots:
                self._index_volume(inode)
            return
        index = next(iter(self._mounts))
        self.nodes[ROOT_INODE] = ImageNode(
            inode=ROOT_INODE,
            parent_inode=ROOT_INODE,
            name=b"",
            volume=index,
            inner_path="",
            amiga_path=self._prefix(index),
            is_dir=True,
            size=0,
            mtime_ns=self._root_time(index),
        )
        self._index_volume(ROOT_INODE)

    def _root_time(self, index: int) -> int | None:
        try:
            return datestamp_ns(self.mount_for(index).amiga_meta("").datestamp)
        except Exception:
            return None

    def _index_volume(self, root_inode: int) -> None:
        root = self.nodes[root_inode]
        mount = self.mount_for(root.volume)
        prefix = root.amiga_path
        sensitive = self.case_sensitive(root.volume)
        active: set[int] = set()

        def visit(parent_inode: int, path: str, depth: int, marker: int) -> None:
            if depth > self._max_depth:
                raise AmigaFSError(
                    _("The Amiga filesystem tree exceeds {maximum} levels.").format(
                        maximum=self._max_depth
                    )
                )
            if marker in active:
                raise AmigaFSError(
                    _("The Amiga filesystem tree contains a cycle at {path}.").format(
                        path=f"{prefix}{path}"
                    )
                )
            active.add(marker)
            child_inodes: list[int] = []
            names: dict[bytes, int] = {}
            folded: set[str] = set()
            try:
                try:
                    entries = sorted(
                        mount.iter_entries(path), key=lambda entry: entry.name.casefold()
                    )
                except RecursionError as exc:
                    raise AmigaFSError(
                        _("The directory {path} is too deeply nested to read.").format(
                            path=f"{prefix}{path}"
                        )
                    ) from exc
                except Exception as exc:
                    raise AmigaFSError(
                        _("The directory {path} cannot be read: {error}").format(
                            path=f"{prefix}{path}", error=exc
                        )
                    ) from exc
                for entry in entries:
                    if len(self.nodes) >= self._max_nodes:
                        raise AmigaFSError(
                            _("The Amiga image contains more than {maximum} entries.").format(
                                maximum=self._max_nodes
                            )
                        )
                    encoded = display_name(entry.name)
                    key = entry.name if sensitive else entry.name.casefold()
                    if encoded in names or key in folded:
                        raise AmigaFSError(
                            _("Two entries in {path} map to the same Linux filename.").format(
                                path=f"{prefix}{path}"
                            )
                        )
                    folded.add(key)
                    inode = len(self.nodes) + 1
                    meta = self._metadata_for(mount, entry.path)
                    link = ""
                    if entry.secondary_type == ST_SOFTLINK:
                        link = "soft"
                    elif entry.secondary_type in (ST_LINKFILE, ST_LINKDIR):
                        link = "hard"
                    is_dir = bool(entry.is_dir) and link != "soft"
                    self.nodes[inode] = ImageNode(
                        inode=inode,
                        parent_inode=parent_inode,
                        name=encoded,
                        volume=root.volume,
                        inner_path=entry.path,
                        amiga_path=f"{prefix}{entry.path}",
                        is_dir=is_dir,
                        size=0 if is_dir else int(entry.length),
                        protection=int(meta.protection),
                        comment=meta.comment,
                        mtime_ns=datestamp_ns(meta.datestamp),
                        link=link,
                        block=int(entry.block),
                    )
                    child_inodes.append(inode)
                    names[encoded] = inode
                    if is_dir and not link:
                        visit(inode, entry.path, depth + 1, int(entry.block))
                    elif is_dir:
                        # A directory link is listed but never followed, so a
                        # link to an ancestor cannot make the tree infinite.
                        self.children[inode] = ()
                        self.children_by_name[inode] = {}
                self.children[parent_inode] = tuple(child_inodes)
                self.children_by_name[parent_inode] = names
            finally:
                active.discard(marker)

        visit(root_inode, "", 0, -1)

    def lookup(self, parent_inode: int, name: bytes) -> ImageNode | None:
        names = self.children_by_name.get(parent_inode, {})
        inode = names.get(name)
        parent = self.nodes.get(parent_inode)
        if inode is None and parent is not None and not self.case_sensitive(parent.volume):
            try:
                wanted = name.decode("utf-8").casefold()
                inode = next(
                    (
                        child_inode
                        for displayed, child_inode in names.items()
                        if displayed.decode("utf-8").casefold() == wanted
                    ),
                    None,
                )
            except UnicodeDecodeError:
                inode = None
        return None if inode is None else self.nodes[inode]

    def node_at_path(self, path: str) -> ImageNode:
        """Resolve one Amiga path such as ``DH0:S/Startup-Sequence``."""

        text = path.strip()
        device, separator, inner = text.rpartition(":")
        if not separator:
            device, inner = "", text
        inner = inner.strip("/")
        if self.layout == "rdb":
            if not device:
                if not inner:
                    return self.nodes[ROOT_INODE]
                raise FileNotFoundError(path)
            wanted = device.casefold()
            start = next(
                (
                    inode
                    for inode in self.children[ROOT_INODE]
                    if wanted
                    in {
                        self.volumes[self.nodes[inode].volume].device_name.casefold(),
                        self.volumes[self.nodes[inode].volume].directory.casefold(),
                        self.volume_title(self.nodes[inode].volume).casefold(),
                    }
                ),
                None,
            )
            if start is None:
                raise FileNotFoundError(path)
        else:
            start = ROOT_INODE
        node = self.nodes[start]
        for part in (piece for piece in inner.split("/") if piece):
            found = self.lookup(node.inode, display_name(part))
            if found is None:
                raise FileNotFoundError(path)
            node = found
        return node

    # ---- reading -----------------------------------------------------
    def read(self, inode: int, offset: int, size: int) -> bytes:
        node = self.nodes[inode]
        if node.is_dir:
            raise IsADirectoryError(node.amiga_path)
        if node.size > self._cache_limit:
            return self._read_range(node, offset, size)
        data = self._cached_file(inode, node)
        return data[offset : offset + size]

    def uses_ranged_reads(self, inode: int) -> bool:
        """Return whether this file bypasses the bounded whole-file cache."""

        node = self.nodes[inode]
        return not node.is_dir and node.size > self._cache_limit

    def _read_whole(self, node: ImageNode) -> bytes:
        if node.link == "soft":
            try:
                return bytes(self.mount_for(node.volume).read_bytes(node.inner_path))
            except Exception:
                return b""
        return bytes(self.mount_for(node.volume).read_bytes(node.inner_path))

    def _read_range(self, node: ImageNode, offset: int, size: int) -> bytes:
        """Read only the blocks needed for a range of an uncached large file."""

        if offset < 0 or size <= 0 or offset >= node.size:
            return b""
        end = min(node.size, offset + size)
        with self._mutation_lock:
            data = self._ranged.read(
                self.mount_for(node.volume), node.inode, node.inner_path, offset, end - offset
            )
            if data is None:
                return self._read_whole(node)[offset:end]
            return data

    def _cached_file(self, inode: int, node: ImageNode) -> bytes:
        cached = self._cache.pop(inode, None)
        if cached is not None:
            self._cache[inode] = cached
            return cached
        with self._mutation_lock:
            data = self._read_whole(node)
        self._cache_file(inode, data)
        return data

    def _cache_file(self, inode: int, data: bytes) -> None:
        old_data = self._cache.pop(inode, None)
        if old_data is not None:
            self._cache_size -= len(old_data)
        if len(data) > self._cache_limit:
            return
        while self._cache and self._cache_size + len(data) > self._cache_limit:
            _old_inode, evicted = self._cache.popitem(last=False)
            self._cache_size -= len(evicted)
        self._cache[inode] = data
        self._cache_size += len(data)

    def _forget_file(self, inode: int) -> None:
        old_data = self._cache.pop(inode, None)
        if old_data is not None:
            self._cache_size -= len(old_data)

    # ---- metadata ----------------------------------------------------
    def metadata(self, inode: int) -> AmigaMeta:
        node = self.nodes[inode]
        if node.volume == VIRTUAL_VOLUME:
            raise ValueError(_("the hard disc root has no Amiga metadata"))
        moment = None if node.mtime_ns is None else datestamp_from_ns(node.mtime_ns)
        return AmigaMeta(protection=node.protection, comment=node.comment, datestamp=moment)

    def set_metadata(
        self,
        inode: int,
        *,
        protection: int | None = None,
        comment: str | None = None,
        mtime_ns: int | None = None,
    ) -> ImageNode:
        node = self.nodes[inode]
        if node.volume == VIRTUAL_VOLUME or node.is_volume_root:
            raise PermissionError(_("a volume root has no file metadata"))
        self._require_volume_writable(node.volume)
        if node.link:
            raise PermissionError(_("metadata of a link cannot be changed"))
        if comment is not None:
            try:
                comment.encode("latin-1")
            except UnicodeEncodeError as exc:
                raise ValueError(
                    _("Amiga comments can contain only ISO 8859-1 (Latin-1) characters")
                ) from exc
            if len(comment) > MAX_COMMENT or any(ord(item) < 32 for item in comment):
                raise ValueError(
                    _("An Amiga comment holds at most {maximum} printable characters").format(
                        maximum=MAX_COMMENT
                    )
                )
        if protection is not None and not 0 <= protection <= 0xFFFFFFFF:
            raise ValueError(_("protection bits must fit in 32 bits"))
        mount = self.mount_for(node.volume)
        new_protection = node.protection if protection is None else protection
        new_comment = node.comment if comment is None else comment
        new_time = node.mtime_ns if mtime_ns is None else datestamp_ns(datestamp_from_ns(mtime_ns))

        def mutate() -> None:
            mount.set_amiga_meta(
                node.inner_path,
                AmigaMeta(
                    protection=new_protection,
                    comment=new_comment,
                    datestamp=None if new_time is None else datestamp_from_ns(new_time),
                ),
            )

        def commit() -> ImageNode:
            updated = replace(
                self.nodes[inode],
                protection=new_protection,
                comment=new_comment,
                mtime_ns=new_time,
            )
            self.nodes[inode] = updated
            return updated

        return self._run_atomic("metadata", node.volume, mutate=mutate, commit=commit)

    def volume_for_inode(self, inode: int) -> VolumeInfo | None:
        node = self.nodes[inode]
        return None if node.volume == VIRTUAL_VOLUME else self.volumes[node.volume]

    # ---- names and paths --------------------------------------------
    def _new_name(self, volume: int, name: bytes) -> str:
        decoded = amiga_name(name)
        if decoded != decoded.strip():
            # The engine would trim the name, and the entry would then not be
            # found under the name the caller asked for.
            raise ValueError(_("Amiga filenames cannot begin or end with a space"))
        limit = self.name_limit(volume)
        if len(decoded) > limit:
            raise FilenameTooLongError(
                _("Amiga filenames on this volume hold at most {maximum} characters").format(
                    maximum=limit
                )
            )
        try:
            return str(validate_name(decoded, limit))
        except AmiganutError as exc:
            raise ValueError(str(exc)) from exc

    def _require_volume_writable(self, volume: int) -> None:
        if not self.writable:
            raise PermissionError(_("image is read-only"))
        if volume == VIRTUAL_VOLUME:
            raise PermissionError(_("The partition list of a hard disc cannot be changed here."))
        if not self.volume_writable(volume):
            raise PermissionError(_("This volume's filesystem cannot be written."))

    def _parent_for_new_entry(self, parent_inode: int) -> ImageNode:
        parent = self.nodes[parent_inode]
        if not parent.is_dir:
            raise NotADirectoryError(parent.amiga_path)
        self._require_volume_writable(parent.volume)
        if parent.link:
            raise PermissionError(_("entries cannot be created through a directory link"))
        return parent

    @staticmethod
    def _child_inner(parent: ImageNode, name: str) -> str:
        return f"{parent.inner_path}/{name}" if parent.inner_path else name

    def _add_node(
        self,
        parent_inode: int,
        name: str,
        *,
        is_dir: bool,
        size: int = 0,
        protection: int = 0,
        comment: str = "",
        mtime_ns: int | None = None,
    ) -> ImageNode:
        parent = self.nodes[parent_inode]
        encoded = display_name(name)
        inode = self._next_inode
        self._next_inode += 1
        inner = self._child_inner(parent, name)
        prefix = self.nodes[self._volume_root(parent_inode)].amiga_path
        node = ImageNode(
            inode=inode,
            parent_inode=parent_inode,
            name=encoded,
            volume=parent.volume,
            inner_path=inner,
            amiga_path=f"{prefix}{inner}",
            is_dir=is_dir,
            size=size,
            protection=protection,
            comment=comment,
            mtime_ns=time.time_ns() if mtime_ns is None else mtime_ns,
        )
        self.nodes[inode] = node
        self.children[parent_inode] = (*self.children.get(parent_inode, ()), inode)
        self.children_by_name.setdefault(parent_inode, {})[encoded] = inode
        if is_dir:
            self.children[inode] = ()
            self.children_by_name[inode] = {}
        return node

    def _volume_root(self, inode: int) -> int:
        cursor = inode
        while True:
            node = self.nodes[cursor]
            if node.is_volume_root or node.inode == ROOT_INODE:
                return cursor
            cursor = node.parent_inode

    def _drop_node(self, node: ImageNode) -> None:
        self.children[node.parent_inode] = tuple(
            inode for inode in self.children[node.parent_inode] if inode != node.inode
        )
        self.children_by_name[node.parent_inode].pop(node.name, None)
        self.children.pop(node.inode, None)
        self.children_by_name.pop(node.inode, None)
        self.nodes.pop(node.inode)
        self._forget_file(node.inode)

    # ---- content mutation -------------------------------------------
    def preflight_file_size(self, inode: int, new_size: int) -> None:
        """Reject a requested file size before a FUSE buffer is expanded."""

        if new_size < 0:
            raise ValueError(_("file size cannot be negative"))
        node = self.nodes[inode]
        if node.is_dir:
            raise IsADirectoryError(node.amiga_path)
        self._preflight(node.volume, new_size, replacing=node.size)

    def _preflight(self, volume: int, new_size: int, *, replacing: int = 0) -> None:
        if new_size > 0xFFFFFFFF:
            raise OSError(errno.EFBIG, _("Amiga files are limited to 4 GiB"))
        try:
            free = int(self.mount_for(volume).free_bytes())
        except Exception:
            return
        if new_size > free + replacing:
            raise DiscFullError(
                _("The volume has {free} bytes available; {wanted} bytes were requested").format(
                    free=free + replacing, wanted=new_size
                )
            )

    def _writing_meta(self, node: ImageNode) -> AmigaMeta:
        # AmigaDOS stamps a file and clears its archive bit whenever it is written.
        return AmigaMeta(
            protection=node.protection & ~FIBF_ARCHIVE,
            comment=node.comment,
            datestamp=amiga_now(),
        )

    def replace_file(self, inode: int, data: bytes) -> None:
        """Replace one file and make the new data visible immediately."""

        node = self.nodes[inode]
        if node.is_dir:
            raise IsADirectoryError(node.amiga_path)
        self._require_volume_writable(node.volume)
        if node.link:
            raise PermissionError(_("a link cannot be written through"))
        if node.write_protected:
            raise PermissionError(_("{path} is write-protected").format(path=node.amiga_path))
        self._preflight(node.volume, len(data), replacing=node.size)
        mount = self.mount_for(node.volume)
        meta = self._writing_meta(node)

        def mutate() -> None:
            if node.delete_protected:
                # Replacing the content of a delete-protected file is allowed;
                # the engine replaces by deleting, so the bit is lifted for the
                # duration of this one transaction and restored with the data.
                mount.set_access(node.inner_path, node.protection & ~FIBF_DELETE)
            mount.write_bytes(node.inner_path, data, meta)

        def commit() -> None:
            self.nodes[inode] = replace(
                self.nodes[inode],
                size=len(data),
                protection=meta.protection,
                mtime_ns=datestamp_ns(meta.datestamp),
            )
            self._cache_file(inode, data)

        self._run_atomic("replace", node.volume, mutate=mutate, commit=commit)

    def import_file(
        self, parent_inode: int, name: bytes, data: bytes, metadata: AmigaMeta
    ) -> ImageNode:
        """Create one file and its Amiga metadata as a single mutation."""

        parent = self._parent_for_new_entry(parent_inode)
        decoded = self._new_name(parent.volume, name)
        if self.lookup(parent_inode, display_name(decoded)) is not None:
            raise FileExistsError(decoded)
        if len(metadata.comment) > MAX_COMMENT:
            raise ValueError(
                _("An Amiga comment holds at most {maximum} printable characters").format(
                    maximum=MAX_COMMENT
                )
            )
        self._preflight(parent.volume, len(data))
        path = self._child_inner(parent, decoded)
        mount = self.mount_for(parent.volume)
        meta = AmigaMeta(
            protection=int(metadata.protection),
            comment=metadata.comment,
            datestamp=metadata.datestamp or amiga_now(),
        )

        def commit() -> ImageNode:
            node = self._add_node(
                parent_inode,
                decoded,
                is_dir=False,
                size=len(data),
                protection=meta.protection,
                comment=meta.comment,
                mtime_ns=datestamp_ns(meta.datestamp),
            )
            self._cache_file(node.inode, data)
            return node

        return self._run_atomic(
            "import",
            parent.volume,
            mutate=lambda: mount.write_bytes(path, data, meta),
            commit=commit,
        )

    def create_file(self, parent_inode: int, name: bytes) -> ImageNode:
        parent = self._parent_for_new_entry(parent_inode)
        decoded = self._new_name(parent.volume, name)
        if self.lookup(parent_inode, display_name(decoded)) is not None:
            raise FileExistsError(decoded)
        path = self._child_inner(parent, decoded)
        mount = self.mount_for(parent.volume)
        meta = AmigaMeta(datestamp=amiga_now())
        return self._run_atomic(
            "create",
            parent.volume,
            mutate=lambda: mount.write_bytes(path, b"", meta),
            commit=lambda: self._add_node(
                parent_inode, decoded, is_dir=False, mtime_ns=datestamp_ns(meta.datestamp)
            ),
        )

    def make_directory(self, parent_inode: int, name: bytes) -> ImageNode:
        parent = self._parent_for_new_entry(parent_inode)
        decoded = self._new_name(parent.volume, name)
        if self.lookup(parent_inode, display_name(decoded)) is not None:
            raise FileExistsError(decoded)
        path = self._child_inner(parent, decoded)
        mount = self.mount_for(parent.volume)
        return self._run_atomic(
            "mkdir",
            parent.volume,
            mutate=lambda: mount.mkdir(path),
            commit=lambda: self._add_node(parent_inode, decoded, is_dir=True),
        )

    def remove(self, parent_inode: int, name: bytes, *, directory: bool) -> None:
        node = self.lookup(parent_inode, name)
        if node is None:
            raise FileNotFoundError(name)
        if node.is_dir != directory:
            if node.is_dir:
                raise IsADirectoryError(node.amiga_path)
            raise NotADirectoryError(node.amiga_path)
        if node.is_volume_root:
            raise PermissionError(_("A partition cannot be removed here."))
        self._require_volume_writable(node.volume)
        if node.link:
            raise PermissionError(_("links cannot be removed safely by AmigaFS"))
        if node.delete_protected:
            raise PermissionError(_("{path} is delete-protected").format(path=node.amiga_path))
        if directory and self.children.get(node.inode):
            raise OSError(errno.ENOTEMPTY, node.amiga_path)
        mount = self.mount_for(node.volume)

        def mutate() -> None:
            self._lift_write_protection(mount, node)
            mount.remove(node.inner_path)

        self._run_atomic(
            "rmdir" if directory else "unlink",
            node.volume,
            mutate=mutate,
            commit=lambda: self._drop_node(node),
        )

    @staticmethod
    def _lift_write_protection(mount: Any, node: ImageNode) -> None:
        """Let a write-protected entry be deleted, as AmigaDOS does.

        AmigaDOS consults only the delete bit when deleting. The engine also
        refuses a write-protected file, so that bit is cleared inside the same
        transaction that removes the entry.
        """

        if not node.is_dir and node.write_protected and not node.delete_protected:
            mount.set_access(node.inner_path, node.protection & ~FIBF_WRITE)

    def rename(
        self, old_parent: int, old_name: bytes, new_parent: int, new_name: bytes
    ) -> ImageNode:
        node = self.lookup(old_parent, old_name)
        if node is None:
            raise FileNotFoundError(old_name)
        if node.is_volume_root:
            raise PermissionError(_("A partition cannot be renamed here."))
        self._require_volume_writable(node.volume)
        target_parent = self._parent_for_new_entry(new_parent)
        if target_parent.volume != node.volume:
            raise OSError(errno.EXDEV, _("files cannot be renamed between partitions"))
        if node.link:
            raise PermissionError(_("links cannot be moved safely by AmigaFS"))
        if node.is_dir and self._is_descendant_or_self(new_parent, node.inode):
            raise ValueError(_("a directory cannot be moved inside itself"))
        decoded = self._new_name(node.volume, new_name)
        new_path = self._child_inner(target_parent, decoded)
        old_path = node.inner_path
        destination = self.lookup(new_parent, display_name(decoded))
        if destination is not None and destination.inode == node.inode:
            if destination.name == display_name(decoded):
                return node
            destination = None
        if destination is not None:
            if destination.link:
                raise PermissionError(_("links cannot be replaced safely by AmigaFS"))
            if destination.delete_protected:
                raise PermissionError(
                    _("{path} is delete-protected").format(path=destination.amiga_path)
                )
            if destination.is_dir != node.is_dir:
                if destination.is_dir:
                    raise IsADirectoryError(destination.amiga_path)
                raise NotADirectoryError(destination.amiga_path)
            if destination.is_dir and self.children.get(destination.inode):
                raise OSError(errno.ENOTEMPTY, destination.amiga_path)
        mount = self.mount_for(node.volume)

        def mutate() -> None:
            if destination is not None:
                self._lift_write_protection(mount, destination)
                mount.remove(destination.inner_path)
                self._fault("rename.destination_removed")
            mount.rename(old_path, new_path)

        def commit() -> ImageNode:
            return self._commit_rename(
                node,
                destination,
                old_parent=node.parent_inode,
                new_parent=new_parent,
                decoded=decoded,
                old_path=old_path,
                new_path=new_path,
            )

        return self._run_atomic("rename", node.volume, mutate=mutate, commit=commit)

    def _commit_rename(
        self,
        node: ImageNode,
        destination: ImageNode | None,
        *,
        old_parent: int,
        new_parent: int,
        decoded: str,
        old_path: str,
        new_path: str,
    ) -> ImageNode:
        if destination is not None:
            self._drop_node(destination)
        self.children[old_parent] = tuple(
            inode for inode in self.children[old_parent] if inode != node.inode
        )
        self.children_by_name[old_parent].pop(node.name, None)
        new_encoded = display_name(decoded)
        self.children[new_parent] = (*self.children.get(new_parent, ()), node.inode)
        self.children_by_name.setdefault(new_parent, {})[new_encoded] = node.inode
        prefix = self.nodes[self._volume_root(new_parent)].amiga_path
        self.nodes[node.inode] = replace(
            node,
            parent_inode=new_parent,
            name=new_encoded,
            inner_path=new_path,
            amiga_path=f"{prefix}{new_path}",
        )
        old_prefix = f"{old_path}/"
        for inode, descendant in tuple(self.nodes.items()):
            if descendant.volume == node.volume and descendant.inner_path.startswith(old_prefix):
                inner = f"{new_path}{descendant.inner_path[len(old_path) :]}"
                self.nodes[inode] = replace(
                    descendant, inner_path=inner, amiga_path=f"{prefix}{inner}"
                )
        return self.nodes[node.inode]

    def set_volume_title(self, volume: int, title: str) -> None:
        """Rename one volume as ``Relabel`` would."""

        self._require_volume_writable(volume)
        mount = self.mount_for(volume)
        try:
            title.encode("latin-1")
            cleaned = str(validate_name(title, 30))
        except (UnicodeEncodeError, AmiganutError) as exc:
            raise ValueError(_("That is not a valid Amiga volume name")) from exc

        def commit() -> None:
            if self.layout == "rdb":
                return
            prefix = f"{cleaned}:"
            for inode, node in tuple(self.nodes.items()):
                self.nodes[inode] = replace(node, amiga_path=f"{prefix}{node.inner_path}")

        self._run_atomic("relabel", volume, mutate=lambda: mount.set_title(cleaned), commit=commit)

    def run_volume_repair(self, volume: int, action: str, repair: Callable[[Any], object]) -> None:
        """Run one engine-level repair as a single checked transaction."""

        self._require_volume_writable(volume)
        mount = self.mount_for(volume)

        def commit() -> None:
            self._reopen_volume(volume)

        self._run_atomic(
            action, volume, mutate=lambda: repair(mount), commit=commit, validate=False
        )

    def _is_descendant_or_self(self, inode: int, ancestor_inode: int) -> bool:
        cursor = inode
        while True:
            if cursor == ancestor_inode:
                return True
            node = self.nodes[cursor]
            if node.inode == ROOT_INODE or node.parent_inode == node.inode:
                return False
            cursor = node.parent_inode

    # ---- transactions ------------------------------------------------
    def _prepare_mutation(self) -> None:
        if not self.writable:
            raise PermissionError(_("image is read-only"))
        if self._failed:
            raise AmigaFSError(_("The writable session has failed; unmount and recover the image."))
        try:
            self.store.verify_unchanged()
            if self.media.identity_store is not None and self.media.workspace is not None:
                identity = self.media.identity_store
                if identity.expected_signature is not None:
                    identity.verify_unchanged()
        except AmigaFSError:
            self._failed = True
            raise

    def _fault(self, stage: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(stage)

    def _validates_each_commit(self, volume: int) -> bool:
        return self.volumes[volume].length <= self._commit_validation_bytes

    def _run_atomic(
        self,
        operation: str,
        volume: int,
        *,
        mutate: Callable[[], object],
        commit: Callable[[], T],
        validate: bool = True,
    ) -> T:
        """Run one mutation in a transaction that either fully applies or never happened."""

        with self._mutation():
            mount = self.mount_for(volume)
            self.store.begin()
            try:
                self._fault(f"{operation}.before")
                mutate()
                mount.flush()
                self._fault(f"{operation}.after")
                if validate and self._validates_each_commit(volume):
                    problems = tuple(mount.validate())
                    if problems:
                        raise AmigaFSError(
                            _(
                                "{operation} was abandoned because it would have left the "
                                "volume inconsistent: {problem}"
                            ).format(operation=operation, problem=problems[0])
                        )
            except Exception as exc:
                self.store.rollback()
                try:
                    self._reopen_volume(volume)
                except Exception as rollback_exc:
                    self._failed = True
                    raise AmigaFSError(
                        _(
                            "{operation} failed and the volume could not be reopened; "
                            "unmount and restore the recovery checkpoint."
                        ).format(operation=operation)
                    ) from rollback_exc
                raise _MutationRolledBack(translate_engine_error(exc)) from exc
            try:
                self._modified = True
                self.store.commit(fault=self._fault)
            except Exception as exc:
                # Part of the operation may have reached the medium. The undo
                # journal holds every before-image, so the session stops here.
                self._failed = True
                self.store.rollback()
                raise AmigaFSError(
                    _(
                        "{operation} could not be written to the medium: {error}. "
                        "Unmount and restore the recovery checkpoint."
                    ).format(operation=operation, error=exc)
                ) from exc
            self._ranged.clear()
            self._refresh_space()
            return commit()

    @contextmanager
    def _mutation(self) -> Iterator[None]:
        with self._mutation_lock:
            self._prepare_mutation()
            try:
                yield
            except _MutationRolledBack as exc:
                raise exc.original.with_traceback(exc.original.__traceback__) from exc

    def sync(self) -> None:
        """Flush writable image changes through to stable storage."""

        if self.writable and not self._closed:
            self.store.sync()

    def _refresh_space(self) -> None:
        total = 0
        free = 0
        for index, mount in self._mounts.items():
            try:
                total += int(mount.size_bytes())
                free += int(mount.free_bytes())
            except Exception:
                total += self.volumes[index].length
        self.total_bytes = total or self.store.size
        self.free_bytes = free

    # ---- integrity ---------------------------------------------------
    def volume_problems(self) -> dict[int, tuple[str, ...]]:
        """Run every mounted volume's structural validator."""

        results: dict[int, tuple[str, ...]] = {}
        with self._mutation_lock:
            for index, mount in self._mounts.items():
                validator = getattr(mount, "validate", None)
                if not callable(validator):
                    results[index] = (_("This filesystem has no structural validator."),)
                    continue
                try:
                    results[index] = tuple(str(problem) for problem in validator())
                except RecursionError:
                    results[index] = (_("The directory tree is too deep to validate."),)
                except AmiganutError as exc:
                    results[index] = (str(exc),)
                except Exception as exc:
                    # The engine trusts some on-disc counts and offsets. Whatever
                    # a damaged value makes it raise is a finding, not a crash.
                    results[index] = (
                        _("A damaged structure stopped validation ({kind}: {error}).").format(
                            kind=type(exc).__name__, error=exc
                        ),
                    )
        return results

    def _require_integrity(self, repairable_codes: frozenset[str] = frozenset()) -> None:
        from amigafs.core.validation import classify_problem

        for index, found in self.volume_problems().items():
            filesystem = self.volumes[index].filesystem
            problems = tuple(
                problem
                for problem in found
                if classify_problem(problem, filesystem) not in repairable_codes
            )
            if not problems or not self.volumes[index].writable:
                continue
            count = len(problems)
            label = self.volumes[index].directory or self.volume_title(index)
            raise AmigaFSError(
                ngettext(
                    "Writable mount refused: validation of {volume} found {count} problem. "
                    "{problem}",
                    "Writable mount refused: validation of {volume} found {count} problems. "
                    "{problem}",
                    count,
                ).format(volume=label, count=count, problem=problems[0])
            )

    def integrity_report(self, *, budget: OperationBudget | None = None) -> IntegrityReport:
        """Return a full integrity report for the currently locked image."""

        from amigafs.core.validation import report_for_image

        return report_for_image(self, budget=budget)

    # ---- closing -----------------------------------------------------
    def close(
        self,
        *,
        clean: bool = True,
        progress: Callable[[int, str], None] | None = None,
    ) -> None:
        if self._closed:
            return
        close_error: Exception | None = None
        store = self.store
        if store.in_transaction:
            store.rollback()
        if self.writable:
            try:
                self.sync()
                if clean and not self._failed:
                    for index, problems in self.volume_problems().items():
                        if problems and self.volumes[index].writable:
                            count = len(problems)
                            raise AmigaFSError(
                                ngettext(
                                    "Post-write validation found {count} problem; "
                                    "the recovery checkpoint was retained. {problem}",
                                    "Post-write validation found {count} problems; "
                                    "the recovery checkpoint was retained. {problem}",
                                    count,
                                ).format(count=count, problem=problems[0])
                            )
            except Exception as exc:
                self._failed = True
                close_error = AmigaFSError(
                    _("Could not safely finalise the writable image: {error}").format(error=exc)
                )
        self._cache.clear()
        self._cache_size = 0
        self._ranged.clear()
        self._mounts.clear()
        workspace = self.media.workspace
        identity = self.media.identity_store
        write_back = (
            self.writable
            and clean
            and not self._failed
            and close_error is None
            and workspace is not None
            and self._modified
        )
        if write_back and workspace is not None:
            try:
                if identity is not None and identity.expected_signature is not None:
                    identity.verify_unchanged()
                store.sync()
                workspace.export(progress=progress)
            except Exception as exc:
                self._failed = True
                close_error = AmigaFSError(
                    _("Could not safely write the updated image back: {error}").format(error=exc)
                )
        retained = self._failed or not clean
        self.media.close()
        self._closed = True
        checkpoint = self.media.checkpoint
        if self.writable and checkpoint is not None and (not retained or not self._modified):
            checkpoint.complete()
        if close_error is not None:
            raise close_error

    def __enter__(self) -> AmigaImage:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        try:
            self.close(clean=exc_info[0] is None)
        except Exception:
            if exc_info[0] is None:
                raise


def validate_image(selected: str | Path) -> tuple[str, ...]:
    """Compatibility wrapper returning human-readable integrity findings."""

    from amigafs.core.validation import validate_image_report

    report = validate_image_report(selected)
    return tuple(
        f"{finding.severity.value}: {finding.code}: {finding.message}"
        for finding in report.findings
    )


__all__ = [
    "ROOT_INODE",
    "VIRTUAL_VOLUME",
    "AmigaImage",
    "ImageNode",
    "amiga_name",
    "amiga_now",
    "datestamp_from_ns",
    "datestamp_ns",
    "display_name",
    "translate_engine_error",
    "validate_image",
]
