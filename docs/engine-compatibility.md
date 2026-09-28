# Engine compatibility policy

AmigaFS reads and writes Amiga filesystems with **amiganut**, the engine of
Amiga File Forge. It is vendored under `src/amigafs/_vendor` at one recorded
commit. `src/amigafs/_vendor/VENDORED.json` lists that commit and the SHA-256
of every vendored file, and `make vendor-check` fails when any file differs.
The Debian build runs the same check and refuses to package a modified engine.

Vendoring was chosen over a package dependency because the engine is not
published separately from Amiga File Forge, and because a filesystem that
writes to irreplaceable media should not change underneath its tests.

## What AmigaFS uses

**Public interface.** The mount objects' `iter_entries`, `stat`, `read_bytes`,
`write_bytes`, `mkdir`, `remove`, `rename`, `amiga_meta`, `set_amiga_meta`,
`set_access`, `set_title`, `size_bytes`, `free_bytes`, `validate` and `flush`;
the RDB reader; the three formatters; and the protection-bit helpers.

**Internals**, confined to two modules:

| Module | Internals | Purpose |
| --- | --- | --- |
| `amigafs.core.ranged` | `_data_blocks`, `extents`, `resolve`, `read_run`, `blocks` | read a range of a large file |
| `amigafs.core.bitmap` | `_chain_blocks`, `_read_header`, `_header_name`, `_comment_block_of`, `_load_cache`, `_store_bitmap`, `_bitmap` and its bookkeeping | rebuild a block-allocation bitmap |

**Error messages.** The engine raises one error type for everything. AmigaFS
checks its own index first, so most conditions never reach the engine. For the
rest it classifies the message: a full volume, an existing name, a directory
that is not empty, a protected entry. It also classifies validator findings as
bitmap, directory-cache or structural.

All of this is pinned by `tests/test_engine_contract.py`.

## Local patches

Two patches are applied after extraction. Both are described in
[`VENDORED.md`](../src/amigafs/_vendor/VENDORED.md) and should be offered
upstream.

1. **Reader reopening.** Drivers that need a second view of their medium ask
   the reader for one instead of reopening the file by name. Without this the
   PFS3 and SFS drivers would bypass AmigaFS's lock, transaction and journal,
   and could not work on a disc received as a descriptor.
2. **Linear-time allocation.** The OFS/FFS allocator finds the same block with
   two searches of the bitmap instead of stepping outwards one block at a
   time. Writing a 3 MiB file to a 40 MiB partition dropped from about seven
   seconds to well under one.

## Known engine behaviour

These are properties of the engine that AmigaFS works with and does not change:

- `amiganut.filesystem.drive` creates and copies drives by file name. AmigaFS
  does not use it, because every medium here is reached through one locked
  descriptor.
- The OFS/FFS allocator takes the nearest free block on either side of a
  file's header, so the blocks of a newly written file alternate around it.
  The result is valid but fragmented, and will load more slowly on real
  hardware than a file written by AmigaDOS.
- Deleting is refused for a write-protected file as well as a delete-protected
  one. AmigaDOS consults only the delete bit, so AmigaFS lifts the write bit
  inside the transaction that deletes the entry.
- Replacing a file deletes and recreates it. AmigaFS supplies the metadata
  explicitly so that the protection bits and comment survive, the datestamp is
  renewed and the archive bit is cleared, as AmigaDOS does on a write.
- A leading or trailing space in a name is trimmed by the engine. AmigaFS
  refuses such a name instead, because the entry would not be found under the
  name that was asked for.
- Datestamps are labelled UTC by the engine. AmigaFS treats them as local
  wall-clock time; see [metadata.md](metadata.md).

## Upgrade gate

```shell
python tools/vendor_amiganut.py --update /path/to/AmigaFileForge --revision TAG
make check
make test-live
make test-live-greaseweazle
make benchmark
```

1. The revision is read with `git archive`, so uncommitted work in that
   checkout is never vendored.
2. A patch that no longer applies means the engine changed where AmigaFS
   depends on it. Read the change before adapting the patch. Delete a patch the
   engine has absorbed.
3. `tests/test_engine_contract.py` must pass unmodified, or each change to it
   must be explained in the pull request.
4. `tests/test_hostile_images.py` must pass. A new crash on damaged input is a
   release blocker even when the image is otherwise unsupported.
5. Review the vendored diff for new file, network or subprocess access. The
   engine runs in-process with the user's privileges.
6. Record the new commit in `CHANGELOG.md`.
