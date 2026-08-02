"""Composition root and lifecycle for the Mattermost Agent application."""

from __future__ import annotations

import contextlib
import json
import time
from importlib.metadata import version
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

from ..agent_packages import (
    AgentFactory,
    AgentPackageRegistry,
    DeepAgentProvider,
    DirectAgentProvider,
)
from ..agent_system import AgentRegistry, AgentRouter
from ..capabilities import (
    CapabilityExecutor,
    CapabilityRegistry,
    build_builtin_bindings,
    create_ppt_capabilities,
)
from ..client import MMClient
from ..config import _log_config_loading, config
from ..control_plane import (
    ApprovalService,
    LangGraphApprovalCoordinator,
    LifecycleService,
    MattermostAccessGuard,
    MattermostApprovalAuthorizer,
    MattermostScopeResolver,
    MessagePipeline,
    SQLiteControlPlane,
)
from ..evaluation import PackageActivationGate
from ..execution import (
    ArtifactRepository,
    ExecutionProfileRegistry,
    ProcessRunner,
    ScriptExecutor,
    WorkspaceBackendFactory,
    WorkspaceManager,
)
from ..governance import (
    ModelGateway,
    ModelPolicyRegistry,
    PolicyRegistry,
    QuotaLedger,
    RegistryPolicyAuthorizer,
)
from ..logger import get_logger, log_event
from ..mcp_bridge import MCPClientBridge, load_mcp_config
from ..memory import Memory
from ..memory_compactor import MemoryCompactor
from ..runtimes import DeepAgentRuntime
from ..skill_packages import SkillPackageRegistry, SkillResolver
from ..ws_client import WebSocketClient
from .actions import ActionCallbackServer, ActionTokenService
from .context import AttachmentProcessor, BotIdentity, ContextBuilder
from .delivery import MattermostDelivery
from .message_handler import MessageHandler
from .probe import MattermostCapabilityProbe

log = get_logger(__name__)


def _validate_database_paths(memory_path: str, checkpoint_path: str) -> None:
    if Path(memory_path).resolve() == Path(checkpoint_path).resolve():
        raise ValueError("MEMORY_DB_PATH and CHECKPOINT_DB_PATH must use separate files")


