# User guide

## What AmigaFS does

AmigaFS shows Amiga media as ordinary Linux folders. It opens floppy images,
hard-disc images, compressed and track-level images, DiskMasher archives,
Kickstart ROMs, physical hard discs and memory cards, and physical floppies in
a Greaseweazle drive. The full table is in the [README](../README.md#what-it-opens).

Read-only is always the default. Read-write is offered only where the format
can be written back without losing anything, and only after the volume has
passed validation.

## Install

The Debian package is the recommended installation on Ubuntu 24.04 amd64:

```shell
sudo apt update
sudo apt install ./nautilus-amigafs_VERSION_amd64.deb
nautilus --quit
```

Open Files again, then check:

```shell
amigafs --help
amigafs diagnostics
```

If `apt` cannot find `python3-pyfuse3`, enable Ubuntu's Universe component with
`sudo add-apt-repository universe`, run `sudo apt update` and install again.

A per-user add-on is the alternative where a system package cannot be
installed. Its `INSTALL.txt` has the procedure. It cannot open a physical disc
that your account does not already have access to.

Track-level images and physical floppies also need the
[Greaseweazle host tools](https://github.com/keirf/greaseweazle/wiki/Software-Installation).

## Open an image

Right-click a supported image in Files and choose **Amiga FS Support → Open
read-only** or **Open read-write**. The mount opens in Files and appears in the
sidebar. Double-clicking an image opens it read-only.

- A floppy image or a hardfile opens as its one volume.
- A hard-disc image with a partition table opens as a folder holding one
  folder per partition: `DH0`, `DH1` and so on.
- A Kickstart ROM opens as a list of its resident modules.

Names are matched without regard to case, as on an Amiga. A name can hold 30
characters on most volumes and 107 on a long-filename volume. It cannot contain
`:` or `/`.

Compressed (`.adz`, `.hdz`) and HxC (`.hfe`) images are decoded into a private
working copy when opened. If you opened one read-write and changed something,
the image is rewritten when you unmount. Until then the file on disk is
untouched.

## Unmount

Use **Amiga FS Support → Unmount** on the image or on the mounted folder.

A read-write unmount is not finished when the folder disappears. AmigaFS
flushes open files, validates the volume, and writes a working copy back to its
image or floppy. Wait for the notification that says it was flushed and
validated. Do not move the image, remove the disc or eject the floppy before
then.

If Files reports that the mount is busy, close the windows and applications
that are using it and try again.

## Work with files

Copy, move, rename and delete in Files as usual. An editor that saves through a
temporary file and a rename works.

| Amiga property | Where to see it | How to change it |
| --- | --- | --- |
| Datestamp | the modification time | set the modification time |
| Write and delete protection | the file is read-only | **Properties → Permissions**, or `chmod` |
| All protection bits | **Properties → Amiga metadata** | `setfattr -n user.amiga.protection -v "-s--rwed" FILE` |
| Comment | **Properties → Amiga metadata** | `setfattr -n user.amiga.comment -v "text" FILE` |
| Volume name | **Properties** of the volume's folder | `setfattr -n user.amiga.volume -v "Name" FOLDER` |

Copying a file out of a mount with Files keeps its content and date but not
its protection bits or comment, because Linux has nowhere to put them. Use
`amigafs export-file` and `amigafs import-file` when those must survive; they
keep the metadata in a small `.inf` file beside the exported file. See
[metadata.md](metadata.md).

A file cannot be moved between partitions by renaming. Files copies and deletes
instead, which is what you would expect between two volumes.

## Create an image

Right-click a folder, or its background, and choose **Amiga FS Support →
Create floppy image…** or **Create hard-disc image…**. Blank fields take the
default shown. The image is published only after it has been validated.

A floppy can be double or high density, any OFS or FFS variant, and bootable.
A hard-disc image has a partition table and one or more equal partitions
formatted as an OFS or FFS variant, PFS3 or SFS.

## Physical floppies and discs

These start from a folder's menu rather than from a file, because there is no
file to right-click. They are described in
[physical-media.md](physical-media.md). In short:

- **Read physical floppy to image…** and **Write to physical floppy…** copy a
  whole disk in either direction.
- **Open physical floppy…** reads the disk into a working copy, and on a clean
  read-write unmount writes back only what changed.
- **Open physical Amiga disc…** opens an attached hard disc or card in place,
  asking for authorisation if your account cannot open it.

## Validate, repair and recover

**Validate image** never changes the image. It reports fatal damage, warnings
and advice. When the damage is of a kind AmigaFS can repair safely, the report
offers **Repair…**, which shows what would be done and asks you to type the
image's filename before doing it.

If a read-write session was interrupted, by a crash or a power cut, the image's
menu shows **Resolve interrupted read-write mount…** instead of **Open
read-write**. You can return the image to how it was before the session, or
keep it as it is. For a compressed image, an HFE image or a floppy, the source
was never changed and you can save the interrupted working copy as a new
image. See [damaged-images.md](damaged-images.md).

## Amiga File Forge

When Amiga File Forge is installed, **Open in Amiga File Forge…** hands the
image to it. Unmount a read-write mount first: the two must not edit one image
at the same time.

## Remove AmigaFS

Unmount every image, confirm that `amigafs status` lists nothing, then:

```shell
sudo apt remove nautilus-amigafs
nautilus --quit
```

For the add-on, run `python3 install.py --restart uninstall` from its extracted
directory. Neither removes images, preferences, recovery checkpoints or repair
audits.

## Get support safely

```shell
amigafs diagnostics --json > amigafs-diagnostics.json
```

The report contains no image contents and no full paths. Read it before
attaching it to a report. Follow [SECURITY.md](../SECURITY.md) for
vulnerabilities.
