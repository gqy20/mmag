"""Composition root and lifecycle for the Mattermost Agent application."""

from __future__ import annotations

import time
from pathlib import Path

from ..agent_packages import (
    AgentFactory,
    AgentPackageRegistry,
    AgentProviderRegistry,
    LangGraphJSONProvider,
    LangGraphTextProvider,
    SingleCapabilityProvider,
)
from ..agent_system import AgentRegistry, AgentRouter
from ..capabilities import (
    CapabilityExecutor,
    CapabilityRegistry,
    build_builtin_bindings,
    build_sdk_bindings,
    create_ppt_capabilities,
)
from ..client import MMClient
from ..config import _log_config_loading, _secret_status, config
from ..control_plane import (
    ApprovalService,
    LangGraphApprovalCoordinator,
    LifecycleService,
    MattermostApprovalAuthorizer,
    MessagePipeline,
    SQLiteControlPlane,
)
from ..execution import (
    ArtifactRepository,
    ExecutionProfileRegistry,
    ProcessRunner,
    ScriptExecutor,
    WorkspaceManager,
)
from ..governance import (
    ModelGateway,
    ModelPolicyRegistry,
    PolicyRegistry,
    QuotaLedger,
    RegistryPolicyAuthorizer,
)
from ..llm import LLM
from ..logger import get_logger
from ..mcp_bridge import MCPClientBridge
from ..memory import Memory
from ..memory_compactor import MemoryCompactor
from ..runtimes import ClaudeSDKRuntimeAdapter, LangGraphRuntimeAdapter
from ..sdk_llm import SDKLLM
from ..skill_packages import SkillPackageRegistry, SkillResolver, SkillResourceLoader
from ..ws_client import WebSocketClient
from .context import AttachmentProcessor, BotIdentity, ContextBuilder
from .delivery import MattermostDelivery
from .message_handler import MessageHandler

log = get_logger(__name__)


