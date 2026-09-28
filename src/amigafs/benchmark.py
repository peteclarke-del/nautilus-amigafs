"""Repeatable amd64 performance baseline for the core image engine."""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import statistics
import tempfile
import time
import tracemalloc
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import cycle
from pathlib import Path
from typing import TypeVar

from amigafs.core.create import create_hard_disc_image
from amigafs.core.image import DEFAULT_MAX_NODES, ROOT_INODE, AmigaImage, ImageNode

MIB = 1024 * 1024
SCHEMA_VERSION = 1
SUPPORTED_MACHINES = frozenset({"amd64", "x86_64"})
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    """The stable generated workload used for amd64 comparisons."""

    capacity: str = "20MB"
    filesystem: str = "FFS-INTL"
    root_entries: int = 32
    depth: int = 16
    small_files: int = 16
    small_file_bytes: int = 1024
    large_file_bytes: int = 4 * MIB
    cache_bytes: int = 64 * 1024
    open_samples: int = 5
    operation_samples: int = 250
    large_read_samples: int = 3
    read_bytes: int = 64 * 1024
    memory_nodes: int = DEFAULT_MAX_NODES
    write_buffer_bytes: int = 8 * MIB

    def __post_init__(self) -> None:
        if not 1 <= self.root_entries <= 1024:
            raise ValueError("root_entries must be between 1 and 1024")
        if not 1 <= self.depth <= 256:
            raise ValueError("depth must be between 1 and 256")
        if not 1 <= self.small_files <= 1024:
            raise ValueError("small_files must be between 1 and 1024")
        if min(self.small_file_bytes, self.cache_bytes, self.read_bytes) <= 0:
            raise ValueError("file, cache and read sizes must be positive")
        if self.small_files * self.small_file_bytes > self.cache_bytes:
            raise ValueError("the complete small-file workload must fit in the cache")
        if self.large_file_bytes <= self.cache_bytes:
            raise ValueError("large_file_bytes must exceed cache_bytes")
        if min(self.open_samples, self.operation_samples, self.large_read_samples) <= 0:
            raise ValueError("sample counts must be positive")
        if not 1 <= self.memory_nodes <= DEFAULT_MAX_NODES:
            raise ValueError(f"memory_nodes must be between 1 and {DEFAULT_MAX_NODES}")
        if self.write_buffer_bytes <= 0:
            raise ValueError("write_buffer_bytes must be positive")


@dataclass(frozen=True, slots=True)
class Budget:
    statistic: str
    comparison: str
    value: float


# These are first-RC guardrails for shared GitHub amd64 runners, not hardware
# claims. Keep docs/performance.md in sync when deliberately changing them.
AMD64_BUDGETS = {
    "indexed_open_ms": Budget("p95", "at_most", 1000.0),
    "root_listing_us": Budget("p95", "at_most", 250.0),
    "deep_traversal_us": Budget("p95", "at_most", 500.0),
    "small_cached_read_us": Budget("p95", "at_most", 100.0),
    "large_ranged_read_mib_s": Budget("median", "at_least", 15.0),
    "open_peak_python_mib": Budget("max", "at_most", 32.0),
    "max_nodes_open_buffer_peak_python_mib": Budget("max", "at_most", 64.0),
}


def _percentile(values: Sequence[float], percentile: float) -> float:
    """Return a nearest-rank percentile, including for small sample sets."""

    if not values:
        raise ValueError("at least one sample is required")
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def _summarise(values: Sequence[float]) -> dict[str, float | int]:
    return {
        "samples": len(values),
        "min": round(min(values), 3),
        "median": round(statistics.median(values), 3),
        "p95": round(_percentile(values, 0.95), 3),
        "max": round(max(values), 3),
    }


def _measure(call: Callable[[], T], samples: int, *, scale: float) -> list[float]:
    durations: list[float] = []
    for _ in range(samples):
        started = time.perf_counter_ns()
        call()
        durations.append((time.perf_counter_ns() - started) / scale)
    return durations


@contextmanager
def _temporary_state_home(path: Path) -> Iterator[None]:
    """Keep disposable write checkpoints out of the user's real state."""

    previous = os.environ.get("XDG_STATE_HOME")
    os.environ["XDG_STATE_HOME"] = str(path)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = previous


def _add_file(image: AmigaImage, parent: int, name: bytes, data: bytes) -> int:
    node = image.create_file(parent, name)
    image.replace_file(node.inode, data)
    return node.inode


