"""Validated, immutable bar datasets imported from configurable CSV exports."""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
from typing import Literal
from zoneinfo import ZoneInfo

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import Bar


class EmptyPeriodError(ValueError):
    """A valid dataset has no observations in the requested interval."""


class CsvProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    delimiter: str = ","
    decimal_separator: Literal[".", ","] = "."
    encoding: str = "utf-8-sig"
    fieldnames: list[str] | None = None
    columns: dict[str, str] = Field(default_factory=lambda: {
        "timestamp": "timestamp", "open": "open", "high": "high", "low": "low", "close": "close",
        "volume": "volume",
    })
    timestamp_format: str | None = None
    timezone: str | None = None

    @model_validator(mode="after")
    def validate_profile(self):
        if len(self.delimiter) != 1 or self.delimiter in "\r\n":
            raise ValueError("delimiter must be one non-newline character")
        allowed = {"timestamp", "date", "time", "open", "high", "low", "close", "volume"}
        if set(self.columns) - allowed or not {"open", "high", "low", "close"} <= self.columns.keys():
            raise ValueError("columns must map OHLC and optional timestamp/date/time/volume fields")
        if ("timestamp" in self.columns) == ("date" in self.columns):
            raise ValueError("map either timestamp or date (with optional time)")
        if "timestamp" in self.columns and "time" in self.columns:
            raise ValueError("time must be paired with date")
        if any(not value for value in self.columns.values()) or len(set(self.columns.values())) != len(self.columns):
            raise ValueError("source column names must be nonempty and unique")
        if self.fieldnames is not None and (not self.fieldnames or len(set(self.fieldnames)) != len(self.fieldnames)):
            raise ValueError("headerless fieldnames must be nonempty and unique")
        if self.timezone is not None:
            export_timezone(self.timezone)
        return self


class DatasetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    symbol: str = Field(min_length=1)
    timeframe: str
    source: str = Field(min_length=1)
    price_type: Literal["mid", "bid", "ask", "last"]
    timestamp_convention: Literal["bar_open", "bar_close"] = "bar_open"

    @field_validator("timeframe")
    @classmethod
    def validate_timeframe(cls, value):
        value = value.upper()
        if not re.fullmatch(r"[MHD][1-9][0-9]*", value):
            raise ValueError("timeframe must be fixed bars such as M1, M15, H1, or D1")
        return value

    @property
    def seconds(self):
        return {"M": 60, "H": 3600, "D": 86400}[self.timeframe[0]] * int(self.timeframe[1:])


class DatasetManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    dataset_id: str
    spec: DatasetSpec
    profile: CsvProfile
    source_file: str
    source_sha256: str
    parquet_sha256: str
    created_at: datetime
    rows: int = Field(gt=0)
    first_timestamp: datetime
    last_timestamp: datetime
    gap_intervals: int = Field(ge=0)
    short_intervals: int = Field(ge=0)
    max_interval_seconds: float = Field(ge=0)


BAR_SCHEMA = pa.schema([
    ("timestamp", pa.timestamp("us", tz="UTC")),
    *[(name, pa.float64()) for name in ("open", "high", "low", "close", "volume")],
])


def file_sha256(path: Path | str) -> str:
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def parse_boundary(value: str | None) -> datetime | None:
    if value is None:
        return None
    stamp = datetime.fromisoformat(value)
    if len(value) == 10:
        stamp = stamp.replace(tzinfo=timezone.utc)
    if stamp.utcoffset() is None:
        raise ValueError("period boundary needs an explicit timezone; date-only boundaries mean UTC midnight")
    return stamp.astimezone(timezone.utc)


def export_timezone(zone: str):
    offset = re.fullmatch(r"UTC([+-])(\d{2}):(\d{2})", zone)
    if offset:
        hours, minutes = int(offset[2]), int(offset[3])
        if hours > 23 or minutes > 59:
            raise ValueError("invalid fixed UTC offset")
        delta = timedelta(hours=hours, minutes=minutes)
        return timezone(delta if offset[1] == "+" else -delta)
    return ZoneInfo(zone)


