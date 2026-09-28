# Administrator guide

## Supported deployment

The support boundary is Ubuntu 24.04 LTS, GNOME/Nautilus 46 or later, FUSE 3
and amd64. The filesystem daemon runs entirely as the logged-in user. Do not
run Nautilus as root, grant `allow_other`, or change `/etc/fuse.conf` for an
ordinary deployment.

The Debian package installs the command and Files integration system-wide
through `apt`. It does not invoke `pip` or touch home directories.

## The one privileged component

The package installs a helper and a polkit action:

| File | Purpose |
| --- | --- |
| `/usr/libexec/amigafs/amigafs-device-helper` | opens one approved removable disc and passes back its descriptor |
| `/usr/share/polkit-1/actions/org.amigafs.device-helper.policy` | requires administrator authentication to run it |

Nothing in the package is setuid. The helper is root-owned, mode `755`, imports
only the Python standard library and runs in isolated mode. What it refuses is
listed in [../packaging/polkit/README.md](../packaging/polkit/README.md).

By default every use needs administrator authentication, which an active local
session keeps for a few minutes. To let a group open removable Amiga discs
without a password, add a rule:

```javascript
// /etc/polkit-1/rules.d/49-amigafs.rules
polkit.addRule(function(action, subject) {
    if (action.id == "org.amigafs.open-physical-disc" &&
        subject.local && subject.active && subject.isInGroup("amiga")) {
        return polkit.Result.YES;
    }
});
```

The helper's own policy still applies. It cannot be made to open the machine's
own discs, a mounted disc or a disc with no Amiga structures.

To remove physical-disc access altogether, delete the policy file or deny the
action in a rule. AmigaFS then opens only discs the account can already open.

Do not add users to the `disk` group for this. That group can read and write
every disc in the machine.

## Optional tools

Greaseweazle is not bundled. Install its host tools separately and make `gw`
visible in the graphical session's `PATH`. Converting an `.hfe`, `.scp` or
`.ipf` image needs the tools but no device. Physical floppies need the device
and its udev rules.

Amiga File Forge is detected through its `amiga-file-forge` launcher on `PATH`
or in `~/.local/bin`. `AMIGA_FILE_FORGE_COMMAND` selects another launcher. It is
split into arguments and never passed to a shell; `{image}` is replaced by the
image path, which is otherwise appended.

## Data and ownership

AmigaFS writes to the source the user opened and to these per-user locations:

| Data | Default location | Removal policy |
| --- | --- | --- |
| Preferences | `${XDG_CONFIG_HOME:-~/.config}/amigafs` | keep across upgrade and uninstall |
| Undo journals and working copies | `${XDG_STATE_HOME:-~/.local/state}/amigafs/recovery` | keep until resolved |
| Repair audits | `${XDG_STATE_HOME:-~/.local/state}/amigafs/repair-audits` | keep; completed audits age out after 90 days |
| Runtime records, logs, floppy tokens | `${XDG_RUNTIME_DIR}/amigafs` | session-scoped; inactive logs age out after 7 days |
| Default mounts | `~/AmigaFS Mounts` | unmount before removal |
| Desktop integration | `/usr/share` for the package; `${XDG_DATA_HOME:-~/.local/share}` for the add-on | removed by the matching uninstaller |

Persistent directories are private to the user and are created without
following symbolic links.

### Disk space

| Session | Space used under the state directory |
| --- | --- |
| Read-write image or physical disc | the journal: 4 KiB for each 4 KiB chunk changed, once per session |
| Read-write `.adz`, `.hdz` or `.hfe` | the whole decoded image |
| Read-write physical floppy | two copies of the floppy |
| Any read-only container | the whole decoded image, in the temporary directory, removed on unmount |

A compressed image is refused if it would expand beyond 8 GiB. Set
`AMIGAFS_MAX_WORKSPACE_BYTES` to change the limit. Make sure the state
directory's filesystem has room before opening a large `.hdz` read-write.

A checkpoint is not a backup. It exists to resolve one interrupted session.

Private JSON state is written to a synchronised temporary file and renamed into
place. If memory or disk space runs out first, the previous record is kept and
the temporary file is removed. If a journal cannot be made durable, the change
it would have protected is not written.

## Service lifecycle

Desktop mounts run as collected transient systemd user services where
available. They receive `SIGINT` at logout or shutdown so that open files are
flushed and the image is finalised.

```shell
amigafs status
systemctl --user list-units 'amigafs-mount-*.service'
journalctl --user --unit 'amigafs-mount-*.service'
```

Stopping the service is a clean unmount. A read-only service is given 30
seconds. A read-write service is given as long as its write-back may take: two
minutes for an image or disc, ten for a compressed or HFE image, thirty-five
for a floppy. The user manager itself is given less than that at logout, so
unmount a floppy or a large compressed image before logging out. If a service
is stopped before it has finished, the working copy is kept for salvage; a
floppy that was being written may be left with only some of its cylinders
updated.

Do not kill a writable daemon with `SIGKILL` during routine administration. If
a host failure does so, leave the image and its checkpoint alone and use the
recovery flow at the next login.

## Upgrade procedure

1. Unmount all images and confirm `amigafs status` lists nothing.
2. Resolve every pending recovery. Never discard one merely to make an upgrade
   proceed.
3. Back up the configuration and state directories.
4. Install the new package with `sudo apt install ./nautilus-amigafs_VERSION_amd64.deb`.
5. Run `nautilus --quit`, open Files and confirm `amigafs diagnostics`.
6. Validate a disposable known-good image before enabling writes.

## Uninstall procedure

Unmount all images, confirm `amigafs status` lists nothing, run
`sudo apt remove nautilus-amigafs` and restart Files. User state is retained.
If a user later asks for complete erasure, confirm that no recovery is pending
and remove only the AmigaFS directories listed above. Never use a recursive
deletion rooted at `$HOME`, an XDG root or the mount parent.

## Diagnostics and incident handling

Use `amigafs diagnostics --json` rather than copying journals wholesale. It
hashes identities and leaves out image contents and full paths. It reports
whether the FUSE device, the Greaseweazle tools, polkit and the device helper
are available.

For a suspected malicious image or unsafe behaviour, stop using the image,
keep it read-only, record the AmigaFS version and follow the private reporting
route in [SECURITY.md](../SECURITY.md).
