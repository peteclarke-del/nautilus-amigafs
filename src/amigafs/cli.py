"""Command-line interface for AmigaFS."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path

from amigafs.core import (
    FindingSeverity,
    apply_repairs,
    create_floppy_image,
    create_hard_disc_image,
    export_file,
    import_file,
    plan_repairs,
    read_image_properties,
    resolve_image,
    validate_image_report,
)
from amigafs.core.formats import floppy_reference
from amigafs.errors import AmigaFSError
from amigafs.mounts import active_mounts, mount_at, wait_for_mount_shutdown
from amigafs.recovery import pending_recovery, recover_image, salvage_workspace

IMAGE_HELP = "an Amiga image, a physical disc such as /dev/sdb, or floppy:A"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amigafs", description="Inspect and mount Commodore Amiga media"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    floppy_parser = subparsers.add_parser(
        "create-floppy", help="create a validated empty floppy image (ADF)"
    )
    floppy_parser.add_argument("directory", help="destination directory")
    floppy_parser.add_argument("--name", default="blank", help="image basename (default: blank)")
    floppy_parser.add_argument("--title", default="Empty", help="volume name (default: Empty)")
    floppy_parser.add_argument(
        "--density", default="dd", choices=("dd", "hd"), help="880 KiB dd or 1760 KiB hd"
    )
    floppy_parser.add_argument(
        "--filesystem", default="OFS", help="OFS, FFS or an -INTL, -DC or -LNFS variant"
    )
    floppy_parser.add_argument(
        "--bootable", action="store_true", help="install a standard boot block"
    )
    disc_parser = subparsers.add_parser(
        "create-hard-disc", help="create a validated RDB hard-disc image (HDF)"
    )
    disc_parser.add_argument("directory", help="destination directory")
    disc_parser.add_argument("--name", default="harddisk", help="image basename")
    disc_parser.add_argument("--title", default="Empty", help="volume name of the first partition")
    disc_parser.add_argument("--capacity", default="40MB", help="image capacity (default: 40MB)")
    disc_parser.add_argument(
        "--filesystem", default="FFS-INTL", help="an OFS/FFS variant, PFS3 or SFS"
    )
    disc_parser.add_argument(
        "--partitions", type=int, default=1, help="number of equal partitions (default: 1)"
    )
    export_parser = subparsers.add_parser(
        "export-file", help="export one image file with an Amiga INF metadata sidecar"
    )
    export_parser.add_argument("image", help=IMAGE_HELP)
    export_parser.add_argument(
        "amiga_path", help="path inside the image, for example S/Startup-Sequence or DH0:C/List"
    )
    export_parser.add_argument("destination", help="new host filename; existing files are refused")
    import_parser = subparsers.add_parser(
        "import-file", help="import one host file and its Amiga metadata"
    )
    import_parser.add_argument("image", help=IMAGE_HELP)
    import_parser.add_argument("source", help="host file to import")
    import_parser.add_argument(
        "--directory",
        default="",
        help="destination drawer, for example Tools or DH0:Tools (default: the volume root)",
    )
    import_parser.add_argument("--name", help="Amiga leaf name; defaults to trusted metadata/name")
    import_metadata = import_parser.add_mutually_exclusive_group()
    import_metadata.add_argument("--sidecar", help="explicit INF sidecar path")
    import_metadata.add_argument(
        "--ignore-sidecar", action="store_true", help="ignore an automatically matching INF"
    )
    inspect_parser = subparsers.add_parser(
        "inspect", help="describe the format, partitions and volumes of an image"
    )
    inspect_parser.add_argument("image", help=IMAGE_HELP)
    inspect_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    validate_parser = subparsers.add_parser(
        "validate", help="validate the filesystem structure without modifying it"
    )
    validate_parser.add_argument("image", help=IMAGE_HELP)
    validate_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    repair_parser = subparsers.add_parser(
        "repair-plan", help="create a read-only dry-run repair plan"
    )
    repair_parser.add_argument("image", help=IMAGE_HELP)
    repair_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    apply_parser = subparsers.add_parser(
        "repair", help="apply a complete eligible low-risk repair plan"
    )
    apply_parser.add_argument("image", help=IMAGE_HELP)
    apply_parser.add_argument(
        "--confirm",
        required=True,
        metavar="IMAGE_FILENAME",
        help="explicitly confirm by entering the exact image filename",
    )
    apply_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    mount_parser = subparsers.add_parser(
        "mount", help="mount an image with FUSE 3 (read-only by default)"
    )
    mount_parser.add_argument("image", help=IMAGE_HELP)
    mount_parser.add_argument("mountpoint", help="an existing empty directory")
    mount_parser.add_argument(
        "--read-write",
        action="store_true",
        help="enable journalled writes where the detected format supports them",
    )
    mount_parser.add_argument("--debug", action="store_true", help="enable FUSE debug logging")
    unmount_parser = subparsers.add_parser("unmount", help="unmount an AmigaFS mount")
    unmount_parser.add_argument("mountpoint", help="the mounted directory")
    unmount_parser.add_argument(
        "--lazy", action="store_true", help="detach even when an application still holds it open"
    )
    status_parser = subparsers.add_parser("status", help="show AmigaFS mount status")
    status_parser.add_argument("mountpoint", nargs="?", help="optionally limit output to one path")
    status_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    diagnostics_parser = subparsers.add_parser(
        "diagnostics", help="print privacy-safe support diagnostics"
    )
    diagnostics_parser.add_argument("--json", action="store_true", help="emit JSON")
    discs_parser = subparsers.add_parser(
        "list-discs", help="list removable and USB discs AmigaFS may open"
    )
    discs_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    discs_parser.add_argument(
        "--all", action="store_true", help="also list discs that are refused, with the reason"
    )
    read_disc_parser = subparsers.add_parser(
        "read-disc", help="copy a whole physical disc to a new hard-disc image"
    )
    read_disc_parser.add_argument("device", help="the disc, for example /dev/sdb")
    read_disc_parser.add_argument("destination", help="new image file; existing files are refused")
    write_disc_parser = subparsers.add_parser(
        "write-disc", help="replace a whole physical disc with a hard-disc image"
    )
    write_disc_parser.add_argument("image", help="a plain Amiga hard-disc image")
    write_disc_parser.add_argument("device", help="the disc to overwrite, for example /dev/sdb")
    write_disc_parser.add_argument(
        "--confirm",
        required=True,
        metavar="DEVICE_NAME",
        help="explicitly confirm by entering the kernel device name, for example sdb",
    )
    read_floppy_parser = subparsers.add_parser(
        "read-floppy", help="capture a physical floppy as a new ADF using Greaseweazle"
    )
    read_floppy_parser.add_argument("drive", help="Greaseweazle drive: A, B, 0, 1, 2 or 3")
    read_floppy_parser.add_argument("destination", help="new .adf file; existing files are refused")
    read_floppy_parser.add_argument("--density", default="auto", choices=("auto", "dd", "hd"))
    write_floppy_parser = subparsers.add_parser(
        "write-floppy", help="write an image to a physical floppy using Greaseweazle"
    )
    write_floppy_parser.add_argument("image", help="an ADF, ADZ, DMS, HFE, SCP or IPF image")
    write_floppy_parser.add_argument("drive", help="Greaseweazle drive: A, B, 0, 1, 2 or 3")
    write_floppy_parser.add_argument(
        "--yes", action="store_true", help="confirm that the floppy may be overwritten"
    )
    config_parser = subparsers.add_parser(
        "config-mount-location", help="show or set the persistent desktop mount location"
    )
    config_parser.add_argument(
        "location",
        nargs="?",
        help="sidebar, runtime, or an absolute directory path",
    )
    config_parser.add_argument(
        "--reset", action="store_true", help="remove the saved value and restore the default"
    )
    install_parser = subparsers.add_parser(
        "install-nautilus", help="install the per-user Nautilus and MIME integration"
    )
    install_parser.add_argument("--restart", action="store_true", help="restart Nautilus now")
    uninstall_parser = subparsers.add_parser(
        "uninstall-nautilus", help="remove the per-user Nautilus and MIME integration"
    )
    uninstall_parser.add_argument("--restart", action="store_true", help="restart Nautilus now")
    desktop_mount_parser = subparsers.add_parser("desktop-mount")
    desktop_mount_parser.add_argument("image")
    desktop_mount_parser.add_argument("--read-write", action="store_true")
    desktop_unmount_parser = subparsers.add_parser("desktop-unmount")
    desktop_unmount_parser.add_argument("mountpoint")
    for name in (
        "desktop-recover",
        "desktop-repair",
        "desktop-validate",
        "desktop-open-file-forge",
        "desktop-write-floppy",
        "desktop-write-disc",
    ):
        subparsers.add_parser(name).add_argument("image")
    for name in ("desktop-mount-floppy", "desktop-mount-disc"):
        subparsers.add_parser(name).add_argument("--read-write", action="store_true")
    for name in ("desktop-read-floppy", "desktop-read-disc"):
        subparsers.add_parser(name).add_argument("directory")
    desktop_open_parser = subparsers.add_parser("desktop-open")
    desktop_open_parser.add_argument("--handed-off", action="store_true")
    desktop_open_parser.add_argument("images", nargs="+")
    subparsers.add_parser("desktop-claims").add_argument("image")
    desktop_create_parser = subparsers.add_parser("desktop-create")
    desktop_create_parser.add_argument("directory")
    desktop_create_parser.add_argument("--kind", default="floppy", choices=("floppy", "hard-disc"))
    subparsers.add_parser("desktop-configure-mount-location")
    recover_parser = subparsers.add_parser(
        "recover", help="inspect or resolve an interrupted writable session"
    )
    recover_parser.add_argument("image", help=IMAGE_HELP)
    recover_action = recover_parser.add_mutually_exclusive_group()
    recover_action.add_argument("--restore", action="store_true", help="restore the checkpoint")
    recover_action.add_argument(
        "--discard", action="store_true", help="keep the current image and delete the checkpoint"
    )
    recover_action.add_argument(
        "--salvage",
        metavar="FILE",
        help="save an interrupted container or floppy working copy as a new image",
    )
    return parser


def _size(value: int | None) -> str:
    if value is None:
        return "unknown"
    amount = float(value)
    for unit in ("bytes", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{value} bytes" if unit == "bytes" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value} bytes"


def _reference(selected: str) -> str | Path:
    """Return the recovery identity of a reference, which for a floppy is its token."""

    if floppy_reference(selected) is not None:
        return resolve_image(selected).primary_path
    return selected


def _inspect(args: argparse.Namespace) -> int:
    properties = read_image_properties(args.image)
    if args.json:
        print(json.dumps(properties.as_dict(), indent=2, sort_keys=True))
        return 0
    print(f"Image: {properties.image_name}")
    print(f"Type: {properties.image_type}")
    if properties.container:
        print(f"Container: {properties.container}")
    print(f"Layout: {properties.layout_label}")
    print(f"Capacity: {_size(properties.capacity_bytes)}")
    if properties.cylinders is not None:
        print(
            f"Geometry: {properties.cylinders} cylinders, {properties.heads} heads, "
            f"{properties.sectors_per_track} sectors per track"
        )
    if properties.disc_product:
        print(
            f"Drive identity: {properties.disc_vendor} {properties.disc_product} "
            f"{properties.disc_revision}".rstrip()
        )
    print(f"Read-write mounting: {'supported' if properties.read_write_supported else 'no'}")
    for volume in properties.volumes:
        label = volume.name or "volume"
        state = "" if volume.mounted else f" (not mounted: {volume.problem})"
        print(f"- {label}: {volume.format} {volume.dos_type} {volume.title!r}{state}")
        print(
            f"    {_size(volume.capacity_bytes)} total, {_size(volume.free_bytes)} free; "
            f"{volume.files} files, {volume.directories} drawers"
        )
        if volume.first_cylinder is not None:
            boot = f", bootable at priority {volume.boot_priority}" if volume.bootable else ""
            print(f"    cylinders {volume.first_cylinder}-{volume.last_cylinder}{boot}")
    print(f"Validation: {properties.validation_state}")
    if properties.first_finding:
        print(f"First finding: {properties.first_finding}")
    return 0


def _create_floppy(args: argparse.Namespace) -> int:
    result = create_floppy_image(
        args.directory,
        name=args.name,
        title=args.title,
        density=args.density,
        filesystem=args.filesystem,
        bootable=args.bootable,
    )
    print(f"Created and verified floppy image: {result.path}")
    print(f"Volume: {result.title}; {result.filesystem}; {result.capacity_bytes} bytes")
    return 0


def _create_hard_disc(args: argparse.Namespace) -> int:
    result = create_hard_disc_image(
        args.directory,
        name=args.name,
        title=args.title,
        capacity=args.capacity,
        filesystem=args.filesystem,
        partitions=args.partitions,
    )
    print(f"Created and verified hard-disc image: {result.path}")
    print(
        f"Partitions: {', '.join(result.partitions)}; {result.filesystem}; "
        f"{result.capacity_bytes} bytes"
    )
    return 0


def _export_file(args: argparse.Namespace) -> int:
    result = export_file(args.image, args.amiga_path, args.destination)
    print(f"Exported {result.amiga_path} to {result.data_path}")
    print(f"Amiga metadata: {result.sidecar_path}")
    return 0


def _import_file(args: argparse.Namespace) -> int:
    result = import_file(
        args.image,
        args.source,
        directory=args.directory,
        name=args.name,
        sidecar=args.sidecar,
        ignore_sidecar=args.ignore_sidecar,
    )
    print(f"Imported {result.source_path} as {result.node.amiga_path}")
    print(f"Metadata source: {result.metadata_source}")
    return 0


def _mount(args: argparse.Namespace) -> int:
    try:
        from amigafs.fuse_adapter.runner import mount_image
    except ImportError as exc:
        raise AmigaFSError(
            "FUSE support is unavailable; install the 'fuse' package extra and FUSE 3 runtime."
        ) from exc
    mode = "read-write" if args.read_write else "read-only"
    desktop_mount = os.environ.get("AMIGAFS_DESKTOP_MOUNT") == "1"
    write_back = None
    progress = None
    if desktop_mount:
        from amigafs.desktop import notify_write_back

        write_back = notify_write_back
        print(f"Starting AmigaFS desktop mount {mode}.")
    else:
        print(f"Mounting {args.image} at {args.mountpoint} {mode}; press Ctrl-C to stop.")

        def progress(percent: int, detail: str) -> None:
            print(f"[{percent:3d}%] {detail}", file=sys.stderr)

        def write_back(name: str) -> None:
            print(f"Writing changes back to {name}; do not remove it.", file=sys.stderr)

    mount_image(
        args.image,
        args.mountpoint,
        read_write=args.read_write,
        debug=args.debug,
        progress=progress,
        write_back_started=write_back,
    )
    if desktop_mount:
        target = Path(args.mountpoint).expanduser().resolve()
        with suppress(OSError):
            target.rmdir()
        with suppress(OSError):
            target.parent.rmdir()
    return 0


def _validate(args: argparse.Namespace) -> int:
    report = validate_image_report(args.image)
    if args.json:
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        print(report.format_text())
    return 1 if any(item.severity is not FindingSeverity.ADVICE for item in report.findings) else 0


def _repair_plan(args: argparse.Namespace) -> int:
    plan = plan_repairs(args.image)
    if args.json:
        print(json.dumps(plan.as_dict(), indent=2, sort_keys=True))
    else:
        print(plan.format_text())
    return 1 if plan.report.fatal_findings else 0


def _repair(args: argparse.Namespace) -> int:
    result = apply_repairs(args.image, confirmation=args.confirm)
    if args.json:
        print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    else:
        print(f"Applied {len(result.actions)} repair action(s).")
        print(result.report.format_text())
        print(f"Audit report: {result.audit_path}")
    return 0


def _unmount(args: argparse.Namespace) -> int:
    from amigafs.desktop import shutdown_timeout

    target = Path(args.mountpoint).expanduser().resolve()
    record = mount_at(target)
    if record is None:
        raise AmigaFSError(f"No active AmigaFS mount was found at {target}.")
    if record.read_write is None:
        raise AmigaFSError(
            "The mount has no lifecycle identity record; its write mode and safe shutdown "
            "cannot be verified."
        )
    if args.lazy and record.read_write:
        raise AmigaFSError(
            "Lazy unmount is allowed only for a registry-confirmed read-only AmigaFS mount."
        )
    command = ["fusermount3", "-u"]
    if args.lazy:
        command.append("-z")
    command.append(str(target))
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        detail = result.stderr.strip() or "fusermount3 failed"
        raise AmigaFSError(f"Could not unmount {target}: {detail}")
    if record.read_write:
        if not wait_for_mount_shutdown(target, timeout=shutdown_timeout(record.image_kind)):
            raise AmigaFSError(
                "The mount detached but its daemon did not confirm final flush and validation."
            )
        if record.image_path is not None and pending_recovery(record.image_path) is not None:
            raise AmigaFSError(
                "The mount detached but its changes were not finalised; a recovery checkpoint "
                "is pending."
            )
    return 0


def _status(args: argparse.Namespace) -> int:
    mounts = active_mounts()
    if args.mountpoint:
        target = str(Path(args.mountpoint).expanduser().resolve())
        mounts = [mount for mount in mounts if mount.mountpoint == target]
    if args.json:
        print(json.dumps([mount.as_dict() for mount in mounts], indent=2, sort_keys=True))
    elif mounts:
        for mount in mounts:
            print(f"{mount.source} on {mount.mountpoint} ({mount.options})")
    else:
        print("No AmigaFS mounts found.")
    return 0


def _diagnostics(args: argparse.Namespace) -> int:
    from amigafs.diagnostics import diagnostic_report

    report = diagnostic_report()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        runtime = report["runtime"]
        fuse = report["fuse"]
        mounts = report["mounts"]
        hardware = report["hardware"]
        print(
            f"AmigaFS {runtime['amigafs']} on {runtime['platform']} {runtime['architecture']} "
            f"(Python {runtime['python']})"
        )
        print(
            f"FUSE device: {'accessible' if fuse['device_accessible'] else 'unavailable'}; "
            f"fusermount3: {'available' if fuse['fusermount3_available'] else 'unavailable'}"
        )
        print(
            f"Greaseweazle tools: {'installed' if hardware['greaseweazle_tools'] else 'missing'}; "
            f"device helper: {'installed' if hardware['device_helper'] else 'missing'}"
        )
        print(f"Active mounts: {len(mounts)}")
        for mount in mounts:
            mode = "read-write" if mount["read_write"] else "read-only/unknown"
            print(f"- {mount['source_name']} as {mount['mount_name']} ({mode})")
        print(report["privacy"])
    return 0


def _list_discs(args: argparse.Namespace) -> int:
    from amigafs.core.devices import list_discs

    discs = list_discs(include_refused=args.all)
    if args.json:
        print(json.dumps([disc.as_dict() for disc in discs], indent=2, sort_keys=True))
        return 0
    if not discs:
        print("No removable or USB disc is available.")
        return 0
    for disc in discs:
        access = "accessible" if disc.accessible else "needs authorisation"
        state = f"refused: {disc.refusal}" if disc.refusal else access
        print(f"{disc.stable_path}")
        print(f"    {disc.model or disc.name}, {_size(disc.size)}, {disc.device} ({state})")
    return 0


def _progress(percent: int, detail: str) -> None:
    print(f"[{percent:3d}%] {detail}", file=sys.stderr)


def _read_disc(args: argparse.Namespace) -> int:
    from amigafs.core.disc_transfer import read_disc

    result = read_disc(args.device, args.destination, progress=_progress)
    print(f"Saved {result.size} bytes from {result.device} as {result.image}")
    print(f"SHA-256: {result.sha256}")
    return 0


def _write_disc(args: argparse.Namespace) -> int:
    from amigafs.core.disc_transfer import write_disc

    result = write_disc(args.image, args.device, confirmation=args.confirm, progress=_progress)
    print(f"Wrote and verified {result.size} bytes on {result.device}")
    print(f"SHA-256: {result.sha256}")
    return 0


def _read_floppy(args: argparse.Namespace) -> int:
    from amigafs.greaseweazle import read_floppy

    result = read_floppy(args.destination, args.drive, density=args.density, progress=_progress)
    print(f"Saved a complete {result.floppy_format.label} image as {result.path}")
    return 0


def _write_floppy(args: argparse.Namespace) -> int:
    from amigafs.greaseweazle import write_floppy

    if not args.yes:
        raise AmigaFSError(
            "Writing replaces everything on the floppy. Repeat the command with --yes to confirm."
        )
    result = write_floppy(args.image, args.drive, progress=_progress)
    state = "and verified" if result.verified else "without read-back verification"
    print(f"Wrote {args.image} to drive {result.drive} {state}")
    return 0


def _config_mount_location(args: argparse.Namespace) -> int:
    from amigafs.preferences import mount_location, reset_mount_location, set_mount_location

    if args.reset and args.location is not None:
        raise AmigaFSError("Specify either a mount location or --reset, not both.")
    if args.reset:
        result = reset_mount_location()
    elif args.location is not None:
        set_mount_location(args.location)
        result = mount_location()
    else:
        result = mount_location()
    print(f"Mount location: {result.root}")
    print(f"Mode: {result.mode}; source: {result.source}")
    if os.environ.get("AMIGAFS_MOUNT_ROOT") is not None:
        print("AMIGAFS_MOUNT_ROOT currently overrides the saved preference.")
    return 0


def _install_nautilus(args: argparse.Namespace) -> int:
    from amigafs.nautilus_install import install_extension

    target = install_extension(restart=args.restart)
    print(f"Installed AmigaFS desktop integration: {target}")
    if not args.restart:
        print("Restart Nautilus to load it: nautilus --quit")
    return 0


def _uninstall_nautilus(args: argparse.Namespace) -> int:
    from amigafs.nautilus_install import uninstall_extension

    target = uninstall_extension(restart=args.restart)
    print(f"Removed AmigaFS desktop integration: {target}")
    return 0


def _desktop(args: argparse.Namespace) -> int:
    from amigafs import desktop

    command = args.command
    if command == "desktop-mount":
        return desktop.desktop_mount(args.image, read_write=args.read_write)
    if command == "desktop-unmount":
        return desktop.desktop_unmount(args.mountpoint)
    if command == "desktop-recover":
        return desktop.desktop_recover(args.image)
    if command == "desktop-repair":
        return desktop.desktop_repair(args.image)
    if command == "desktop-validate":
        return desktop.desktop_validate(args.image)
    if command == "desktop-open-file-forge":
        return desktop.desktop_open_file_forge(args.image)
    if command == "desktop-write-floppy":
        return desktop.desktop_write_floppy(args.image)
    if command == "desktop-write-disc":
        return desktop.desktop_write_disc(args.image)
    if command == "desktop-mount-floppy":
        return desktop.desktop_mount_floppy(read_write=args.read_write)
    if command == "desktop-mount-disc":
        return desktop.desktop_mount_disc(read_write=args.read_write)
    if command == "desktop-read-floppy":
        return desktop.desktop_read_floppy(args.directory)
    if command == "desktop-read-disc":
        return desktop.desktop_read_disc(args.directory)
    if command == "desktop-open":
        return desktop.desktop_open(args.images, handed_off=args.handed_off)
    if command == "desktop-claims":
        return desktop.desktop_claims(args.image)
    if command == "desktop-create":
        return desktop.desktop_create(args.directory, kind=args.kind)
    if command == "desktop-configure-mount-location":
        return desktop.desktop_configure_mount_location()
    raise AmigaFSError(f"Unknown desktop action: {command}")


def _recover(args: argparse.Namespace) -> int:
    reference = _reference(args.image)
    if args.salvage:
        target = salvage_workspace(reference, args.salvage)
        print(f"Saved the interrupted working copy as {target}")
        print("Run 'amigafs recover IMAGE --discard' to remove the working copy.")
        return 0
    print(recover_image(reference, restore=args.restore, discard=args.discard))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    handlers = {
        "create-floppy": _create_floppy,
        "create-hard-disc": _create_hard_disc,
        "export-file": _export_file,
        "import-file": _import_file,
        "inspect": _inspect,
        "validate": _validate,
        "repair-plan": _repair_plan,
        "repair": _repair,
        "mount": _mount,
        "unmount": _unmount,
        "status": _status,
        "diagnostics": _diagnostics,
        "list-discs": _list_discs,
        "read-disc": _read_disc,
        "write-disc": _write_disc,
        "read-floppy": _read_floppy,
        "write-floppy": _write_floppy,
        "config-mount-location": _config_mount_location,
        "install-nautilus": _install_nautilus,
        "uninstall-nautilus": _uninstall_nautilus,
        "recover": _recover,
    }
    try:
        handler = handlers.get(args.command, _desktop)
        return handler(args)
    except AmigaFSError as exc:
        if args.command == "mount" and os.environ.get("AMIGAFS_DESKTOP_MOUNT") == "1":
            from amigafs.desktop import notify_mount_failure
            from amigafs.privacy import safe_user_message

            message = safe_user_message(exc)
            notify_mount_failure(message)
            print(f"amigafs: {message}", file=sys.stderr)
        else:
            print(f"amigafs: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
