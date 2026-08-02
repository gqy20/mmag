"""Model routing, quotas and usage accounting."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..runtimes import AgentResult, AgentRuntime, RunRequest


class BudgetExceededError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    subject_id: str
    cost_usd: float
    input_tokens: int
    output_tokens: int
    reserved_cost_usd: float = 0.0
    period: str = ""


class QuotaLedger:
    def __init__(self, *, default_limit_usd: float = 10.0, store=None):
        self.default_limit_usd = default_limit_usd
        self.store = store
        self._usage: dict[str, UsageSnapshot] = {}
        self._reservations: dict[str, tuple[str, float]] = {}
        self._lock = RLock()

    @staticmethod
    def _period() -> str:
        return datetime.now(UTC).strftime("%Y-%m")

    def reserve(self, reservation_id: str, subject_id: str, estimated_cost_usd: float) -> None:
        period = self._period()
        if self.store is not None:
            try:
                self.store.reserve_quota(
                    reservation_id,
                    subject_id=subject_id,
                    period=period,
                    run_id=reservation_id,
                    cost_usd=estimated_cost_usd,
                    limit_usd=self.default_limit_usd,
                    expires_at=time.time() + 86_400,
                )
            except ValueError as error:
                if str(error) == "quota_exceeded":
                    raise BudgetExceededError(f"budget exceeded for {subject_id}") from error
                raise
            return
        with self._lock:
            current = self.snapshot(subject_id)
            reserved = sum(
                amount for owner, amount in self._reservations.values() if owner == subject_id
            )
            if current.cost_usd + reserved + estimated_cost_usd > self.default_limit_usd:
                raise BudgetExceededError(f"budget exceeded for {subject_id}")
            self._reservations.setdefault(
                reservation_id, (subject_id, estimated_cost_usd)
            )

    def ensure_available(self, subject_id: str, estimated_cost_usd: float) -> None:
        current = self.snapshot(subject_id)
        if current.cost_usd + estimated_cost_usd > self.default_limit_usd:
            raise BudgetExceededError(
                f"budget exceeded for {subject_id}: "
                f"{current.cost_usd + estimated_cost_usd:.4f} > {self.default_limit_usd:.4f}"
            )

    def record(self, subject_id: str, result: AgentResult) -> UsageSnapshot:
        with self._lock:
            current = self.snapshot(subject_id)
            usage = result.usage
            updated = UsageSnapshot(
                subject_id,
                current.cost_usd + usage.cost_usd,
                current.input_tokens + usage.input_tokens,
                current.output_tokens + usage.output_tokens,
            )
            self._usage[subject_id] = updated
            return updated

    def settle(self, reservation_id: str, subject_id: str, result: AgentResult) -> UsageSnapshot:
        if self.store is not None:
            self.store.settle_quota(
                reservation_id,
                cost_usd=result.usage.cost_usd,
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
            )
            return self.snapshot(subject_id)
        with self._lock:
            self._reservations.pop(reservation_id, None)
            return self.record(subject_id, result)

    def release(self, reservation_id: str) -> None:
        if self.store is not None:
            self.store.release_quota(reservation_id)
            return
        with self._lock:
            self._reservations.pop(reservation_id, None)

    def snapshot(self, subject_id: str) -> UsageSnapshot:
        if self.store is not None:
            period = self._period()
            cost, input_tokens, output_tokens, reserved = self.store.quota_snapshot(
                subject_id, period
            )
            return UsageSnapshot(
                subject_id, cost, input_tokens, output_tokens, reserved, period
            )
        return self._usage.get(subject_id, UsageSnapshot(subject_id, 0.0, 0, 0))


class ModelGateway:
    """Select a configured runtime and enforce a per-actor cost envelope."""

    def __init__(
        self,
        runtimes: dict[str, AgentRuntime],
        *,
        ledger: QuotaLedger | None = None,
        default_route: str = "default",
        estimated_run_cost_usd: float = 0.2,
        audit_sink=None,
    ):
        if default_route not in runtimes:
            raise ValueError(f"default runtime route {default_route!r} is missing")
        self.runtimes = dict(runtimes)
        self.ledger = ledger or QuotaLedger()
        self.default_route = default_route
        self.estimated_run_cost_usd = estimated_run_cost_usd
        self.audit_sink = audit_sink

    def validate_route(self, route: str) -> None:
        if route not in self.runtimes:
            raise ValueError(f"unknown model route {route!r}")

    async def run(self, request: RunRequest, *, route: str | None = None) -> AgentResult:
        subject = request.context.actor_id
        policy_route = str(request.metadata.get("route") or "")
        if route is not None and policy_route and route != policy_route:
            raise ValueError("explicit route conflicts with the Model Policy snapshot")
        selected = route or policy_route or self.default_route
        try:
            runtime = self.runtimes[selected]
        except KeyError as error:
            raise LookupError(f"unknown model route {selected!r}") from error
        reservation_id = request.context.run_id or f"{request.context.trace_id}:{uuid.uuid4().hex}"
        declared_limit = request.metadata.get("max_cost_usd", "")
        estimated_cost = (
            float(declared_limit) if declared_limit else self.estimated_run_cost_usd
        )
        self.ledger.reserve(reservation_id, subject, estimated_cost)
        model_name = ""
        resolve_model = getattr(runtime, "resolve_model", None)
        if resolve_model is not None:
            try:
                model_name = str(resolve_model(request))
            except Exception:
                self.ledger.release(reservation_id)
                raise
        try:
            self._audit_route(request, selected, model_name)
        except Exception:
            self.ledger.release(reservation_id)
            raise
        try:
            result = await runtime.run(request)
        except Exception:
            self.ledger.release(reservation_id)
            raise
        if result.status.value != "waiting_approval":
            self.ledger.settle(reservation_id, subject, result)
        return result

    def _audit_route(self, request: RunRequest, route: str, model_name: str) -> None:
        if self.audit_sink is None:
            return
        self.audit_sink.append_audit(
            "model.route",
            actor_id=request.context.actor_id,
            scope_id=request.context.scope,
            trace_id=request.context.trace_id,
            target=route,
            decision="selected",
            details={
                "schema_version": "1.0",
                "run_id": request.context.run_id,
                "agent_ref": request.metadata.get("agent_ref", ""),
                "model_policy_ref": request.metadata.get("model_policy_ref", ""),
                "model_policy_hash": request.metadata.get("model_policy_hash", ""),
                "model_class": request.metadata.get("model_class", ""),
                "model": model_name,
                "max_output_tokens": request.max_tokens,
                "temperature": request.temperature,
            },
        )

    async def resume(
        self,
        thread_id: str,
        decision: dict,
        *,
        route: str | None = None,
    ) -> AgentResult:
        snapshot = decision.get("runtime_snapshot", {})
        metadata = snapshot.get("metadata", {}) if isinstance(snapshot, dict) else {}
        policy_route = str(metadata.get("route") or "") if isinstance(metadata, dict) else ""
        if route is not None and policy_route and route != policy_route:
            raise ValueError("explicit resume route conflicts with the original Model Policy")
        selected = route or policy_route or self.default_route
        try:
            runtime = self.runtimes[selected]
        except KeyError as error:
            raise LookupError(f"unknown model route {selected!r}") from error
        resume = getattr(runtime, "resume", None)
        if resume is None:
            raise TypeError(f"runtime {selected!r} does not support durable resume")
        result = await resume(thread_id, decision)
        if result.status.value != "waiting_approval":
            context = snapshot.get("context", {}) if isinstance(snapshot, dict) else {}
            subject = str(context.get("actor_id") or "")
            if not subject:
                raise RuntimeError("resume is missing the original quota subject")
            self.ledger.settle(thread_id, subject, result)
        return result
