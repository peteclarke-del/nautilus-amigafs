from pathlib import Path

import pytest

from amigafs.benchmark import (
    AMD64_BUDGETS,
    BenchmarkConfig,
    _metric,
    _percentile,
    main,
)
from amigafs.core.image import DEFAULT_MAX_NODES


def test_nearest_rank_percentile_handles_small_samples() -> None:
    assert _percentile([4.0, 1.0, 3.0, 2.0], 0.5) == 2.0
    assert _percentile([4.0, 1.0, 3.0, 2.0], 0.95) == 4.0
    with pytest.raises(ValueError, match="at least one"):
        _percentile([], 0.95)


@pytest.mark.parametrize(
    ("name", "passing", "failing"),
    [
        ("indexed_open_ms", 100.0, 3000.0),
        ("large_ranged_read_mib_s", 50.0, 1.0),
    ],
)
def test_metric_applies_budget_direction(name: str, passing: float, failing: float) -> None:
    passed_result, passed = _metric(name, "unit", [passing])
    failed_result, failed = _metric(name, "unit", [failing])

    assert passed
    assert passed_result["passed"] is True
    assert not failed
    assert failed_result["passed"] is False
    assert passed_result["budget"] == {
        "statistic": AMD64_BUDGETS[name].statistic,
        "comparison": AMD64_BUDGETS[name].comparison,
        "value": AMD64_BUDGETS[name].value,
    }


def test_cli_writes_report_and_enforces_budgets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = {
        "schema_version": 1,
        "budgets_passed": False,
        "platform": {"architecture": "x86_64"},
        "metrics": {},
    }
    monkeypatch.setattr("amigafs.benchmark.run_benchmark", lambda: report)
    output = tmp_path / "nested" / "result.json"

    assert main(["--output", str(output), "--check-budgets"]) == 1
    assert output.is_file()
    assert '"budgets_passed": false' in output.read_text(encoding="utf-8")


def test_benchmark_config_keeps_large_reads_outside_cache() -> None:
    config = BenchmarkConfig()
    assert config.large_file_bytes > config.cache_bytes
    assert config.large_file_bytes % 256 == 0
    assert config.root_entries <= 47
    assert config.memory_nodes == DEFAULT_MAX_NODES
    assert config.write_buffer_bytes == 8 * 1024 * 1024


@pytest.mark.parametrize(
    "settings",
    [
        {"root_entries": 1025},
        {"small_files": 0},
        {"depth": 257},
        {"large_file_bytes": 1024, "cache_bytes": 1024},
        {"open_samples": 0},
        {"memory_nodes": DEFAULT_MAX_NODES + 1},
        {"write_buffer_bytes": 0},
    ],
)
def test_benchmark_config_rejects_invalid_workloads(settings: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        BenchmarkConfig(**settings)
