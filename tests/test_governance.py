import pytest

from mmag.capabilities import CapabilityEffect, CapabilityExecutor, CapabilitySpec, CapabilityStatus
from mmag.governance import (
    BudgetExceededError,
    EnvironmentSecretProvider,
    GovernanceContext,
    ModelGateway,
    PolicyEffect,
    PolicyEngine,
    PolicyRegistry,
    PolicyRule,
    QuotaLedger,
    RegistryPolicyAuthorizer,
    bind_governance_context,
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


def test_policy_is_fail_closed_when_no_rule_matches():
    decision = PolicyEngine().evaluate(_write_spec(), {}, GovernanceContext("u1", "project:p1"))
    assert decision.effect is PolicyEffect.DENY


def test_policy_matches_dynamic_arguments_to_request_resources():
    engine = PolicyEngine(
        (
            PolicyRule(
                "read-current-channel",
                PolicyEffect.ALLOW,
                actions=("get_posts",),
                resource_arguments={"channel_id": "conversation_id"},
            ),
        )
    )
    spec = CapabilitySpec(
        "get_posts",
        "read",
        {"type": "object"},
        lambda: None,
        permission="mattermost:post:read",
    )
    context = GovernanceContext(
        "u1",
        "mattermost:team-1/channel-1",
        resources={"conversation_id": "channel-1"},
    )

    allowed = engine.evaluate(spec, {"channel_id": "channel-1"}, context)
    cross_channel = engine.evaluate(spec, {"channel_id": "channel-2"}, context)
    missing_target = engine.evaluate(spec, {}, context)

    assert allowed.effect is PolicyEffect.ALLOW
    assert cross_channel.effect is PolicyEffect.DENY
    assert missing_target.effect is PolicyEffect.DENY


def test_versioned_policy_registry_enforces_mmchat_resources_and_writes():
    from pathlib import Path

    registry = PolicyRegistry()
    registry.load_directory(Path(__file__).resolve().parents[1] / "policies")
    engine = registry.get("mmchat@1.3.0")
    get_posts = CapabilitySpec(
        "get_posts",
        "read",
        {"type": "object"},
        lambda: None,
        permission="mattermost:post:read",
    )
    external_mcp = CapabilitySpec(
        "mcp_crawl_search_text",
        "search",
        {"type": "object"},
        lambda: None,
        permission="mcp:crawl:search_text:invoke",
    )
    context = GovernanceContext(
        "u1",
        "mattermost:team-1/channel-1",
        resources={"conversation_id": "channel-1", "actor_id": "u1"},
    )

    allowed = engine.evaluate(get_posts, {"channel_id": "channel-1"}, context)
    denied = engine.evaluate(get_posts, {"channel_id": "channel-2"}, context)
    approval = engine.evaluate(_write_spec(), {"filename": "report.md"}, context)
    mcp_approval = engine.evaluate(external_mcp, {"query": "policy"}, context)

    assert allowed.effect is PolicyEffect.ALLOW
    assert denied.effect is PolicyEffect.DENY
    assert approval.effect is PolicyEffect.DENY
    assert mcp_approval.effect is PolicyEffect.REQUIRE_APPROVAL


@pytest.mark.asyncio
async def test_registry_authorizer_uses_current_package_policy_and_allowlist():
    from pathlib import Path

    registry = PolicyRegistry()
    registry.load_directory(Path(__file__).resolve().parents[1] / "policies")
    executor = CapabilityExecutor(RegistryPolicyAuthorizer(registry))
    spec = CapabilitySpec(
        "get_posts",
        "read",
        {
            "type": "object",
            "properties": {"channel_id": {"type": "string"}},
            "required": ["channel_id"],
        },
        lambda channel_id: {"channel_id": channel_id},
        permission="mattermost:post:read",
    )
    context = GovernanceContext(
        "u1",
        "mattermost:team-1/channel-1",
        resources={"conversation_id": "channel-1"},
        policy_ref="mmchat@1.3.0",
        allowed_capabilities=("get_posts",),
    )

    with bind_governance_context(context):
        allowed = await executor.execute(spec, {"channel_id": "channel-1"})
        denied = await executor.execute(spec, {"channel_id": "channel-2"})

    assert allowed.status is CapabilityStatus.SUCCESS
    assert denied.status is CapabilityStatus.FORBIDDEN


@pytest.mark.asyncio
async def test_registry_authorizer_denies_calls_without_package_context():
    registry = PolicyRegistry()
    executor = CapabilityExecutor(RegistryPolicyAuthorizer(registry))

    result = await executor.execute(_write_spec(), {})

    assert result.status is CapabilityStatus.FORBIDDEN


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


@pytest.mark.asyncio
async def test_model_gateway_uses_snapshot_route_and_rejects_conflicts():
    class Runtime:
        async def run(self, request: RunRequest) -> AgentResult:
            return AgentResult("ok", "stub")

    gateway = ModelGateway({"default": Runtime(), "research": Runtime()})
    request = RunRequest(
        context=RunContext("trace", "user", "channel", "project:p1", run_id="run"),
        messages=({"role": "user", "content": "hi"},),
        metadata={"route": "research"},
    )
    assert (await gateway.run(request)).text == "ok"
    with pytest.raises(ValueError, match="conflicts"):
        await gateway.run(request, route="default")


def test_quota_ledger_persists_atomic_reservations_and_settlement(tmp_path):
    from mmag.control_plane import SQLiteControlPlane

    store = SQLiteControlPlane(tmp_path / "quota.db")
    ledger = QuotaLedger(default_limit_usd=0.3, store=store.quota)
    ledger.reserve("run-1", "user", 0.2)
    assert ledger.snapshot("user").reserved_cost_usd == pytest.approx(0.2)
    with pytest.raises(BudgetExceededError):
        ledger.reserve("run-2", "user", 0.2)

    result = AgentResult(
        "ok",
        "stub",
        usage=TokenUsage(input_tokens=12, output_tokens=4, cost_usd=0.1),
    )
    ledger.settle("run-1", "user", result)
    ledger.settle("run-1", "user", result)
    store.close()

    reopened = SQLiteControlPlane(tmp_path / "quota.db")
    snapshot = QuotaLedger(default_limit_usd=0.3, store=reopened.quota).snapshot("user")
    assert snapshot.cost_usd == pytest.approx(0.1)
    assert snapshot.input_tokens == 12
    assert snapshot.output_tokens == 4
    assert snapshot.reserved_cost_usd == 0
    reopened.close()
