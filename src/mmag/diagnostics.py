"""Read-only diagnostic queries shared by developer tooling and evaluations."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_TEXT_TRACE = re.compile(r"\btrace=([^\s]+)")
_TEXT_RUN = re.compile(r"\brun=([^\s]+)")


def resolve_project_path(value: str, *, project_root: Path) -> Path:
    """Resolve configured runtime paths the same way the application does."""
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (project_root / path).resolve()


@dataclass(frozen=True, slots=True)
class DiagnosticReport:
    identifier: str
    trace_id: str = ""
    run_ids: tuple[str, ...] = ()
    run_graph: tuple[dict[str, Any], ...] = ()
    audits: tuple[dict[str, Any], ...] = ()
    logs: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def found(self) -> bool:
        return bool(self.trace_id or self.audits or self.logs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "identifier": self.identifier,
            "trace_id": self.trace_id,
            "run_ids": list(self.run_ids),
            "run_graph": list(self.run_graph),
            "audits": list(self.audits),
            "logs": list(self.logs),
            "warnings": list(self.warnings),
        }


@dataclass(slots=True)
class DiagnosticReader:
    database_path: Path
    log_directory: Path
    _log_cache: tuple[dict[str, Any], ...] | None = field(default=None, init=False)

    def report(self, identifier: str) -> DiagnosticReport:
        trace_id, database_warning = self._resolve_from_database(identifier)
        log_events = self._load_log_events()
        if not trace_id:
            trace_id = self._resolve_from_logs(identifier, log_events)
        audits = self._query_audits(trace_id) if trace_id else ()
        matched_logs = tuple(
            event
            for event in log_events
            if trace_id
            and str(event.get("trace_id") or "") == trace_id
        )
        run_ids = sorted(
            {
                value
                for value in (
                    *(str(item.get("run_id") or "") for item in matched_logs),
                    *(
                        str(item.get("details", {}).get("run_id") or "")
                        for item in audits
                        if isinstance(item.get("details"), dict)
                    ),
                )
                if value
            }
        )
        warnings = tuple(value for value in (database_warning,) if value)
        run_graph = self._run_graph(audits, matched_logs)
        return DiagnosticReport(
            identifier=identifier,
            trace_id=trace_id,
            run_ids=tuple(run_ids),
            run_graph=run_graph,
            audits=audits,
            logs=matched_logs,
            warnings=warnings,
        )

    @staticmethod
    def _run_graph(
        audits: tuple[dict[str, Any], ...], logs: tuple[dict[str, Any], ...]
    ) -> tuple[dict[str, Any], ...]:
        runs: dict[str, dict[str, Any]] = {}
        for item in (*audits, *logs):
            details = item.get("details")
            details = details if isinstance(details, dict) else {}
            run_id = str(item.get("run_id") or details.get("run_id") or "")
            if not run_id:
                continue
            record = runs.setdefault(
                run_id,
                {
                    "run_id": run_id,
                    "parent_run_id": "",
                    "workflow_id": "",
                    "status": "",
                    "last_event": "",
                },
            )
            for key in ("parent_run_id", "workflow_id"):
                value = str(item.get(key) or details.get(key) or "")
                if value:
                    record[key] = value
            status = str(item.get("status") or item.get("decision") or "")
            if status:
                record["status"] = status
            event = str(item.get("event") or item.get("event_type") or "")
            if event:
                record["last_event"] = event
        return tuple(
            sorted(
                runs.values(),
                key=lambda item: (str(item["parent_run_id"]), str(item["run_id"])),
            )
        )

    def latest_event(self, event_name: str) -> dict[str, Any] | None:
        matches = [
            event for event in self._load_log_events() if event.get("event") == event_name
        ]
        return matches[-1] if matches else None

    def _resolve_from_database(self, identifier: str) -> tuple[str, str]:
        if not self.database_path.is_file():
            return "", f"database_not_found:{self.database_path}"
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """SELECT trace_id FROM audit_events
                    WHERE trace_id=? OR json_extract(details, '$.run_id')=?
                    ORDER BY created_at DESC LIMIT 1""",
                    (identifier, identifier),
                ).fetchone()
        except sqlite3.Error as error:
            return "", f"database_query_failed:{type(error).__name__}"
        return (str(row[0] or "") if row else ""), ""

    def _query_audits(self, trace_id: str) -> tuple[dict[str, Any], ...]:
        if not self.database_path.is_file():
            return ()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """SELECT event_type, target, decision, details, created_at,
                              actor_id, scope_id, trace_id
                    FROM audit_events WHERE trace_id=? ORDER BY created_at ASC""",
                    (trace_id,),
                ).fetchall()
        except sqlite3.Error:
            return ()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                details = json.loads(str(item.get("details") or "{}"))
            except json.JSONDecodeError:
                details = {}
            item["details"] = details if isinstance(details, dict) else {}
            result.append(item)
        return tuple(result)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.database_path}?mode=ro", uri=True)

    def _load_log_events(self) -> tuple[dict[str, Any], ...]:
        if self._log_cache is not None:
            return self._log_cache
        events: list[dict[str, Any]] = []
        paths = sorted(
            self.log_directory.glob("mmag-*.log*"),
            key=lambda path: (path.stat().st_mtime, path.name),
        ) if self.log_directory.is_dir() else []
        for path in paths:
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line_number, line in enumerate(lines, start=1):
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    payload = self._parse_text_event(line)
                if not isinstance(payload, dict):
                    continue
                payload = dict(payload)
                payload["log_file"] = path.name
                payload["log_line"] = line_number
                events.append(payload)
        events.sort(key=lambda item: str(item.get("timestamp") or ""))
        self._log_cache = tuple(events)
        return self._log_cache

    @staticmethod
    def _resolve_from_logs(identifier: str, events: tuple[dict[str, Any], ...]) -> str:
        for event in reversed(events):
            if event.get("trace_id") == identifier:
                return identifier
            if event.get("run_id") == identifier:
                return str(event.get("trace_id") or "")
        return ""

    @staticmethod
    def _parse_text_event(line: str) -> dict[str, Any] | None:
        trace = _TEXT_TRACE.search(line)
        run = _TEXT_RUN.search(line)
        if trace is None and run is None:
            return None
        event_match = re.search(r"\bevent=([^\s]+)", line)
        return {
            "event": event_match.group(1) if event_match else "log.message",
            "trace_id": trace.group(1) if trace else "",
            "run_id": run.group(1) if run else "",
            "message": line,
        }
