from __future__ import annotations

import argparse
import csv
import json
from importlib.metadata import version
from pathlib import Path
import sqlite3
import sys
from zoneinfo import ZoneInfoNotFoundError

import duckdb

from .datasets import CsvProfile, DatasetSpec, import_csv, inspect_dataset, parse_boundary
from .experiments import ExperimentStore, RunRequest, evaluate, execute_experiment
from .models import BacktestConfig
from .tick_data import TickCsvProfile, TickDatasetSpec, import_ticks, inspect_ticks, is_tick_dataset


def _run_arguments(parser):
    parser.add_argument('--data', required=True, type=Path, help='Canonical CSV or imported dataset directory')
    parser.add_argument('--strategy', default='sma', help='sma, tick-sma, /path/strategy.py[:factory] or module:factory')
    parser.add_argument('--params', default='{}', help='JSON keyword parameters or @/path/parameters.json')
    parser.add_argument('--initial-cash', type=float, default=10_000)
    parser.add_argument('--commission-bps', type=float, default=0)
    parser.add_argument('--spread-bps', type=float, default=0)
    parser.add_argument('--slippage-bps', type=float, default=0)
    parser.add_argument('--price-type', choices=('mid', 'bid', 'ask', 'last'), default='mid',
                        help='Reference price for canonical CSV; imported datasets use their manifest')
    parser.add_argument('--start', help='Inclusive UTC date or timezone-aware ISO timestamp')
    parser.add_argument('--end', help='Exclusive UTC date or timezone-aware ISO timestamp')
    parser.add_argument('--warmup-bars', '--warmup-ticks', dest='warmup_bars', type=int, default=0,
                        help='Prior observations used to initialize strategy state without trading')
    parser.add_argument('--output', required=True, type=Path, help='New output directory')
    parser.add_argument('--registry', type=Path, help='SQLite journal; defaults to output parent/experiments.sqlite3')


def _request(args):
    raw = Path(args.params[1:]).expanduser().read_text(encoding='utf-8') if args.params.startswith('@') else args.params
    parameters = json.loads(raw)
    if not isinstance(parameters, dict):
        raise ValueError('--params must be a JSON object')
    return RunRequest(data=args.data, output=args.output, strategy=args.strategy, parameters=parameters,
                      config=BacktestConfig(args.initial_cash, args.commission_bps, args.spread_bps, args.slippage_bps, args.price_type),
                      start=parse_boundary(args.start), end=parse_boundary(args.end), warmup_bars=args.warmup_bars)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    # Preserve v0.1 usage: backtester --data ... is equivalent to backtester run --data ...
    if arguments and arguments[0].startswith('--') and arguments[0] not in ('--help', '--version'):
        arguments.insert(0, 'run')
    parser = argparse.ArgumentParser(description='Historical data import and reproducible Python backtesting')
    parser.add_argument('--version', action='version', version=f'backtester {version("mishadev-backtester")}')
    subparsers = parser.add_subparsers(dest='command', required=True)
    run_parser = subparsers.add_parser('run', help='Run one tracked experiment')
    _run_arguments(run_parser)
    evaluation_parser = subparsers.add_parser('evaluate', help='Evaluate fixed parameters on chronological train/validation/test')
    _run_arguments(evaluation_parser)
    evaluation_parser.add_argument('--train-end', required=True)
    evaluation_parser.add_argument('--validation-end', required=True)
    import_parser = subparsers.add_parser('import-data', help='Import CSV bars into an immutable Parquet dataset')
    import_parser.add_argument('--input', required=True, type=Path)
    import_parser.add_argument('--output', required=True, type=Path)
    import_parser.add_argument('--profile', type=Path, help='CSV mapping/timezone JSON; default is canonical OHLCV')
    import_parser.add_argument('--symbol', required=True)
    import_parser.add_argument('--timeframe', required=True)
    import_parser.add_argument('--source', required=True)
    import_parser.add_argument('--price-type', required=True, choices=('mid', 'bid', 'ask', 'last'))
    import_parser.add_argument('--timestamp-convention', default='bar_open', choices=('bar_open', 'bar_close'))
    tick_parser = subparsers.add_parser('import-ticks', help='Import every bid/ask quote without aggregation')
    tick_parser.add_argument('--input', required=True, type=Path)
    tick_parser.add_argument('--output', required=True, type=Path)
    tick_parser.add_argument('--profile', type=Path, help='Tick CSV mapping/timezone JSON')
    tick_parser.add_argument('--timezone', help='Explicit IANA zone or UTC+03:00; overrides profile timezone')
    tick_parser.add_argument('--symbol', required=True)
    tick_parser.add_argument('--source', default='QuantDataManager')
    inspect_parser = subparsers.add_parser('inspect-data', help='Verify dataset integrity and display its manifest')
    inspect_parser.add_argument('path', type=Path)
    journal_parser = subparsers.add_parser('experiments', help='List recent experiment records')
    journal_parser.add_argument('--registry', required=True, type=Path)
    journal_parser.add_argument('--limit', type=int, default=20)
    args = parser.parse_args(arguments)
    try:
        if args.command == 'import-data':
            profile = CsvProfile.model_validate_json(args.profile.read_text(encoding='utf-8')) if args.profile else CsvProfile()
            spec = DatasetSpec(symbol=args.symbol, timeframe=args.timeframe, source=args.source,
                               price_type=args.price_type, timestamp_convention=args.timestamp_convention)
            document = import_csv(args.input, args.output, spec, profile).model_dump(mode='json')
        elif args.command == 'import-ticks':
            profile = TickCsvProfile.model_validate_json(args.profile.read_text(encoding='utf-8')) if args.profile else TickCsvProfile()
            if args.timezone:
                profile = TickCsvProfile.model_validate({**profile.model_dump(), 'timezone': args.timezone})
            spec = TickDatasetSpec(symbol=args.symbol, source=args.source)
            document = import_ticks(args.input, args.output, spec, profile).model_dump(mode='json')
        elif args.command == 'inspect-data':
            manifest = inspect_ticks(args.path) if is_tick_dataset(args.path) else inspect_dataset(args.path)
            document = manifest.model_dump(mode='json')
        elif args.command == 'experiments':
            if not args.registry.is_file():
                raise ValueError('experiment journal does not exist')
            document = ExperimentStore(args.registry).list(args.limit)
        elif args.command == 'evaluate':
            document = evaluate(_request(args), parse_boundary(args.train_end), parse_boundary(args.validation_end), args.registry)
        else:
            result, _ = execute_experiment(_request(args), args.registry)
            document = result.summary()
    except (ValueError, TypeError, OSError, ImportError, AttributeError, SyntaxError, ZoneInfoNotFoundError, sqlite3.Error, csv.Error, duckdb.Error) as exc:
        parser.error(str(exc))
    print(json.dumps(document, allow_nan=False, indent=2))
    return 0
