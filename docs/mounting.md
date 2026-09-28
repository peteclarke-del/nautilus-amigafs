# Mounting

## Requirements

Linux on amd64 with Python 3.11 or later and FUSE 3. For ordinary desktop use,
install the Debian package as described in the
[README](../README.md#installation). For development on Ubuntu 24.04:

```shell
sudo apt install python3-venv python3-dev build-essential fuse3 libfuse3-dev pkg-config
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[fuse]'
```

## What to pass to `mount`

| Reference | Meaning |
| --- | --- |
| `workbench.adf`, `system.hdf`, `game.dms`, … | an image file of any supported kind |
| `/dev/disk/by-id/usb-…`, `/dev/sdb` | a whole physical disc |
| `floppy:A`, `floppy:B`, `floppy:0` … `floppy:3` | the floppy in a Greaseweazle drive |
| `amigafs:///path/to/image` | a local image, as a URI; accepted by the desktop handler |

```shell
amigafs inspect system.hdf
mkdir -p "$HOME/AmigaFS/system"
amigafs mount system.hdf "$HOME/AmigaFS/system"
```

The command stays in the foreground and prints progress for a source that has
to be decoded or read. The mountpoint must exist and be empty. From another
terminal:

```shell
find "$HOME/AmigaFS/system" -maxdepth 3
getfattr -d -m user.amiga "$HOME/AmigaFS/system/DH0/S/Startup-Sequence"
amigafs status
amigafs unmount "$HOME/AmigaFS/system"
```

Pressing Ctrl-C in the mount's terminal is a clean unmount: open files are
flushed, the volume is validated and a working copy is written back.

For a read-only mount that must disappear at once, `amigafs unmount --lazy`
detaches it and lets open handles finish in the background. A read-write mount
cannot be detached lazily.

## Read-write mounts

```shell
amigafs mount --read-write system.hdf "$HOME/AmigaFS/system"
```

Before anything is written AmigaFS:

1. refuses if an earlier session on this source is unresolved;
2. takes an exclusive lock, so no other AmigaFS process can open the source;
3. refuses an image file that has hard links;
4. indexes every mountable volume and runs its validator, refusing if a
   writable volume has any problem;
5. starts the undo journal, or keeps the decoded working copy.

While mounted, each operation is one transaction; see
[architecture.md](architecture.md#transactions). If anything else changes the
source, further writes are refused and the session must be recovered.

At unmount AmigaFS flushes every open file, validates every writable volume,
writes a working copy back to its container or floppy, and only then removes
the checkpoint. `amigafs unmount` waits for all of that and fails if the
session was not finalised.

| Source | How long a read-write unmount may take |
| --- | --- |
| image file or physical disc | seconds |
| `.adz`, `.hdz` | the time to recompress the image |
| `.hfe` | the time Greaseweazle takes to encode it, typically several seconds |
| physical floppy | the time to write and verify the changed cylinders |

## What the mount looks like

| Property | Value |
| --- | --- |
| Mount options | `nodev`, `nosuid`, `noexec`; `ro` unless read-write |
| Filesystem type | `fuse.amigafs` |
| Owner of every entry | the mounting user |
| Directory mode | `755` where writable, else `555` |
| File mode | `644`, or `444` when write-protected or on a read-only mount |
| Modification time | the entry's Amiga datestamp |
| Block size | 512 |
| Hard links, symbolic links, device nodes | not created |

`statfs` reports the capacity and free space of all mounted volumes together.

## Errors you may see

| Error | Cause |
| --- | --- |
| `EROFS` | the mount is read-only |
| `EACCES` | the file is write- or delete-protected, or the entry is a link or the partition list |
| `ENOSPC` | the volume is full; reported before a buffer grows |
| `EFBIG` | an Amiga file cannot exceed 4 GiB |
| `ENAMETOOLONG` | the name is longer than the volume allows |
| `EINVAL` | the name contains `:`, a control character, a character outside Latin-1, or a leading or trailing space |
| `EEXIST` | a sibling has that name, compared without regard to case |
| `ENOTEMPTY` | the directory still has entries |
| `EXDEV` | a rename between two partitions |
| `EBUSY` | the file is open, and was about to be deleted or replaced |
| `EIO` | the engine could not read or write the structure; validate the image |

## Live tests

`make test-live` mounts through the real kernel. It is opt-in because an
exposed `/dev/fuse` does not prove that a container or CI runner may mount. The
CI job for it verifies that it can open the device first, and fails rather than
accepting skipped tests.
