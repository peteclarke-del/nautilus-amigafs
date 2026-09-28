# Privileged device helper

Linux gives whole-disc device nodes to `root` and the `disk` group. AmigaFS does
not run its filesystem daemon with either. When the account cannot open a
disc, AmigaFS starts one small helper through polkit:

```text
pkexec /usr/libexec/amigafs/amigafs-device-helper --device /dev/sdb --mode rw
```

The helper is `src/amigafs/core/device_policy.py`, installed unchanged. It
imports only the Python standard library and runs with `python3 -I`, so neither
the caller's environment nor its home directory can change what it executes.

It does exactly one thing: it applies the device policy as root, opens the one
approved disc and passes the open descriptor back over the socket it was
started with. It then exits. The daemon receives a single descriptor for a
single disc and never gains a privilege.

## What the helper refuses

- anything that is not a whole disc named `sdX` or `mmcblkN`;
- a disc that is neither removable nor attached through USB or an SD/MMC host,
  which excludes the computer's own discs;
- a drive with no medium;
- a disc with any partition mounted, in use as swap, or held by the device
  mapper or software RAID;
- a write-protected disc, when write access is requested;
- a disc with no Rigid Disk Block in its first sixteen blocks and no Amiga
  volume signature in its first block. `--allow-blank` lifts this one check and
  is used only by `amigafs write-disc`, which demands typed confirmation.

The device is opened with `O_EXCL`, which the kernel refuses while it holds the
disc for a mount, and the mount table is read again after the open.

## Authorisation

`org.amigafs.device-helper.policy` requires administrator authentication and
lets an active local session keep it for a few minutes (`auth_admin_keep`).
An administrator who wants a different rule can add a file under
`/etc/polkit-1/rules.d/` for the action `org.amigafs.open-physical-disc`.

## Without the helper

The per-user add-on cannot install a root-owned helper. It opens a disc only
when the account already has access, for example through the udev rule in
`packaging/udev/`, or through membership of the `disk` group, which is not
recommended because that group can read and write every disc in the machine.
AmigaFS only uses a helper that is owned by `root` and not writable by anyone
else, and only from `/usr/libexec/amigafs` or `/usr/lib/amigafs`.