def _build_fixture(directory: Path, config: BenchmarkConfig) -> Path:
    created = create_hard_disc_image(
        directory,
        name="benchmark",
        title="Benchmark",
        capacity=config.capacity,
        filesystem=config.filesystem,
    )
    # The partition table is part of the workload: the volume is the first
    # partition's drawer, as it is for every hard-disc image.
    with AmigaImage.open(created.path, writable=True, commit_validation_bytes=0) as image:
        volume = image.children[ROOT_INODE][0]
        benchmark = image.make_directory(volume, b"BENCH")
        for number in range(config.root_entries - 1):
            image.create_file(volume, f"R{number:02d}".encode("ascii"))

        deep_parent = benchmark.inode
        for number in range(config.depth):
            deep_parent = image.make_directory(deep_parent, f"D{number:02d}".encode("ascii")).inode

        small = image.make_directory(benchmark.inode, b"SMALL")
        for number in range(config.small_files):
            payload = bytes([number % 251]) * config.small_file_bytes
            _add_file(image, small.inode, f"S{number:02d}".encode("ascii"), payload)

        repetitions = math.ceil(config.large_file_bytes / 256)
        payload = (bytes(range(256)) * repetitions)[: config.large_file_bytes]
        _add_file(image, benchmark.inode, b"LARGE", payload)
    return created.path


def _resolve_path(image: AmigaImage, names: Sequence[bytes]) -> int:
    inode = image.children[ROOT_INODE][0]
    for name in names:
        node = image.lookup(inode, name)
        if node is None:
            raise RuntimeError(f"benchmark fixture entry disappeared: {name!r}")
        inode = node.inode
    return inode


def _metric(name: str, unit: str, values: Sequence[float]) -> tuple[dict[str, object], bool]:
    summary = _summarise(values)
    budget = AMD64_BUDGETS[name]
    observed = float(summary[budget.statistic])
    passed = (
        observed <= budget.value if budget.comparison == "at_most" else observed >= budget.value
    )
    return (
        {
            "unit": unit,
            **summary,
            "budget": {
                "statistic": budget.statistic,
                "comparison": budget.comparison,
                "value": budget.value,
            },
            "passed": passed,
        },
        passed,
    )


def _profile_memory_stress(config: BenchmarkConfig) -> float:
    """Measure a full 47-way index plus one large FUSE-style write buffer."""

    tracemalloc.start()
    try:
        nodes: dict[int, ImageNode] = {}
        children: dict[int, tuple[int, ...]] = {}
        children_by_name: dict[int, dict[bytes, int]] = {}
        last_directory = (
            (config.memory_nodes - 2) // 47 + ROOT_INODE
            if config.memory_nodes > ROOT_INODE
            else ROOT_INODE
        )
        for inode in range(1, config.memory_nodes + 1):
            is_root = inode == ROOT_INODE
            parent = ROOT_INODE if is_root else (inode - 2) // 47 + ROOT_INODE
            name = b"" if is_root else f"N{inode:09d}".encode("ascii")
            leaf = name.decode("ascii")
            inner = "" if is_root else f"{nodes[parent].inner_path}/{leaf}".lstrip("/")
            is_dir = inode <= last_directory
            nodes[inode] = ImageNode(
                inode=inode,
                parent_inode=parent,
                name=name,
                volume=0,
                inner_path=inner,
                amiga_path=f"DH0:{inner}",
                is_dir=is_dir,
                size=0,
                mtime_ns=0,
            )
            if is_dir:
                children[inode] = ()
                children_by_name[inode] = {}
            if not is_root:
                children[parent] = (*children[parent], inode)
                children_by_name[parent][name] = inode
        write_buffers = {config.memory_nodes: bytearray(config.write_buffer_bytes)}
        if len(nodes) != config.memory_nodes or len(write_buffers[config.memory_nodes]) != (
            config.write_buffer_bytes
        ):
            raise RuntimeError("memory stress allocation was incomplete")
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak / MIB


