# AmigaFS threat model

This model covers the amd64 Ubuntu/Nautilus deployment. Review it whenever a
new source format, writable operation, IPC mechanism or privilege boundary is
added.

## Assets and trust boundaries

AmigaFS protects the host user's files, the host's own discs, the integrity and
confidentiality of Amiga media, recovery state, mount records and preferences.

**Untrusted:** the content of every image, disc and floppy, including
partition tables, filenames and comments; desktop URIs; FUSE requests; the
output of external tools; and paths below user-selected directories.

**Trusted:** the logged-in user, the installed AmigaFS code and its vendored
engine, the kernel FUSE driver, polkit, and the user's systemd and Nautilus
session.

**Outside the boundary:** running AmigaFS or Nautilus as root, hostile access
by another account to the user's session, remote URIs, and a compromised
kernel or desktop session.

## Principal threats and controls

| Threat | Impact | Controls |
| --- | --- | --- |
| Malformed partition tables, bitmaps, directories or block chains | crash, excessive work, out-of-volume access or corruption | content-driven detection; partitions clipped to the medium; an index bounded to 100,000 entries and 256 levels with cycle detection; five-minute inspection budgets; validation before the first write; every engine exception on untrusted data treated as a finding; deterministic hostile-image tests and fuzz targets |
| Decompression and conversion bombs | disk or memory exhaustion | an 8 GiB expansion limit for gzip, 64 MiB input limits for archives and 256 MiB for track images, bounded streaming copies, and timeouts on external tools |
| Symlink, hard-link, rename or replacement races | validate one file and write another, or redirect state writes | one descriptor per medium, opened once and locked; inode and device revalidation after the lock; refusal of writable images with hard links; signature checks before every write; state created descriptor-relative without following links; journals created exclusively |
| Concurrent or interrupted writers | lost updates or a partly written volume | shared reader and exclusive writer locks; one transaction per operation; before-images made durable before the first write; rollback by discarding staged writes; validation at unmount |
| Restoring recovery state onto the wrong medium | overwriting an unrelated disc or image | capacity check and a hash of the pre-session start, middle and end of the medium before anything is restored; stable device names recorded unresolved |
| Privilege escalation through the device helper | raw access to the host's discs | helper owned by root, not writable by others, run only from two system paths; standard library only, isolated mode; whole removable or USB discs only; mounted, swap and mapped discs refused; Amiga structures required; `O_EXCL` open and a second mount check afterwards; administrator authentication |
| Descriptor substitution | the daemon writes to a disc other than the one approved | the receiver checks that the descriptor is a block device, has the requested device number and has exactly the requested access; the handover socket is mode `600` in a private directory and checks the peer's user |
| Malicious FUSE caller input | namespace escape, invalid metadata or memory growth | inode-based operations; strict name, comment and protection validation; capacity checked before a buffer grows; a 1 GiB bound on one transaction; `nodev,nosuid,noexec` |
| Desktop URI or command injection | remote-file access or command execution | local `file:` and `amigafs:` schemes only; authority, query, fragment and NUL refused; argv-only subprocesses; escaped desktop fields; an allowlisted environment for detached children; dialog selections checked against the offered list |
| Physical overwrite | accidental data loss, a source that changes during the write, or falsely reported success | responsive-device probe; drive allowlist; explicit confirmation, typed for a disc; a private stable snapshot; mandatory verification for sector images; read-back comparison for discs |
| Lossy conversion of protected media | destroying copy protection or weak bits | a track image or floppy is mounted only when every sector decodes to a standard AmigaDOS layout; extended ADFs with raw tracks are refused; track images are written to floppies unchanged |
| Disclosure through diagnostics or UI | leaking image data, unrelated paths or credentials | diagnostics export bounded basenames, allowlisted mount flags and hashed identities; desktop errors and log excerpts redact absolute paths and control characters and are length-bounded |

Read-only is the default. A format that cannot be written back without loss is
never mounted read-write.

## Residual risks

- **The engine is in-process** and shares the user's privileges. It was not
  written to resist hostile input in every path. AmigaFS bounds what it asks of
  the engine and contains what the engine raises, but a hostile image that
  makes the engine loop inside one call is limited only by the desktop
  timeout. Pinning, the [upgrade gate](engine-compatibility.md) and fuzzing
  are part of the release process.
- **A floppy swapped during a mount is not detected.** Greaseweazle offers no
  media-change signal that AmigaFS uses. The changed cylinders are written to
  whatever disk is in the drive.
- **A physical disc removed during a write** is recovered from the journal
  only if the same disc is attached again.
- **External tools are trusted.** `gw` and `pkexec` are found on `PATH`. A
  hostile `PATH` in the user's own session is outside the boundary.
- **Physical hardware has not been exercised.** See
  [release-readiness.md](release-readiness.md).

## Security test expectations

CI runs the unit tests against corrupt and boundary images, the deterministic
hostile-image suite, and short coverage-guided sessions for the sidecar parser,
URI handling and volume validation. A crash or hang is a defect even when the
input is otherwise unsupported. A security fix adds a regression test that
contains no private image data.
