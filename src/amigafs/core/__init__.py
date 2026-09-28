"""Filesystem-independent Amiga image handling.

The public names are loaded on first use. Importing a single submodule, as the
recovery and mount layers do, therefore never pulls in the whole engine.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .create import CreatedImage, create_floppy_image, create_hard_disc_image
    from .formats import ImageCapabilities, ResolvedImage, image_capabilities_hint, resolve_image
    from .image import AmigaImage, ImageNode, validate_image
    from .properties import ImageProperties, VolumeProperties, read_image_properties
    from .repair import (
        RepairAction,
        RepairPlan,
        RepairResult,
        RepairRisk,
        apply_repairs,
        plan_repairs,
        plan_repairs_from_report,
    )
    from .transfer import ExportedFile, ImportedFile, export_file, import_file
    from .validation import (
        COMPATIBILITY_PROFILE_ID,
        COMPATIBILITY_PROFILE_VERSION,
        VALIDATION_REPORT_SCHEMA_VERSION,
        FindingSeverity,
        IntegrityFinding,
        IntegrityReport,
        validate_image_report,
    )

_EXPORTS = {
    "COMPATIBILITY_PROFILE_ID": "validation",
    "COMPATIBILITY_PROFILE_VERSION": "validation",
    "VALIDATION_REPORT_SCHEMA_VERSION": "validation",
    "AmigaImage": "image",
    "CreatedImage": "create",
    "ExportedFile": "transfer",
    "FindingSeverity": "validation",
    "ImageCapabilities": "formats",
    "ImageNode": "image",
    "ImageProperties": "properties",
    "ImportedFile": "transfer",
    "IntegrityFinding": "validation",
    "IntegrityReport": "validation",
    "RepairAction": "repair",
    "RepairPlan": "repair",
    "RepairResult": "repair",
    "RepairRisk": "repair",
    "ResolvedImage": "formats",
    "VolumeProperties": "properties",
    "apply_repairs": "repair",
    "create_floppy_image": "create",
    "create_hard_disc_image": "create",
    "export_file": "transfer",
    "image_capabilities_hint": "formats",
    "import_file": "transfer",
    "plan_repairs": "repair",
    "plan_repairs_from_report": "repair",
    "read_image_properties": "properties",
    "resolve_image": "formats",
    "validate_image": "image",
    "validate_image_report": "validation",
}


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{module}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted([*globals(), *_EXPORTS])


__all__ = [  # noqa: F822 - resolved lazily by __getattr__
    "COMPATIBILITY_PROFILE_ID",
    "COMPATIBILITY_PROFILE_VERSION",
    "VALIDATION_REPORT_SCHEMA_VERSION",
    "AmigaImage",
    "CreatedImage",
    "ExportedFile",
    "FindingSeverity",
    "ImageCapabilities",
    "ImageNode",
    "ImageProperties",
    "ImportedFile",
    "IntegrityFinding",
    "IntegrityReport",
    "RepairAction",
    "RepairPlan",
    "RepairResult",
    "RepairRisk",
    "ResolvedImage",
    "VolumeProperties",
    "apply_repairs",
    "create_floppy_image",
    "create_hard_disc_image",
    "export_file",
    "image_capabilities_hint",
    "import_file",
    "plan_repairs",
    "plan_repairs_from_report",
    "read_image_properties",
    "resolve_image",
    "validate_image",
    "validate_image_report",
]
