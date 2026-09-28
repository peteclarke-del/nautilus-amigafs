from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from amigafs._vendor.amiganut.file import AmigaMeta
from amigafs.core.image import ROOT_INODE, AmigaImage
from amigafs.errors import AmigaFSError
from amigafs.recovery import pending_recovery
from tests.image_fixture import create_floppy, create_hard_disc, tree


class InjectedFault(RuntimeError):
    pass


def _fault_at(*stages: str) -> Callable[[str], None]:
    remaining = set(stages)

    def inject(stage: str) -> None:
        if stage in remaining:
            remaining.discard(stage)
            raise InjectedFault(stage)

    return inject


def _snapshot(image: AmigaImage) -> tuple[dict[str, int | None], int]:
    return tree(image), image.free_bytes


@pytest.mark.parametrize("filesystem", ["FFS", "OFS-DC", "PFS3", "SFS"])
@pytest.mark.parametrize("moment", ["before", "after"])
def test_namespace_faults_are_atomic(tmp_path: Path, filesystem: str, moment: str) -> None:
    if filesystem in {"PFS3", "SFS"}:
        path = create_hard_disc(tmp_path, filesystem=filesystem, capacity="8MB", partitions=1)
    else:
        path = create_floppy(tmp_path, filesystem=filesystem)
    stages = tuple(f"{name}.{moment}" for name in ("create", "mkdir", "unlink", "rmdir"))
    image = AmigaImage.open(path, writable=True, fault_injector=_fault_at(*stages))
    try:
        top = image.children[ROOT_INODE][0] if image.layout == "rdb" else ROOT_INODE
        image.make_directory(top, b"Spare")  # consumes the mkdir fault
    except InjectedFault:
        top = image.children[ROOT_INODE][0] if image.layout == "rdb" else ROOT_INODE
    try:
        drawer = image.lookup(top, b"Spare") or image.make_directory(top, b"Spare")
        before = _snapshot(image)
        disk = path.read_bytes()
        with pytest.raises(InjectedFault):
            image.create_file(top, b"Never")
        with pytest.raises(InjectedFault):
            image.remove(top, b"Empty", directory=False)
        with pytest.raises(InjectedFault):
            image.remove(top, b"Spare", directory=True)
        assert _snapshot(image) == before
        assert path.read_bytes() == disk
        assert drawer.inode in image.nodes
        # The session survives every rolled-back operation.
        image.create_file(top, b"Works")
        image.remove(top, b"Spare", directory=True)
    finally:
        image.close()
    assert pending_recovery(path) is None
    with AmigaImage.open(path) as reopened:
        assert reopened.integrity_report().findings == ()
        names = {node.name for node in reopened.nodes.values()}
        assert b"Works" in names and b"Never" not in names and b"Spare" not in names


