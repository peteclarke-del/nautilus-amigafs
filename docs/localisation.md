# Localising the desktop integration

AmigaFS uses the gettext domain `amigafs` for the Nautilus menu, properties,
notifications and Zenity dialogs. English is the fallback when a catalogue or
an individual translation is missing.

The catalogue covers interface text, validation summaries, repair plans and
progress, image-property labels, and the messages of the mount, recovery,
creation, device and floppy workflows.

These are **not** translated:

- stable finding codes, repair action identifiers and recovery state values;
- Amiga paths, volume names, comments and other text owned by the image;
- protection letters (`hsparwed`), which are AmigaDOS notation;
- findings produced by the engine's validator, and low-level detail from the
  engine, Greaseweazle or the operating system. They follow a translated
  context phrase unchanged, so diagnostics keep the original evidence;
- messages from the privileged device helper. It is deliberately
  self-contained and loads no catalogue. AmigaFS translates its refusal codes
  on the unprivileged side.

## Update the template

Install GNU gettext, then run:

```shell
make messages
```

This regenerates `po/amigafs.pot`. Keep placeholders such as `{image}`,
`{drive}` and `{count}` unchanged; translators may reorder them. Keep command
names and environment variables verbatim.

## Add a translation

```shell
mkdir -p src/amigafs/locale/fr/LC_MESSAGES
msgfmt po/fr.po -o src/amigafs/locale/fr/LC_MESSAGES/amigafs.mo
```

Compiled catalogues below `src/amigafs/locale` are included in the wheel and
source packages. During development `AMIGAFS_LOCALE_DIR` may point at another
locale tree.

Test both the extension and its dialogs in a session using that locale. A
translation is not release-ready until long messages, 200 percent scaling and
a screen reader have been checked on a real GNOME desktop.
