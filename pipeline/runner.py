"""Orchestration: run the four stages in order and record what each one did.

This is the module a scheduler would call. It owns the stage sequence, the timings,
and writing each layer to disk; the layer modules stay pure transformations.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from . import bronze, gold, landing, metrics, quality, silver
from .config import OUTPUT, layer_dir
from .quality import Finding

log = logging.getLogger(__name__)


@dataclass
class StageResult:
    """One stage of the run, as the stage log reports it."""

    name: str
    did: str
    rows_out: int
    detail: str
    seconds: float

    @property
    def millis(self) -> float:
        return self.seconds * 1000

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.name,
            "did": self.did,
            "rows_out": self.rows_out,
            "detail": self.detail,
            "ms": round(self.millis, 1),
        }


@dataclass
class RunResult:
    """Everything one pipeline run produced."""

    stages: list[StageResult] = field(default_factory=list)
    landed: dict[str, landing.LandedFile] = field(default_factory=dict)
    bronze: dict[str, pd.DataFrame] = field(default_factory=dict)
    silver: dict[str, pd.DataFrame] = field(default_factory=dict)
    gold: dict[str, pd.DataFrame] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    load_config: pd.DataFrame = field(default_factory=pd.DataFrame)
    seconds: float = 0.0

    @property
    def quarantined(self) -> int:
        return sum(int(frame["is_quarantined"].sum()) for frame in self.silver.values())

    @property
    def silver_rows(self) -> int:
        return sum(len(frame) for frame in self.silver.values())


class _Timer:
    """Context manager that appends a StageResult when the block finishes."""

    def __init__(self, result: RunResult, name: str, did: str):
        self.result, self.name, self.did = result, name, did
        self.rows_out = 0
        self.detail = ""

    def __enter__(self) -> "_Timer":
        self.started = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        elapsed = time.perf_counter() - self.started
        self.result.stages.append(
            StageResult(self.name, self.did, self.rows_out, self.detail, elapsed)
        )
        log.info("%-8s %8d rows  %7.0f ms  %s", self.name, self.rows_out, elapsed * 1000, self.detail)


def run(write: bool = True, output_format: str = "csv") -> RunResult:
    """Execute landing → bronze → silver → gold.

    Args:
        write: Whether to persist each layer and the reports under ``output/``.
        output_format: ``"csv"`` or ``"parquet"``. Parquet needs pyarrow.

    Returns:
        The run result, including the metrics document and quality findings.
    """
    result = RunResult()
    started = time.perf_counter()

    with _Timer(result, "Landing", "read delimited files exactly as delivered") as timer:
        result.landed = landing.read_all()
        result.load_config = landing.read_load_config()
        total_bytes = sum(f.bytes_on_disk for f in result.landed.values())
        timer.rows_out = sum(f.rows for f in result.landed.values())
        timer.detail = f"{len(result.landed)} files · {total_bytes / 1_048_576:.2f} MB"

    with _Timer(result, "Bronze", "union both hospitals per table, stamp datasource") as timer:
        result.bronze = bronze.build(result.landed)
        timer.rows_out = sum(len(frame) for frame in result.bronze.values())
        timer.detail = f"{len(result.bronze)} tables"

    with _Timer(result, "Silver", "conform to the common model, type, check, version") as timer:
        result.silver = silver.build(result.bronze)
        timer.rows_out = result.silver_rows
        timer.detail = f"{result.quarantined} quarantined"

    with _Timer(result, "Gold", "load the star schema from current, clean rows") as timer:
        result.gold = gold.build(result.silver)
        timer.rows_out = sum(len(frame) for frame in result.gold.values())
        timer.detail = f"{len(result.gold)} tables"

    with _Timer(result, "Report", "aggregate metrics and run quality checks") as timer:
        result.metrics = metrics.compute(result.gold, result.silver)
        result.findings = quality.check(result.gold, result.silver)
        timer.rows_out = len(result.findings)
        summary = quality.summarise(result.findings)
        timer.detail = (
            f"{summary['critical']} critical · {summary['serious']} serious · "
            f"{summary['warning']} warning · {summary['passing']} passing"
        )

    result.seconds = time.perf_counter() - started

    if write:
        write_outputs(result, output_format=output_format)

    return result


def _write_frame(frame: pd.DataFrame, path: Path, output_format: str) -> None:
    if output_format == "parquet":
        try:
            frame.to_parquet(path.with_suffix(".parquet"), index=False)
            return
        except ImportError:
            log.warning("parquet engine unavailable (pip install pyarrow) — writing CSV instead")
    frame.to_csv(path.with_suffix(".csv"), index=False)


def write_outputs(result: RunResult, output_format: str = "csv") -> None:
    """Persist every layer plus the metrics and data quality reports."""
    for layer, tables in (("bronze", result.bronze), ("silver", result.silver), ("gold", result.gold)):
        directory = layer_dir(layer)
        for name, frame in tables.items():
            _write_frame(frame, directory / name, output_format)

    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "metrics.json").write_text(json.dumps(result.metrics, indent=2, default=str))
    (OUTPUT / "data_quality.json").write_text(
        json.dumps(
            {
                "summary": quality.summarise(result.findings),
                "findings": [finding.as_dict() for finding in result.findings],
            },
            indent=2,
            default=str,
        )
    )
    (OUTPUT / "stage_log.json").write_text(
        json.dumps([stage.as_dict() for stage in result.stages], indent=2)
    )
    log.info("wrote %s", OUTPUT)