def _localize(stamp: datetime, zone: str) -> datetime:
    timezone_info = export_timezone(zone)
    candidates = [stamp.replace(tzinfo=timezone_info, fold=fold) for fold in (0, 1)]
    valid = [candidate for candidate in candidates
             if candidate.astimezone(timezone.utc).astimezone(timezone_info).replace(tzinfo=None) == stamp]
    if not valid:
        raise ValueError("nonexistent local time during a DST transition; export UTC timestamps")
    if len({candidate.utcoffset() for candidate in valid}) > 1:
        raise ValueError("ambiguous local time during a DST transition; export UTC timestamps")
    return valid[0].astimezone(timezone.utc)


def _timestamp(row: dict, profile: CsvProfile) -> datetime:
    if "timestamp" in profile.columns:
        value = row[profile.columns["timestamp"]].strip()
    else:
        value = row[profile.columns["date"]].strip()
        if "time" in profile.columns:
            value += " " + row[profile.columns["time"]].strip()
    stamp = (datetime.strptime(value, profile.timestamp_format) if profile.timestamp_format
             else datetime.fromisoformat(value))
    if stamp.utcoffset() is not None:
        return stamp.astimezone(timezone.utc)
    if profile.timezone is not None:
        return _localize(stamp, profile.timezone)
    # Only the canonical date-only format has an implicit UTC timezone.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return stamp.replace(tzinfo=timezone.utc)
    raise ValueError("naive export timestamps require an explicit profile timezone")


