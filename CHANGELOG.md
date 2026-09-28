# Changelog

All notable changes to Nautilus AmigaFS are recorded here. The project follows
[Semantic Versioning](https://semver.org/) and keeps unreleased work at the top.

## Unreleased

## 0.1.1 - 2026-09-28

### Fixed

- Files offers the menu and the Properties page for an image on a network
  share. The extension used to ignore every file whose address did not begin
  `file:`, which left out Windows shares opened in Files although the image
  could be inspected and mounted by name.

## 0.1.0 - 2026-09-28

The first release. It was started from the platform-neutral parts of
[Nautilus AcornFS](https://github.com/peteclarke-del/nautilus-acornfs) 0.2.0.

### Added

- Read-only and read-write FUSE 3 mounting of OFS and FFS in all eight
  variants (`DOS\0`–`DOS\7`), PFS3 and SFS.
- Floppy images, hard-disc images with a Rigid Disk Block, and hardfiles
  without a partition table. A partitioned disc is one mount with a folder per
  partition.
- Compressed images (`.adz`, `.hdz`), rewritten atomically after a clean
  read-write unmount.
- HxC `.hfe` v1 and v3 images, decoded and re-encoded through the Greaseweazle
  host tools with the container version preserved.
- Read-only DiskMasher archives, extended ADFs with standard tracks,
  SuperCard Pro and SPS images, and Kickstart ROMs.
- Physical hard discs and memory cards, opened through a polkit helper that
  passes one open descriptor to the unprivileged daemon.
- Physical floppies through Greaseweazle: read to an image, write from an
  image with verification, and mount with changed cylinders written back on a
  clean unmount.
- Whole-disc imaging in both directions, with typed confirmation and
  read-back verification for writes.
- Transactions that stage every change in memory, and an undo journal whose
  size depends on what changed.
- Validation with typed findings and versioned JSON; automatic repair of
  block-allocation bitmaps and directory caches with confirmation,
  checkpointing and a retained audit.
- Amiga datestamps as modification times; protection bits and comments as
  extended attributes; write and delete protection as Linux permissions.
- Creation of empty floppy images and partitioned hard-disc images.
- Import and export with Amiga File Forge `.inf` sidecars, and hand-off to an
  installed Amiga File Forge.
- A Nautilus submenu for images and another for folders, Properties pages for
  images and mounted entries, MIME types identified by content, and a hidden
  handler for double-click and `amigafs:` URIs.
- A reproducible Debian package, a per-user add-on and a release builder.

### Engine

- Vendored amiganut 1.1.1 from Amiga File Forge 1.7.0, commit
  `d2808429a53cfa2b1cd08077cb475aa0027cc542`, with its DiskMasher decoder.
- Local patch: drivers obtain a second view of their medium from the reader
  instead of reopening the image by name.
- Local patch: the OFS/FFS allocator searches the bitmap instead of stepping
  outwards one block at a time. The chosen block is unchanged.

### Security

- A source is identified from its content. A name only decides whether Files
  offers a menu.
- The computer's own discs, mounted discs, discs in use and discs with no
  Amiga structures are refused by the device policy, which the privileged
  helper applies again as root.
- A track-level image or floppy is mounted only when every sector decodes to a
  standard AmigaDOS layout, so copy protection is never flattened.
- Every exception the engine raises on untrusted data is treated as a finding
  or a refusal.
- Recovery refuses a medium whose capacity or pre-session content does not
  match the checkpoint.

### Not yet verified

- No physical disc, floppy drive, polkit prompt or Nautilus session has been
  exercised. See [docs/release-readiness.md](docs/release-readiness.md).
