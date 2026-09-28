# Physical discs and floppies

AmigaFS works with two kinds of physical media. A **hard disc, CompactFlash or
SD card** taken from an Amiga is attached to the computer, usually through a
USB adapter, and opened in place as a block device. A **floppy** is read and
written through a [Greaseweazle](https://github.com/keirf/greaseweazle),
because a PC floppy controller cannot decode Amiga tracks.

> Neither path has been exercised on real hardware by the project yet. The
> logic is covered by tests against stand-ins. Use expendable media first, and
> take an image of any disc you care about before mounting it read-write.

## Hard discs and memory cards

### Which discs are offered

```shell
amigafs list-discs
amigafs list-discs --all
```

`--all` also lists discs that are refused, with the reason. A disc is eligible
only when all of the following hold.

| Requirement | Why |
| --- | --- |
| It is a whole disc named `sdX` or `mmcblkN` | a partition, loop device or mapped volume is not an Amiga disc |
| It is removable, or attached through USB or an SD/MMC host | the computer's own discs are never opened |
| A medium is present | an empty card reader has nothing to open |
| Nothing on it is mounted by Linux | two filesystems must not write one disc |
| It is not swap and is not held by the device mapper or software RAID | it is in use |
| It carries a Rigid Disk Block in its first sixteen blocks, or an Amiga volume in block 0 | raw access is not given to an ordinary PC disc |

The computer's own discs are not listed at all, even with `--all`.

If your desktop mounted a partition of the disc automatically, unmount it in
Files first. A PiStorm or Emu68 card often has a FAT boot partition that Linux
mounts. AmigaFS does not read an Amiga partition inside a PC partition table;
see [TODO.md](../TODO.md).

Use the stable name under `/dev/disk/by-id` where you can. A kernel name such
as `/dev/sdb` can belong to a different disc after the next replug, and
AmigaFS records recovery state under the stable name.

### Permission

A whole-disc device belongs to `root` and the `disk` group. AmigaFS tries, in
order:

1. **Existing access.** If the account can already open the device, it is
   opened directly, after the same policy checks.
2. **The polkit helper.** The Debian package installs
   `/usr/libexec/amigafs/amigafs-device-helper`. AmigaFS starts it through
   `pkexec`; you are asked for an administrator password; the helper applies
   the policy as root, opens the one disc and passes the open descriptor back.
   An active local session keeps the authorisation for a few minutes.

The filesystem daemon never runs with privileges. It holds one descriptor for
one disc.

The per-user add-on cannot install the helper. With it, only the first route is
available. `packaging/udev/70-amigafs-removable-discs.rules` is an optional
rule that gives the logged-in user direct access to removable discs. It is not
installed by default because it applies to every removable disc, not only
Amiga ones.

### Mounting

In Files, right-click a folder background and choose **Amiga FS Support → Open
physical Amiga disc read-only…** or **read-write…**, then pick the disc. In a
terminal:

```shell
mkdir -p ~/AmigaFS/card
amigafs mount /dev/disk/by-id/usb-Example_CF_Card ~/AmigaFS/card
amigafs mount --read-write /dev/disk/by-id/usb-Example_CF_Card ~/AmigaFS/card
amigafs unmount ~/AmigaFS/card
```

Each partition is a folder named after its device. A read-write mount is
refused if validation finds damage in any partition that would be writable.

Do not unplug the disc while it is mounted. Unmount, and wait for the
notification that it was written and validated.

### If a session is interrupted

Every block is journalled before it is changed. After a crash, power loss or
unplugged cable the next read-write mount is refused until the session is
resolved:

```shell
amigafs recover /dev/disk/by-id/usb-Example_CF_Card
amigafs recover /dev/disk/by-id/usb-Example_CF_Card --restore
amigafs recover /dev/disk/by-id/usb-Example_CF_Card --discard
```

`--restore` returns every changed block to its pre-mount content. `--discard`
keeps the disc as it is; validate it afterwards. In Files the same choice is
under **Resolve interrupted session on …** in a folder's menu.

Restoring checks the capacity of the disc and the content of its start, middle
and end against what was recorded. If a different disc is attached, nothing is
written.

The journal is kept under `~/.local/state/amigafs/recovery`. It is the only
way back, so do not delete it by hand.

### Imaging

```shell
amigafs read-disc /dev/sdb backup.hdf
amigafs write-disc system.hdf /dev/sdb --confirm sdb
```

`read-disc` never changes the disc and never overwrites a file. Runs of empty
sectors are stored as holes, so the image of a mostly empty disc is small on
disk.

`write-disc` replaces everything on the destination. It accepts a plain
hard-disc image, requires the kernel device name as confirmation, refuses a
disc that is mounted or has an unresolved session, and reads the whole disc
back afterwards to verify it. This is the one operation that accepts a disc
with no Amiga structures on it, so that a blank card can be prepared. There is
no undo.

## Floppies

### Setting up

Install the Greaseweazle host tools from their
[official instructions](https://github.com/keirf/greaseweazle/wiki/Software-Installation),
install their udev rules, and confirm in the graphical session:

```shell
gw info
```

Files shows the floppy actions only when the `gw` command is installed and a
Greaseweazle serial device is accessible. It checks this without starting a
process, so the actions appear on the first right-click.

Drives are `A` and `B` on a PC cable, or `0` to `3` on a Shugart bus. Before
offering a choice, AmigaFS measures the spindle speed of each candidate and
lists only drives that report index pulses, so insert the disk first. Probing
starts the motor briefly and does not read or write data.

### Reading a floppy to an image

**Read physical floppy to image…**, or:

```shell
amigafs read-floppy A game.adf
amigafs read-floppy A big.adf --density hd
```

Double density is tried first and high density second. An image is kept only
if every sector was read. A disk with unreadable or non-AmigaDOS tracks cannot
be represented by a sector image and is refused; capture it as a track-level
image with `gw read disk.scp` instead.

### Writing an image to a floppy

**Write to physical floppy…** on an image, or:

```shell
amigafs write-floppy --yes game.adf A
```

| Image | How it is written | Verified |
| --- | --- | --- |
| `.adf` | with an explicit Amiga format | yes |
| `.adz`, `.dms` | decoded to sectors first, then as above | yes |
| `.hfe`, `.scp`, `.ipf` | passed to Greaseweazle unchanged | no; a track image cannot be compared by reading it back |

The source is copied to a private snapshot before writing starts, so a later
edit cannot change a write in progress. Success is reported only after
Greaseweazle confirms that all tracks verified. After a disconnect, a write
error or a verification error, treat the floppy as incomplete.

### Mounting a floppy

**Open physical floppy read-only…** or **read-write…**, or:

```shell
amigafs mount floppy:A ~/AmigaFS/floppy
amigafs mount --read-write floppy:A ~/AmigaFS/floppy
```

1. The whole disk is read into a private working copy. This takes about a
   minute, during which the progress dialog follows the tracks.
2. The working copy is validated and mounted. Everything you do in the mount
   happens to the working copy.
3. On a clean read-write unmount, the working copy is validated again, compared
   with what was read, and only the cylinders that changed are written back
   and verified.

Leave the disk in the drive for the whole session. AmigaFS cannot detect a
disk being swapped while it is mounted: the changed cylinders would be written
to whatever disk is in the drive at unmount.

A read-only mount never writes. A read-write session in which nothing changed
never writes either.

### If the write-back fails

The changes are not lost. The working copy is kept and the next read-write
mount of that drive is refused until it is resolved:

```shell
amigafs recover floppy:A
amigafs recover floppy:A --salvage rescued.adf
amigafs recover floppy:A --discard
```

`--salvage` saves the working copy as an ordinary image, which can be checked
and written to a good disk with `write-floppy`. In Files the same choices are
under **Resolve interrupted session on drive-A…**.

## Copy-protected disks

A disk with protection or a non-standard layout is deliberately not mounted:
editing its sectors and writing them back would destroy what makes it work.
Such a disk can still be captured as a track-level image with Greaseweazle,
and a track-level image can still be written to a floppy unchanged.
