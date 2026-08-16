"""Small operational primitives for observability and recoverability."""

from __future__ import annotations

import shutil
import sqlite3
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_LOW_CARDINALITY_LABELS = frozenset(
    {
        "agent_ref",
        "capability",
        "decision",
        "delivery_kind",
        "effect",
        "entity_type",
        "error_code",
        "event_type",
        "retryable",
        "runtime",
        "skill_ref",
        "status",
    }
)


@dataclass(frozen=True, slots=True)
class MetricSample:
    name: str
    kind: str
    labels: tuple[tuple[str, str], ...]
    value: float
    count: int = 0


class Metrics:
    def __init__(self) -> None:
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], int] = defaultdict(int)
        self._totals: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._counts: dict[tuple[str, tuple[tuple[str, str], ...]], int] = defaultdict(int)
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._lock = threading.RLock()

    def increment(self, name: str, **labels: str) -> None:
        key = (name, self._labels(labels))
        with self._lock:
            self._counters[key] += 1

    def value(self, name: str, **labels: str) -> int:
        with self._lock:
            return self._counters[(name, self._labels(labels))]

    def observe(self, name: str, value: float, **labels: str) -> None:
        if value < 0:
            raise ValueError("metric observations cannot be negative")
        key = (name, self._labels(labels))
        with self._lock:
            self._totals[key] += value
            self._counts[key] += 1

    def set_gauge(self, name: str, value: float, **labels: str) -> None:
        with self._lock:
            self._gauges[(name, self._labels(labels))] = value

    def snapshot(self) -> tuple[MetricSample, ...]:
        with self._lock:
            samples = [
                MetricSample(name, "counter", labels, float(value))
                for (name, labels), value in self._counters.items()
            ]
            samples.extend(
                MetricSample(name, "histogram", labels, total, self._counts[(name, labels)])
                for (name, labels), total in self._totals.items()
            )
            samples.extend(
                MetricSample(name, "gauge", labels, value)
                for (name, labels), value in self._gauges.items()
            )
        return tuple(sorted(samples, key=lambda item: (item.name, item.kind, item.labels)))

    @staticmethod
    def _labels(labels: dict[str, str]) -> tuple[tuple[str, str], ...]:
        forbidden = set(labels) - _LOW_CARDINALITY_LABELS
        if forbidden:
            names = ", ".join(sorted(forbidden))
            raise ValueError(f"high-cardinality metric labels are forbidden: {names}")
        return tuple(sorted((key, str(value)) for key, value in labels.items()))


class TraceExporter(Protocol):
    """Optional exporter boundary; implementations receive content-free attributes only."""

    def record(self, name: str, attributes: dict[str, str | int | float]) -> None: ...


class SafeTrace:
    _ALLOWED_ATTRIBUTES = frozenset(
        {
            "agent_ref",
            "capability",
            "duration_ms",
            "error_code",
            "parent_span_id",
            "run_id",
            "scope_id",
            "skill_ref",
            "span_id",
            "status",
            "trace_id",
        }
    )

    def __init__(self, exporter: TraceExporter | None = None) -> None:
        self.exporter = exporter

    def record(self, name: str, **attributes: str | int | float) -> None:
        if self.exporter is None:
            return
        forbidden = set(attributes) - self._ALLOWED_ATTRIBUTES
        if forbidden:
            raise ValueError("trace attributes include content-bearing or unknown fields")
        self.exporter.record(name, dict(attributes))


def backup_sqlite(source: str | Path, destination: str | Path) -> Path:
    source_path = Path(source)
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(source_path)
    try:
        target_connection = sqlite3.connect(destination_path)
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
    finally:
        source_connection.close()
    return destination_path


def purge_expired_rows(connection: sqlite3.Connection, *, before: float | None = None) -> int:
    cutoff = before if before is not None else time.time()
    cursor = connection.execute(
        "DELETE FROM url_cache WHERE expires_at IS NOT NULL AND expires_at < ?", (cutoff,)
    )
    connection.commit()
    return cursor.rowcount


def atomic_copy(source: str | Path, destination: str | Path) -> Path:
    """Copy non-database deployment artifacts without partial destination writes."""
    source_path = Path(source)
    destination_path = Path(destination)
    temporary = destination_path.with_suffix(destination_path.suffix + ".tmp")
    shutil.copy2(source_path, temporary)
    temporary.replace(destination_path)
    return destination_path
