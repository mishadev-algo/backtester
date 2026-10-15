from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from .models import Bar


def load_csv(path: Path | str) -> tuple[Bar, ...]:
    """Load ISO timestamps or YYYY-MM-DD dates; date-only rows mean UTC midnight."""
    bars: list[Bar] = []
    with Path(path).open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        required = {"timestamp", "open", "high", "low", "close"}
        if reader.fieldnames is None or not required <= set(reader.fieldnames):
            raise ValueError("CSV requires timestamp,open,high,low,close columns; volume is optional")
        if len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError("CSV column names must be unique")
        for row in reader:
            try:
                if None in row:
                    raise ValueError("row has more fields than the header")
                stamp = row["timestamp"].strip()
                timestamp = datetime.fromisoformat(stamp)
                if len(stamp) == 10:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                if timestamp.utcoffset() is None:
                    raise ValueError("intraday timestamps require a UTC offset or Z")
                timestamp = timestamp.astimezone(timezone.utc)
                prices = {name: float(row[name]) for name in ("open", "high", "low", "close")}
                bar = Bar(timestamp, **prices, volume=float(row.get("volume") or 0))
                if bars and bar.timestamp <= bars[-1].timestamp:
                    raise ValueError("timestamps must be strictly increasing and unique")
                bars.append(bar)
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError(f"CSV line {reader.line_num}: {exc}") from exc
    if not bars:
        raise ValueError("CSV contains no price bars")
    return tuple(bars)