class Agent:
    """Application lifecycle; domain execution lives in composed services."""

    def __init__(self) -> None:
        _validate_database_paths(config.memory_db_path, config.checkpoint_db_path)
        self.mm = MMClient()
        self.memory = Memory(
            config.memory_db_path,
            installation_id=config.mm_installation_id,
            tenant_id=config.mm_tenant_id,
        )
        self.control_store = SQLiteControlPlane(config.memory_db_path)
        lifecycle = LifecycleService(self.control_store)
        approvals = ApprovalService(self.control_store, lifecycle)

        self.policy_registry = PolicyRegistry()
        self.policy_registry.load_directory(Path(config.policies_path))
        self.model_policy_registry = ModelPolicyRegistry()
        self.model_policy_registry.load_directory(Path(config.model_policies_path))
        self.capability_executor = CapabilityExecutor(
            RegistryPolicyAuthorizer(
                self.policy_registry,
                audit_sink=self.control_store,
            )
        )
        self.access_guard = MattermostAccessGuard(
            self.mm,
            installation_id=config.mm_installation_id,
            tenant_id=config.mm_tenant_id,
        )

        self.package_activation_gate = PackageActivationGate(self.control_store.releases)
        self.skill_package_registry = SkillPackageRegistry(
            activation_gate=self.package_activation_gate
        )
        self.skill_package_registry.load_directory(Path(config.skill_packages_path))
        self.execution_profile_registry = ExecutionProfileRegistry()
        self.execution_profile_registry.load_directory(Path(config.execution_profiles_path))
        self.execution_workspaces = WorkspaceManager(
            Path(config.execution_workspace_path),
            retention_seconds=config.execution_workspace_retention_seconds,
        )
        self.execution_workspaces.cleanup_stale()
        self.artifact_repository = ArtifactRepository(
            Path(config.artifact_store_path),
            self.control_store,
        )
        incomplete, orphaned = self.artifact_repository.reconciliation
        if incomplete or orphaned:
            log.warning(
                "Artifact Repository 已回收 incomplete=%d orphaned=%d",
                incomplete,
                orphaned,
            )
        self.process_runner = ProcessRunner(Path(config.execution_runtime_root))
        self.script_executor = ScriptExecutor(
            self.execution_profile_registry,
            self.process_runner,
            self.execution_workspaces,
            self.artifact_repository,
            self.control_store,
        )
        self.workspace_backend_factory = WorkspaceBackendFactory(
            self.execution_profile_registry,
            self.execution_workspaces,
            self.capability_executor,
            self.artifact_repository,
            allow_unsafe_local=config.allow_unsafe_local_exec,
        )
        self.execution_capabilities = (
            *create_ppt_capabilities(self.script_executor),
            *self.workspace_backend_factory.capabilities,
        )

        self.capability_registry = CapabilityRegistry()
        builtin_bindings = build_builtin_bindings(
            self.mm,
            self.memory,
            artifacts=self.artifact_repository,
            executor=self.capability_executor,
            access_guard=self.access_guard,
            additional_specs=self.execution_capabilities,
        )
        for binding in builtin_bindings:
            self.capability_registry.register(binding)
        self.mcp_config = load_mcp_config(config.mcp_config_path)
        self.deep_agent_runtime = DeepAgentRuntime(
            capability_registry=self.capability_registry,
            checkpoint_path=config.checkpoint_db_path,
            audit_sink=self.control_store,
            workspace_backend_factory=self.workspace_backend_factory,
        )
        self.runtime = ModelGateway(
            {"default": self.deep_agent_runtime},
            ledger=QuotaLedger(
                default_limit_usd=config.model_budget_usd,
                store=self.control_store.quota,
            ),
            audit_sink=self.control_store,
        )

        self.agent_package_registry = AgentPackageRegistry(
            policy_registry=self.policy_registry,
            model_policy_registry=self.model_policy_registry,
            skill_registry=self.skill_package_registry,
            execution_profile_registry=self.execution_profile_registry,
            activation_gate=self.package_activation_gate,
        )
        self.agent_package_registry.load_directory(Path(config.agent_packages_path))
        for package in self.agent_package_registry.list():
            model_policy = self.model_policy_registry.get(
                package.manifest.model_policy_ref
            )
            self.runtime.validate_route(model_policy.route)
            self.deep_agent_runtime.validate_model_policy(
                model_class=model_policy.model_class,
                max_tokens=model_policy.max_output_tokens,
                temperature=model_policy.temperature,
            )
        self.agent_factory = AgentFactory(
            DeepAgentProvider(
                self.runtime,
                self.capability_registry,
                self.model_policy_registry,
                additional_capabilities=self.mcp_config.capability_names,
                platform_provenance={
                    "deepagents_version": version("deepagents"),
                    "langgraph_version": version("langgraph"),
                    "mcp_config_version": str(self.mcp_config.version),
                    "mcp_config_hash": self.mcp_config.sha256,
                },
            ),
            DirectAgentProvider(
                self.capability_registry,
                self.capability_executor,
            ),
        )
        self.agent_registry = AgentRegistry(
            self.agent_factory.create_all(self.agent_package_registry.list())
        )
        self.agent_router = AgentRouter(self.agent_registry)
        self.skill_resolver = SkillResolver(
            self.skill_package_registry,
            self.capability_registry,
            personal_skills=self.control_store.personal_skills,
        )
        default_agent = self.agent_registry.default()
        default_package = self.agent_package_registry.get(default_agent.descriptor.name)

        self.mcp_bridge = MCPClientBridge(
            self.capability_registry,
            config=self.mcp_config,
            executor=self.capability_executor,
        )
        self.approval_coordinator = LangGraphApprovalCoordinator(
            self.control_store,
            lifecycle,
            approvals,
            self.runtime,
            authorizer=MattermostApprovalAuthorizer(self.mm),
            access_guard=self.access_guard,
            skill_registry=self.skill_package_registry,
        )
        self.start_time = time.time()
        self.stats = {"messages": 0, "responses": 0, "dropped_messages": 0}
        self.working_memory: dict[str, list] = {}
        self.identity = BotIdentity()
        self.scope_resolver = MattermostScopeResolver(
            self.mm,
            installation_id=config.mm_installation_id,
            tenant_id=config.mm_tenant_id,
        )
        self.compactor = MemoryCompactor(
            memory=self.memory,
            runtime=self.runtime,
            mm_client=self.mm,
            config=config,
        )
        self.action_tokens: ActionTokenService | None = None
        self.action_server: ActionCallbackServer | None = None
        if config.mm_action_callback_url or config.mm_action_signing_secret:
            self.action_tokens = self._build_action_tokens()
        self.delivery = MattermostDelivery(
            self.mm,
            self.memory,
            self.identity,
            self.stats,
            artifacts=self.artifact_repository,
            outbox_store=self.control_store,
            scope_resolver=self.scope_resolver,
            access_guard=self.access_guard,
        )
        attachment_processor = AttachmentProcessor(self.mm)
        if default_package.manifest.prompt.system_ref is None:
            raise RuntimeError("default Agent requires a system prompt")
        context_builder = ContextBuilder(
            self.mm,
            self.memory,
            self.working_memory,
            self.identity,
            default_package.prompts[default_package.manifest.prompt.system_ref],
            scope_resolver=self.scope_resolver,
        )
        self.message_handler = MessageHandler(
            mm_client=self.mm,
            memory=self.memory,
            compactor=self.compactor,
            capability_registry=self.capability_registry,
            agent_router=self.agent_router,
            skill_resolver=self.skill_resolver,
            audit_store=self.control_store,
            approval_coordinator=self.approval_coordinator,
            working_memory=self.working_memory,
            identity=self.identity,
            attachment_processor=attachment_processor,
            context_builder=context_builder,
            delivery=self.delivery,
            stats=self.stats,
            action_tokens=self.action_tokens,
            scope_resolver=self.scope_resolver,
            personal_skills=self.control_store.personal_skills,
            work_cases=self.control_store.work_cases,
            interactions=self.control_store.interactions,
            intent_runtime=self.runtime,
        )
        self.capability_probe = MattermostCapabilityProbe(self.mm)
        if self.action_tokens is not None:
            callback_path = urlsplit(config.mm_action_callback_url).path or "/actions"
            self.action_server = ActionCallbackServer(
                config.mm_action_listen_host,
                config.mm_action_listen_port,
                self.message_handler.handle_action_callback,
                path=callback_path,
            )
        self.ws: WebSocketClient | None = None
        self.pipeline: MessagePipeline | None = None
        self.running = False

    async def start(self) -> None:
        _log_config_loading()
        log_event(log, "application.starting", status="starting")
        me = await self.mm.get_me_async()
        self.identity.user_id = me["id"]
        self.identity.username = me["username"]
        log_event(log, "mattermost.identity_loaded", status="ready")
        await self._probe_mattermost()

        log_event(
            log,
            "model.configured",
            status="ready" if config.anthropic_api_key else "missing_secret",
            model=config.anthropic_model,
            api_key_configured=bool(config.anthropic_api_key),
        )
        if not config.anthropic_api_key:
            log.error("ANTHROPIC_API_KEY 未设置")
            return
        await self.deep_agent_runtime.start()
        await self._connect_mcp()
        self._preload_channels()

        self.pipeline = MessagePipeline(
            self.control_store,
            self.message_handler.process_inbound,
            self.delivery.deliver,
            max_concurrency=config.pipeline_max_concurrency,
            max_pending=config.pipeline_max_pending,
        )
        await self.pipeline.start()
        if self.action_server is not None:
            await self.action_server.start()
            log.info(
                "Mattermost Action callback 监听 %s:%d",
                config.mm_action_listen_host,
                config.mm_action_listen_port,
            )
        self.message_handler.pipeline = self.pipeline
        self.ws = WebSocketClient(
            url=config.ws_url,
            token=config.mm_token,
            on_event=self.on_ws_event,
            on_response=self.on_ws_response,
        )
        self.running = True
        log.info("Agent 就绪，默认 Runtime=Deep Agents/LangGraph，等待消息")
        await self.ws.run()

    async def _connect_mcp(self) -> None:
        try:
            count = await self.mcp_bridge.load_and_connect()
            log.info("MCP 已连接 %d 个 Server", count)
        except Exception as error:
            log.warning("MCP 加载失败（不影响启动）: %s", error)

    async def _probe_mattermost(self) -> None:
        capabilities = await self.capability_probe.probe()
        self.control_store.append_audit(
            "mattermost.capabilities",
            scope_id=config.mm_team_id,
            target=config.mm_url,
            decision="trusted" if capabilities.trusted_transport else "skipped",
            details=capabilities.to_dict(),
        )
        log.info(
            "Mattermost capabilities version=%s edition=%s files=%s actions=%s",
            capabilities.server_version,
            capabilities.edition,
            capabilities.files_enabled,
            capabilities.interactive_messages_enabled,
        )

    def _preload_channels(self) -> None:
        try:
            channels: list[dict] = []
            if config.mm_channel_id:
                channel = self.mm.get_channel(config.mm_channel_id)
                if channel:
                    channels.append(channel)
            elif config.mm_team_id:
                channels = list(self.mm._get(f"/teams/{config.mm_team_id}/channels"))[:10]
            for channel in channels:
                channel_id = channel["id"]
                self.backfill_channel(channel_id)
                self.working_memory[channel_id] = self.memory.get_recent_messages(
                    channel_id,
                    limit=config.max_context_messages,
                )
        except Exception as error:
            log.warning("预加载频道失败，将使用空上下文: %s", error)

    def backfill_channel(self, channel_id: str) -> int:
        latest_ms = int(self.memory.get_latest_message_ts(channel_id) * 1000)
        new_count = 0
        page = 0
        while True:
            posts = self.mm.get_posts_page(channel_id, page=page, per_page=200)
            if not posts:
                break
            reached_existing = False
            for post in posts:
                if latest_ms and (post.get("create_at", 0) or 0) <= latest_ms:
                    reached_existing = True
                    break
                post["channel_id"] = channel_id
                post["username"] = self.mm.get_username(post.get("user_id", ""))
                if self.memory.log_message(post):
                    new_count += 1
                else:
                    self.stats["dropped_messages"] += 1
            if reached_existing or len(posts) < 200:
                break
            page += 1
            time.sleep(0.1)
        return new_count

    async def on_ws_event(self, message: dict) -> None:
        event_type = message.get("event", "")
        if event_type == "posted":
            await self.message_handler.on_posted(message)
        elif event_type == "post_edited":
            self.message_handler.on_post_edited(message)
        elif event_type == "post_deleted":
            self.message_handler.on_post_deleted(message)
        elif event_type in {"channel_created", "channel_updated", "channel_deleted"}:
            self.mm.invalidate_channel(self._ws_entity_id(message, "channel", "channel_id"))
        elif event_type == "user_updated":
            self.mm.invalidate_user(self._ws_entity_id(message, "user", "user_id"))
        elif event_type in {"user_added", "user_removed", "channel_member_updated"}:
            self.mm.invalidate_channel(self._ws_entity_id(message, "channel", "channel_id"))
            self.mm.invalidate_user(self._ws_entity_id(message, "user", "user_id"))
        elif event_type not in {
            "hello",
            "typing",
            "reaction_added",
            "reaction_removed",
            "status_change",
            "ephemeral_message",
        }:
            log.debug("未处理事件: %s", event_type)

    @staticmethod
    def _ws_entity_id(message: dict, object_key: str, id_key: str) -> str:
        data = message.get("data") or {}
        raw = data.get(object_key)
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = None
        if isinstance(raw, dict) and raw.get("id"):
            return str(raw["id"])
        return str(data.get(id_key) or "")

    @staticmethod
    def on_ws_response(message: dict) -> None:
        if message.get("error"):
            log.error("WebSocket 响应错误: %s", message["error"])
        else:
            log.debug("WebSocket 响应: status=%s", message.get("status", "?"))

    async def stop(self) -> None:
        self.running = False
        for name, close in (
            ("ws", self.ws.close if self.ws is not None else None),
            (
                "action_server",
                self.action_server.close if self.action_server is not None else None,
            ),
            ("action_tasks", self.message_handler.close_actions),
            ("pipeline", self.pipeline.close if self.pipeline is not None else None),
            ("deep_agent_runtime", self.deep_agent_runtime.close),
            ("mcp_bridge", self.mcp_bridge.close_all),
        ):
            if close is None:
                continue
            try:
                await close()
            except Exception as error:
                log.error("%s 关闭失败: %s", name, error, exc_info=True)
        try:
            from ..url_analyzer import close_client

            await close_client()
            await self.mm.aclose()
        except Exception as error:
            log.error("HTTP 客户端关闭失败: %s", error, exc_info=True)
        self.memory.close()
        self.control_store.close()
        log.info("Agent 已停止")

    def _build_action_tokens(self) -> ActionTokenService:
        if not config.mm_action_callback_url or not config.mm_action_signing_secret:
            raise ValueError(
                "MM_ACTION_CALLBACK_URL and MM_ACTION_SIGNING_SECRET must be configured together"
            )
        parsed = urlsplit(config.mm_action_callback_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("MM_ACTION_CALLBACK_URL must be an absolute HTTP(S) URL")
        trusted_local = parsed.hostname == "localhost"
        with contextlib.suppress(ValueError):
            trusted_local = trusted_local or ip_address(parsed.hostname).is_loopback
        if parsed.scheme != "https" and not trusted_local:
            raise ValueError("Mattermost Action callback must use HTTPS outside localhost")
        return ActionTokenService(
            config.mm_action_signing_secret,
            self.control_store,
            ttl_seconds=config.mm_action_token_ttl_seconds,
        )