class Agent:
    """Application lifecycle; domain execution lives in composed services."""

    def __init__(self) -> None:
        self.mm = MMClient()
        self.memory = Memory(config.memory_db_path)
        self.control_store = SQLiteControlPlane(config.memory_db_path)
        lifecycle = LifecycleService(self.control_store)
        approvals = ApprovalService(self.control_store, lifecycle)

        self.policy_registry = PolicyRegistry()
        self.policy_registry.load_directory(Path(config.policies_path))
        self.model_policy_registry = ModelPolicyRegistry()
        self.model_policy_registry.load_directory(Path(config.model_policies_path))
        self.capability_executor = CapabilityExecutor(
            RegistryPolicyAuthorizer(self.policy_registry)
        )

        self.skill_package_registry = SkillPackageRegistry()
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
        self.execution_capabilities = create_ppt_capabilities(self.script_executor)

        self.capability_registry = CapabilityRegistry()
        builtin_bindings = build_builtin_bindings(
            self.mm,
            self.memory,
            executor=self.capability_executor,
            additional_specs=self.execution_capabilities,
        )
        for binding in builtin_bindings:
            self.capability_registry.register(binding)
        self.langgraph_runtime = LangGraphRuntimeAdapter(
            LLM(),
            capability_registry=self.capability_registry,
            checkpoint_path=config.memory_db_path,
        )
        self.runtime = ModelGateway(
            {"default": self.langgraph_runtime},
            ledger=QuotaLedger(default_limit_usd=config.model_budget_usd),
        )

        self.skill_resource_loader = SkillResourceLoader()
        self.agent_package_registry = AgentPackageRegistry(
            policy_registry=self.policy_registry,
            model_policy_registry=self.model_policy_registry,
            skill_registry=self.skill_package_registry,
            execution_profile_registry=self.execution_profile_registry,
        )
        self.agent_package_registry.load_directory(Path(config.agent_packages_path))
        self.agent_provider_registry = AgentProviderRegistry()
        self.agent_provider_registry.register(
            LangGraphTextProvider(
                self.runtime,
                self.capability_registry,
                self.model_policy_registry,
                additional_capabilities=config.mcp_allowed_tools,
                skill_resources=self.skill_resource_loader,
            )
        )
        self.agent_provider_registry.register(
            LangGraphJSONProvider(
                self.runtime,
                self.capability_registry,
                self.skill_resource_loader,
            )
        )
        self.agent_provider_registry.register(
            SingleCapabilityProvider(
                self.capability_registry,
                self.capability_executor,
                self.skill_resource_loader,
            )
        )
        self.agent_factory = AgentFactory(self.agent_provider_registry)
        self.agent_registry = AgentRegistry(
            self.agent_factory.create_all(self.agent_package_registry.list())
        )
        self.agent_router = AgentRouter(self.agent_registry)
        self.skill_resolver = SkillResolver(
            self.skill_package_registry,
            self.capability_registry,
        )
        default_agent = self.agent_registry.default()
        default_package = self.agent_package_registry.get(default_agent.descriptor.name)

        self.mcp_bridge = MCPClientBridge(
            self.capability_registry,
            allowed_tools=config.mcp_allowed_tools,
            executor=self.capability_executor,
        )
        self.approval_coordinator = LangGraphApprovalCoordinator(
            self.control_store,
            lifecycle,
            approvals,
            self.runtime,
            authorizer=MattermostApprovalAuthorizer(self.mm),
            skill_registry=self.skill_package_registry,
            skill_resources=self.skill_resource_loader,
        )
        self.start_time = time.time()
        self.stats = {"messages": 0, "responses": 0, "dropped_messages": 0}
        self.working_memory: dict[str, list] = {}
        self.identity = BotIdentity()
        self.compactor = MemoryCompactor(
            memory=self.memory,
            runtime=self.runtime,
            mm_client=self.mm,
            config=config,
        )
        self.delivery = MattermostDelivery(self.mm, self.memory, self.identity, self.stats)
        attachment_processor = AttachmentProcessor(self.mm)
        context_builder = ContextBuilder(
            self.mm,
            self.memory,
            self.working_memory,
            self.identity,
            default_package.prompts[default_package.manifest.prompt.system_ref],
            default_package.prompts[default_package.manifest.prompt.task_ref],
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
        )
        self.sdk_llm: SDKLLM | None = None
        self.ws: WebSocketClient | None = None
        self.pipeline: MessagePipeline | None = None
        self.running = False

    async def start(self) -> None:
        _log_config_loading()
        log.info("🤖 Agent 启动中...")
        me = await self.mm.get_me_async()
        self.identity.user_id = me["id"]
        self.identity.username = me["username"]
        log.info("Bot: @%s (%s)", self.identity.username, self.identity.user_id)

        log.info(
            "模型: %s | Key: %s", config.anthropic_model, _secret_status(config.anthropic_api_key)
        )
        if not config.anthropic_api_key:
            log.error("ANTHROPIC_API_KEY 未设置")
            return
        await self.langgraph_runtime.start()
        await self._connect_mcp()
        await self._configure_optional_sdk()
        self._preload_channels()

        self.pipeline = MessagePipeline(
            self.control_store,
            self.message_handler.process_inbound,
            self.delivery.deliver,
            max_concurrency=config.pipeline_max_concurrency,
            max_pending=config.pipeline_max_pending,
        )
        await self.pipeline.start()
        self.message_handler.pipeline = self.pipeline
        self.ws = WebSocketClient(
            url=config.ws_url,
            token=config.mm_token,
            on_event=self.on_ws_event,
            on_response=self.on_ws_response,
        )
        self.running = True
        log.info("Agent 就绪，默认 Runtime=LangGraph，等待消息")
        await self.ws.run()

    async def _connect_mcp(self) -> None:
        try:
            count = await self.mcp_bridge.load_and_connect()
            log.info("MCP 已连接 %d 个 Server", count)
        except Exception as error:
            log.warning("MCP 加载失败（不影响启动）: %s", error)

    async def _configure_optional_sdk(self) -> None:
        if not config.use_sdk_llm:
            return
        sdk_llm = SDKLLM()
        try:
            bindings = build_sdk_bindings(
                self.mm,
                self.memory,
                context_provider=sdk_llm.get_capability_context,
                executor=self.capability_executor,
                additional_specs=self.execution_capabilities,
            )
            bindings.extend(self.mcp_bridge.get_sdk_bindings())
            await sdk_llm.start(tool_funcs=bindings)
            self.runtime.runtimes["default"] = ClaudeSDKRuntimeAdapter(sdk_llm)
            self.compactor.runtime = self.runtime
            self.sdk_llm = sdk_llm
        except Exception as error:
            log.error("SDK Runtime 初始化失败，继续使用 LangGraph: %s", error)
            await sdk_llm.stop()

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
        elif event_type not in {
            "hello",
            "post_edited",
            "post_deleted",
            "typing",
            "reaction_added",
            "reaction_removed",
            "status_change",
            "channel_created",
            "channel_updated",
            "user_updated",
            "ephemeral_message",
        }:
            log.debug("未处理事件: %s", event_type)

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
            ("pipeline", self.pipeline.close if self.pipeline is not None else None),
            ("sdk_llm", self.sdk_llm.stop if self.sdk_llm is not None else None),
            ("langgraph_runtime", self.langgraph_runtime.close),
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
