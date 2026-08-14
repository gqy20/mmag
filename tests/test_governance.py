from pathlib import Path

import pytest
import yaml

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
from mmag.runtimes import AgentResult, RunContext, RunRequest, RuntimeStatus, TokenUsage

ROOT = Path(__file__).resolve().parents[1]
MMCHAT_POLICY_REF = yaml.safe_load(
    (ROOT / "agents" / "mmchat" / "agent.yml").read_text(encoding="utf-8")
)["spec"]["policy_ref"]


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
    registry = PolicyRegistry()
    registry.load_directory(ROOT / "policies")
    engine = registry.get(MMCHAT_POLICY_REF)
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
        "mattermost:install-1:tenant-1:chn:channel-1",
        resources={"conversation_id": "channel-1", "actor_id": "u1"},
    )

    allowed = engine.evaluate(get_posts, {"channel_id": "channel-1"}, context)
    denied = engine.evaluate(get_posts, {"channel_id": "channel-2"}, context)
    approval = engine.evaluate(_write_spec(), {"filename": "report.md"}, context)
    mcp_approval = engine.evaluate(external_mcp, {"query": "policy"}, context)

    profile = CapabilitySpec(
        "get_user_profile",
        "profile",
        {"type": "object"},
        lambda: None,
        permission="memory:user_profile:read",
    )
    shared_profile = engine.evaluate(profile, {"user_id": "u1"}, context)
    personal_profile = engine.evaluate(
        profile,
        {"user_id": "u1"},
        GovernanceContext(
            "u1",
            "mattermost:install-1:tenant-1:usr:u1",
            resources={"conversation_id": "dm-1", "actor_id": "u1"},
        ),
    )

    assert allowed.effect is PolicyEffect.ALLOW
    assert denied.effect is PolicyEffect.DENY
    assert approval.effect is PolicyEffect.DENY
    assert mcp_approval.effect is PolicyEffect.REQUIRE_APPROVAL
    assert shared_profile.effect is PolicyEffect.DENY
    assert personal_profile.effect is PolicyEffect.ALLOW


@pytest.mark.asyncio
async def test_registry_authorizer_uses_current_package_policy_and_allowlist():
    registry = PolicyRegistry()
    registry.load_directory(ROOT / "policies")
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
        "mattermost:install-1:tenant-1:chn:channel-1",
        resources={"conversation_id": "channel-1"},
        policy_ref=MMCHAT_POLICY_REF,
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


@pytest.mark.asyncio
async def test_model_gateway_uses_distinct_quota_reservations_per_trace_and_actor(tmp_path):
    from mmag.control_plane import SQLiteControlPlane

    class Runtime:
        async def run(self, request: RunRequest) -> AgentResult:
            return AgentResult(
                "ok",
                "stub",
                usage=TokenUsage(input_tokens=2, output_tokens=1, cost_usd=0.1),
            )

    store = SQLiteControlPlane(tmp_path / "gateway-quota.db")
    gateway = ModelGateway(
        {"default": Runtime()},
        ledger=QuotaLedger(default_limit_usd=1.0, store=store.quota),
    )

    async def run(trace_id: str, actor_id: str) -> None:
        await gateway.run(
            RunRequest(
                context=RunContext(
                    trace_id, actor_id, "channel", "project:p1", run_id="shared-run"
                ),
                messages=({"role": "user", "content": "hi"},),
            )
        )

    await run("trace-1", "user-1")
    await run("trace-2", "user-1")
    await run("trace-3", "user-2")

    assert gateway.ledger.snapshot("user-1").cost_usd == pytest.approx(0.2)
    assert gateway.ledger.snapshot("user-2").cost_usd == pytest.approx(0.1)
    store.close()


@pytest.mark.asyncio
async def test_model_gateway_resumes_and_settles_original_quota_reservation(tmp_path):
    from mmag.control_plane import SQLiteControlPlane

    class Runtime:
        async def run(self, request: RunRequest) -> AgentResult:
            return AgentResult("waiting", "stub", status=RuntimeStatus.WAITING_APPROVAL)

        async def resume(self, thread_id: str, decision: dict) -> AgentResult:
            return AgentResult(
                "done",
                "stub",
                usage=TokenUsage(input_tokens=3, output_tokens=2, cost_usd=0.1),
            )

    context = RunContext(
        "approval-trace", "user-1", "channel", "project:p1", run_id="approval-run"
    )
    store = SQLiteControlPlane(tmp_path / "resume-quota.db")
    gateway = ModelGateway(
        {"default": Runtime()},
        ledger=QuotaLedger(default_limit_usd=1.0, store=store.quota),
    )
    paused = await gateway.run(
        RunRequest(context=context, messages=({"role": "user", "content": "hi"},))
    )
    assert paused.status is RuntimeStatus.WAITING_APPROVAL

    completed = await gateway.resume(
        "approval-run",
        {
            "runtime_snapshot": {
                "context": {
                    "run_id": context.run_id,
                    "trace_id": context.trace_id,
                    "actor_id": context.actor_id,
                }
            }
        },
    )

    assert completed.status is RuntimeStatus.COMPLETED
    snapshot = gateway.ledger.snapshot("user-1")
    assert snapshot.cost_usd == pytest.approx(0.1)
    assert snapshot.reserved_cost_usd == 0
    store.close()


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
