from __future__ import annotations

from collections import defaultdict
import csv
from dataclasses import asdict, fields
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import shutil

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .models import BacktestResult
from .datasets import export_timezone


CHART_MAX_POINTS = 5_000


def _record(item):
    # Report rows contain immutable scalars/datetimes; avoid asdict's deepcopy.
    return {field.name: getattr(item, field.name) for field in fields(item)}


def _encode(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _write_json_report(result, path, metadata, summary):
    """Keep all equity rows without constructing a second copy of the run."""
    header = {"schema_version": 1, "metadata": metadata,
              "config": asdict(result.config), "summary": summary}
    with path.open("w", encoding="utf-8") as destination:
        destination.write("{\n")
        for name, value in header.items():
            destination.write(json.dumps(name) + ": ")
            json.dump(value, destination, default=_encode, allow_nan=False)
            destination.write(",\n")
        arrays = (("fills", result.fills, _record), ("equity", result.equity, _record),
                  ("events", result.events, lambda event: event))
        for array_index, (name, rows, convert) in enumerate(arrays):
            destination.write(json.dumps(name) + ": [\n")
            for index, item in enumerate(rows):
                if index:
                    destination.write(",\n")
                destination.write(json.dumps(convert(item), default=_encode, allow_nan=False))
            destination.write("\n]")
            destination.write(",\n" if array_index < len(arrays) - 1 else "\n")
        destination.write("}\n")


def _chart_series(result, max_points=CHART_MAX_POINTS):
    """Select chart points while computing drawdown on every original tick.

    Each bucket retains its equity min/max and worst drawdown, in source order.
    The first/last points are always retained. Exported data is never sampled.
    """
    if max_points < 5:
        raise ValueError("chart max_points must be at least 5")
    count = len(result.equity)
    width = 1 if count <= max_points else math.ceil(count / ((max_points - 2) // 3))
    selected = {}
    low = high = worst = None
    peak = result.config.initial_cash

    def flush():
        if low is not None:
            for item in (low, high, worst):
                selected[item[0]] = item[1:]

    for index, point in enumerate(result.equity):
        peak = max(peak, point.equity)
        item = (index, point.timestamp, point.equity, (point.equity / peak - 1) * 100)
        if index % width == 0:
            flush()
            low = high = worst = item
        else:
            if item[2] < low[2]:
                low = item
            if item[2] > high[2]:
                high = item
            if item[3] < worst[3]:
                worst = item
        if index == 0 or index == count - 1:
            selected[index] = item[1:]
    flush()
    return [selected[index] for index in sorted(selected)]


def daily_pnl(result: BacktestResult, day_timezone: str = "UTC") -> list[tuple[str, float]]:
    changes: dict[str, list[float]] = defaultdict(list)
    previous = result.config.initial_cash
    zone = export_timezone(day_timezone)
    for point in result.equity:
        day = point.timestamp.astimezone(zone).date().isoformat()
        changes[day].append(point.equity - previous)
        previous = point.equity
    return [(day, math.fsum(values)) for day, values in changes.items()]


def write_report(result: BacktestResult, output: Path | str, metadata: dict | None = None) -> Path:
    """Create a new report directory; existing outputs are never overwritten."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    try:
        _write_report_files(result, output, metadata)
    except BaseException:
        shutil.rmtree(output)
        raise
    return output


def _write_report_files(result: BacktestResult, output: Path, metadata: dict | None) -> None:
    summary = result.summary()
    chart = _chart_series(result)
    report_metadata = dict(metadata or {})
    server_timezone = (result.execution_metadata or {}).get("broker", {}).get("server_timezone")
    server_zone = export_timezone(server_timezone) if server_timezone else None
    if server_zone is not None:
        report_metadata["report_time_basis"] = {
            "canonical_timestamps": "UTC", "server_timezone": server_timezone,
            "chart": "broker server wall clock; hover also includes UTC",
            "daily_pnl.csv": "UTC equity changes", "daily_pnl_server.csv": "broker server day equity changes",
        }
    report_metadata["chart_sampling"] = {
        "source_points": len(result.equity), "rendered_points": len(chart),
        "max_points": CHART_MAX_POINTS,
        "method": "bucket equity min/max and worst full-resolution drawdown; first/last retained",
        "data_exports": "all original equity points retained in result.json and equity.csv",
    }
    _write_json_report(result, output / "result.json", report_metadata, summary)
    for name, rows, fields in (
        ("equity.csv", result.equity, ("timestamp", "cash", "quantity", "equity")),
        ("fills.csv", result.fills, ("timestamp", "signal_timestamp", "side", "quantity", "reference_price", "price", "commission")),
    ):
        if result.data_kind == "ticks":
            fields = (*fields, "tick_sequence")
            if name == "fills.csv":
                fields = (*fields, "signal_tick_sequence")
        if result.execution_metadata and name == "fills.csv":
            fields = (*fields, "reason", "order_id", "position_id", "quantity_unit")
        if server_zone is not None and name == "fills.csv":
            fields = (*fields, "server_time", "signal_server_time")
        with (output / name).open("w", newline="", encoding="utf-8") as destination:
            writer = csv.DictWriter(destination, fieldnames=fields)
            writer.writeheader()
            for item in rows:
                values = _record(item)
                if server_zone is not None and name == "fills.csv":
                    values["server_time"] = item.timestamp.astimezone(server_zone)
                    values["signal_server_time"] = item.signal_timestamp.astimezone(server_zone)
                writer.writerow({key: values[key].isoformat() if isinstance(values[key], datetime) else values[key] for key in fields})
    with (output / "daily_pnl.csv").open("w", newline="", encoding="utf-8") as destination:
        writer = csv.writer(destination)
        writer.writerow(("date", "net_pnl"))
        writer.writerows(daily_pnl(result))
    if server_zone is not None:
        with (output / "daily_pnl_server.csv").open("w", newline="", encoding="utf-8") as destination:
            writer = csv.writer(destination)
            writer.writerow(("date", "net_pnl"))
            writer.writerows(daily_pnl(result, server_timezone))
        with (output / "events.csv").open("w", newline="", encoding="utf-8") as destination:
            writer = csv.writer(destination)
            writer.writerow(("timestamp", "server_time", "server_day", "event", "tick_sequence", "details_json"))
            for event in result.events:
                server_time = event["timestamp"].astimezone(server_zone)
                writer.writerow((event["timestamp"].isoformat(), server_time.isoformat(), server_time.date().isoformat(),
                                 event["event"], event["tick_sequence"], json.dumps(event, default=_encode, allow_nan=False)))
    timestamps = [point[0] for point in chart]
    if server_zone is not None:
        # Plotly normalizes offset-aware dates; pass explicit wall-clock labels
        # and retain true UTC instants in hover data instead of shifting data.
        timestamps = [stamp.astimezone(server_zone).replace(tzinfo=None).isoformat() for stamp in timestamps]
    utc_hover = [point[0].astimezone(timezone.utc).isoformat() for point in chart]
    values = [point[1] for point in chart]
    drawdowns = [point[2] for point in chart]
    figure = make_subplots(rows=2, cols=1, shared_xaxes=True, subplot_titles=("Account equity", "Drawdown (%)"))
    hover = "%{x}<br>%{y}<br>UTC: %{customdata}<extra>%{fullData.name}</extra>"
    figure.add_trace(go.Scatter(x=timestamps, y=values, name="Equity", customdata=utc_hover, hovertemplate=hover), row=1, col=1)
    figure.add_trace(go.Scatter(x=timestamps, y=drawdowns, name="Drawdown", fill="tozeroy",
                               customdata=utc_hover, hovertemplate=hover), row=2, col=1)
    figure.update_xaxes(title_text=f"Broker server time ({server_timezone})" if server_zone is not None else "Time (UTC)", row=2, col=1)
    figure.update_layout(title=f"Backtest: return {summary['total_return']:.2%}, max drawdown {summary['max_drawdown']:.2%}",
                         template="plotly_white", height=750)
    if len(chart) < len(result.equity):
        figure.add_annotation(text=f"Chart: {len(chart):,} selected points from {len(result.equity):,}; exports contain every point",
                              xref="paper", yref="paper", x=0, y=1.12, showarrow=False)
    figure.write_html(output / "report.html", include_plotlyjs=True, full_html=True)
