"""Amiganut: the AmigaDOS filing-system engine used by Amiga File Forge.

Amiganut reads and writes the media an Amiga actually used:

* **OFS** and **FFS** volumes (``DOS\\0`` to ``DOS\\5``), on 880 KiB and
  1.76 MiB floppies and on hard-drive partitions of any size.
* **RDB** (Rigid Disk Block) partitioned hard-drive files, so one ``.hdf``
  can present several independently mountable volumes.
* **Kickstart** ROM images, decoded into their resident-module list.

The public API is deliberately small and is the only surface Amiga File Forge
depends on:

``amiganut.filesystem``
    ``create_filesystem``, ``reader_for``, ``identify``, ``geometry_from_geo``
    and the ``AmigaMetadata`` / ``Datestamped`` / ``Filetyped`` protocols.
``amiganut.disc.mount``
    ``resolve_mount``, which turns ``image.adf:C/List`` into a mounted volume
    and an inner path.
``amiganut.disc.cli``
    The ``adisc`` command line and the bulk-copy helpers it shares with the
    workbench.
``amiganut.file``
    ``Access``, ``AmigaMeta`` and the protection-bit helpers.
``amiganut.basic``
    AmigaBASIC tokenising and detokenising.
``amiganut.kickfs``
    Kickstart ROM identity and module decoding.
"""

from .version import __version__

__all__ = ["__version__"]
