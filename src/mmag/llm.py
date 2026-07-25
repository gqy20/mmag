"""
LLM 适配器 (Anthropic) — 支持单轮对话 + Agentic Tool Use 循环 (LangGraph)
"""

import operator
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any, TypedDict, cast

from anthropic import AsyncAnthropic
from langgraph.graph import END, START, StateGraph

from .config import config
from .logger import get_logger, trace
from .tools import ToolRegistry

# step-3.7-flash 等国产模型训练痕迹: 在 tools=[] 模式下, 偶发按 ChatML 模板
# 把"伪 tool call"直接输出成 XML 字符串 (而非结构化 tool_use block)。
# Anthropic SDK 只看结构化 tool_use, 不会 parse 这段, 会作为普通 text 漏给用户。
# 业界做法: Qwen-Agent / Step-Agent-SDK 在 SDK 层做 XML → 结构化 call 的反向解析。
# mmag 走的是 Anthropic SDK, 没这能力, 所以在 return 前兜底过滤。
_RE_TOOL_CALL_XML = re.compile(r"<tool_call>\s*.*?\s*</tool_call>", re.DOTALL)

# step-3.7-flash / Qwen 等模型偶发把思考过程作为普通 text 输出(而非结构化
# ThinkingBlock),导致整段内心独白直接漏到频道。Anthropic SDK 只认 block.type ==
# "thinking" 的结构化块, 文本里的 <think>...</think> 它不解析。需要后处理剥掉。
_RE_THINKING = re.compile(r"<think>.*?</think>", re.DOTALL)

if TYPE_CHECKING:
    from anthropic.types import (
        MessageParam,
        TextBlockParam,
        ToolResultBlockParam,
        ToolUseBlockParam,
    )

log = get_logger(__name__)


class LLMError(Exception):
    """LLM 调用失败的领域异常 — 包装 SDK 异常/网络异常供上层决策"""


