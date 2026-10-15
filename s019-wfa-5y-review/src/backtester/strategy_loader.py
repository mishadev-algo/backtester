"""Load strategy factories from modules or standalone Python source files."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import importlib
import importlib.util
from pathlib import Path
import sys
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class LoadedStrategy:
    spec: str
    factory: Callable
    source_sha256: str | None
    source_path: Path | None


def _file_reference(spec: str) -> tuple[Path, str] | None:
    path, separator, name = spec.rpartition(":")
    if separator and path.endswith(".py"):
        return Path(path).expanduser().resolve(), name
    if spec.endswith(".py"):
        return Path(spec).expanduser().resolve(), "create_strategy"
    return None


@contextmanager
def load_strategy(spec: str, data_kind: str = "bars") -> Iterator[LoadedStrategy]:
    reference = _file_reference(spec)
    if reference is not None:
        path, name = reference
        if not name.isidentifier():
            raise ValueError("strategy file entry point must be a Python identifier")
        source = path.read_bytes()
        source_hash = hashlib.sha256(source).hexdigest()
        module_name = f"_backtester_strategy_{uuid4().hex}"
        module_spec = importlib.util.spec_from_file_location(module_name, path)
        if module_spec is None:
            raise ValueError(f"cannot load Python strategy file: {path}")
        module = importlib.util.module_from_spec(module_spec)
        # Dataclass/type introspection needs the module registered during execution.
        sys.modules[module_name] = module
        try:
            # Compile exactly the bytes we hashed, avoiding stale .pyc files on reruns.
            exec(compile(source, str(path), "exec"), module.__dict__)
            factory = getattr(module, name, None)
            if not callable(factory):
                raise ValueError(f"strategy file must define callable {name!r}: {path}")
            yield LoadedStrategy(f"{path}:{name}", factory, source_hash, path)
        finally:
            sys.modules.pop(module_name, None)
        return

    if spec == "sma":
        spec = "backtester.strategies:create_tick_strategy" if data_kind == "ticks" else "backtester.strategies:create_strategy"
    elif spec == "tick-sma":
        spec = "backtester.strategies:create_tick_strategy"
    module_name, separator, name = spec.partition(":")
    if not separator or not module_name or not name:
        raise ValueError("strategy must be sma, tick-sma, path.py[:factory], or module:factory")
    module = importlib.import_module(module_name)
    factory = getattr(module, name)
    if not callable(factory):
        raise ValueError("strategy factory must be callable")
    path = Path(module.__file__).resolve() if getattr(module, "__file__", None) else None
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest() if path and path.is_file() else None
    yield LoadedStrategy(spec, factory, source_hash, path)
