# Amiga metadata mapping

AmigaFS keeps the on-disc metadata authoritative. The POSIX view is an
interoperability layer and does not invent sidecar files.

## Extended attributes

| Attribute | Value | Writable |
| --- | --- | --- |
| `user.amiga.protection` | the eight protection letters as `List` prints them, for example `----rwed` | yes |
| `user.amiga.comment` | the file comment, as UTF-8; absent when there is none | yes; removing it clears the comment |
| `user.amiga.locked` | `1` when the entry is write- or delete-protected, else `0` | yes |
| `user.amiga.volume` | the volume name | on a volume's root folder only, which relabels the volume |
| `user.amiga.path` | the Amiga path, for example `DH0:S/Startup-Sequence` | no |
| `user.amiga.source` | the filesystem, for example `FFS-INTL`, `PFS3`, `SFS`; `RDB` on the partition list | no |
| `user.amiga.link` | `hard` or `soft` on a link | no |

```shell
getfattr -d -m user.amiga Startup-Sequence
setfattr -n user.amiga.protection -v "-s--rwed" Startup-Sequence
setfattr -n user.amiga.comment -v "Boot script" Startup-Sequence
setfattr -x user.amiga.comment Startup-Sequence
```

`user.amiga.protection` accepts eight letters in `hsparwed` order with `-` for
a bit that is not set, a shorter list of the letters to grant such as `rwed`,
or the raw value in hexadecimal such as `&40`.

## Protection bits

The low four bits are stored inverted on disc: a set bit *denies* the
operation. AmigaFS shows the letters as AmigaDOS does and hides the inversion.

| Letter | Meaning | Linux presentation |
| --- | --- | --- |
| `r` | readable | none; files are always shown readable |
| `w` | writable | without it the file is mode `444` and cannot be opened for writing |
| `e` | executable | none; mounts are `noexec` |
| `d` | deletable | without it the file cannot be removed or replaced by a rename |
| `a` | archived | cleared whenever AmigaFS writes the file |
| `p`, `s`, `h` | pure, script, hold | preserved |

`chmod` maps to the `w` and `d` bits together. Removing every write permission
protects the file against writing and deletion; granting write permission
lifts both. The other bits are never changed by `chmod`. Ownership cannot be
changed: AmigaDOS has none, and entries are shown as owned by the mounting
user.

Write-protected content cannot be rewritten. Delete-protected content *can* be
rewritten, and keeps its protection.

## Datestamps

An AmigaDOS datestamp is local time with no time zone, counted from 1 January
1978 in fiftieths of a second. AmigaFS presents it as the same wall-clock time
on the host: a file stamped 12:00 on the Amiga is shown as 12:00 in Files,
whatever the host's zone. This is also how the kernel's own Amiga filesystem
driver reads it.

Setting a modification time, with `touch` or by copying with timestamps
preserved, stores the host's local wall-clock time. A time before 1978 is
stored as 1 January 1978. Writing a file stamps it with the current time, as
AmigaDOS does.

## Names

| Volume | Longest name |
| --- | --- |
| OFS and FFS, including international and directory-cache variants | 30 characters |
| Long-filename OFS and FFS (`DOS\6`, `DOS\7`) | 107 characters |
| PFS3 and SFS | as the volume declares |

Amiga names are ISO 8859-1 (Latin-1) and are shown as UTF-8. Lookup ignores
case, as on an Amiga, while the stored spelling stays visible. Creating an
entry whose name differs from a sibling only by case is refused. An SFS volume
formatted as case-sensitive is treated as such.

A new name must be representable in Latin-1, must not contain `:`, `/`, `\` or
a control character, and must not begin or end with a space. AmigaFS never
silently changes a name it was given. A name that is too long is refused with
`ENAMETOOLONG`; any other unacceptable name with `EINVAL`.

When an existing name on a damaged or hostile image contains a character POSIX
cannot represent, it is displayed unambiguously: `/` as `∕`, control characters
as Unicode control pictures, and the names `.` and `..` in full-width forms.

## Links

Hard and soft links are listed but never followed during indexing, so a link to
an ancestor cannot make the tree infinite. AmigaFS does not create, change,
move or remove links, because it cannot maintain the link chains safely.
Reading *through* a link is left to the engine and has not been verified
against real media; treat the content of a link as unconfirmed.

## Partitions

On a hard disc each partition is a folder named after its device. The folders
themselves cannot be created, renamed or removed, and a file cannot be renamed
from one partition to another; a file manager copies and deletes instead.

## Kickstart ROMs

A ROM's resident modules appear as files. Names are case-sensitive, the comment
is the module's identification string, and every module is write- and
delete-protected. ROMs are always read-only.

## Sidecars

Mounting never creates sidecar files. The explicit `export-file` command
writes a host file and a matching `.inf` record in the form Amiga File Forge
uses:

```text
Games/Program ----r-e- 00000007 "The game loader"
```

The fields are the path inside the volume, the protection bits, the length in
hexadecimal and the comment if there is one. A path containing spaces is
quoted. The datestamp travels as the host file's modification time.
Publication is create-only, so neither destination is overwritten or left half
written.

`import-file` accepts the same record. It checks a recorded length against the
host file before anything is written, then creates the data and metadata as
one change. Without a sidecar it uses neutral defaults: everything permitted,
no comment, and the host file's own modification time. A record that carries
no Amiga protection field, such as one written for another system, is refused
rather than guessed at.