class LLM:
    """Anthropic Claude 封装 — 支持单轮对话和 Agentic Tool Use"""

    def __init__(self):
        kwargs: dict[str, Any] = {"api_key": config.anthropic_api_key}
        if config.anthropic_base_url:
            kwargs["base_url"] = config.anthropic_base_url
        # 原生异步 client — SDK 内部用 httpx.AsyncClient，避免 to_thread 线程池开销
        self.client = AsyncAnthropic(**kwargs)
        self.model = config.anthropic_model
        self.call_count = 0
        log.info(
            "LLM 初始化完成 | 模型: %s | API: %s", self.model, config.anthropic_base_url or "官方"
        )

    # ---- 单轮对话 ----

    async def chat(self, messages: list[dict], system: str = "", max_tokens: int = 1024) -> str:
        """无工具的普通对话 — 也是 agent_loop 在 tools 为空时的降级入口

        注意: StepFun 等兼容 API 可能返回 ThinkingBlock (思考过程)，
        需要过滤掉，只取 TextBlock 的文本内容。
        """
        self.call_count += 1
        t0 = time.monotonic()
        try:
            kwargs: dict[str, Any] = {
                "model": self.model,
                "max_tokens": max_tokens,
                "messages": messages,
            }
            if system:
                kwargs["system"] = system
            response = await self.client.messages.create(**kwargs)
            texts = _parse_response(response).texts
            elapsed = time.monotonic() - t0
            log.debug("%s LLM 单轮调用 (%.3fs, %d 字符输出)", trace.prefix(), elapsed, len(texts))
            raw_text = "\n".join(texts)
            cleaned = _strip_model_artifacts(raw_text).strip()
            return cleaned if cleaned else "(模型返回为空)"
        except Exception as e:
            elapsed = time.monotonic() - t0
            log.error("%s LLM 调用失败 (%.3fs): %s", trace.prefix(), elapsed, e, exc_info=True)
            raise LLMError(str(e)) from e

    async def chat_with_system(
        self, system_prompt: str, user_message: str, max_tokens: int = 1024
    ) -> str:
        """带系统提示词的对话快捷方法"""
        return await self.chat(
            messages=[{"role": "user", "content": user_message}],
            system=system_prompt,
            max_tokens=max_tokens,
        )

    # ---- Agentic Tool Use 循环 (LangGraph StateGraph) ----

    async def agent_loop(
        self,
        messages: list[dict],
        system: str = "",
        tools: list[Any] | None = None,
        tool_registry: ToolRegistry | None = None,
        max_rounds: int = config.max_tool_rounds,
        max_tokens: int = 4096,
    ) -> str:
        """多轮 Agentic 循环：LLM 自主决定是否调用工具，直到产出最终回复。

        基于 LangGraph StateGraph 实现:
          - agent 节点: 调 Anthropic API, 解析 text/tool_use
          - tools 节点: 执行工具, 注入 tool_result
          - 条件边: 有 tool_calls → tools; 无 tool_calls → END
          - 末轮强制禁用 tools (B-1 方案), 与旧 for 循环行为一致

        Args:
            messages: 对话历史消息列表
            system: 系统提示词
            tools: 工具定义列表（Anthropic API 格式），如果为空则降级为纯文本模式
            tool_registry: 工具注册表（用于执行工具）
            max_rounds: 最大工具调用轮数（安全阀，防止无限循环）
            max_tokens: 每轮 LLM 最大 token 数

        Returns:
            最终文本回复
        """
        if not tools or not tool_registry:
            log.debug("%s agent_loop: 无工具配置，降级为普通聊天", trace.prefix())
            return await self.chat(messages, system, max_tokens)

        tools_schema = [t for t in tools if isinstance(t, dict)]
        loop_t0 = time.monotonic()

        log.info(
            "%s Agent Loop (LangGraph) | 消息数=%d 工具数=%d 最大轮次=%d",
            trace.prefix(),
            len(messages),
            len(tools_schema),
            max_rounds,
        )

        # ---- 图状态定义 ----
        class _State(TypedDict):
            messages: Annotated[list[dict], operator.add]
            round: int
            final_text: str

        # ---- agent 节点: 调 LLM, 解析响应 ----
        async def _agent_node(state: _State) -> dict:
            self.call_count += 1
            round_i = state["round"] + 1
            is_final = round_i >= max_rounds
            tools_now: list[Any] = [] if is_final else tools_schema

            try:
                kwargs: dict[str, Any] = {
                    "model": self.model,
                    "max_tokens": max_tokens,
                    "messages": state["messages"],
                    "tools": tools_now,
                }
                if system:
                    kwargs["system"] = system
                response = await self.client.messages.create(**kwargs)
            except Exception as e:
                raise LLMError(f"Round {round_i} LLM 调用失败: {e}") from e

            parsed = _parse_response(response)
            text_parts = parsed.texts
            tool_calls = parsed.tool_calls

            # 末轮: 强制收尾, 不再执行工具
            if is_final:
                final = _strip_model_artifacts("\n".join(text_parts)).strip()
                if not final:
                    log.warning(
                        "%s Round %d/%d 末轮强制收尾仍无 text, fallback",
                        trace.prefix(), round_i, max_rounds,
                    )
                    final = "⚠️ 处理超时，请重试"
                log.info(
                    "%s Round %d/%d 末轮强制收尾, text %d 字符",
                    trace.prefix(), round_i, max_rounds, len(final),
                )
                return {"final_text": final}

            # 无工具调用 → 提取文本, 结束
            if not tool_calls:
                final = _strip_model_artifacts("\n".join(text_parts)).strip()
                if not final:
                    log.warning(
                        "%s Round %d/%d → text 为空, fallback",
                        trace.prefix(), round_i, max_rounds,
                    )
                    final = "⚠️ 处理超时，请重试"
                return {"final_text": final}

            # 有工具调用 → 构造 assistant 消息, 传递给 tools 节点
            log.info(
                "%s Round %d/%d → %d 个工具调用 [%s]",
                trace.prefix(), round_i, max_rounds,
                len(tool_calls), ", ".join(tc["name"] for tc in tool_calls),
            )
            assistant_content: list[TextBlockParam | ToolUseBlockParam] = []
            if text_parts:
                assistant_content.append({"type": "text", "text": "\n".join(text_parts)})
            for tc in tool_calls:
                assistant_content.append(
                    cast(
                        "ToolUseBlockParam",
                        {"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["input"]},
                    )
                )
            return {
                "messages": [cast("MessageParam", {"role": "assistant", "content": assistant_content})],
                "round": round_i,
            }

        # ---- tools 节点: 执行工具, 收集 tool_result ----
        async def _tools_node(state: _State) -> dict:
            new_msgs: list[dict] = []
            last_msg = state["messages"][-1]
            content = last_msg.get("content", [])
            tool_calls = [
                c for c in content
                if isinstance(c, dict) and c.get("type") == "tool_use"
            ]
            for tc in tool_calls:
                result_str = await tool_registry.execute(tc["name"], tc["input"])
                tool_result_content: list[ToolResultBlockParam] = [
                    cast(
                        "ToolResultBlockParam",
                        {"type": "tool_result", "tool_use_id": tc["id"], "content": result_str},
                    )
                ]
                new_msgs.append(
                    cast("MessageParam", {"role": "user", "content": tool_result_content})
                )
            return {"messages": new_msgs}

        # ---- 条件边: agent 之后去 tools 还是 END ----
        def _should_continue(state: _State) -> str:
            if state.get("final_text"):
                return END
            return "tools"

        # ---- 编译并执行 ----
        graph = StateGraph(_State)
        graph.add_node("agent", _agent_node)
        graph.add_node("tools", _tools_node)
        graph.add_edge(START, "agent")
        graph.add_conditional_edges("agent", _should_continue, {END: END, "tools": "tools"})
        graph.add_edge("tools", "agent")

        app = graph.compile()
        result = await app.ainvoke(
            {"messages": list(messages), "round": 0, "final_text": ""},
            {"recursion_limit": max_rounds * 2 + 1},
        )

        final_text = result.get("final_text", "")
        total_elapsed = time.monotonic() - loop_t0
        if not final_text:
            log.warning(
                "%s Agent Loop 未产出 final_text, fallback (总耗时 %.3fs)",
                trace.prefix(), total_elapsed,
            )
            final_text = "⚠️ 处理超时，请重试"

        log.info("%s Agent Loop 完成, 输出 %d 字符 (总耗时 %.3fs)",
                 trace.prefix(), len(final_text), total_elapsed)
        return final_text


# ============================================================
# 内部辅助函数
# ============================================================


@dataclass
class ParsedResponse:
    """LLM 响应的结构化解析结果"""

    texts: list[str] = field(default_factory=list)  # 所有 TextBlock 文本（按顺序）
    tool_calls: list[dict] = field(default_factory=list)  # 所有 ToolUseBlock


def _parse_response(response) -> ParsedResponse:
    """把 LLM 响应拆成结构化数据

    处理三种 block:
      - thinking: 思考过程，直接跳过
      - text: 收集 .text（去空）
      - tool_use: 收集 (id, name, input)

    用 getattr 校验 type 是因为 block 是 SDK 的 Pydantic 联合类型，类型分支由 type 字段决定。
    其他字段 (text, id, name, input) 在 type 匹配后是 SDK 强保证的，可直接读。
    """
    parsed = ParsedResponse()
    for block in response.content:
        block_type = getattr(block, "type", None)

        if block_type == "thinking":
            continue
        elif block_type == "text":
            if block.text.strip():
                parsed.texts.append(block.text)
        elif block_type == "tool_use":
            parsed.tool_calls.append(
                {"id": block.id, "name": block.name, "input": dict(block.input)}
            )
    return parsed


def _strip_tool_call_xml(text: str) -> str:
    """过滤 step-3.7-flash 等国产模型意外输出的 invoke XML 字符串

    背景:
      step-3.7-flash / Qwen / DeepSeek 等模型训练时用 ChatML 模板, 偶发在
      tools=[] 模式下把"伪 tool call"按训练痕迹直接输出成 XML 字符串
      (而非结构化 tool_use block)。Anthropic SDK 只看 tool_use block, 不会
      parse 这段, 所以会作为普通 text 漏给用户。

    设计:
      - 非贪婪 + re.DOTALL: 一次只剥一个块, 不会误伤中间的有效文本
      - 快路径: 不含 "<tool_call>" 子串时直接返回, 避免热路径 regex 开销
      - 整段都是 XML (剥后空): 由调用方决定走兜底, 本函数不抛、不改语义
    """
    if "<tool_call>" not in text:
        return text
    return _RE_TOOL_CALL_XML.sub("", text)


def _strip_thinking_tags(text: str) -> str:
    """过滤 step-3.7-flash 等模型把 thinking 过程作为普通 text 输出的部分

    背景:
      Anthropic Claude 有结构化 ThinkingBlock (block.type == "thinking"),
      SDK 自动识别。Anthropic 兼容 API 下的国产模型 (step-3.7-flash / Qwen 等)
      训练时把思考过程也作为普通 text 输出, 通常包裹在 <think>...</think> 里,
      SDK 不会解析, 整段内心独白会漏到频道。

    设计同 _strip_tool_call_xml:
      - 非贪婪 + re.DOTALL: 一次剥一个块, 不误伤中间的有效文本
      - 快路径: 不含 "<think" 子串时直接返回
      - 剥后空由调用方走兜底
    """
    if "<think>" not in text:
        return text
    return _RE_THINKING.sub("", text)


def _strip_model_artifacts(text: str) -> str:
    """组合入口: 一次剥掉所有已知的国产模型训练痕迹输出

    顺序: 先剥 tool_call XML (历史更长, 命中更多), 再剥 thinking 标签
    (step-3.7-flash 偶发会同时出现两种)。两个剥后空都交给调用方走兜底。
    """
    return _strip_thinking_tags(_strip_tool_call_xml(text))
