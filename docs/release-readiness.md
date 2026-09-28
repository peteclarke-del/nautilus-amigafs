# Release readiness and support boundaries

This checklist supplements `TODO.md`. It does not turn an untested environment
into a completed item.

## Supported first-release boundary

- Ubuntu 24.04 LTS on amd64 only.
- GNOME/Nautilus 46 or later through the Nautilus 4 extension API.
- FUSE 3 with unprivileged mounts owned by the current user.
- Read-write for OFS and FFS in all eight variants, PFS3 and SFS, on floppy
  images, hard-disc images with or without a partition table, `.adz`, `.hdz`
  and `.hfe` images, physical discs and physical floppies.
- Read-only for DiskMasher archives, extended ADFs with standard tracks, SCP
  and IPF images and Kickstart ROMs.
- Local regular files and removable or USB block devices. Network filesystems
  are outside the verified matrix.
- No claim yet for any real hardware, for ARM, or for SFS2.

## Candidate gate

- [x] A project licence and Debian copyright metadata exist.
- [x] `make check` passes: vendored-engine verification, Ruff, mypy and the
  unit suite.
- [x] `make test-live` passes on a host with kernel FUSE, covering read-only
  and read-write lifecycles for every filesystem, a killed daemon, a dirty
  handle at shutdown and the systemd service path.
- [x] `make test-live-greaseweazle` passes: real `gw convert` round trips for
  HFE v1, HFEv3, high-density HFE and SCP.
- [x] `make benchmark` meets its budgets.
- [x] Deterministic hostile-image tests pass for every filesystem.
- [ ] CI has run on GitHub's runners. The workflow is written but has not been
  executed.
- [ ] `make deb`, `make addon`, `make release` and `make package-smoke` have
  been run from a tagged commit. They need a Git history and have only been
  exercised through their unit tests.
- [ ] Coverage-guided fuzzing has been run. The harnesses are written; Atheris
  was not available on the development machine.
- [ ] Images written by AmigaFS have been opened in an emulator and on a real
  Amiga, for every filesystem.
- [ ] The polkit helper has been exercised with a real prompt and a real disc.
- [ ] A real Greaseweazle has read, written and written back floppies.
- [ ] A clean GNOME session passes the
  [desktop acceptance matrix](desktop-acceptance.md).
- [ ] Logout and host shutdown safely finalise dirty writable handles.
- [ ] The threat model, fuzzing and dependency scan contain no unresolved
  release blocker.
- [ ] Release archives, checksums, signatures and changelog are generated from
  an annotated tag. Signing awaits a key policy.

## Evidence to retain

Record the commit and host versions for manual checks, CI URLs for automated
jobs, the benchmark JSON, package contents, SBOM, checksums, signature
fingerprints and any accepted exception with its owner and review date. Do not
put private images or unrelated absolute paths in public evidence.
