# Debian package

AmigaFS produces one installable package for Ubuntu 24.04 LTS on amd64:

```text
nautilus-amigafs_VERSION_amd64.deb
```

It contains the command, the FUSE adapter, the Nautilus extension, MIME and
desktop integration, documentation, the vendored filesystem engine, the device
helper and its polkit action. One package keeps the command and the desktop
extension on the same version.

## Runtime dependency policy

Ubuntu supplies Python, FUSE, pyfuse3, Trio, Nautilus and the desktop utilities
through normal package dependencies. Nothing is downloaded at install time and
no wheel is bundled: the filesystem engine is part of the AmigaFS source tree,
pinned and verified as described in
[`src/amigafs/_vendor/VENDORED.md`](../../src/amigafs/_vendor/VENDORED.md).

Package installation never invokes `pip`, accesses the network, compiles code
or writes to a home directory. Maintainer scripts only refresh the shared MIME
and desktop databases.

```text
Depends:
  python3 (>= 3.11~), python3 (<< 3.13), fuse3, python3-pyfuse3 (>= 3.3),
  python3-trio (>= 0.24), python3-nautilus, gir1.2-nautilus-4.0,
  shared-mime-info, desktop-file-utils, libnotify-bin, zenity
Recommends:
  pkexec, polkitd
```

polkit is recommended and not required. Without it AmigaFS opens a physical
disc only when the account already has access to it.

Greaseweazle is an optional external integration and is not bundled.

## Installed files

| Path | Notes |
| --- | --- |
| `/usr/bin/amigafs` | the command |
| `/usr/lib/python3/dist-packages/amigafs*` | the package, including `amigafs/_vendor` |
| `/usr/share/nautilus-python/extensions/nautilus_amigafs.py` | the Files loader |
| `/usr/share/mime/packages/amigafs.xml` | MIME types |
| `/usr/share/applications/org.amigafs.NautilusAmigaFS.desktop` | hidden handler |
| `/usr/libexec/amigafs/amigafs-device-helper` | root-owned, mode `755`, **not** setuid |
| `/usr/share/polkit-1/actions/org.amigafs.device-helper.policy` | the polkit action |
| `/usr/share/doc/nautilus-amigafs/` | documentation and copyright |

The build refuses to package a helper that does not start with
`#!/usr/bin/python3 -I` or that imports the `amigafs` package. The helper runs
as root, so it must not be influenced by the caller's environment or by code
outside the standard library.

The package does not modify `fuse.conf`, install anything setuid, add users to
groups or install udev rules. The optional rule in `packaging/udev` is for
administrators who want it.

## Reproducible build

```shell
python3 -m venv .venv
. .venv/bin/activate
python -m pip install '.[release]'
make deb
```

`make deb` needs a Git checkout. It derives `SOURCE_DATE_EPOCH` from the
current commit unless it is set, verifies the vendored engine against its
manifest, builds the package twice, compares the digests, and writes the
package and `nautilus-amigafs-deb-manifest.json` below `build/debian`. An exact
`vMAJOR.MINOR.PATCH` tag produces that version; other commits receive a
`+gitYYYYMMDD.REVISION` suffix.

The manifest records every installed file, the dependencies, and the vendored
engine's version, source commit and patches.
