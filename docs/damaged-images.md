# Damaged images and recovery

AmigaFS treats every image as untrusted and never repairs one merely because it
was inspected or mounted. Keep the original before any recovery work, and work
on a copy.

## Assessment workflow

1. Copy the original image to read-only archival storage. For a physical disc,
   take an image with `amigafs read-disc` and work on that.
2. Run `amigafs validate IMAGE` on a working copy.
3. Save `amigafs validate --json IMAGE` with the working copy.
4. Run `amigafs repair-plan IMAGE` to group the findings into possible
   operations.
5. If the whole plan is marked applicable, run
   `amigafs repair IMAGE --confirm IMAGE_FILENAME` on the working copy only.
6. Mount read-only and copy important files out before anything else.

A finding that blocks a read-write mount does not necessarily block a
read-only one. A volume whose directory tree cannot be traversed safely, whose
partition table is damaged, or whose root cannot be found is refused outright.
AmigaFS reports what it found; it does not guess at missing blocks.

## Findings

| Code | Severity | Meaning |
| --- | --- | --- |
| `bitmap.inconsistent` | fatal | the block-allocation bitmap is marked invalid, marks a used block free, or disagrees with the root block's count |
| `dircache.stale` | fatal | a directory cache disagrees with the file headers it summarises |
| `volume.structure` | fatal | a bad checksum, a damaged chain, a missing block or any other structural damage |
| `image.open_failed` | fatal | the source could not be opened or indexed at all |
| `volume.not_mounted` | warning or advice | a partition was left out: beyond the end of the medium, or a filesystem AmigaFS cannot read |

JSON reports carry a `schema_version`, a `compatibility_profile` and a
`safe_for_write` flag. Use those fields and the finding codes rather than
parsing messages; messages are translated and may change.

## What can be repaired automatically

Two repairs, both for OFS and FFS volumes, and both leaving every file and
directory untouched.

**Rebuild the block-allocation bitmap.** Every block the directory tree reaches
is marked in use and everything else is marked free. This is what the Amiga's
own disk validator does after an interrupted write. The rebuild walks the
whole tree first and is abandoned, before anything is written, if any block is
claimed twice, lies outside the volume, or sits on a damaged chain. A bitmap
built from a tree that cannot be trusted would mark live data free.

**Rebuild the directory caches.** On a `DOS\4` or `DOS\5` volume, each
directory's cache is rewritten from the headers it lists.

When both are needed the bitmap is rebuilt first, because rebuilding a cache
may allocate blocks.

Everything else is reported for a human decision and is never applied. The
plan is all or nothing: if a volume has a repairable finding *and* structural
damage, nothing is applied, because a repair based on a damaged tree is
unsound.

PFS3 and SFS volumes are validated but have no automatic repair.

## How a repair is applied

`amigafs repair` requires the exact image filename as confirmation. It then:

1. validates again and rebuilds the plan, refusing unless the whole plan is
   still applicable;
2. writes an audit record before opening the image for writing;
3. takes the exclusive lock and starts the undo journal;
4. applies each action as one transaction;
5. validates the whole image and requires it to be clean;
6. removes the journal and marks the audit complete.

A failure at any point leaves the journal in place for `amigafs recover`. The
audit is kept under `~/.local/state/amigafs/repair-audits`.

## Interrupted writable sessions

An interrupted writable mount is a different thing from damage that was
already there. `amigafs recover IMAGE` reports which kind of checkpoint is
pending.

**An image file or physical disc** has an undo journal. `--restore` returns
every changed block to its pre-mount content. `--discard` accepts the image as
it stands; validate it afterwards.

**A compressed image, an HFE image or a physical floppy** has a working copy.
The source was never touched. `--salvage FILE` saves the working copy as a new
image; `--restore` and `--discard` both remove it.

Never delete recovery state by hand. The recovery command takes the locks and
checks that the medium is the one the checkpoint was made for.

## Images that cannot be mounted

- A track-level image or physical floppy that does not decode to a complete
  standard AmigaDOS disk. Converting it to sectors as a repair would discard
  weak bits, deliberate anomalies and other track-level protection. Keep it as
  a track-level image.
- An extended ADF that holds raw tracks, for the same reason.
- A DiskMasher archive that fails its checksums.
- A volume with a DOS type AmigaFS has no driver for, including `SFS\2`.
