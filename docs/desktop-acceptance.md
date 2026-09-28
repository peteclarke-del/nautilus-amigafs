# GNOME desktop and hardware acceptance

Run this checklist in a clean Ubuntu 24.04 amd64 session with GNOME/Nautilus
46 or later. Automated tests cover the menu model, the dialog command
contract, the kernel FUSE operations, the device policy and the Greaseweazle
command contract. They cannot establish visual layout, assistive-technology
behaviour, Nautilus's own drag-and-drop and trash integration, a real polkit
prompt, or anything about real drives and discs.

**None of this checklist has been run yet.** Record the AmigaFS commit, the
Ubuntu, GNOME, Nautilus, Zenity, polkit, Orca and Greaseweazle versions, the
hardware used, the display scale, the theme and the outcome. Do not include
private image paths or image contents in public evidence.

## Preparation

1. Install the Debian package and restart Files.
2. Create fixtures with `amigafs create-floppy` and `amigafs create-hard-disc`
   for each filesystem. Do not use irreplaceable media.
3. Create HFE v1, HFEv3 and SCP fixtures from a generated ADF with
   `gw convert`, and keep the ADF for comparison.
4. Record `amigafs status --json` and `amigafs diagnostics --json` before and
   after the session.
5. Run `make test-live` and `make test-live-greaseweazle` on the same host
   first.

## Keyboard and assistive technology

- Navigate to an image using only the keyboard, open the context menu, enter
  **Amiga FS Support** and invoke every action offered.
- Do the same from a folder background for the creation and physical-media
  actions.
- Confirm that focus is visible, menu names and descriptions are announced,
  Tab and Shift+Tab reach every control, Enter activates the stated primary
  action, and Escape or **Cancel** changes nothing.
- With Orca, verify the title, prompt, field labels, progress state,
  destructive warning, error detail and completion status of every dialog.
- Confirm that the disc list announces the model and capacity of each disc,
  and that the typed-confirmation dialogs announce what must be typed.

## Visual matrix

Repeat image creation, validation with findings, repair confirmation, recovery,
the disc list and the floppy drive selector at:

| Theme | Window and display condition |
| --- | --- |
| Light | normal width and 100 percent scale |
| Dark | normal width and 100 percent scale |
| Light | narrow usable desktop area |
| Dark | 200 percent scale |

No control, warning or progress value may be clipped.

## Nautilus file workflows

On a writable mount of each filesystem, using Files and not a terminal:

1. Drag a host file into the mount and copy a mounted file back to the host.
2. Copy and move files with the clipboard, including between two partitions
   of one hard disc.
3. Rename a file and a populated drawer, then permanently delete both.
4. Edit a file in a GNOME editor that saves by replacing a temporary file, and
   confirm the change survives unmount and remount.
5. Attempt **Move to Trash**. Record whether Files offers it. If it does, it
   must complete coherently or fail with an accurate message.
6. Change permissions in **Properties** and confirm the protection bits with
   `getfattr`.
7. Open image and mounted-file properties and compare them with
   `amigafs inspect`.
8. Copy a drawer with `.info` icon files in and out and confirm the icons
   arrive intact.
9. Install AmigaFS beside Nautilus AcornFS and confirm that an Acorn `.adf`
   and an Amiga `.adf` each get only their own menu.

Unmount from the menu, confirm the sidebar entry disappears, then run
`amigafs validate` and confirm that no recovery is pending.

## Emulator and real-machine round trip

For each filesystem: write files with AmigaFS, unmount, then open the image in
an emulator or on a real Amiga and confirm that

- the volume mounts and the AmigaDOS validator does not run;
- the files read back byte for byte, with their protection bits, comments and
  datestamps;
- a bootable floppy created by AmigaFS boots;
- files then written by AmigaDOS are read correctly by AmigaFS.

## polkit and physical discs

Use an expendable CompactFlash or SD card holding an Amiga partition table.

1. Confirm `amigafs list-discs` lists the card and does **not** list the
   computer's own discs.
2. Mount it read-only from Files. Confirm the polkit prompt names AmigaFS,
   that cancelling it mounts nothing, and that a wrong password mounts
   nothing.
3. Mount read-write, change files, unmount and validate.
4. Mount read-write, change files and pull the cable. Reattach the card and
   restore the session. Confirm the card matches its earlier image.
5. Confirm that a card with a mounted FAT partition is refused with the
   reason, and that a non-Amiga USB stick is refused.
6. Attach a different card and attempt to restore the first card's session.
   It must be refused.
7. Run `read-disc` and `write-disc` and compare checksums.
8. With the per-user add-on instead of the package, confirm that a disc the
   account cannot open is refused with an explanation.

## Greaseweazle and physical floppies

Use expendable double-density and high-density disks.

1. Confirm the floppy actions appear only while the device is connected.
2. Read a known disk to an image and compare it with a known-good image.
3. Write an ADF, an ADZ, a DMS and an HFE to disks and boot each on a real
   Amiga.
4. Mount a floppy read-only and confirm nothing is written.
5. Mount read-write, change one file, unmount, and confirm that only a few
   cylinders were written and that the disk reads correctly on a real Amiga.
6. Mount read-write, change a file, remove the disk and unmount. Confirm the
   failure is reported, that the changes can be salvaged, and that the
   salvaged image is correct.
7. Confirm a copy-protected original is refused for mounting and can still be
   captured and written as a track image.
8. Confirm drive detection on both a PC cable and a Shugart bus.

Any crash, hang, lost metadata, silent renaming, ambiguous dialog, stale mount
or write to the wrong medium is a release blocker.