def _measure_fixture(dat_path: Path, config: BenchmarkConfig) -> dict[str, object]:
    open_values: list[float] = []
    image: AmigaImage | None = None
    for sample in range(config.open_samples):
        started = time.perf_counter_ns()
        opened = AmigaImage.open(dat_path, cache_bytes=config.cache_bytes)
        open_values.append((time.perf_counter_ns() - started) / 1_000_000)
        if sample == config.open_samples - 1:
            image = opened
        else:
            opened.close()
    if image is None:
        raise ValueError("open_samples must be positive")

    try:
        volume = image.children[ROOT_INODE][0]
        root_values = _measure(
            lambda: tuple(image.nodes[inode] for inode in image.children[volume]),
            config.operation_samples,
            scale=1000,
        )
        deep_names = [
            b"BENCH",
            *(f"D{number:02d}".encode("ascii") for number in range(config.depth)),
        ]
        deep_values = _measure(
            lambda: _resolve_path(image, deep_names), config.operation_samples, scale=1000
        )

        small_inode = _resolve_path(image, [b"BENCH", b"SMALL"])
        small_inodes = image.children[small_inode]
        for inode in small_inodes:
            image.read(inode, 0, config.small_file_bytes)
        rotating_small_inodes = cycle(small_inodes)
        small_values = _measure(
            lambda: image.read(next(rotating_small_inodes), 0, config.small_file_bytes),
            config.operation_samples,
            scale=1000,
        )

        large_inode = _resolve_path(image, [b"BENCH", b"LARGE"])
        large_values: list[float] = []
        for _ in range(config.large_read_samples):
            total = 0
            started = time.perf_counter_ns()
            for offset in range(0, config.large_file_bytes, config.read_bytes):
                total += len(image.read(large_inode, offset, config.read_bytes))
            elapsed = (time.perf_counter_ns() - started) / 1_000_000_000
            if total != config.large_file_bytes:
                raise RuntimeError("large ranged read returned the wrong byte count")
            large_values.append((total / MIB) / elapsed)
    finally:
        image.close()

    tracemalloc.start()
    memory_image: AmigaImage | None = None
    try:
        memory_image = AmigaImage.open(dat_path, cache_bytes=config.cache_bytes)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        if memory_image is not None:
            memory_image.close()
        tracemalloc.stop()

    measured = {
        "indexed_open_ms": ("ms", open_values),
        "root_listing_us": ("us", root_values),
        "deep_traversal_us": ("us", deep_values),
        "small_cached_read_us": ("us", small_values),
        "large_ranged_read_mib_s": ("MiB/s", large_values),
        "open_peak_python_mib": ("MiB", [peak / MIB]),
        "max_nodes_open_buffer_peak_python_mib": (
            "MiB",
            [_profile_memory_stress(config)],
        ),
    }
    metrics: dict[str, object] = {}
    passed = True
    for name, (unit, values) in measured.items():
        result, metric_passed = _metric(name, unit, values)
        metrics[name] = result
        passed = passed and metric_passed
    return {"metrics": metrics, "budgets_passed": passed}


def run_benchmark(config: BenchmarkConfig | None = None) -> dict[str, object]:
    """Generate the stable workload, measure it and return a JSON-safe report."""

    config = config or BenchmarkConfig()
    machine = platform.machine().casefold()
    if machine not in SUPPORTED_MACHINES:
        raise RuntimeError(f"the published performance baseline supports amd64 only, not {machine}")
    with tempfile.TemporaryDirectory(prefix="amigafs-benchmark-") as temporary:
        workspace = Path(temporary)
        with _temporary_state_home(workspace / "state"):
            dat_path = _build_fixture(workspace, config)
            measurements = _measure_fixture(dat_path, config)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "source_revision": os.environ.get("GITHUB_SHA"),
        "platform": {
            "architecture": machine,
            "python": platform.python_version(),
            "system": platform.platform(),
        },
        "fixture": {
            "capacity": config.capacity,
            "filesystem": config.filesystem,
            "root_entries": config.root_entries,
            "depth": config.depth,
            "small_files": config.small_files,
            "small_file_bytes": config.small_file_bytes,
            "large_file_bytes": config.large_file_bytes,
            "cache_bytes": config.cache_bytes,
            "memory_nodes": config.memory_nodes,
            "write_buffer_bytes": config.write_buffer_bytes,
        },
        **measurements,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Nautilus AmigaFS amd64 benchmark")
    parser.add_argument("--output", type=Path, help="write the full JSON report to this path")
    parser.add_argument(
        "--check-budgets", action="store_true", help="exit unsuccessfully if an RC budget fails"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run_benchmark()
    except Exception as exc:
        print(f"Benchmark failed: {exc}")
        return 2
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
        print(f"Wrote benchmark report to {args.output}")
    else:
        print(encoded, end="")
    if args.check_budgets and not report["budgets_passed"]:
        print("One or more amd64 performance budgets failed.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
