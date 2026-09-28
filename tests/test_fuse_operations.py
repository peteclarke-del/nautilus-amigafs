from __future__ import annotations

import asyncio
import errno
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import call, patch

import pyfuse3
import pytest

from amigafs.core.image import ROOT_INODE, AmigaImage
from amigafs.errors import AmigaFSError
from amigafs.fuse_adapter.operations import AmigaOperations
from tests.image_fixture import create_empty_floppy, create_floppy, create_hard_disc

CONTEXT = SimpleNamespace(uid=1000, gid=1000, pid=1, umask=0)


def run_async(function: Any, *args: Any) -> Any:
    async def invoke() -> Any:
        return await function(*args)

    return asyncio.run(invoke())


def fields(**updates: bool) -> SimpleNamespace:
    defaults = dict.fromkeys(
        (
            "update_size",
            "update_mode",
            "update_uid",
            "update_gid",
            "update_atime",
            "update_mtime",
            "update_ctime",
        ),
        False,
    )
    return SimpleNamespace(**{**defaults, **updates})


def errno_of(function: Any, *args: Any) -> int:
    with pytest.raises(pyfuse3.FUSEError) as raised:
        run_async(function, *args)
    return int(raised.value.errno)


def walk(operations: AmigaOperations, *names: bytes) -> Any:
    entry = run_async(operations.getattr, ROOT_INODE, CONTEXT)
    for name in names:
        entry = run_async(operations.lookup, entry.st_ino, name, CONTEXT)
    return entry


@pytest.fixture
def operations(tmp_path: Path) -> Any:
    with AmigaImage.open(create_floppy(tmp_path)) as image:
        yield AmigaOperations(image)


@pytest.fixture
def writable(tmp_path: Path) -> Any:
    path = create_floppy(tmp_path)
    with AmigaImage.open(path, writable=True) as image:
        yield AmigaOperations(image), path


def test_lookup_and_read_nested_file(operations: AmigaOperations) -> None:
    note = walk(operations, b"docs", b"DEEP", b"Nested", b"Note")
    info = run_async(operations.open, note.st_ino, os.O_RDONLY, CONTEXT)
    assert run_async(operations.read, info.fh, 0, 1024) == b"nested"
    assert run_async(operations.read, info.fh, 2, 3) == b"ste"
    assert stat.S_ISREG(note.st_mode) and note.st_mode & 0o777 == 0o444
    assert note.st_size == 6
    assert note.st_nlink == 1
    docs = walk(operations, b"Docs")
    assert stat.S_ISDIR(docs.st_mode) and docs.st_mode & 0o777 == 0o555
    assert errno_of(operations.lookup, ROOT_INODE, b"Missing", CONTEXT) == errno.ENOENT
    assert errno_of(operations.lookup, note.st_ino, b"x", CONTEXT) == errno.ENOTDIR
    assert run_async(operations.lookup, docs.st_ino, b"..", CONTEXT).st_ino == ROOT_INODE
    assert errno_of(operations.open, docs.st_ino, os.O_RDONLY, CONTEXT) == errno.EISDIR
    assert errno_of(operations.opendir, note.st_ino, CONTEXT) == errno.ENOTDIR
    assert errno_of(operations.read, 999, 0, 1) == errno.EBADF


