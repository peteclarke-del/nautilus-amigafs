# Nautilus AmigaFS

Nautilus AmigaFS is a userspace filesystem for Commodore Amiga media. It mounts
floppy and hard-disc images, compressed and track-level images, Kickstart ROMs,
physical hard discs and memory cards, and physical floppies read through a
Greaseweazle, all through FUSE 3. A small extension integrates the mounts with
GNOME Files (Nautilus). Other Linux applications can use the mounts without
Nautilus.

It is the Amiga counterpart of
[Nautilus AcornFS](https://github.com/peteclarke-del/nautilus-acornfs) and
keeps its safety model: mounts are read-only by default, and every writable
mount takes an exclusive lock, validates the volume before the first write,
keeps the means to undo the whole session, detects changes made behind its
back and validates again before it reports a clean unmount.

Protection bits, comments, datestamps, the source filesystem and the original
Amiga path are available as extended attributes. See [TODO.md](TODO.md) for
what is not done yet, and in particular for what has not been tried on real
hardware. The desktop walkthrough is in [docs/user-guide.md](docs/user-guide.md)
and physical discs and floppies are covered in
[docs/physical-media.md](docs/physical-media.md).

## What it opens

| Source | Recognised by | Read | Write |
| --- | --- | --- | --- |
| Floppy image (`.adf`), 880 KiB and 1760 KiB | `DOS\0`–`DOS\7` boot block | yes | yes |
| Hard-disc image with a partition table (`.hdf`) | Rigid Disk Block in the first 16 blocks | yes | yes |
| Hardfile without a partition table (`.hdf`) | volume signature in block 0 | yes | yes |
| Compressed image (`.adz`, `.hdz`) | gzip header | yes | yes, rewritten on clean unmount |
| HxC image (`.hfe`), v1 and v3 | HFE header | yes | yes, re-encoded on clean unmount |
| DiskMasher archive (`.dms`) | `DMS!` header | yes | no |
| Extended ADF with standard tracks | `UAE-1ADF` or `UAE--ADF` header | yes | no |
| SuperCard Pro (`.scp`) and SPS (`.ipf`) images | their headers | yes | no |
| Kickstart ROM (`.rom`, `.kick`) | ROM header and resident modules | yes | no |
| Physical hard disc, CF or SD card | kernel description and on-disc structures | yes | yes |
| Physical floppy in a Greaseweazle drive | `floppy:A`, `floppy:B`, `floppy:0`–`floppy:3` | yes | yes, changed cylinders written back |

Every source is identified from its content. A file's name is only used to
decide whether Files should offer a menu for it at all.

| Filesystem | Read | Write |
| --- | --- | --- |
| OFS and FFS (`DOS\0`, `DOS\1`) | yes | yes |
| International OFS and FFS (`DOS\2`, `DOS\3`) | yes | yes |
| Directory-cache OFS and FFS (`DOS\4`, `DOS\5`) | yes | yes |
| Long-filename OFS and FFS (`DOS\6`, `DOS\7`) | yes | yes |
| Professional File System 3 (`PFS\1`–`PFS\3`, `PDS\3`) | yes | yes |
| Smart File System (`SFS\0`) | yes | yes |
| Smart File System 2 (`SFS\2`) | no | no |

A hard disc with several partitions appears as one mount with a folder for each
partition, named after its device (`DH0`, `DH1`, …). A partition whose
filesystem AmigaFS cannot read is reported and left out; the others still mount.

The filesystems are read and written by
[amiganut](src/amigafs/_vendor/VENDORED.md), the engine of
[Amiga File Forge](https://github.com/peteclarke-del/AmigaFileForge), vendored
here at a pinned commit.

## Current functionality

- Identify every supported source from its header, without decoding or
  unpacking it, so building a menu is cheap.
- Mount every supported source read-only through FUSE 3, and every writable one
  read-write.
- Traverse directories and open files from Nautilus and other Linux
  applications, with case-insensitive lookup as on an Amiga.
- Create, replace, truncate, rename and delete files and directories.
- Present each entry's Amiga datestamp as its modification time, and set it.
- Read and set protection bits and comments through extended attributes, and
  map the write and delete bits to ordinary Linux read-only permissions.
- Run each change as one transaction that either fully applies or never
  happened. Nothing reaches the medium until the change is complete and, on a
  volume of up to 64 MiB, has been validated.
- Keep an undo journal for each writable session, whose size depends on what
  changed and not on the size of the disc, and restore it after a crash.
- Keep a private working copy for compressed, track-level and physical-floppy
  sources, and write it back only after a clean unmount.
- Open physical discs without giving the filesystem daemon any privilege,
  through a polkit helper that passes back one open descriptor.
- Refuse the computer's own discs, mounted discs and discs with no Amiga
  structures.
- Read a physical floppy to a new image, write an image to a physical floppy
  with verification, and copy a whole physical disc to and from an image.
- Validate partitions, directories and block allocation with typed reports and
  versioned JSON.
- Rebuild a damaged block-allocation bitmap or directory cache, with typed
  confirmation, a recovery checkpoint and a retained audit.
- Create empty floppy images and partitioned hard-disc images.
- Import and export individual files with the `.inf` metadata sidecars Amiga
  File Forge uses.
- Hand an image to an installed Amiga File Forge without invoking a shell.
- Keep mount, validation, recovery and unmount actions in one Nautilus submenu,
  and show image and entry details in Properties.
- Run desktop mounts as collected systemd user services with graceful logout
  cleanup.
- Read large files a range at a time with bounded read-ahead.
- Export privacy-safe support information through `amigafs diagnostics --json`.
- Translate all desktop text through gettext.

## Installation

No release has been published yet. Until one is, install from a checkout as
described under [Development](#development). The two release forms below are
built by `make deb` and `make addon`.

### Debian package

The package targets Ubuntu 24.04 LTS on amd64, Python 3.11 or 3.12, FUSE 3 and
GNOME Files 46 or later.

```shell
sudo apt update
sudo apt install ./nautilus-amigafs_VERSION_amd64.deb
nautilus --quit
```

`apt` installs the required Ubuntu packages. The package installs the command,
the Files integration, the device helper and its polkit action. It does not use
`pip`, compile code or access the network except through `apt`. If `apt` cannot
find `python3-pyfuse3`, enable Ubuntu's Universe component:

```shell
sudo add-apt-repository universe
sudo apt update
```

Verify the installation with:

```shell
amigafs --help
amigafs status
amigafs diagnostics
test -f /usr/share/nautilus-python/extensions/nautilus_amigafs.py
```

### Per-user add-on

The add-on installs into `~/.local` without root privileges:

```shell
unzip nautilus-amigafs-addon-VERSION.zip -d nautilus-amigafs-addon
cd nautilus-amigafs-addon
python3 install.py --restart install
```

It cannot install the root-owned device helper, so it opens a physical disc
only when the account already has access to it. Everything else works the same
way. If `amigafs` is not found in a new terminal, add `~/.local/bin` to `PATH`.

### Upgrade or uninstall

Unmount every AmigaFS image first. Upgrade by installing the replacement
package. Remove the system package with `sudo apt remove nautilus-amigafs`, or
the add-on with `python3 install.py --restart uninstall`. Neither removes
images, preferences, recovery checkpoints or repair audits.

## Use in Files

Right-click a supported image and open **Amiga FS Support**. Depending on the
format it offers **Open read-only**, **Open read-write**, **Validate image**,
**Repair image…**, **Write to physical floppy…**, **Write to physical disc…**
and **Open in Amiga File Forge…**. Actions that the format or the attached
hardware cannot perform are not shown.

Right-click a folder, or the background of one, for the actions that do not
start from a file:

- **Create floppy image…** and **Create hard-disc image…**
- **Open physical floppy read-only…** and **read-write…**, and **Read physical
  floppy to image…**, when a Greaseweazle is attached
- **Open physical Amiga disc read-only…** and **read-write…**, and **Read
  physical disc to image…**, when a removable or USB disc is attached
- **Resolve interrupted session on …** when a physical disc or floppy session
  did not finish
- **Mount location…**

The mounted image opens in Files and appears in its sidebar. **Unmount** is in
the same submenu, on the image and on the mounted folder. Double-clicking a
recognised image opens it read-only, as does a local URI such as
`amigafs:///path/to/workbench.adf`.

Desktop mounts default to `~/AmigaFS Mounts`. Change that with **Mount
location…** or:

```shell
amigafs config-mount-location runtime
amigafs config-mount-location
amigafs config-mount-location --reset
```

## Use in a terminal

```shell
amigafs inspect workbench.adf
amigafs validate --json system.hdf

mkdir -p ~/AmigaFS/workbench
amigafs mount workbench.adf ~/AmigaFS/workbench
amigafs mount --read-write system.hdf ~/AmigaFS/system
amigafs status
amigafs unmount ~/AmigaFS/system
```

The mount command stays in the foreground so that failures remain visible.
Mounts use `nodev`, `nosuid` and `noexec`. The mountpoint must already exist
and be empty.

Create images:

```shell
amigafs create-floppy ~/Images --name blank --title Empty --filesystem FFS
amigafs create-floppy ~/Images --name boot --density hd --bootable
amigafs create-hard-disc ~/Images --name system --capacity 200MB \
    --filesystem PFS3 --partitions 2
```

A real Amiga needs the PFS3 or SFS
handler in its ROM or on the disc to boot from such a partition; AmigaFS does
not install one.

Transfer a file without losing its Amiga metadata:

```shell
amigafs export-file system.hdf DH0:S/Startup-Sequence ./Startup-Sequence
amigafs import-file system.hdf ./Startup-Sequence --directory DH1:Backup
```

Export refuses to overwrite either `Startup-Sequence` or
`Startup-Sequence.inf`. Import uses a single matching `.inf`, validates its
recorded length and commits the data and metadata as one change. See
[docs/metadata.md](docs/metadata.md).

## Physical floppies

Install the Greaseweazle host tools from their
[official instructions](https://github.com/keirf/greaseweazle/wiki/Software-Installation)
and make sure `gw info` finds the device in the graphical session.

```shell
amigafs read-floppy A game.adf
amigafs write-floppy --yes game.adf A
amigafs mount floppy:A ~/AmigaFS/floppy
amigafs mount --read-write floppy:A ~/AmigaFS/floppy
```

Mounting a floppy reads the whole disk into a private working copy, which takes
about a minute. A read-write mount writes only the cylinders that changed back
to the floppy when it is unmounted, and verifies them. Leave the floppy in the
drive until the unmount is reported complete. If the write-back fails, the
changes are kept and can be saved with
`amigafs recover floppy:A --salvage rescued.adf`.

A sector image is always written with an explicit Amiga disk format. A
track-level image is passed to Greaseweazle unchanged, so a copy-protected
image that cannot be mounted can still be written to a floppy.

## Physical hard discs and memory cards

```shell
amigafs list-discs
amigafs mount /dev/disk/by-id/usb-Example_CF_Card ~/AmigaFS/card
amigafs mount --read-write /dev/sdb ~/AmigaFS/card
amigafs read-disc /dev/sdb card-backup.hdf
amigafs write-disc system.hdf /dev/sdb --confirm sdb
```

AmigaFS only opens whole discs that are removable or attached through USB or an
SD/MMC host, have nothing mounted by Linux, and carry a Rigid Disk Block or an
Amiga volume. When the account cannot open the disc, the Debian package's
helper asks for administrator authentication through polkit and passes back a
single open descriptor. The filesystem daemon never runs with privileges.

A read-write mount journals every block before changing it, so a session
interrupted by a crash or an unplugged cable can be restored with
`amigafs recover DEVICE --restore`. Take an image with `read-disc` before the
first read-write mount of a disc you care about.

`write-disc` replaces everything on the destination and cannot be undone. It
requires the kernel device name as confirmation and reads the disc back
afterwards to verify it.

## Validate, repair and recover

Validation never changes the image:

```shell
amigafs validate system.hdf
amigafs repair-plan system.hdf
amigafs repair system.hdf --confirm system.hdf
amigafs recover system.hdf
amigafs recover system.hdf --restore
```

Findings are `FATAL`, `WARNING` or `ADVICE`. Fatal findings prevent a
read-write mount before anything is written. JSON reports include stable
finding codes and a `safe_for_write` flag.

Two repairs can be applied automatically, both to OFS and FFS volumes and both
without changing any file or directory: rebuilding the block-allocation bitmap
from the directory tree, as the Amiga's own disk validator does, and rebuilding
directory caches. Everything else is reported for a human decision. See
[docs/damaged-images.md](docs/damaged-images.md).

Validation, properties and repair share a budget of five minutes, 100,000
visited items and 256 directory levels.

## Development

```shell
sudo apt install python3-venv python3-dev build-essential fuse3 libfuse3-dev pkg-config
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev,fuse]'
make check
amigafs install-nautilus --restart
```

`make check` verifies the vendored engine against its manifest, then runs Ruff,
mypy and pytest. The ordinary test run needs no hardware, no FUSE mount and no
Greaseweazle: the floppy drive, the physical disc and the device helper are
exercised against stand-ins.

| Command | What it adds |
| --- | --- |
| `make test-live` | mounts through the real kernel FUSE driver, including a killed daemon and the systemd service path |
| `make test-live-greaseweazle` | real `gw convert` round trips for HFE v1, HFEv3 and SCP; no device needed |
| `make benchmark` | the amd64 performance workload and its budgets |
| `make fuzz-smoke` | short coverage-guided runs; needs the `fuzz` extra |
| `make package-smoke` | install, upgrade and uninstall of a built wheel |
| `make deb` / `make addon` / `make release` | the release artefacts |

Refresh the vendored engine with `tools/vendor_amiganut.py`; see
[src/amigafs/_vendor/VENDORED.md](src/amigafs/_vendor/VENDORED.md) and
[docs/engine-compatibility.md](docs/engine-compatibility.md).

## Security

Images, discs and desktop references are treated as untrusted input. See the
[security policy](SECURITY.md) and [threat model](docs/threat-model.md).
AmigaFS-owned state is created privately without following symbolic links, and
exported diagnostics contain only bounded basenames, allowlisted mount flags and
hashed identities.

## Licence

Nautilus AmigaFS is distributed under the [MIT Licence](LICENSE). The vendored
engine is MIT-licensed and its DiskMasher decompressors are ports of the
public-domain xDMS.
