# Architecture decisions

## Runtime and filesystem binding

AmigaFS uses Python 3.11 or later and pyfuse3, a maintained binding for
libfuse 3 with async request handling. Image code is independent of pyfuse3, so
it is tested without mounting and without elevated privileges.

The host baseline is Ubuntu 24.04 LTS, FUSE 3 and Nautilus 46 or later through
the Nautilus 4 GObject-introspection API.

## Package boundaries

| Package | Responsibility |
| --- | --- |
| `amigafs.core` | source identification, storage, volumes, validation, repair and filesystem policy |
| `amigafs.fuse_adapter` | translation between pyfuse3 and core operations |
| `amigafs.cli` | commands that call the same services as FUSE and the desktop |
| `amigafs.desktop` | detached desktop actions and their Zenity dialogs |
| `amigafs_nautilus` | the Nautilus menu and Properties integration |
| `amigafs._vendor` | the pinned filesystem engine and DiskMasher decoder |

The Nautilus extension launches the command and does nothing else. It never
holds an image open for writing and contains no filesystem parsing.

## Two stages: resolve, then open

**Resolving** a source (`amigafs.core.formats`) reads at most the first sixteen
blocks of a file, or the kernel's description of a device. It answers what kind
of source this is and which actions are safe to offer. It never decodes,
unpacks, locks or reads a physical medium, because it runs while Files builds a
menu and again for every mount lookup.

**Opening** a source (`amigafs.core.media`) takes the lock, decodes a container
into a working copy if there is one, and finds the volumes from the decoded
sectors: a Rigid Disk Block and its partitions, or one volume starting at
block 0, or a Kickstart ROM's resident modules.

A name is never evidence. `.adf` is also an Acorn suffix and `.rom` is used by
every system, so a file is accepted only for what its content shows.

## Storage: one store, one descriptor

`ImageStore` (`amigafs.core.blockio`) owns the single descriptor through which a
medium is read and written, whether that is an image file, a decoded working
copy or a block device. It takes a shared lock for reading and an exclusive
lock for writing, confirms that the path still names the inode it opened,
refuses a writable image with hard links, and records a signature of the
medium so that a change made by anything else is detected before the next
write.

The engine reaches the medium only through `StoreReader`, an implementation of
the engine's block-reader interface over the store. Closing a reader never
closes the store, because the engine's drivers open and close several views of
one medium while the lock has to outlive all of them.

## Transactions

A mutation runs inside a transaction:

1. `begin` starts an in-memory overlay. Reads see staged writes; the medium is
   untouched.
2. The engine performs the operation and flushes its own caches into the
   overlay.
3. On a volume of up to 64 MiB the engine's validator runs against the staged
   state. An operation that would leave the volume inconsistent is abandoned.
4. `commit` reads the current content of every chunk that is about to change,
   appends those before-images to the undo journal and synchronises the
   journal. Only then are the new chunks written and synchronised.

Abandoning a transaction discards the overlay. That is a complete rollback,
because nothing had been written. The engine's driver object is then recreated,
since its in-memory caches may describe the abandoned state.

A failure during step 4, after the journal is durable, may leave part of the
operation on the medium. The session then refuses further writes and keeps its
checkpoint, which restores the pre-mount state.

One operation may stage at most 1 GiB. FUSE growth is capacity-checked before a
write buffer expands.

## Recovery

A writable session on an image file or physical disc keeps an **undo journal**:
the previous content of each 4 KiB chunk the first time the session changes it.
The journal grows with what changed, not with the size of the disc, which is
what makes a multi-gigabyte hard disc practical. Restoring writes every
before-image back. A final record that is short or fails its checksum is
ignored: it was synchronised before the write it protects began, so that write
never started.

Before restoring, the start, middle and end of the medium are hashed as they
were before the session, by overlaying the journal's before-images on the
current content, and compared with the hash recorded when the session began. A
different disc in the same slot, or a different file at the same path, is
refused.

A session on a **container** edits a working copy kept in the checkpoint
directory. The source is replaced only when the session closes cleanly, through
a sibling temporary file and an atomic rename that keeps the original mode,
ownership and extended attributes. An interrupted session therefore leaves the
source untouched, and the working copy can be salvaged as a new image.

| Source | Working copy | Written back by |
| --- | --- | --- |
| `.adz`, `.hdz` | gunzipped sectors | gzip |
| `.hfe` | sectors decoded by `gw convert` | `gw convert`, same HFE version |
| physical floppy | sectors read by `gw read` | `gw write`, changed cylinders only |

A checkpoint is removed after a clean close. It is also removed when a session
ends without having written anything, because it then protects nothing.

## Physical discs and privilege

The filesystem daemon is never privileged. A disc is opened in one of three
ways, in this order: from a descriptor handed over by the desktop launcher;
directly, when the account already has access; or through the polkit helper.

The helper is `amigafs/core/device_policy.py` installed unchanged as
`/usr/libexec/amigafs/amigafs-device-helper`. It imports only the standard
library and runs with `python3 -I`. It applies the device policy as root, opens
one disc with `O_EXCL`, and passes the descriptor back over the socket it was
given as standard output. The caller checks that the descriptor is a block
device, is the device it asked for, and has exactly the access it asked for.

A desktop mount runs as a transient systemd user service, which is outside the
graphical session and cannot show a polkit prompt. The launcher therefore opens
the disc itself, inside the session, and hands the descriptor to the daemon
over a private socket in the runtime directory, after checking that the peer
belongs to the same user.

See [physical-media.md](physical-media.md) and
[../packaging/polkit/README.md](../packaging/polkit/README.md).

## The index and the POSIX view

Opening a volume indexes its whole directory tree once, bounded by 100,000
entries and 256 levels, with cycle detection by block. Stable inode numbers
last for the life of the mount. Ordinary reads are served from the index and a
bounded whole-file cache; files larger than the cache are read a range at a
time from their block list, with up to 256 KiB of read-ahead per handle under a
4 MiB budget.

All writable handles for one inode share one userspace buffer, so a write
through any handle is immediately visible through every other, and a `flush`
or `fsync` commits the combined buffer as one transaction. Metadata changes are
coalesced per inode and committed with the data. When the FUSE loop stops, the
runner commits every remaining dirty inode before the image is validated and
closed.

Writable replies use zero entry and attribute timeouts, and successful changes
send best-effort invalidations to the kernel. A failed invalidation never turns
a committed change into a reported failure.

## Mount identity and shutdown

`/proc/self/mountinfo` is authoritative for active mounts. A private per-user
record adds the canonical source path, its device and inode, the kind of
source, the daemon's process ID and the access mode. A physical floppy is
identified by a private token file, one per drive, whose inode and lock stand
in for the drive.

The record is removed only after the image has been flushed, validated and
written back. Callers waiting for a writable unmount therefore wait for the
record to disappear, for as long as a write-back can take: two minutes for an
image or disc, ten for a container, thirty-five for a floppy.

## Engine boundary

The engine is vendored, not imported from Amiga File Forge's application
package. Its public mount interface is used wherever it provides what is
needed. The few internals that ranged reads and bitmap repair require are
confined to `amigafs.core.ranged` and `amigafs.core.bitmap`.

The engine does not defend every on-disc count and offset against hostile
values. AmigaFS therefore treats any exception the engine raises on untrusted
data as a finding or a refusal, never as a crash.

See [engine-compatibility.md](engine-compatibility.md).