def test_timestamps_come_from_the_amiga_datestamp(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    with AmigaImage.open(path, writable=True) as image:
        image.set_metadata(image.node_at_path("C/List").inode, mtime_ns=700_000_000 * 1_000_000_000)
    with AmigaImage.open(path) as image:
        entry = walk(AmigaOperations(image), b"C", b"List")
        assert entry.st_mtime_ns == 700_000_000 * 1_000_000_000
        assert entry.st_ctime_ns == entry.st_mtime_ns


def test_partitions_appear_as_folders(tmp_path: Path) -> None:
    path = create_hard_disc(tmp_path, filesystem="PFS3", capacity="8MB", partitions=2)
    with AmigaImage.open(path, writable=True) as image:
        operations = AmigaOperations(image)
        root = run_async(operations.getattr, ROOT_INODE, CONTEXT)
        # The partition list itself can never be written to.
        assert root.st_mode & 0o777 == 0o555
        second = walk(operations, b"DH1")
        assert second.st_mode & 0o777 == 0o755
        assert run_async(operations.getxattr, ROOT_INODE, b"user.amiga.source", CONTEXT) == b"RDB"
        assert (
            run_async(operations.getxattr, second.st_ino, b"user.amiga.source", CONTEXT) == b"PFS3"
        )
        assert (
            run_async(operations.getxattr, second.st_ino, b"user.amiga.volume", CONTEXT)
            == b"System1"
        )
        listed = walk(operations, b"DH1", b"C", b"List")
        assert (
            run_async(operations.getxattr, listed.st_ino, b"user.amiga.path", CONTEXT)
            == b"DH1:C/List"
        )
        assert errno_of(operations.mkdir, ROOT_INODE, b"DH2", 0o755, CONTEXT) == errno.EACCES
        assert (
            errno_of(
                operations.rename,
                second.st_ino,
                b"Empty",
                walk(operations, b"DH0").st_ino,
                b"Moved",
                0,
                CONTEXT,
            )
            == errno.EXDEV
        )
        statistics = run_async(operations.statfs, CONTEXT)
        assert statistics.f_bsize == 512
        assert statistics.f_blocks > statistics.f_bfree > 0
        assert statistics.f_namemax >= 30


def test_large_sequential_reads_use_bounded_read_ahead(tmp_path: Path) -> None:
    contents = bytes(range(256)) * 16
    path = create_floppy(tmp_path, files=(("Large", contents, 0, ""),))
    with AmigaImage.open(path, cache_bytes=64) as image:
        large = image.node_at_path("Large")
        operations = AmigaOperations(image, read_ahead_bytes=512, read_ahead_cache_bytes=1024)
        info = run_async(operations.open, large.inode, os.O_RDONLY, CONTEXT)
        with patch.object(image, "read", wraps=image.read) as read:
            assert run_async(operations.read, info.fh, 0, 256) == contents[:256]
            assert run_async(operations.read, info.fh, 256, 256) == contents[256:512]
            assert read.call_args_list == [
                call(large.inode, 0, 256),
                call(large.inode, 256, 768),
            ]
            assert run_async(operations.read, info.fh, 512, 256) == contents[512:768]
            assert read.call_count == 2
            assert operations._read_ahead_size <= 1024
            assert run_async(operations.read, info.fh, 3000, 128) == contents[3000:3128]
            assert read.call_args_list[-1] == call(large.inode, 3000, 128)
            assert operations._read_ahead_size == 0
        run_async(operations.release, info.fh)
        assert info.fh not in operations._read_states


def test_read_ahead_total_budget_evicts_oldest_handle(tmp_path: Path) -> None:
    contents = bytes(range(256)) * 16
    path = create_floppy(tmp_path, files=(("Large", contents, 0, ""),))
    with AmigaImage.open(path, cache_bytes=64) as image:
        large = image.node_at_path("Large")
        operations = AmigaOperations(image, read_ahead_bytes=512, read_ahead_cache_bytes=512)
        first = run_async(operations.open, large.inode, os.O_RDONLY, CONTEXT)
        second = run_async(operations.open, large.inode, os.O_RDONLY, CONTEXT)
        for info in (first, second):
            run_async(operations.read, info.fh, 0, 128)
            run_async(operations.read, info.fh, 128, 128)
        assert operations._read_states[first.fh].buffer == b""
        assert len(operations._read_states[second.fh].buffer) == 512
        assert operations._read_ahead_size == 512
        run_async(operations.release, first.fh)
        run_async(operations.release, second.fh)
        assert operations._read_ahead_size == 0


def test_writable_access_discards_read_ahead_on_every_handle(tmp_path: Path) -> None:
    contents = bytes(range(256)) * 16
    path = create_floppy(tmp_path, files=(("Large", contents, 0, ""),))
    with AmigaImage.open(path, writable=True, cache_bytes=64) as image:
        large = image.node_at_path("Large")
        operations = AmigaOperations(image, read_ahead_bytes=512, read_ahead_cache_bytes=1024)
        reader = run_async(operations.open, large.inode, os.O_RDONLY, CONTEXT)
        run_async(operations.read, reader.fh, 0, 128)
        run_async(operations.read, reader.fh, 128, 128)
        assert operations._read_ahead_size == 512
        writer = run_async(operations.open, large.inode, os.O_WRONLY, CONTEXT)
        assert operations._read_ahead_size == 0
        assert operations._read_states[reader.fh].buffer == b""
        run_async(operations.release, writer.fh)
        run_async(operations.release, reader.fh)


def test_read_ahead_configuration_rejects_unsafe_limits(operations: AmigaOperations) -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        AmigaOperations(operations.image, read_ahead_bytes=-1)
    with pytest.raises(ValueError, match="at least two"):
        AmigaOperations(operations.image, sequential_read_threshold=1)


def test_every_write_is_rejected_on_a_read_only_mount(operations: AmigaOperations) -> None:
    readme = walk(operations, b"Docs", b"ReadMe")
    docs = walk(operations, b"Docs")
    assert errno_of(operations.open, readme.st_ino, os.O_WRONLY, CONTEXT) == errno.EROFS
    assert errno_of(operations.create, docs.st_ino, b"New", 0o644, 0, CONTEXT) == errno.EROFS
    assert errno_of(operations.mkdir, docs.st_ino, b"New", 0o755, CONTEXT) == errno.EROFS
    assert errno_of(operations.unlink, docs.st_ino, b"ReadMe", CONTEXT) == errno.EROFS
    assert errno_of(operations.rmdir, docs.st_ino, b"Deep", CONTEXT) == errno.EROFS
    assert (
        errno_of(operations.rename, docs.st_ino, b"ReadMe", docs.st_ino, b"x", 0, CONTEXT)
        == errno.EROFS
    )
    assert (
        errno_of(operations.setxattr, readme.st_ino, b"user.amiga.comment", b"x", CONTEXT)
        == errno.EROFS
    )
    assert (
        errno_of(
            operations.setattr,
            readme.st_ino,
            SimpleNamespace(st_size=0),
            fields(update_size=True),
            None,
            CONTEXT,
        )
        == errno.EROFS
    )
    assert errno_of(operations.access, readme.st_ino, os.W_OK, CONTEXT) == errno.EROFS
    assert run_async(operations.access, readme.st_ino, os.R_OK, CONTEXT) is True


def test_writable_operation_flushes_existing_file(writable: Any) -> None:
    operations, path = writable
    readme = walk(operations, b"Docs", b"ReadMe")
    assert readme.st_mode & 0o777 == 0o644
    info = run_async(operations.open, readme.st_ino, os.O_WRONLY | os.O_TRUNC, CONTEXT)
    assert run_async(operations.write, info.fh, 0, b"New contents\n") == 13
    run_async(operations.fsync, info.fh, False)
    run_async(operations.release, info.fh)
    read_info = run_async(operations.open, readme.st_ino, os.O_RDONLY, CONTEXT)
    assert run_async(operations.read, read_info.fh, 0, 1024) == b"New contents\n"
    run_async(operations.release, read_info.fh)
    operations.image.close()
    with AmigaImage.open(path) as image:
        node = image.node_at_path("Docs/ReadMe")
        assert image.read(node.inode, 0, 1024) == b"New contents\n"
        assert node.comment == "Introduction"


def test_multiple_writable_handles_share_one_coherent_buffer(writable: Any) -> None:
    operations, path = writable
    readme = walk(operations, b"Docs", b"ReadMe")
    first = run_async(operations.open, readme.st_ino, os.O_RDWR | os.O_TRUNC, CONTEXT)
    second = run_async(operations.open, readme.st_ino, os.O_RDWR, CONTEXT)
    run_async(operations.write, first.fh, 0, b"shared")
    assert run_async(operations.read, second.fh, 0, 6) == b"shared"
    run_async(operations.write, second.fh, 6, b"-buffer")
    assert run_async(operations.getattr, readme.st_ino, CONTEXT).st_size == 13
    run_async(operations.fsync, first.fh, False)
    run_async(operations.release, first.fh)
    run_async(operations.write, second.fh, 13, b"!")
    run_async(operations.release, second.fh)
    operations.image.close()
    with AmigaImage.open(path) as image:
        assert image.read(image.node_at_path("Docs/ReadMe").inode, 0, 14) == b"shared-buffer!"


def test_truncate_from_one_handle_is_visible_to_every_handle(writable: Any) -> None:
    operations, path = writable
    readme = walk(operations, b"Docs", b"ReadMe")
    first = run_async(operations.open, readme.st_ino, os.O_RDWR, CONTEXT)
    second = run_async(operations.open, readme.st_ino, os.O_RDWR | os.O_TRUNC, CONTEXT)
    assert run_async(operations.read, first.fh, 0, 1024) == b""
    assert run_async(operations.getattr, readme.st_ino, CONTEXT).st_size == 0
    run_async(operations.write, second.fh, 0, b"replacement")
    assert run_async(operations.getattr, readme.st_ino, CONTEXT).st_size == 11
    run_async(operations.release, first.fh)
    run_async(operations.release, second.fh)
    operations.image.close()
    with AmigaImage.open(path) as image:
        assert image.read(image.node_at_path("Docs/ReadMe").inode, 0, 99) == b"replacement"


def test_truncate_and_extend_through_setattr(writable: Any) -> None:
    operations, path = writable
    listed = walk(operations, b"C", b"List")
    shortened = run_async(
        operations.setattr,
        listed.st_ino,
        SimpleNamespace(st_size=10),
        fields(update_size=True),
        None,
        CONTEXT,
    )
    assert shortened.st_size == 10
    extended = run_async(
        operations.setattr,
        listed.st_ino,
        SimpleNamespace(st_size=20),
        fields(update_size=True),
        None,
        CONTEXT,
    )
    assert extended.st_size == 20
    operations.image.close()
    with AmigaImage.open(path) as image:
        node = image.node_at_path("C/List")
        assert image.read(node.inode, 0, 99) == bytes(range(10)) + bytes(10)


def test_graceful_shutdown_flushes_dirty_open_handles(writable: Any) -> None:
    operations, path = writable
    readme = walk(operations, b"Docs", b"ReadMe")
    info = run_async(operations.open, readme.st_ino, os.O_WRONLY | os.O_TRUNC, CONTEXT)
    run_async(operations.write, info.fh, 0, b"dirty but recoverable")
    run_async(operations.setxattr, readme.st_ino, b"user.amiga.comment", b"pending", CONTEXT)
    operations.flush_pending()
    assert operations._dirty == set()
    assert operations._metadata_updates == {}
    assert info.fh in operations._handles
    operations.image.close()
    with AmigaImage.open(path) as image:
        node = image.node_at_path("Docs/ReadMe")
        assert image.read(node.inode, 0, 99) == b"dirty but recoverable"
        assert node.comment == "pending"


def test_failed_shutdown_flush_keeps_buffer_dirty_for_recovery(tmp_path: Path) -> None:
    image = AmigaImage.open(create_floppy(tmp_path), writable=True)
    try:
        operations = AmigaOperations(image)
        readme = walk(operations, b"Docs", b"ReadMe")
        info = run_async(operations.open, readme.st_ino, os.O_WRONLY | os.O_TRUNC, CONTEXT)
        run_async(operations.write, info.fh, 0, b"cannot be flushed")
        with (
            patch.object(image, "replace_file", side_effect=AmigaFSError("injected failure")),
            pytest.raises(AmigaFSError, match="pending FUSE data and metadata"),
        ):
            operations.flush_pending()
        assert readme.st_ino in operations._dirty
        assert bytes(operations._write_buffers[readme.st_ino]) == b"cannot be flushed"
    finally:
        image.close(clean=False)


@pytest.mark.parametrize("filesystem", ["OFS", "FFS-DC", "FFS-LNFS", "PFS3", "SFS"])
def test_writable_fuse_lifecycle_for_every_filesystem(tmp_path: Path, filesystem: str) -> None:
    if filesystem in {"PFS3", "SFS"}:
        path = create_hard_disc(tmp_path, filesystem=filesystem, capacity="8MB", partitions=1)
        top_names: tuple[bytes, ...] = (b"DH0",)
    else:
        path = create_floppy(tmp_path, filesystem=filesystem)
        top_names = ()
    with AmigaImage.open(path, writable=True) as image:
        operations = AmigaOperations(image)
        top = walk(operations, *top_names).st_ino
        folder = run_async(operations.mkdir, top, b"Writable", 0o755, CONTEXT)
        info, created = run_async(
            operations.create, folder.st_ino, b"Created", 0o644, os.O_WRONLY, CONTEXT
        )
        run_async(operations.write, info.fh, 0, b"created through FUSE")
        run_async(operations.release, info.fh)
        run_async(operations.rename, folder.st_ino, b"Created", top, b"Renamed", 0, CONTEXT)
        renamed = run_async(operations.lookup, top, b"renamed", CONTEXT)
        assert renamed.st_ino == created.st_ino
        assert (
            errno_of(
                operations.rename,
                top,
                b"Renamed",
                top,
                b"Empty",
                pyfuse3.RENAME_NOREPLACE,
                CONTEXT,
            )
            == errno.EEXIST
        )
        assert (
            errno_of(
                operations.rename, top, b"Renamed", top, b"Empty", pyfuse3.RENAME_EXCHANGE, CONTEXT
            )
            == errno.ENOTSUP
        )
        assert errno_of(operations.rmdir, top, b"Docs", CONTEXT) == errno.ENOTEMPTY
        assert errno_of(operations.mkdir, top, b"docs", 0o755, CONTEXT) == errno.EEXIST
        run_async(operations.unlink, top, b"Renamed", CONTEXT)
        run_async(operations.rmdir, top, b"Writable", CONTEXT)
        assert errno_of(operations.lookup, top, b"Renamed", CONTEXT) == errno.ENOENT
    with AmigaImage.open(path) as image:
        assert image.integrity_report().findings == ()


def test_open_files_cannot_be_removed_or_replaced(writable: Any) -> None:
    operations, _path = writable
    docs = walk(operations, b"Docs")
    readme = walk(operations, b"Docs", b"ReadMe")
    info = run_async(operations.open, readme.st_ino, os.O_RDONLY, CONTEXT)
    assert errno_of(operations.unlink, docs.st_ino, b"ReadMe", CONTEXT) == errno.EBUSY
    created, _entry = run_async(
        operations.create, docs.st_ino, b"Draft", 0o644, os.O_WRONLY, CONTEXT
    )
    run_async(operations.release, created.fh)
    assert (
        errno_of(operations.rename, docs.st_ino, b"Draft", docs.st_ino, b"ReadMe", 0, CONTEXT)
        == errno.EBUSY
    )
    run_async(operations.release, info.fh)
    run_async(operations.rename, docs.st_ino, b"Draft", docs.st_ino, b"ReadMe", 0, CONTEXT)


def test_names_are_validated_with_the_right_errors(tmp_path: Path) -> None:
    path = create_empty_floppy(tmp_path, filesystem="FFS")
    with AmigaImage.open(path, writable=True) as image:
        operations = AmigaOperations(image)
        assert (
            errno_of(operations.create, ROOT_INODE, b"n" * 31, 0o644, 0, CONTEXT)
            == errno.ENAMETOOLONG
        )
        assert (
            errno_of(operations.mkdir, ROOT_INODE, b"n" * 31, 0o755, CONTEXT) == errno.ENAMETOOLONG
        )
        for name in (b"a:b", "snow☃".encode(), b"\xff\xfe", b" padded"):
            assert errno_of(operations.create, ROOT_INODE, name, 0o644, 0, CONTEXT) == errno.EINVAL
        assert image.children[ROOT_INODE] == ()


def test_translated_overlong_name_still_reports_enametoolong(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("amigafs.core.image._", lambda message: f"translated {message}")
    path = create_empty_floppy(tmp_path, filesystem="FFS")
    with AmigaImage.open(path, writable=True) as image:
        operations = AmigaOperations(image)
        assert (
            errno_of(operations.create, ROOT_INODE, b"n" * 31, 0o644, 0, CONTEXT)
            == errno.ENAMETOOLONG
        )


def test_write_protected_file_is_presented_and_enforced_read_only(writable: Any) -> None:
    operations, path = writable
    docs = walk(operations, b"Docs")
    readme = walk(operations, b"Docs", b"ReadMe")
    run_async(operations.setxattr, readme.st_ino, b"user.amiga.locked", b"1", CONTEXT)
    entry = run_async(operations.getattr, readme.st_ino, CONTEXT)
    assert entry.st_mode & 0o777 == 0o444
    assert run_async(operations.getxattr, readme.st_ino, b"user.amiga.locked", CONTEXT) == b"1"
    assert (
        run_async(operations.getxattr, readme.st_ino, b"user.amiga.protection", CONTEXT)
        == b"----r-e-"
    )
    assert errno_of(operations.open, readme.st_ino, os.O_WRONLY, CONTEXT) == errno.EACCES
    assert errno_of(operations.access, readme.st_ino, os.W_OK, CONTEXT) == errno.EACCES
    assert errno_of(operations.unlink, docs.st_ino, b"ReadMe", CONTEXT) == errno.EACCES
    run_async(operations.setxattr, readme.st_ino, b"user.amiga.locked", b"false", CONTEXT)
    run_async(operations.unlink, docs.st_ino, b"ReadMe", CONTEXT)
    operations.image.close()
    with AmigaImage.open(path) as image:
        assert image.lookup(image.node_at_path("Docs").inode, b"ReadMe") is None


def test_chmod_maps_to_the_write_and_delete_bits(writable: Any) -> None:
    operations, path = writable
    script = walk(operations, b"S", b"Startup-Sequence")
    locked = run_async(
        operations.setattr,
        script.st_ino,
        SimpleNamespace(st_mode=0o100444),
        fields(update_mode=True),
        None,
        CONTEXT,
    )
    assert locked.st_mode & 0o777 == 0o444
    # The script bit the file already carried is untouched.
    assert (
        run_async(operations.getxattr, script.st_ino, b"user.amiga.protection", CONTEXT)
        == b"-s--r-e-"
    )
    unlocked = run_async(
        operations.setattr,
        script.st_ino,
        SimpleNamespace(st_mode=0o100644),
        fields(update_mode=True),
        None,
        CONTEXT,
    )
    assert unlocked.st_mode & 0o777 == 0o644
    stamped = run_async(
        operations.setattr,
        script.st_ino,
        SimpleNamespace(st_mtime_ns=650_000_000 * 1_000_000_000),
        fields(update_mtime=True),
        None,
        CONTEXT,
    )
    assert stamped.st_mtime_ns == 650_000_000 * 1_000_000_000
    assert (
        errno_of(
            operations.setattr,
            script.st_ino,
            SimpleNamespace(st_uid=0),
            fields(update_uid=True),
            None,
            CONTEXT,
        )
        == errno.ENOTSUP
    )
    operations.image.close()
    with AmigaImage.open(path) as image:
        node = image.node_at_path("S/Startup-Sequence")
        assert node.protection == 0x40
        assert node.mtime_ns == 650_000_000 * 1_000_000_000


def test_amiga_extended_attributes_are_visible_and_persisted(writable: Any) -> None:
    operations, path = writable
    listed = walk(operations, b"C", b"List")
    names = run_async(operations.listxattr, listed.st_ino, CONTEXT)
    assert set(names) == {
        b"user.amiga.protection",
        b"user.amiga.locked",
        b"user.amiga.source",
        b"user.amiga.path",
        b"user.amiga.volume",
    }
    assert run_async(operations.getxattr, listed.st_ino, b"user.amiga.source", CONTEXT) == b"FFS"
    assert (
        run_async(operations.getxattr, listed.st_ino, b"user.amiga.path", CONTEXT)
        == b"Workbench:C/List"
    )
    assert (
        errno_of(operations.getxattr, listed.st_ino, b"user.amiga.comment", CONTEXT)
        == errno.ENODATA
    )
    run_async(
        operations.setxattr, listed.st_ino, b"user.amiga.comment", "Größe ©".encode(), CONTEXT
    )
    run_async(operations.setxattr, listed.st_ino, b"user.amiga.protection", b"hsparwed", CONTEXT)
    assert b"user.amiga.comment" in run_async(operations.listxattr, listed.st_ino, CONTEXT)
    assert (
        run_async(operations.getxattr, listed.st_ino, b"user.amiga.comment", CONTEXT)
        == "Größe ©".encode()
    )
    run_async(operations.setxattr, listed.st_ino, b"user.amiga.protection", b"rwed", CONTEXT)
    assert (
        run_async(operations.getxattr, listed.st_ino, b"user.amiga.protection", CONTEXT)
        == b"----rwed"
    )
    run_async(operations.setxattr, listed.st_ino, b"user.amiga.protection", b"&40", CONTEXT)
    root_names = run_async(operations.listxattr, ROOT_INODE, CONTEXT)
    assert set(root_names) == {b"user.amiga.source", b"user.amiga.path", b"user.amiga.volume"}
    run_async(operations.setxattr, ROOT_INODE, b"user.amiga.volume", b"Relabelled", CONTEXT)
    operations.image.close()
    with AmigaImage.open(path) as image:
        node = image.node_at_path("C/List")
        assert (node.comment, node.protection) == ("Größe ©", 0x40)
        assert image.volume_title(0) == "Relabelled"


def test_comment_can_be_removed(writable: Any) -> None:
    operations, path = writable
    readme = walk(operations, b"Docs", b"ReadMe")
    run_async(operations.removexattr, readme.st_ino, b"user.amiga.comment", CONTEXT)
    assert (
        errno_of(operations.getxattr, readme.st_ino, b"user.amiga.comment", CONTEXT)
        == errno.ENODATA
    )
    assert (
        errno_of(operations.removexattr, readme.st_ino, b"user.amiga.protection", CONTEXT)
        == errno.ENOTSUP
    )
    operations.image.close()
    with AmigaImage.open(path) as image:
        assert image.node_at_path("Docs/ReadMe").comment == ""


def test_metadata_set_on_an_open_file_is_committed_with_its_data(writable: Any) -> None:
    operations, path = writable
    readme = walk(operations, b"Docs", b"ReadMe")
    info = run_async(operations.open, readme.st_ino, os.O_WRONLY, CONTEXT)
    with patch.object(
        operations.image, "set_metadata", wraps=operations.image.set_metadata
    ) as commit:
        run_async(operations.setxattr, readme.st_ino, b"user.amiga.comment", b"one", CONTEXT)
        run_async(
            operations.setxattr, readme.st_ino, b"user.amiga.protection", b"-s--rwed", CONTEXT
        )
        assert commit.call_count == 0
        assert (
            run_async(operations.getxattr, readme.st_ino, b"user.amiga.comment", CONTEXT) == b"one"
        )
        run_async(operations.write, info.fh, 0, b"X")
        run_async(operations.release, info.fh)
        # Both changes were coalesced into one image transaction.
        assert commit.call_count == 1
    operations.image.close()
    with AmigaImage.open(path) as image:
        node = image.node_at_path("Docs/ReadMe")
        assert (node.comment, node.protection) == ("one", 0x40)
        assert image.read(node.inode, 0, 5) == b"Xead "


def test_failed_metadata_batch_remains_pending(tmp_path: Path) -> None:
    image = AmigaImage.open(create_floppy(tmp_path), writable=True)
    try:
        operations = AmigaOperations(image)
        readme = walk(operations, b"Docs", b"ReadMe")
        info = run_async(operations.open, readme.st_ino, os.O_RDONLY, CONTEXT)
        run_async(operations.setxattr, readme.st_ino, b"user.amiga.comment", b"kept", CONTEXT)
        with patch.object(image, "set_metadata", side_effect=AmigaFSError("injected")):
            assert errno_of(operations.flush, info.fh) == errno.EIO
        assert operations._metadata_updates[readme.st_ino].comment == "kept"
    finally:
        image.close(clean=False)


def test_invalid_or_read_only_attribute_changes_are_rejected(writable: Any) -> None:
    operations, path = writable
    before = path.read_bytes()
    listed = walk(operations, b"C", b"List")
    for name, value in (
        (b"user.amiga.protection", b"rwx"),
        (b"user.amiga.protection", b""[:0] + b"zzzz"),
        (b"user.amiga.comment", b"c" * 80),
        (b"user.amiga.comment", "snow☃".encode()),
        (b"user.amiga.comment", b"two\nlines"),
        (b"user.amiga.comment", b"\xff\xfe"),
        (b"user.amiga.locked", b"maybe"),
    ):
        assert errno_of(operations.setxattr, listed.st_ino, name, value, CONTEXT) == errno.EINVAL
    for name in (b"user.amiga.source", b"user.amiga.path", b"user.amiga.link"):
        assert errno_of(operations.setxattr, listed.st_ino, name, b"x", CONTEXT) == errno.EPERM
    assert (
        errno_of(operations.setxattr, listed.st_ino, b"user.other", b"x", CONTEXT) == errno.ENOTSUP
    )
    assert (
        errno_of(operations.setxattr, listed.st_ino, b"user.amiga.volume", b"x", CONTEXT)
        == errno.EPERM
    )
    assert (
        errno_of(operations.setxattr, ROOT_INODE, b"user.amiga.comment", b"x", CONTEXT)
        == errno.EPERM
    )
    assert (
        errno_of(operations.setxattr, ROOT_INODE, b"user.amiga.volume", b"bad:name", CONTEXT)
        == errno.EINVAL
    )
    assert path.read_bytes() == before


def test_namespace_mutations_notify_the_kernel_cache(writable: Any) -> None:
    operations, _path = writable
    with (
        patch.object(pyfuse3, "invalidate_inode") as inode,
        patch.object(pyfuse3, "invalidate_entry_async") as entry,
    ):
        created = run_async(operations.mkdir, ROOT_INODE, b"Notify", 0o755, CONTEXT)
        entry.assert_any_call(ROOT_INODE, b"Notify", deleted=0, ignore_enoent=True)
        inode.assert_any_call(ROOT_INODE, attr_only=True)
        run_async(operations.rmdir, ROOT_INODE, b"Notify", CONTEXT)
        entry.assert_any_call(ROOT_INODE, b"Notify", deleted=created.st_ino, ignore_enoent=True)


def test_kernel_notification_failures_never_fail_a_committed_write(writable: Any) -> None:
    operations, path = writable
    with (
        patch.object(pyfuse3, "invalidate_inode", side_effect=OSError("no kernel")),
        patch.object(pyfuse3, "invalidate_entry_async", side_effect=RuntimeError("no kernel")),
    ):
        run_async(operations.mkdir, ROOT_INODE, b"Committed", 0o755, CONTEXT)
    operations.image.close()
    with AmigaImage.open(path) as image:
        assert image.node_at_path("Committed").is_dir


def test_capacity_is_reported_as_enospc_before_buffers_grow(tmp_path: Path) -> None:
    path = create_empty_floppy(tmp_path, filesystem="FFS")
    with AmigaImage.open(path, writable=True) as image:
        operations = AmigaOperations(image)
        info, entry = run_async(operations.create, ROOT_INODE, b"Huge", 0o644, os.O_WRONLY, CONTEXT)
        assert errno_of(operations.write, info.fh, 2_000_000, b"x") == errno.ENOSPC
        assert len(operations._write_buffers[entry.st_ino]) == 0
        assert (
            errno_of(
                operations.setattr,
                entry.st_ino,
                SimpleNamespace(st_size=2_000_000),
                fields(update_size=True),
                None,
                CONTEXT,
            )
            == errno.ENOSPC
        )
        assert (
            errno_of(
                operations.setattr,
                entry.st_ino,
                SimpleNamespace(st_size=1 << 33),
                fields(update_size=True),
                None,
                CONTEXT,
            )
            == errno.EFBIG
        )
        assert errno_of(operations.write, info.fh, -1, b"x") == errno.EINVAL
        run_async(operations.write, info.fh, 0, b"fits")
        run_async(operations.release, info.fh)
    with AmigaImage.open(path) as image:
        assert image.read(image.node_at_path("Huge").inode, 0, 9) == b"fits"
