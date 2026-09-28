# Security policy

## Supported versions

No public release has been made yet. Security fixes are applied only to
`main`. The first supported release line will be documented here when it is
published.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository. Do not open a
public issue for a vulnerability that could corrupt media, write to a disc the
user did not choose, escape mount boundaries, gain privileges through the
device helper, disclose local data or execute commands. Include the affected
commit or version, the Ubuntu, Nautilus and polkit versions, the kind of
source, the mount mode, a minimal reproduction and privacy-safe diagnostics
where possible.

Do not attach proprietary disk images, recovery journals, working copies,
credentials or unrelated paths. `amigafs diagnostics --json` is designed to be
shared without image contents.

Maintainers acknowledge actionable reports, reproduce them against generated
fixtures where possible, assess the impact on the media and the host, and
prepare regression tests with the fix. An advisory is published after users
have a safe upgrade path.

## Security boundary

Amiga media are untrusted input. Read-only is the default, and a format that
cannot be written back without loss is never mounted read-write. Mounts use
`nodev`, `nosuid` and `noexec` and remain owned by the current user. Writable
operations use exclusive locks, transactions, an undo journal or a private
working copy, external-change detection and validation before the first write
and after the last.

The filesystem daemon never runs with privileges. The one privileged component
is the device helper, which opens a single policy-approved removable disc
after administrator authentication and passes back its descriptor. It opens
neither the computer's own discs nor a disc that Linux has mounted. See
[packaging/polkit/README.md](packaging/polkit/README.md).

The supported boundary does not include hostile access by another account to
the user's session, remote image URIs, or running AmigaFS or Nautilus as root.

The assets, attacker inputs, controls and residual risks are in
[docs/threat-model.md](docs/threat-model.md).