def import_csv(source: Path | str, output: Path | str, spec: DatasetSpec,
               profile: CsvProfile | None = None, *, batch_size: int = 65_536) -> DatasetManifest:
    source, output = Path(source), Path(output)
    profile = profile or CsvProfile()
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if output.exists():
        raise FileExistsError(f"dataset already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Parse an immutable copy, so the recorded source hash describes the exact bytes read.
    with tempfile.TemporaryDirectory(prefix=".dataset-import-", dir=output.parent) as temporary:
        snapshot = Path(temporary) / "source.csv"
        with source.open("rb") as origin, snapshot.open("wb") as destination:
            shutil.copyfileobj(origin, destination)
        source_hash = file_sha256(snapshot)
        parquet = Path(temporary) / "bars.parquet"
        rows, gap_intervals, short_intervals, maximum = 0, 0, 0, 0.0
        first = previous = None
        batch = []
        with snapshot.open(newline="", encoding=profile.encoding) as incoming, pq.ParquetWriter(
            parquet, BAR_SCHEMA, compression="zstd"
        ) as writer:
            reader = csv.DictReader(incoming, delimiter=profile.delimiter, fieldnames=profile.fieldnames)
            headers = reader.fieldnames or []
            required = {value for key, value in profile.columns.items() if key != "volume"}
            if len(headers) != len(set(headers)) or not required <= set(headers):
                raise ValueError("CSV headers must be unique and contain all mapped price/time columns")
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
                    bar = Bar(stamp, *(number(name) for name in ("open", "high", "low", "close")), number("volume", 0.0))
                    if previous is not None:
                        interval = (stamp - previous).total_seconds()
                        if interval <= 0:
                            raise ValueError("timestamps must be strictly increasing and unique")
                        maximum = max(maximum, interval)
                        gap_intervals += interval > spec.seconds
                        short_intervals += interval < spec.seconds
                    first = first or stamp
                    previous = stamp
                    batch.append({name: getattr(bar, name) for name in BAR_SCHEMA.names})
                    rows += 1
                    if len(batch) >= batch_size:
                        writer.write_table(pa.Table.from_pylist(batch, schema=BAR_SCHEMA))
                        batch.clear()
                except (ValueError, TypeError, AttributeError, KeyError) as exc:
                    raise ValueError(f"CSV line {reader.line_num}: {exc}") from exc
            if batch:
                writer.write_table(pa.Table.from_pylist(batch, schema=BAR_SCHEMA))
        if not rows:
            raise ValueError("CSV contains no price bars")
        parquet_hash = file_sha256(parquet)
        identity = json.dumps({"parquet": parquet_hash, "spec": spec.model_dump()}, sort_keys=True).encode()
        manifest = DatasetManifest(
            dataset_id=hashlib.sha256(identity).hexdigest(), spec=spec, profile=profile,
            source_file=source.name, source_sha256=source_hash, parquet_sha256=parquet_hash,
            created_at=datetime.now(timezone.utc), rows=rows, first_timestamp=first, last_timestamp=previous,
            gap_intervals=gap_intervals, short_intervals=short_intervals, max_interval_seconds=maximum,
        )
        # Reserve this directory atomically; a failed import never leaves a published dataset.
        output.mkdir(exist_ok=False)
        try:
            shutil.copyfile(parquet, output / "bars.parquet")
            (output / "manifest.json").write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
        except BaseException:
            shutil.rmtree(output)
            raise
    return manifest


def inspect_dataset(path: Path | str) -> DatasetManifest:
    path = Path(path)
    manifest = DatasetManifest.model_validate_json((path / "manifest.json").read_text(encoding="utf-8"))
    if file_sha256(path / "bars.parquet") != manifest.parquet_sha256:
        raise ValueError("dataset checksum mismatch: bars.parquet has changed")
    identity = json.dumps({"parquet": manifest.parquet_sha256, "spec": manifest.spec.model_dump()}, sort_keys=True).encode()
    if manifest.dataset_id != hashlib.sha256(identity).hexdigest():
        raise ValueError("dataset identity does not match its data/specification")
    footer = pq.read_metadata(path / "bars.parquet")
    if footer.num_rows != manifest.rows or not footer.schema.to_arrow_schema().equals(BAR_SCHEMA):
        raise ValueError("dataset schema/row count does not match its manifest")
    return manifest


def load_dataset(path: Path | str, start: datetime | None = None, end: datetime | None = None,
                 *, tail: int | None = None) -> tuple[Bar, ...]:
    path = Path(path)
    manifest = inspect_dataset(path)
    if start is not None and end is not None and start >= end:
        raise ValueError("period start must be earlier than end")
    if tail is not None and tail < 1:
        raise ValueError("tail must be positive")
    conditions, parameters = [], [str(path / "bars.parquet")]
    for boundary, operator in ((start, ">="), (end, "<")):
        if boundary is not None:
            if boundary.utcoffset() is None:
                raise ValueError("period boundaries must be timezone-aware")
            conditions.append(f"timestamp {operator} ?")
            parameters.append(boundary)
    # Normalize in SQL before Python conversion; this avoids a hidden pytz dependency
    # in DuckDB's TIMESTAMPTZ -> datetime converter (pandas 3 no longer installs pytz).
    query = "SELECT timestamp AT TIME ZONE 'UTC', open, high, low, close, volume FROM read_parquet(?)"
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY timestamp DESC" if tail is not None else " ORDER BY timestamp"
    if tail is not None:
        query += " LIMIT ?"
        parameters.append(tail)
    bars = []
    with duckdb.connect() as connection:
        cursor = connection.execute(query, parameters)
        while batch := cursor.fetchmany(65_536):
            for stamp, opening, high, low, close, volume in batch:
                stamp = stamp.replace(tzinfo=timezone.utc)
                duration = timedelta(seconds=manifest.spec.seconds)
                if manifest.spec.timestamp_convention == "bar_open":
                    open_stamp, close_stamp = stamp, stamp + duration
                else:
                    open_stamp, close_stamp = stamp - duration, stamp
                bars.append(Bar(stamp, opening, high, low, close, volume, open_stamp, close_stamp))
    if not bars:
        raise EmptyPeriodError("selected period contains no price bars")
    if tail is not None:
        bars.reverse()
    return tuple(bars)
