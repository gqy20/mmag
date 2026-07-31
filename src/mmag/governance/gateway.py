"""Model routing, quotas and usage accounting."""

from __future__ import annotations

from dataclasses import dataclass
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


class QuotaLedger:
    def __init__(self, *, default_limit_usd: float = 10.0):
        self.default_limit_usd = default_limit_usd
        self._usage: dict[str, UsageSnapshot] = {}
        self._lock = RLock()

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

    def snapshot(self, subject_id: str) -> UsageSnapshot:
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
    ):
        if default_route not in runtimes:
            raise ValueError(f"default runtime route {default_route!r} is missing")
        self.runtimes = dict(runtimes)
        self.ledger = ledger or QuotaLedger()
        self.default_route = default_route
        self.estimated_run_cost_usd = estimated_run_cost_usd

    async def run(self, request: RunRequest, *, route: str | None = None) -> AgentResult:
        subject = request.context.actor_id
        self.ledger.ensure_available(subject, self.estimated_run_cost_usd)
        selected = route or self.default_route
        try:
            runtime = self.runtimes[selected]
        except KeyError as error:
            raise LookupError(f"unknown model route {selected!r}") from error
        result = await runtime.run(request)
        self.ledger.record(subject, result)
        return result
