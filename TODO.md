# Nautilus AmigaFS backlog

## Objective

A safe userspace filesystem for Commodore Amiga media. Mounted media behave
like ordinary folders in Nautilus, terminals, editors and file dialogs. A
Nautilus extension provides the actions; a FUSE daemon provides the
filesystem.

A checked item is implemented and backed by automated tests. An item that
needs hardware, a desktop session, an emulator or a release stays open until
it has been exercised there, however thoroughly the logic has been tested
against stand-ins.

## Guiding principles

- Keep the filesystem engine independent of Nautilus and GNOME.
- Run the daemon as the current user, without privileges.
- Treat every image and disc as untrusted input.
- Default to read-only.
- Never repair a volume merely because it was mounted.
- Keep every change transactional and every session recoverable.
- Never flatten track-level data to sectors without being asked.
- Preserve Amiga metadata even where POSIX has no equivalent.
- Share tested filesystem logic with Amiga File Forge instead of duplicating
  it.

## Done

### Foundation

- [x] Project scaffold, licence, contribution guide, lint and type
  configuration.
- [x] Vendored engine with a manifest, a verification tool and contract tests.
- [x] Generated fixtures for every format; no private media in the repository.
- [x] Changelog and release policy.

### Sources

- [x] Content-driven identification that never decodes or locks.
- [x] ADF, 880 KiB and 1760 KiB.
- [x] HDF with a Rigid Disk Block; hardfile without one; `.geo` sidecar.
- [x] ADZ and HDZ, read-write.
- [x] HFE v1 and v3, read-write, with density read from the header.
- [x] DMS, extended ADF, SCP and IPF, read-only.
- [x] Kickstart ROM, read-only.
- [x] Physical disc by descriptor, with device policy and polkit helper.
- [x] Physical floppy by Greaseweazle, with write-back of changed cylinders.

### Filesystems

- [x] OFS and FFS, `DOS\0` to `DOS\7`, read-write.
- [x] PFS3 and SFS, read-write.
- [x] Partitions presented as folders; unsupported partitions reported and
  skipped.

### Safety

- [x] Shared and exclusive locks; identity checks; hard-link refusal.
- [x] In-memory transactions; rollback by discarding staged writes.
- [x] Validation before commit on volumes up to 64 MiB, and at mount and
  unmount on all.
- [x] Undo journal with torn-record handling and medium fingerprinting.
- [x] Working-copy checkpoints with salvage.
- [x] External-change detection.
- [x] Deterministic hostile-image tests for every filesystem.
- [x] Bounded index, inspection budgets and cooperative cancellation.

### POSIX view

- [x] lookup, getattr, readdir, open, read, write, create, mkdir, unlink,
  rmdir, rename, setattr, statfs, flush, fsync, release.
- [x] Shared write buffers per inode; coalesced metadata updates; flush at
  shutdown.
- [x] Ranged reads and bounded read-ahead.
- [x] Datestamps, protection bits and comments.

### Tools and desktop

- [x] `inspect`, `validate`, `repair-plan`, `repair`, `recover`.
- [x] `mount`, `unmount`, `status`, `diagnostics`.
- [x] `create-floppy`, `create-hard-disc`.
- [x] `export-file`, `import-file` with `.inf` sidecars.
- [x] `list-discs`, `read-disc`, `write-disc`, `read-floppy`, `write-floppy`.
- [x] Nautilus menus for images and folders; Properties pages.
- [x] MIME types, desktop handler and `amigafs:` URIs.
- [x] Mount-location preference.
- [x] Amiga File Forge hand-off.
- [x] gettext throughout the desktop.

### Packaging

- [x] Debian package builder with the helper and polkit action.
- [x] Per-user add-on and lifecycle installer.
- [x] Reproducible release builder with SBOM and checksums.

## Open: must be exercised before a first release

### Real hardware and sessions

- [ ] Mount, edit and unmount a real CompactFlash or SD card through the
  polkit helper.
- [ ] Interrupt a physical-disc session by pulling the cable, and restore it.
- [ ] Read, write and write back real floppies with a Greaseweazle, on both
  bus types and both densities.
- [ ] Boot a floppy and a hard disc written by AmigaFS on a real Amiga.
- [ ] Run the [desktop acceptance matrix](docs/desktop-acceptance.md).
- [ ] Verify logout and shutdown with a read-write mount open.

### Interoperability

- [ ] Open images written by AmigaFS in an emulator, for every filesystem, and
  confirm the AmigaDOS validator does not run.
- [ ] Open real-world images: Workbench disks, a full hard-disc installation,
  discs written by PFS3AIO and SmartFileSystem themselves.
- [ ] Verify reading through hard and soft links against real media.
- [ ] Verify SFS volumes with a pending transaction log.

### Build and release

- [ ] Put the project under version control and run CI.
- [ ] Run `make deb`, `make addon`, `make release` and `make package-smoke`
  from a commit.
- [ ] Run the fuzz harnesses with Atheris.
- [ ] Review the regenerated `po/amigafs.pot` and verify no message is
  missing.
- [ ] Decide a signing-key policy.

## Open: improvements

### Engine, to propose upstream

- [ ] Offer both local patches to Amiga File Forge.
- [ ] Allocate file data sequentially rather than alternating around the
  header, so files written here are not fragmented on real hardware.
- [ ] Harden the engine's readers against hostile counts and offsets, so that
  AmigaFS's containment is a second line of defence.
- [ ] A ranged-read interface, so `amigafs.core.ranged` can stop using
  internals.
- [ ] A bitmap-rebuild operation, so `amigafs.core.bitmap` can stop using
  internals.

### Formats

- [ ] An Amiga partition table inside a PC partition, as on PiStorm and Emu68
  cards.
- [ ] SFS2.
- [ ] FAT (CrossDOS) floppies and partitions, or a clear hand-off to the
  kernel's driver.
- [ ] Encrypted Cloanto ROMs with a key file.
- [ ] Read-write DiskMasher and extended ADF, if a lossless encoder is
  available.
- [ ] Extended ADFs and track images with more than 80 cylinders.

### Behaviour

- [ ] Stream large writes to the engine instead of holding three copies in
  memory.
- [ ] Validate incrementally on large volumes, so that per-operation
  validation is not limited to 64 MiB.
- [ ] Present soft links as symbolic links.
- [ ] Create and remove links.
- [ ] Expose and edit partition attributes: boot priority, bootable flag,
  device name.
- [ ] Install a boot block on an existing floppy.
- [ ] Detect a floppy swapped during a mount, if Greaseweazle can report a
  disk change.
- [ ] Format a physical disc in place.
- [ ] Repair for PFS3 and SFS.
- [ ] Show progress for the write-back of a desktop mount in one dialog that
  follows the daemon, rather than a pulsing one.

### Platform

- [ ] arm64 and arm/v7 containers and packages.
- [ ] Other file managers.
