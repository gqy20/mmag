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
_ARTIFACT_REF = re.compile(r"^artifact://[a-f0-9]{32}$")


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
    capability_calls: tuple[dict[str, Any], ...] = ()
    approvals: tuple[dict[str, Any], ...] = ()
    artifacts: tuple[dict[str, Any], ...] = ()
    deliveries: tuple[dict[str, Any], ...] = ()
    audits: tuple[dict[str, Any], ...] = ()
    logs: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def found(self) -> bool:
        return bool(
            self.trace_id
            or self.run_graph
            or self.capability_calls
            or self.approvals
            or self.artifacts
            or self.deliveries
            or self.audits
            or self.logs
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "identifier": self.identifier,
            "trace_id": self.trace_id,
            "run_ids": list(self.run_ids),
            "run_graph": list(self.run_graph),
            "capability_calls": list(self.capability_calls),
            "approvals": list(self.approvals),
            "artifacts": list(self.artifacts),
            "deliveries": list(self.deliveries),
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
        control_runs = self._query_agent_runs(trace_id, identifier)
        approvals = self._query_approvals(trace_id, identifier)
        matched_logs = tuple(
            event
            for event in log_events
            if trace_id
            and str(event.get("trace_id") or "") == trace_id
        )
        run_graph = self._run_graph(audits, matched_logs, control_runs)
        run_keys = tuple(
            dict.fromkeys(
                value
                for item in run_graph
                for value in (
                    str(item.get("run_id") or ""),
                    str(item.get("thread_id") or ""),
                )
                if value
            )
        )
        capability_calls = self._capability_calls(audits)
        artifacts = self._query_artifacts(run_keys, identifier)
        deliveries = self._query_deliveries(run_keys, identifier)
        run_ids = tuple(sorted(str(item["run_id"]) for item in run_graph))
        warnings = tuple(
            value
            for value in (
                database_warning,
                *self._graph_warnings(
                    run_graph,
                    capability_calls,
                    approvals,
                    artifacts,
                    deliveries,
                ),
            )
            if value
        )
        return DiagnosticReport(
            identifier=identifier,
            trace_id=trace_id,
            run_ids=run_ids,
            run_graph=run_graph,
            capability_calls=capability_calls,
            approvals=approvals,
            artifacts=artifacts,
            deliveries=deliveries,
            audits=audits,
            logs=matched_logs,
            warnings=warnings,
        )

    @staticmethod
    def _run_graph(
        audits: tuple[dict[str, Any], ...],
        logs: tuple[dict[str, Any], ...],
        control_runs: tuple[dict[str, Any], ...] = (),
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
                    "thread_id": "",
                    "agent_ref": "",
                    "skill_ref": "",
                    "status": "",
                    "last_event": "",
                },
            )
            for key in ("parent_run_id", "workflow_id"):
                value = str(item.get(key) or details.get(key) or "")
                if value:
                    record[key] = value
            for key in ("thread_id", "agent_ref", "skill_ref"):
                value = str(item.get(key) or details.get(key) or "")
                if value:
                    record[key] = value
            status = str(item.get("status") or item.get("decision") or "")
            if status:
                record["status"] = status
            event = str(item.get("event") or item.get("event_type") or "")
            if event:
                record["last_event"] = event
        for item in control_runs:
            run_id = str(item["run_id"])
            record = runs.setdefault(
                run_id,
                {
                    "run_id": run_id,
                    "parent_run_id": "",
                    "workflow_id": "",
                    "thread_id": "",
                    "agent_ref": "",
                    "skill_ref": "",
                    "status": "",
                    "last_event": "",
                },
            )
            record["parent_run_id"] = str(item.get("parent_run_id") or "")
            record["workflow_id"] = str(item.get("workflow_id") or "")
            record["thread_id"] = str(item.get("thread_id") or "")
            record["agent_ref"] = str(item.get("agent_ref") or "")
            record["skill_ref"] = str(item.get("skill_ref") or "")
            record["status"] = str(item.get("status") or "")
            if not record["last_event"]:
                record["last_event"] = "control_plane.agent_run"
        return tuple(
            sorted(
                runs.values(),
                key=lambda item: (str(item["parent_run_id"]), str(item["run_id"])),
            )
        )

    @staticmethod
    def _capability_calls(
        audits: tuple[dict[str, Any], ...],
    ) -> tuple[dict[str, Any], ...]:
        calls: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for index, item in enumerate(audits):
            if item.get("event_type") != "runtime.tool.call":
                continue
            details = item.get("details")
            details = details if isinstance(details, dict) else {}
            call_id = str(details.get("runtime_call_id") or f"unknown:{index}")
            if call_id not in calls:
                order.append(call_id)
            calls[call_id] = {
                "capability_call_id": call_id,
                "run_id": str(details.get("run_id") or ""),
                "parent_run_id": str(details.get("parent_run_id") or ""),
                "capability": str(item.get("target") or ""),
                "status": str(item.get("decision") or ""),
                "parent_capability_call_id": str(
                    details.get("parent_runtime_call_id") or ""
                ),
                "duration_ms": details.get("duration_ms"),
                "error_code": str(details.get("error_code") or ""),
            }
        return tuple(calls[call_id] for call_id in order)

    @staticmethod
    def _graph_warnings(
        run_graph: tuple[dict[str, Any], ...],
        capability_calls: tuple[dict[str, Any], ...],
        approvals: tuple[dict[str, Any], ...],
        artifacts: tuple[dict[str, Any], ...],
        deliveries: tuple[dict[str, Any], ...],
    ) -> tuple[str, ...]:
        by_id = {str(item["run_id"]): item for item in run_graph}
        children: dict[str, list[dict[str, Any]]] = {}
        for item in run_graph:
            parent_run_id = str(item.get("parent_run_id") or "")
            if parent_run_id:
                children.setdefault(parent_run_id, []).append(item)
        warnings: list[str] = []
        terminal = {"succeeded", "exhausted", "failed", "cancelled"}
        for run_id, item in by_id.items():
            status = str(item.get("status") or "")
            descendants = children.get(run_id, ())
            if status == "waiting_child" and not any(
                str(child.get("status") or "") not in terminal for child in descendants
            ):
                warnings.append(f"waiting_child_without_active_child:{run_id}")
            if status in terminal:
                for child in descendants:
                    if str(child.get("status") or "") not in terminal:
                        warnings.append(
                            f"terminal_parent_with_active_child:{run_id}:{child['run_id']}"
                        )
        for call in capability_calls:
            if call.get("status") == "running":
                warnings.append(
                    f"capability_call_nonterminal:{call['capability_call_id']}"
                )
        for approval in approvals:
            child_run_id = str(approval.get("child_run_id") or "")
            approval_child = by_id.get(child_run_id)
            if (
                approval.get("status") == "pending"
                and approval_child is not None
                and approval_child.get("status") != "waiting_approval"
            ):
                warnings.append(
                    f"approval_without_waiting_run:{approval['approval_id']}:{child_run_id}"
                )
        artifact_refs = {str(item["ref"]) for item in artifacts}
        for delivery in deliveries:
            if delivery.get("status") == "failed":
                warnings.append(f"delivery_failed:{delivery['delivery_id']}")
            for artifact_ref in delivery.get("artifact_refs", ()):
                if artifact_ref not in artifact_refs:
                    warnings.append(
                        f"delivery_missing_artifact:{delivery['delivery_id']}:{artifact_ref}"
                    )
        return tuple(warnings)

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
                    WHERE trace_id<>'' AND (
                        trace_id=? OR json_extract(details, '$.run_id')=?
                        OR json_extract(details, '$.runtime_call_id')=?)
                    ORDER BY created_at DESC LIMIT 1""",
                    (identifier, identifier, identifier),
                ).fetchone()
                if row is None:
                    row = connection.execute(
                        """SELECT json_extract(payload, '$.trace_id')
                        FROM lifecycle_entities
                        WHERE entity_type='agent_run'
                          AND (entity_id=?
                               OR json_extract(payload, '$.parent_run_id')=?
                               OR json_extract(payload, '$.workflow_id')=?)
                        ORDER BY updated_at DESC LIMIT 1""",
                        (identifier, identifier, identifier),
                    ).fetchone()
                if row is None:
                    approval = connection.execute(
                        "SELECT arguments FROM approval_requests WHERE id=?",
                        (identifier,),
                    ).fetchone()
                    if approval is not None:
                        row = (_approval_trace(_json_object(approval[0])),)
                if row is None:
                    artifact = connection.execute(
                        "SELECT run_id FROM artifacts WHERE id=?", (identifier,)
                    ).fetchone()
                    if artifact is not None:
                        row = (self._trace_for_run_reference(connection, artifact[0]),)
                if row is None:
                    delivery = connection.execute(
                        "SELECT agent_run_id FROM outbox_deliveries WHERE id=?",
                        (identifier,),
                    ).fetchone()
                    if delivery is not None:
                        row = (self._trace_for_run_reference(connection, delivery[0]),)
        except sqlite3.Error as error:
            return "", f"database_query_failed:{type(error).__name__}"
        return (str(row[0] or "") if row else ""), ""

    @staticmethod
    def _trace_for_run_reference(connection: sqlite3.Connection, run_id: Any) -> str:
        reference = str(run_id or "")
        if not reference:
            return ""
        row = connection.execute(
            """SELECT json_extract(payload, '$.trace_id')
            FROM lifecycle_entities
            WHERE entity_type='agent_run'
              AND (entity_id=? OR json_extract(payload, '$.thread_id')=?)
            ORDER BY updated_at DESC LIMIT 1""",
            (reference, reference),
        ).fetchone()
        if row is not None:
            return str(row[0] or "")
        row = connection.execute(
            """SELECT trace_id FROM audit_events
            WHERE trace_id<>'' AND json_extract(details, '$.run_id')=?
            ORDER BY created_at DESC LIMIT 1""",
            (reference,),
        ).fetchone()
        return str(row[0] or "") if row else ""

    def _query_agent_runs(
        self, trace_id: str, identifier: str
    ) -> tuple[dict[str, Any], ...]:
        if not self.database_path.is_file():
            return ()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """SELECT entity_id, state, payload FROM lifecycle_entities
                    WHERE entity_type='agent_run'
                      AND ((? <> '' AND json_extract(payload, '$.trace_id')=?)
                           OR entity_id=?
                           OR json_extract(payload, '$.parent_run_id')=?
                           OR json_extract(payload, '$.workflow_id')=?)
                    ORDER BY created_at ASC""",
                    (trace_id, trace_id, identifier, identifier, identifier),
                ).fetchall()
        except sqlite3.Error:
            return ()
        result = []
        for row in rows:
            payload = _json_object(row["payload"])
            result.append(
                {
                    "run_id": str(row["entity_id"]),
                    "parent_run_id": str(payload.get("parent_run_id") or ""),
                    "workflow_id": str(payload.get("workflow_id") or ""),
                    "thread_id": str(payload.get("thread_id") or ""),
                    "agent_ref": str(payload.get("agent_ref") or ""),
                    "skill_ref": str(payload.get("skill_ref") or ""),
                    "status": str(row["state"]),
                }
            )
        return tuple(result)

    def _query_artifacts(
        self, run_ids: tuple[str, ...], identifier: str
    ) -> tuple[dict[str, Any], ...]:
        if not self.database_path.is_file():
            return ()
        filters = ["id=?"]
        parameters: list[Any] = [identifier]
        if run_ids:
            filters.append(f"run_id IN ({','.join('?' for _ in run_ids)})")
            parameters.extend(run_ids)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    "SELECT id, run_id, kind, metadata FROM artifacts WHERE "
                    + " OR ".join(filters)
                    + " ORDER BY created_at ASC",
                    parameters,
                ).fetchall()
        except sqlite3.Error:
            return ()
        result = []
        for row in rows:
            metadata = _json_object(row["metadata"])
            result.append(
                {
                    "artifact_id": str(row["id"]),
                    "ref": f"artifact://{row['id']}",
                    "run_id": str(row["run_id"]),
                    "kind": str(row["kind"]),
                    "size_bytes": _safe_int(metadata.get("size_bytes")),
                    "schema_version": str(metadata.get("schema_version") or ""),
                }
            )
        return tuple(result)

    def _query_deliveries(
        self, run_ids: tuple[str, ...], identifier: str
    ) -> tuple[dict[str, Any], ...]:
        if not self.database_path.is_file():
            return ()
        filters = ["id=?"]
        parameters: list[Any] = [identifier]
        if run_ids:
            filters.append(f"agent_run_id IN ({','.join('?' for _ in run_ids)})")
            parameters.extend(run_ids)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """SELECT id, agent_run_id, status, message_kind, attempts,
                              artifact_refs, remote_id, last_error
                    FROM outbox_deliveries WHERE """
                    + " OR ".join(filters)
                    + " ORDER BY created_at ASC",
                    parameters,
                ).fetchall()
        except sqlite3.Error:
            return ()
        result = []
        for row in rows:
            refs = _json_array(row["artifact_refs"])
            result.append(
                {
                    "delivery_id": str(row["id"]),
                    "run_id": str(row["agent_run_id"]),
                    "status": str(row["status"]),
                    "message_kind": str(row["message_kind"]),
                    "attempts": int(row["attempts"] or 0),
                    "artifact_refs": [
                        ref
                        for value in refs
                        if (ref := str(value)) and _ARTIFACT_REF.fullmatch(ref)
                    ],
                    "remote_delivered": bool(row["remote_id"]),
                    "has_error": bool(row["last_error"]),
                }
            )
        return tuple(result)

    def _query_approvals(
        self, trace_id: str, identifier: str
    ) -> tuple[dict[str, Any], ...]:
        if not self.database_path.is_file():
            return ()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """SELECT approval.id, approval.capability_name, approval.arguments,
                              lifecycle.state
                    FROM approval_requests approval
                    JOIN lifecycle_entities lifecycle
                      ON lifecycle.entity_type='approval_request'
                     AND lifecycle.entity_id=approval.id
                    ORDER BY approval.created_at ASC"""
                ).fetchall()
        except sqlite3.Error:
            return ()
        result = []
        for row in rows:
            arguments = _json_object(row["arguments"])
            approval_trace = _approval_trace(arguments)
            thread_id = str(arguments.get("thread_id") or "")
            delegated = arguments.get("delegated_child")
            delegated = delegated if isinstance(delegated, dict) else {}
            child_run_id = str(delegated.get("run_id") or "")
            if not (
                str(row["id"]) == identifier
                or (trace_id and approval_trace == trace_id)
                or identifier in {thread_id, child_run_id}
            ):
                continue
            result.append(
                {
                    "approval_id": str(row["id"]),
                    "capability": str(row["capability_name"]),
                    "status": str(row["state"]),
                    "thread_id": thread_id,
                    "child_run_id": child_run_id,
                }
            )
        return tuple(result)

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


def _json_object(value: Any) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value or "{}"))
    except (json.JSONDecodeError, TypeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _json_array(value: Any) -> list[Any]:
    try:
        decoded = json.loads(str(value or "[]"))
    except (json.JSONDecodeError, TypeError):
        return []
    return decoded if isinstance(decoded, list) else []


def _safe_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _approval_trace(arguments: dict[str, Any]) -> str:
    candidates: list[Any] = [arguments.get("capability_context")]
    runtime_snapshot = arguments.get("runtime_snapshot")
    if isinstance(runtime_snapshot, dict):
        candidates.append(runtime_snapshot.get("context"))
    delegated = arguments.get("delegated_child")
    if isinstance(delegated, dict):
        resume = delegated.get("resume")
        if isinstance(resume, dict):
            candidates.append(resume.get("capability_context"))
            child_snapshot = resume.get("runtime_snapshot")
            if isinstance(child_snapshot, dict):
                candidates.append(child_snapshot.get("context"))
    delegated_parent = arguments.get("delegated_parent")
    if isinstance(delegated_parent, dict):
        candidates.append(delegated_parent.get("capability_context"))
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate.get("trace_id"):
            return str(candidate["trace_id"])
    return ""