def test_content_and_metadata_faults_leave_the_original_untouched(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    image = AmigaImage.open(
        path,
        writable=True,
        fault_injector=_fault_at("replace.after", "metadata.after", "import.after"),
    )
    try:
        node = image.node_at_path("Docs/ReadMe")
        original = image.read(node.inode, 0, node.size)
        disk = path.read_bytes()
        with pytest.raises(InjectedFault):
            image.replace_file(node.inode, b"lost")
        with pytest.raises(InjectedFault):
            image.set_metadata(node.inode, comment="lost", protection=0xFF)
        with pytest.raises(InjectedFault):
            image.import_file(ROOT_INODE, b"Lost", b"data", AmigaMeta(comment="lost"))
        assert path.read_bytes() == disk
        assert image.read(node.inode, 0, node.size) == original
        assert image.nodes[node.inode].comment == "Introduction"
        assert image.lookup(ROOT_INODE, b"Lost") is None
        image.replace_file(node.inode, b"kept")
        image.set_metadata(node.inode, comment="kept")
    finally:
        image.close()
    with AmigaImage.open(path) as reopened:
        node = reopened.node_at_path("Docs/ReadMe")
        assert reopened.read(node.inode, 0, 99) == b"kept"
        assert node.comment == "kept"
        assert reopened.integrity_report().findings == ()


def test_replacement_rename_restores_both_names_after_midpoint_fault(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    image = AmigaImage.open(
        path, writable=True, fault_injector=_fault_at("rename.destination_removed")
    )
    try:
        docs = image.node_at_path("Docs").inode
        draft = image.create_file(docs, b"Draft")
        image.replace_file(draft.inode, b"draft")
        before = _snapshot(image)
        with pytest.raises(InjectedFault):
            image.rename(docs, b"Draft", docs, b"ReadMe")
        assert _snapshot(image) == before
        assert image.read(image.node_at_path("Docs/ReadMe").inode, 0, 14) == b"Read me first."
        assert image.read(draft.inode, 0, 9) == b"draft"
        image.rename(docs, b"Draft", docs, b"ReadMe")
    finally:
        image.close()
    with AmigaImage.open(path) as reopened:
        assert reopened.read(reopened.node_at_path("Docs/ReadMe").inode, 0, 9) == b"draft"
        assert reopened.integrity_report().findings == ()


def test_an_operation_that_would_corrupt_the_volume_is_abandoned(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    image = AmigaImage.open(path, writable=True)
    try:
        mount = image.mount_for(0)
        honest = mount.write_bytes

        def sabotage(name: str, data: bytes, meta: AmigaMeta | None = None) -> None:
            honest(name, data, meta)
            block = mount.stat(name).block
            raw = bytearray(mount.volume.reader.read_block(block))
            raw[40] ^= 0xFF
            mount.volume.reader.write_block(block, bytes(raw))

        mount.write_bytes = sabotage  # type: ignore[method-assign]
        disk = path.read_bytes()
        with pytest.raises(AmigaFSError, match="would have left the volume inconsistent"):
            image.create_file(ROOT_INODE, b"Broken")
        assert path.read_bytes() == disk
        assert image.lookup(ROOT_INODE, b"Broken") is None
        # The driver was reopened, so the sabotage is gone with its cached state.
        image.create_file(ROOT_INODE, b"Sound")
    finally:
        image.close()
    with AmigaImage.open(path) as reopened:
        assert reopened.integrity_report().findings == ()


def test_large_volumes_defer_validation_until_unmount(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    image = AmigaImage.open(path, writable=True, commit_validation_bytes=0)
    calls = 0
    try:
        mount = image.mount_for(0)
        validate = mount.validate

        def counted() -> list[str]:
            nonlocal calls
            calls += 1
            return list(validate())

        mount.validate = counted  # type: ignore[method-assign]
        image.create_file(ROOT_INODE, b"One")
        image.make_directory(ROOT_INODE, b"Two")
        assert calls == 0
    finally:
        image.close()
    assert calls == 1


def test_concurrent_creates_commit_unique_consistent_inodes(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    image = AmigaImage.open(path, writable=True)
    created: list[int] = []
    errors: list[BaseException] = []

    def create(number: int) -> None:
        try:
            created.append(image.create_file(ROOT_INODE, f"Thread{number}".encode()).inode)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    try:
        threads = [threading.Thread(target=create, args=(number,)) for number in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors
        assert len(set(created)) == 8
    finally:
        image.close()
    with AmigaImage.open(path) as reopened:
        assert sum(node.name.startswith(b"Thread") for node in reopened.nodes.values()) == 8
        assert reopened.integrity_report().findings == ()


def test_a_write_that_fails_after_journalling_fails_closed(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    original = path.read_bytes()
    image = AmigaImage.open(path, writable=True, fault_injector=_fault_at("commit.journalled"))
    try:
        with pytest.raises(AmigaFSError, match="could not be written to the medium"):
            image.create_file(ROOT_INODE, b"Partial")
        with pytest.raises(AmigaFSError, match="session has failed"):
            image.create_file(ROOT_INODE, b"Refused")
    finally:
        image.close()
    info = pending_recovery(path)
    assert info is not None and info.kind == "journal"
    from amigafs.recovery import recover_image

    recover_image(path, restore=True)
    assert path.read_bytes() == original
    assert pending_recovery(path) is None
