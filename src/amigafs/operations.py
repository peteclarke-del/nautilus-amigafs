"""Cooperative cancellation for long-running, safety-sensitive operations."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from amigafs.errors import OperationCancelled, OperationLimitExceeded
from amigafs.i18n import _

CancellationCheck = Callable[[], bool]
ProgressCallback = Callable[[int, str], None]
DEFAULT_OPERATION_TIMEOUT = 5 * 60.0
DEFAULT_OPERATION_ITEMS = 100_000
DEFAULT_OPERATION_DEPTH = 256


@dataclass(slots=True)
class OperationBudget:
    """A shared wall-clock and structural budget for one untrusted operation."""

    deadline: float
    max_items: int = DEFAULT_OPERATION_ITEMS
    max_depth: int = DEFAULT_OPERATION_DEPTH
    items: int = 0
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)

    @classmethod
    def create(
        cls,
        *,
        timeout: float = DEFAULT_OPERATION_TIMEOUT,
        max_items: int = DEFAULT_OPERATION_ITEMS,
        max_depth: int = DEFAULT_OPERATION_DEPTH,
        clock: Callable[[], float] = time.monotonic,
    ) -> OperationBudget:
        if timeout <= 0 or max_items <= 0 or max_depth <= 0:
            raise ValueError("operation budgets must be positive")
        return cls(clock() + timeout, max_items=max_items, max_depth=max_depth, clock=clock)

    def checkpoint(
        self,
        cancelled: CancellationCheck | None = None,
        *,
        items: int = 0,
        depth: int | None = None,
    ) -> None:
        cancellation_point(cancelled)
        if items < 0:
            raise ValueError("operation item increments cannot be negative")
        self.items += items
        if self.items > self.max_items:
            raise OperationLimitExceeded(
                _("The operation stopped after reaching its safe item limit.")
            )
        if depth is not None and depth > self.max_depth:
            raise OperationLimitExceeded(
                _("The operation stopped after reaching its safe directory-depth limit.")
            )
        if self.clock() >= self.deadline:
            raise OperationLimitExceeded(_("The operation exceeded its safe time limit."))


def cancellation_point(cancelled: CancellationCheck | None) -> None:
    """Stop only at a boundary where the caller has left persistent state safe."""

    if cancelled is not None and cancelled():
        raise OperationCancelled(_("The operation was cancelled safely."))


def report_progress(progress: ProgressCallback | None, percent: int, message: str) -> None:
    """Publish a normalised progress update when a caller requested one."""

    if progress is not None:
        progress(max(0, min(100, percent)), message)


__all__ = [
    "CancellationCheck",
    "OperationBudget",
    "ProgressCallback",
    "cancellation_point",
    "report_progress",
]
