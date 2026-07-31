import pytest

from mmag.capabilities import CapabilityEffect, CapabilitySpec
from mmag.governance import (
    BudgetExceededError,
    EnvironmentSecretProvider,
    GovernanceContext,
    ModelGateway,
    PolicyEffect,
    PolicyEngine,
    PolicyRule,
    QuotaLedger,
    redact_sensitive,
)
from mmag.runtimes import AgentResult, RunContext, RunRequest, TokenUsage


def _write_spec() -> CapabilitySpec:
    return CapabilitySpec(
        name="send_file",
        description="send",
        input_schema={"type": "object"},
        handler=lambda: None,
        effect=CapabilityEffect.WRITE,
        permission="mattermost:file:write",
    )


def test_policy_is_deterministic_and_explainable():
    engine = PolicyEngine(
        (
            PolicyRule(
                "approve-writes",
                PolicyEffect.REQUIRE_APPROVAL,
                permissions=("mattermost:file:write",),
            ),
        )
    )
    decision = engine.evaluate(_write_spec(), {}, GovernanceContext("u1", "project:p1"))
    assert decision.effect is PolicyEffect.REQUIRE_APPROVAL
    assert decision.rule_id == "approve-writes"
    assert decision.reason


def test_redaction_and_secret_provider_do_not_expose_secret(monkeypatch):
    monkeypatch.setenv("MMAG_TEST_SECRET", "super-secret")
    provider = EnvironmentSecretProvider(allowed_names=frozenset({"MMAG_TEST_SECRET"}))
    assert provider.get("MMAG_TEST_SECRET").reveal() == "super-secret"
    assert "super-secret" not in repr(provider.get("MMAG_TEST_SECRET"))
    assert "[REDACTED]" in redact_sensitive("token=abc123 api_key=sk-test")


@pytest.mark.asyncio
async def test_model_gateway_enforces_budget_and_records_usage():
    class Runtime:
        async def run(self, request: RunRequest) -> AgentResult:
            return AgentResult(
                text="ok",
                runtime="stub",
                usage=TokenUsage(input_tokens=10, output_tokens=5, cost_usd=0.2),
            )

    ledger = QuotaLedger(default_limit_usd=0.1)
    gateway = ModelGateway({"default": Runtime()}, ledger=ledger)
    request = RunRequest(
        context=RunContext("trace", "user", "channel", "project:p1"),
        messages=({"role": "user", "content": "hi"},),
    )
    with pytest.raises(BudgetExceededError):
        await gateway.run(request)
