# Nautilus integration

## Install the Files integration

The Debian package installs AmigaFS and the Files integration together:

```shell
sudo apt install ./nautilus-amigafs_VERSION_amd64.deb
nautilus --quit
```

It installs `/usr/bin/amigafs`, a Nautilus loader, a MIME package and a hidden
desktop handler below `/usr`. For a development checkout, activate the virtual
environment and run:

```shell
amigafs install-nautilus --restart
```

Run it again after moving or recreating the environment. Remove it with
`amigafs uninstall-nautilus --restart`. Both refuse to touch an integration
file that AmigaFS did not generate.

## How a file gets a menu

1. **Suffix.** Files offers the menu only for `.adf`, `.adz`, `.dms`, `.hdf`,
   `.hda`, `.hdz`, `.rdsk`, `.hfe`, `.scp`, `.ipf`, `.rom` and `.kick`. An
   ordinary file costs nothing: it is never opened.
2. **Header.** The first blocks are read to confirm that the content is
   Amiga. An Acorn `.adf`, a PC `.rom` or an empty file gets no menu.
3. **Capabilities.** The menu shows the actions that kind of source supports.

A file on a network share is offered the same menu, provided the desktop
makes the share reachable by name, as it does for Windows shares under
`/run/user/UID/gvfs`. The header is then read over the network.

`.img`, `.raw` and `.dsk` are not claimed, because most such files are not
Amiga media. They can still be opened by name with `amigafs mount`.

Building a menu never starts a process, decodes a container or touches a
physical drive.

## MIME types

| Type | Identified by |
| --- | --- |
| `application/x-amiga-disk-format` | `DOS\0`–`DOS\7` at offset 0; `*.adf` at low weight |
| `application/x-amiga-hard-disk` | `RDSK` in the first 16 blocks, or a PFS3 or SFS signature; `*.hdf` at low weight |
| `application/x-amiga-compressed-disk` | `*.adz`, `*.hdz` |
| `application/x-amiga-extended-adf` | `UAE-1ADF` or `UAE--ADF` |
| `application/x-amiga-kickstart` | the ROM header; `*.kick` |
| `application/x-diskmasher` | `DMS!`; `*.dms` |
| `application/x-hxc-hfe` | `HXCPICFE` or `HXCHFEV3`; `*.hfe` |
| `application/x-supercardpro` | `SCP`; `*.scp` |
| `application/x-ipf-disk` | `CAPS`; `*.ipf` |

`.adf` and `.hdf` are also Acorn suffixes. They are registered at a lower
weight than the content magic, so an image is typed by what it holds. Nautilus
AcornFS and Nautilus AmigaFS can be installed together; each offers its menu
only for its own media. `application/x-hxc-hfe` is defined identically by both.

Double-clicking a recognised image opens it read-only.

Files chooses the application for a double-click from the name of a file when
it cannot read the content cheaply, as on a network share, so an Amiga `.adf`
may be given to AcornFS and an Acorn one to AmigaFS. Whichever is started
looks at the content. If the image is not its own, it asks the other mounter
with `desktop-claims`, and passes the image on when the answer is yes. An
image that was passed on is never passed back. This needs Nautilus AmigaFS
0.2.0 and a Nautilus AcornFS that knows `desktop-claims`; an older sibling is
not asked twice and nothing is handed to it.

Applications may open a
local URI such as `amigafs:///path/to/workbench.adf`. Remote hosts, other
schemes, queries and fragments are refused.

## Menu on an image

All actions are under one **Amiga FS Support** submenu.

| Action | Shown when |
| --- | --- |
| **Open read-only** | always |
| **Open read-write** | the format can be written back |
| **Validate image** | always |
| **Repair image…** | the image is a plain floppy or hard-disc image |
| **Write to physical floppy…** | a Greaseweazle is attached and the image is a floppy |
| **Write to physical disc…** | the image is a hard-disc image and a removable disc is attached |
| **Open in Amiga File Forge…** | its launcher is installed |
| **Unmount** | the image is mounted; replaces the open and repair actions |
| **Resolve interrupted read-write mount…** | a session on this image did not finish; replaces **Open read-write** and **Repair image…** |
| **Mount location…** | always |

A track-level image that cannot be mounted, because it is copy-protected, still
offers **Write to physical floppy…**.

## Menu on a folder

Right-click a folder or the background of one.

| Action | Shown when |
| --- | --- |
| **Create floppy image…**, **Create hard-disc image…** | the folder is writable |
| **Open physical floppy read-only…** / **read-write…** | a Greaseweazle is attached |
| **Read physical floppy to image…** | a Greaseweazle is attached and the folder is writable |
| **Open physical Amiga disc read-only…** / **read-write…** | a removable or USB disc is eligible |
| **Read physical disc to image…** | as above, and the folder is writable |
| **Resolve interrupted session on …** | a physical disc or floppy session did not finish |
| **Unmount** | the folder is a mount; replaces everything else |
| **Mount location…** | always |

## Dialogs

Actions run as separate processes, so Files stays responsive. Each shows a
finite result.

- **Mounting** shows progress and then the mounted folder. Reading a floppy
  follows the tracks.
- **Unmounting** read-write shows progress while changes are flushed,
  validated and written back, and confirms only when that has finished.
- **Destructive actions** ask first. Writing a floppy needs a confirmation.
  Repairing an image, and writing a physical disc, need the exact filename or
  device name typed in.
- **Validation** shows the whole report. When the damage can be repaired, its
  primary button is **Repair…**.
- **Long read-only work**, such as validation or reading a disc, has **Cancel
  safely**, which stops at a point that leaves nothing half done. A repair or
  a physical write cannot be cancelled once it has started.

Without Zenity, an action that needs a choice explains the equivalent terminal
command instead.

## Properties

**Properties** of an image has an **Amiga disk image** page: the kind of
source, its container, layout, geometry and drive identity, then for each
volume its filesystem, name, size, free space and contents, and the validation
result.

Two cases are summarised from the header alone, without opening the image: an
image that is currently mounted, because its daemon holds the lock, and a
container larger than 8 MiB, because decoding it would stall Files.

**Properties** of a file or folder inside a mount has an **Amiga metadata**
page: the source filesystem, Amiga path, volume name, protection bits and
comment.

## Mount location

Desktop mounts default to `~/AmigaFS Mounts`, which gives the most reliable
sidebar entry. `runtime` uses a private directory that disappears with the
session. An absolute path is also accepted. `AMIGAFS_MOUNT_ROOT` overrides the
saved value for one environment. A change applies to future mounts; an image
that is already mounted is found at its existing location.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No **Amiga FS Support** menu | run `nautilus --quit` and reopen Files; check that `python3-nautilus` is installed |
| The menu appears on the wrong files, or not on an image | run `amigafs inspect FILE`; the header decides, not the name |
| No floppy actions | run `gw info` in the same session; check the Greaseweazle udev rules |
| No disc actions | run `amigafs list-discs --all`; the reason for each refusal is listed |
| A password prompt does not appear | run `amigafs diagnostics`; the device helper and polkit must both be reported present |
| An old version keeps running | a loader under `~/.local/share/nautilus-python/extensions` takes precedence over the system one; uninstall the add-on |
