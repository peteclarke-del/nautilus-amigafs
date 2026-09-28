# Vendored components

AmigaFS carries a pinned snapshot of the filesystem engine from
[Amiga File Forge](https://github.com/peteclarke-del/AmigaFileForge) instead of
depending on a published package. `VENDORED.json` records the source commit and
the SHA-256 of every file here. `make vendor-check` fails when any of them
differs, so nothing in this directory can change without a reviewed refresh.

| Component | Source | Licence |
| --- | --- | --- |
| `amiganut/` | `amiganut/` in Amiga File Forge | MIT (`LICENSE.amiga-file-forge`) |
| `dms/dms.py`, `dms/dms_codec.py` | `app/dms.py`, `app/dms_codec.py` | MIT; the codec ports public-domain xDMS 1.3 |
| `dms/__init__.py`, `dms/errors.py`, `dms/checksum.py` | written for AmigaFS | MIT |

The three local `dms` files supply the two names the decoder imports from the
Amiga File Forge application package, which AmigaFS never imports.

## Local patches

Patches are applied in order by `tools/vendor_amiganut.py --update` and should
be offered upstream. A patch that upstream accepts is deleted here at the next
refresh.

### `0001-amiganut-reader-reopen.patch`

The PFS3 and SFS drivers, the RDB partition opener and the two formatters each
opened a second view of the medium with `BlockReader(reader.path, …)`. That
reopens the image by name, which bypasses the single locked descriptor AmigaFS
holds, cannot work for a physical disc received as a descriptor from the polkit
helper, and would write around the transaction and undo journal.

The patch adds `BlockReader.reopen()` and `BlockReader.read_all()` and routes
those call sites through them. Behaviour for a plain path-backed reader is
unchanged.

### `0002-amiganut-linear-allocation.patch`

`AmigaDOSVolume._allocate` searched outwards from the preferred block one block
at a time, in Python, for every block allocated. Writing a 3 MiB file to a
40 MiB FFS partition took about seven seconds. The patch finds the same block
with two C-level searches of the bitmap. The chosen block is identical in every
case, including the tie-break towards the later block; the equivalence is
covered by `tests/test_engine_contract.py`.

## Refreshing

```shell
python tools/vendor_amiganut.py --update /path/to/AmigaFileForge --revision v1.6.0
make check
```

The revision is read with `git archive`, so uncommitted work in that checkout
is never vendored. Review the diff, run the whole suite including
`make test-live`, and update `docs/engine-compatibility.md` if the engine's
behaviour changed.
