"""User-facing AmigaFS errors."""


class AmigaFSError(Exception):
    """Base class for errors safe to display to a user."""


class OperationCancelled(AmigaFSError):
    """A cooperative operation stopped at a persistent-state-safe boundary."""


class OperationLimitExceeded(AmigaFSError):
    """Untrusted input exhausted a configured operation budget."""


class DeviceAccessError(AmigaFSError):
    """A physical disc or floppy drive could not be opened safely."""


class FilenameTooLongError(ValueError):
    """An image entry name exceeds the filesystem's encoded length limit."""


class DiscFullError(AmigaFSError):
    """The volume has no room for the requested data."""


class UnsupportedImageError(AmigaFSError):
    """An image cannot be mapped to a safely supported mount profile."""
