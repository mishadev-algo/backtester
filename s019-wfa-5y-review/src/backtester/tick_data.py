"""Lossless quote imports: preserve every record and ordering within timestamps."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from typing import Literal

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .datasets import EmptyPeriodError, _timestamp, export_timezone, file_sha256
from .models import Tick


class TickCsvProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    delimiter: str = ","
    decimal_separator: Literal[".", ","] = "."
    encoding: str = "utf-8-sig"
    fieldnames: list[str] | None = None
    columns: dict[str, str] = Field(default_factory=lambda: {
        "timestamp": "DateTime", "bid": "Bid", "ask": "Ask", "volume": "Volume",
    })
    timestamp_format: str | None = "%Y%m%d %H:%M:%S.%f"
    timezone: str | None = None

    @model_validator(mode="after")
    def validate_profile(self):
        if len(self.delimiter) != 1 or self.delimiter in "\r\n":
            raise ValueError("delimiter must be one non-newline character")
        if not {"timestamp", "bid", "ask"} <= self.columns.keys() or set(self.columns) - {"timestamp", "bid", "ask", "volume"}:
            raise ValueError("tick columns must map timestamp, bid, ask and optional volume")
        if any(not value for value in self.columns.values()) or len(set(self.columns.values())) != len(self.columns):
            raise ValueError("source column names must be nonempty and unique")
        if self.fieldnames is not None and (not self.fieldnames or len(set(self.fieldnames)) != len(self.fieldnames)):
            raise ValueError("headerless fieldnames must be nonempty and unique")
        if self.timezone is not None:
            export_timezone(self.timezone)
        return self


class TickDatasetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    symbol: str = Field(min_length=1)
    source: str = Field(min_length=1)
    price_type: Literal["bid_ask"] = "bid_ask"


class TickManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    data_kind: Literal["ticks"] = "ticks"
    dataset_id: str
    spec: TickDatasetSpec
    profile: TickCsvProfile
    source_file: str
    source_sha256: str
    parquet_sha256: str
    created_at: datetime
    rows: int = Field(gt=0)
    first_timestamp: datetime
    last_timestamp: datetime
    equal_timestamp_records: int = Field(ge=0)
    max_interval_seconds: float = Field(ge=0)


TICK_SCHEMA = pa.schema([
    ("sequence", pa.int64()), ("timestamp", pa.timestamp("us", tz="UTC")),
    *[(name, pa.float64()) for name in ("bid", "ask", "volume")],
])


def import_ticks(source: Path | str, output: Path | str, spec: TickDatasetSpec,
                 profile: TickCsvProfile, *, batch_size: int = 65_536) -> TickManifest:
    source, output = Path(source), Path(output)
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if output.exists():
        raise FileExistsError(f"dataset already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".tick-import-", dir=output.parent) as temporary:
        snapshot = Path(temporary) / "source.csv"
        with source.open("rb") as origin, snapshot.open("wb") as destination:
            shutil.copyfileobj(origin, destination)
        source_hash = file_sha256(snapshot)
        parquet = Path(temporary) / "ticks.parquet"
        first = previous = None
        rows, same_time, maximum = 0, 0, 0.0
        batch = []
        with snapshot.open(newline="", encoding=profile.encoding) as incoming, pq.ParquetWriter(
            parquet, TICK_SCHEMA, compression="zstd"
        ) as writer:
            reader = csv.DictReader(incoming, delimiter=profile.delimiter, fieldnames=profile.fieldnames)
            headers = reader.fieldnames or []
            required = {value for key, value in profile.columns.items() if key != "volume"}
            if len(headers) != len(set(headers)) or not required <= set(headers):
                raise ValueError("CSV must contain mapped timestamp, bid, ask columns with unique headers")
            for row in reader:
                try:
                    if None in row or any(row.get(key) is None for key in required):
                        raise ValueError("row field count does not match its header")
                    stamp = _timestamp(row, profile)
                    def number(name, default=None):
                        column = profile.columns.get(name)
                        text = row.get(column) if column else None
                        if (text is None or text.strip() == "") and default is not None:
                            return default
                        return float(text.strip().replace(profile.decimal_separator, "."))
                    tick = Tick(stamp, number("bid"), number("ask"), number("volume", 0.0), rows)
                    if previous is not None:
                        interval = (stamp - previous).total_seconds()
                        if interval < 0:
                            raise ValueError("tick timestamps must be nondecreasing; preserve source order")
                        same_time += interval == 0
                        maximum = max(maximum, interval)
                    first = first or stamp
                    previous = stamp
                    batch.append({name: getattr(tick, name) for name in TICK_SCHEMA.names})
                    rows += 1
                    if len(batch) >= batch_size:
                        writer.write_table(pa.Table.from_pylist(batch, schema=TICK_SCHEMA))
                        batch.clear()
                except (ValueError, TypeError, AttributeError, KeyError) as exc:
                    raise ValueError(f"CSV line {reader.line_num}: {exc}") from exc
            if batch:
                writer.write_table(pa.Table.from_pylist(batch, schema=TICK_SCHEMA))
        if not rows:
            raise ValueError("CSV contains no ticks")
        parquet_hash = file_sha256(parquet)
        identity = json.dumps({"parquet": parquet_hash, "spec": spec.model_dump(), "data_kind": "ticks"}, sort_keys=True).encode()
        manifest = TickManifest(dataset_id=hashlib.sha256(identity).hexdigest(), spec=spec, profile=profile,
                                source_file=source.name, source_sha256=source_hash, parquet_sha256=parquet_hash,
                                created_at=datetime.now(timezone.utc), rows=rows, first_timestamp=first,
                                last_timestamp=previous, equal_timestamp_records=same_time, max_interval_seconds=maximum)
        output.mkdir(exist_ok=False)
        try:
            shutil.copyfile(parquet, output / "ticks.parquet")
            (output / "manifest.json").write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
        except BaseException:
            shutil.rmtree(output)
            raise
    return manifest


def inspect_ticks(path: Path | str) -> TickManifest:
    path = Path(path)
    manifest = TickManifest.model_validate_json((path / "manifest.json").read_text(encoding="utf-8"))
    if file_sha256(path / "ticks.parquet") != manifest.parquet_sha256:
        raise ValueError("dataset checksum mismatch: ticks.parquet has changed")
    identity = json.dumps({"parquet": manifest.parquet_sha256, "spec": manifest.spec.model_dump(), "data_kind": "ticks"}, sort_keys=True).encode()
    if manifest.dataset_id != hashlib.sha256(identity).hexdigest():
        raise ValueError("dataset identity does not match its quotes/specification")
    footer = pq.read_metadata(path / "ticks.parquet")
    if footer.num_rows != manifest.rows or not footer.schema.to_arrow_schema().equals(TICK_SCHEMA):
        raise ValueError("tick dataset schema/row count does not match its manifest")
    return manifest


def is_tick_dataset(path: Path | str) -> bool:
    manifest = json.loads((Path(path) / "manifest.json").read_text(encoding="utf-8"))
    return manifest.get("data_kind") == "ticks"


def load_ticks(path: Path | str, start: datetime | None = None, end: datetime | None = None,
               *, tail: int | None = None) -> tuple[Tick, ...]:
    path = Path(path)
    inspect_ticks(path)
    if start is not None and end is not None and start >= end:
        raise ValueError("period start must be earlier than end")
    if tail is not None and tail < 1:
        raise ValueError("tail must be positive")
    conditions, parameters = [], [str(path / "ticks.parquet")]
    for boundary, operator in ((start, ">="), (end, "<")):
        if boundary is not None:
            if boundary.utcoffset() is None:
                raise ValueError("period boundaries must be timezone-aware")
            conditions.append(f"timestamp {operator} ?")
            parameters.append(boundary)
    query = "SELECT timestamp AT TIME ZONE 'UTC', bid, ask, volume, sequence FROM read_parquet(?)"
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY sequence DESC" if tail is not None else " ORDER BY sequence"
    if tail is not None:
        query += " LIMIT ?"
        parameters.append(tail)
    ticks = []
    with duckdb.connect() as connection:
        cursor = connection.execute(query, parameters)
        while batch := cursor.fetchmany(65_536):
            for stamp, bid, ask, volume, sequence in batch:
                ticks.append(Tick(stamp.replace(tzinfo=timezone.utc), bid, ask, volume, sequence))
    if not ticks:
        raise EmptyPeriodError("selected period contains no ticks")
    if tail is not None:
        ticks.reverse()
    return tuple(ticks)
