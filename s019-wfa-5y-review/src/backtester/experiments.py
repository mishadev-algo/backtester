"""Synchronous experiments with a durable SQLite record and versioned inputs."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import platform
import sqlite3
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .data import load_csv
from .datasets import EmptyPeriodError, file_sha256, inspect_dataset, load_dataset
from .engine import run_backtest
from .models import BacktestConfig, BacktestResult
from .reporting import write_report
from .strategy_loader import load_strategy
from .tick_data import inspect_ticks, is_tick_dataset, load_ticks
from .tick_engine import run_tick_backtest


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
    data: Path
    output: Path
    strategy: str = "sma"
    parameters: dict[str, Any] = Field(default_factory=dict)
    config: BacktestConfig = Field(default_factory=BacktestConfig)
    start: datetime | None = None
    end: datetime | None = None
    warmup_bars: int = Field(default=0, ge=0, strict=True)
    label: str = "backtest"

    @model_validator(mode="after")
    def validate_request(self):
        for stamp in (self.start, self.end):
            if stamp is not None and stamp.utcoffset() is None:
                raise ValueError("period boundaries must have explicit timezones")
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError("period start must be earlier than end")
        json.dumps(self.parameters, allow_nan=False)
        return self


class ExperimentStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS experiments (
                run_id TEXT PRIMARY KEY, status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed')),
                created_at TEXT NOT NULL, finished_at TEXT, request_json TEXT NOT NULL,
                summary_json TEXT, error TEXT, output TEXT NOT NULL UNIQUE
            )""")

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def start(self, request: RunRequest) -> str:
        run_id = uuid4().hex
        output = str(request.output.resolve())
        with self._connect() as connection:
            connection.execute("INSERT INTO experiments(run_id,status,created_at,request_json,output) VALUES(?,?,?,?,?)",
                               (run_id, "running", datetime.now(timezone.utc).isoformat(), request.model_dump_json(), output))
        return run_id

    def finish(self, run_id: str, result: BacktestResult | None = None, error: str | None = None):
        if (result is None) == (error is None):
            raise ValueError("provide either a result or an error")
        with self._connect() as connection:
            updated = connection.execute("""UPDATE experiments SET status=?,finished_at=?,summary_json=?,error=?
                WHERE run_id=? AND status='running'""",
                ("succeeded" if result is not None else "failed", datetime.now(timezone.utc).isoformat(),
                 json.dumps(result.summary(), allow_nan=False) if result is not None else None, error, run_id))
            if updated.rowcount != 1:
                raise ValueError("experiment is missing or already finished")

    def list(self, limit: int = 20) -> list[dict]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM experiments ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]


def load_input(path: Path, start: datetime | None = None, end: datetime | None = None, *, tail: int | None = None):
    if path.is_dir():
        tick_data = is_tick_dataset(path)
        manifest = inspect_ticks(path) if tick_data else inspect_dataset(path)
        # Fetch only selected rows with predicate pushdown; include prior bars for warmup at caller's request.
        bars = load_ticks(path, start, end, tail=tail) if tick_data else load_dataset(path, start, end, tail=tail)
        metadata = {"dataset_id": manifest.dataset_id, "data_sha256": manifest.parquet_sha256,
                    "dataset": manifest.model_dump(mode="json"), "data_file": path.name,
                    "data_kind": "ticks" if tick_data else "bars"}
    else:
        checksum = file_sha256(path)
        bars = load_csv(path)
        if file_sha256(path) != checksum:
            raise ValueError("input CSV changed while it was being read")
        bars = tuple(bar for bar in bars if (start is None or bar.timestamp >= start)
                     and (end is None or bar.timestamp < end))
        if tail is not None:
            bars = bars[-tail:]
        metadata = {"data_sha256": checksum, "data_file": path.name, "data_kind": "bars"}
    if not bars:
        raise EmptyPeriodError("selected period contains no price bars")
    return bars, metadata


def code_identity() -> dict:
    root = Path(__file__).parent
    digest = hashlib.sha256()
    for path in sorted(root.glob("*.py")):
        digest.update(path.name.encode() + b"\0" + path.read_bytes())
    return {"engine_version": version("mishadev-backtester"), "engine_source_sha256": digest.hexdigest(),
            "python_version": platform.python_version(),
            "dependencies": {name: version(name) for name in ("duckdb", "pyarrow", "pydantic", "plotly", "numpy", "pandas")}}


