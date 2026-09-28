# Versioning and release policy

Nautilus AmigaFS uses Semantic Versioning. The version in `pyproject.toml` is
the single source of truth and release tags use the matching
`vMAJOR.MINOR.PATCH` form. Below 1.0 a minor version may change command or
integration interfaces; patch versions remain compatible.

Every user-visible change updates the `Unreleased` section of `CHANGELOG.md`.
A release moves those entries beneath a dated heading and updates
`pyproject.toml` in the same reviewed commit. Versions are never inferred from
a branch or rewritten during a build.

## Release procedure

1. Confirm the release items in `TODO.md` and
   [release-readiness.md](release-readiness.md). Hardware, desktop and
   emulator checks cannot be replaced by unit tests.
2. Run `make check`, `make benchmark`, `make test-live` and
   `make test-live-greaseweazle`.
3. Install `.[release]` and run `make release`, `make package-smoke` and
   `make deb`. Install the `.deb` into a clean Ubuntu 24.04 environment,
   exercise the command, the Files loader and the device helper, and remove
   it.
4. Set the version, date the changelog section and review the whole diff.
5. Merge the release commit, create the annotated tag and publish release
   notes derived from the changelog.
6. Verify `build/release/SHA256SUMS`, inspect the SBOM and sign the source
   archive and checksum manifest under the approved key policy.
7. Keep the previous release and its recovery documentation available.

The release job builds the wheel, source archive, add-on and Debian package
with one commit-derived timestamp, builds twice and refuses differing output,
and writes a CycloneDX SBOM and a checksum manifest. The Debian build refuses a
vendored engine that does not match its manifest.

## Compatibility and support

The on-disc safety boundary has priority over interface compatibility. A
release refuses an uncertain write rather than weakening validation to keep an
old command outcome.

Two formats are part of the compatibility surface because they outlive the
process that wrote them:

| Format | Where | Policy |
| --- | --- | --- |
| Recovery manifest and undo journal | the state directory | versioned. A release that changes either must still restore sessions written by the previous release, or say in the changelog that they must be resolved before upgrading. |
| Validation and repair-plan JSON | command output | `schema_version` changes when a field is removed or changes meaning. Finding codes are stable within a schema version. |

Vendored-engine upgrades follow
[engine-compatibility.md](engine-compatibility.md).