def execute_experiment(request: RunRequest, registry: Path | str | None = None):
    if request.output.exists():
        raise FileExistsError(f"report already exists: {request.output}")
    store = ExperimentStore(registry or request.output.parent / "experiments.sqlite3")
    run_id = store.start(request)
    try:
        bars, data_metadata = load_input(request.data, request.start, request.end)
        config = request.config
        if "dataset" in data_metadata:
            config = replace(config, price_type=data_metadata["dataset"]["spec"]["price_type"])
        warmup = ()
        if request.warmup_bars and request.start is not None:
            # Only bars strictly before the evaluation boundary are visible during warmup.
            try:
                earlier, earlier_metadata = load_input(request.data, end=request.start, tail=request.warmup_bars)
                if earlier_metadata["data_sha256"] != data_metadata["data_sha256"]:
                    raise ValueError("dataset changed between evaluation and warmup reads")
            except EmptyPeriodError:
                pass
            else:
                warmup = earlier[-request.warmup_bars:]
        tick_mode = data_metadata["data_kind"] == "ticks"
        identity = code_identity()
        with load_strategy(request.strategy, data_metadata["data_kind"]) as loaded:
            strategy = loaded.factory(**deepcopy(request.parameters))
            if tick_mode and getattr(strategy, "execution_mode", None) == "orders" and "dataset" in data_metadata:
                if strategy.broker_spec.symbol != data_metadata["dataset"]["spec"]["symbol"]:
                    raise ValueError("broker contract symbol does not match the imported tick dataset")
            callback = "on_tick" if tick_mode else "on_bar"
            if not callable(getattr(strategy, callback, None)):
                raise ValueError(f"factory must return an object with {callback}(history)")
            result = (run_tick_backtest(bars, strategy, config, warmup_ticks=warmup) if tick_mode
                      else run_backtest(bars, strategy, config, warmup_bars=warmup))
        metadata = {
            **data_metadata, **identity, "run_id": run_id, "label": request.label,
            "resolved_config": asdict(result.config),
            "strategy": loaded.spec, "strategy_source_sha256": loaded.source_sha256,
            "strategy_file": str(loaded.source_path) if loaded.source_path else None,
            "parameters": request.parameters,
            "period": {"start_inclusive": request.start.isoformat() if request.start else None,
                       "end_exclusive": request.end.isoformat() if request.end else None},
            "warmup_bars_requested": request.warmup_bars, "warmup_bars_used": len(warmup),
            "execution": ("long-only fractional units; decisions after current quote; fills at next source tick ask/bid; equity marked at bid"
                          if tick_mode else "long-only fractional units; decisions at close, fills at next open"),
            "warmup_unit": "ticks" if tick_mode else "bars",
        }
        if result.execution_metadata:
            metadata["execution"] = result.execution_metadata["model"]
            metadata["order_execution"] = result.execution_metadata
        write_report(result, request.output, metadata)
        store.finish(run_id, result=result)
        return result, metadata
    except BaseException as exc:
        store.finish(run_id, error=f"{type(exc).__name__}: {exc}")
        raise


def evaluate(request: RunRequest, train_end: datetime, validation_end: datetime,
             registry: Path | str | None = None) -> dict:
    """Three chronological segments with fixed parameters, fresh accounts, and past-only warmup."""
    if train_end.utcoffset() is None or validation_end.utcoffset() is None or train_end >= validation_end:
        raise ValueError("train_end and validation_end must be timezone-aware and strictly increasing")
    if request.start is not None and request.start >= train_end:
        raise ValueError("train_end must be after the selected start")
    if request.end is not None and validation_end >= request.end:
        raise ValueError("validation_end must be before the selected end")
    segments = [("train", request.start, train_end), ("validation", train_end, validation_end),
                ("test", validation_end, request.end)]
    # Check all periods before creating artifacts or executing user strategies.
    for _, start, end in segments:
        load_input(request.data, start, end)
    request.output.mkdir(parents=True, exist_ok=False)
    registry = registry or request.output.parent / "experiments.sqlite3"
    report = {"schema_version": 1, "parameters": request.parameters,
              "method": "fixed parameters; independent accounts; chronological train/validation/test",
              "segments": {}}
    for label, start, end in segments:
        segment = request.model_copy(update={"output": request.output / label, "start": start,
                                             "end": end, "label": label})
        result, metadata = execute_experiment(segment, registry)
        report["segments"][label] = {"run_id": metadata["run_id"], "summary": result.summary(),
                                    "output": str(segment.output)}
    (request.output / "evaluation.json").write_text(json.dumps(report, allow_nan=False, indent=2) + "\n", encoding="utf-8")
    return report
