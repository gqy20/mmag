"""
LLM 适配器 (Anthropic) — 支持单轮对话 + Agentic Tool Use 循环
"""

import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from anthropic import AsyncAnthropic

# step-3.7-flash 等国产模型训练痕迹: 在 tools=[] 模式下, 偶发按 ChatML 模板
# 把"伪 tool call"直接输出成 XML 字符串 (而非结构化 tool_use block)。
# Anthropic SDK 只看结构化 tool_use, 不会 parse 这段, 会作为普通 text 漏给用户。
# 业界做法: Qwen-Agent / Step-Agent-SDK 在 SDK 层做 XML → 结构化 call 的反向解析。
# mmag 走的是 Anthropic SDK, 没这能力, 所以在 return 前兜底过滤。
_RE_TOOL_CALL_XML = re.compile(r"<tool_call>\s*.*?\s*</tool_call>", re.DOTALL)

from .config import config
from .logger import get_logger, trace
from .tools import ToolRegistry

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
            # 过滤 step-3.7-flash 训练痕迹的 <tool_call> XML 噪声,
            # 剥后空就保持哨兵, 让上层 Plan D 兜底决定是否重试
            cleaned = _strip_tool_call_xml(raw_text).strip()
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

    # ---- Agentic Tool Use 循环 ----

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
        # 无工具时降级为普通聊天
        if not tools or not tool_registry:
            log.debug("%s agent_loop: 无工具配置，降级为普通聊天", trace.prefix())
            return await self.chat(messages, system, max_tokens)

        tools_schema = [t for t in tools if isinstance(t, dict)]
        working_messages = list(messages)  # 拷贝，避免污染原始消息
        loop_t0 = time.monotonic()

        log.info(
            "%s Agent Loop 开始 | 消息数=%d 工具数=%d 最大轮次=%d",
            trace.prefix(),
            len(working_messages),
            len(tools_schema),
            max_rounds,
        )

        for round_i in range(1, max_rounds + 1):
            self.call_count += 1
            round_t0 = time.monotonic()

            # ---- 末轮强制收尾 (B-1 方案) ----
            # step-3.7-flash 等模型在带 tools 参数时, 拿到 tool_result 后倾向继续 tool_use
            # 而不出 text, 出现 R2 (以及后续轮) 反复调工具的"死循环"。
            # 最后一轮临时禁用 tools 调一次, 让模型必须出 text:
            #   - 有 text → 直接用 (用户得到的是模型自己的"智能收尾"答复)
            #   - 无 text (兜底) → 返回"处理超时"提示
            # 灵感: LangGraph chat_agent_executor._are_more_steps_needed
            #       (remaining_steps < 2 && has_tool_calls 时直接 override 响应)
            is_final_round = round_i == max_rounds
            tools_for_this_round: list[Any] = [] if is_final_round else tools_schema

            try:
                kwargs: dict[str, Any] = {
                    "model": self.model,
                    "max_tokens": max_tokens,
                    "messages": working_messages,
                    "tools": tools_for_this_round,
                }
                if system:
                    kwargs["system"] = system

                response = await self.client.messages.create(**kwargs)
            except Exception as e:
                elapsed = time.monotonic() - round_t0
                log.error(
                    "%s Round %d/%d LLM 调用失败 (%.3fs): %s",
                    trace.prefix(),
                    round_i,
                    max_rounds,
                    elapsed,
                    e,
                    exc_info=True,
                )
                raise LLMError(f"Round {round_i} LLM 调用失败: {e}") from e

            # ---- 解析响应 (用 _parse_response 统一处理 thinking/text/tool_use) ----
            parsed = _parse_response(response)
            text_parts = parsed.texts
            tool_calls = parsed.tool_calls

            round_elapsed = time.monotonic() - round_t0

            # ---- 末轮强制收尾: 哪怕模型在 tools=[] 模式下还调了工具, 也不再执行 ----
            if is_final_round:
                # 过滤 step-3.7-flash 训练痕迹的 <tool_call> XML 噪声
                final_text = _strip_tool_call_xml("\n".join(text_parts)).strip()
                total_elapsed = time.monotonic() - loop_t0
                if final_text:
                    log.info(
                        "%s Round %d/%d 末轮强制收尾, 取到 text %d 字符 (%.3fs) | 总 %.3fs",
                        trace.prefix(),
                        round_i,
                        max_rounds,
                        len(final_text),
                        round_elapsed,
                        total_elapsed,
                    )
                    return final_text
                # 末轮 + 过滤后空 (整段都是 XML 或本来就没 text): 兜底,
                # 不让 "(模型返回为空)" 这种内部哨兵漏给用户
                log.warning(
                    "%s Round %d/%d 末轮强制收尾仍无 text, fallback (%.3fs)",
                    trace.prefix(),
                    round_i,
                    max_rounds,
                    round_elapsed,
                )
                return "⚠️ 处理超时，请重试"

            # ---- 判断是否需要继续循环 ----
            if not tool_calls:
                # 过滤 step-3.7-flash 训练痕迹的 <tool_call> XML 噪声
                final_text = _strip_tool_call_xml("\n".join(text_parts)).strip()
                total_elapsed = time.monotonic() - loop_t0
                log.info(
                    "%s Round %d/%d → 纯文本回复 (%.3fs) | 总耗时 %.3fs | 输出 %d 字符",
                    trace.prefix(),
                    round_i,
                    max_rounds,
                    round_elapsed,
                    total_elapsed,
                    len(final_text),
                )
                if final_text:
                    return final_text
                # step-3.7-flash 等模型偶发 R1 直接出空 text (无 tool_call 也没 text),
                # 或整段都是 <tool_call> XML (剥后空): 走这里 — 不再把
                # "(模型返回为空)" 这种内部哨兵漏给用户, 统一兜底
                log.warning(
                    "%s Round %d/%d → 纯文本回复但 text 为空, fallback (%.3fs)",
                    trace.prefix(),
                    round_i,
                    max_rounds,
                    round_elapsed,
                )
                return "⚠️ 处理超时，请重试"

            # ---- 有工具调用 → 执行 → 注入结果 → 继续下一轮 ----
            tool_names = [tc["name"] for tc in tool_calls]
            log.info(
                "%s Round %d/%d → %d 个工具调用 [%s] (%.3fs)",
                trace.prefix(),
                round_i,
                max_rounds,
                len(tool_calls),
                ", ".join(tool_names),
                round_elapsed,
            )

            # 把 LLM 这一轮的完整响应加入历史（含文本 + 工具调用意图）
            # 用 SDK 的 TypedDict 标注，保留裸 dict 形式（runtime 不验证，pylance/mypy 能检查）
            assistant_content: list[TextBlockParam | ToolUseBlockParam] = []
            if text_parts:
                assistant_content.append({"type": "text", "text": "\n".join(text_parts)})
            for tc in tool_calls:
                assistant_content.append(
                    cast(
                        "ToolUseBlockParam",
                        {
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": tc["name"],
                            "input": tc["input"],
                        },
                    )
                )
            working_messages.append(
                cast("MessageParam", {"role": "assistant", "content": assistant_content})
            )

            # 逐个执行工具，收集结果
            for tc in tool_calls:
                result_str = await tool_registry.execute(tc["name"], tc["input"])
                tool_result_content: list[ToolResultBlockParam] = [
                    cast(
                        "ToolResultBlockParam",
                        {
                            "type": "tool_result",
                            "tool_use_id": tc["id"],
                            "content": result_str,
                        },
                    )
                ]
                working_messages.append(
                    cast("MessageParam", {"role": "user", "content": tool_result_content})
                )

        # 达到最大轮次限制
        total_elapsed = time.monotonic() - loop_t0
        log.warning(
            "%s Agent Loop 达到最大轮次 %d (总耗时 %.3fs)，强制结束",
            trace.prefix(),
            max_rounds,
            total_elapsed,
        )
        # 取最后一轮的文本部分作为最终回复
        last_texts = []
        for msg in reversed(working_messages):
            if msg.get("role") == "assistant":
                content = msg.get("content", [])
                if isinstance(content, list):
                    for c in content:
                        if (
                            isinstance(c, dict)
                            and c.get("type") == "text"
                            and c.get("text", "").strip()
                        ):
                            last_texts.append(c["text"])
                elif isinstance(content, str) and content.strip():
                    last_texts.append(content)
                break
        # 过滤 step-3.7-flash 训练痕迹的 <tool_call> XML 噪声;
        # 剥后空走兜底, 不让 XML 漏给用户
        cleaned = _strip_tool_call_xml("\n".join(reversed(last_texts))).strip()
        return cleaned if cleaned else "⚠️ 处理超时，请重试"


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
    """过滤 step-3.7-flash 等国产模型意外输出的 <tool_call>...</tool_call> XML 字符串

    背景:
      step-3.7-flash / Qwen / DeepSeek 等模型训练时用 ChatML 模板, 偶发在
      tools=[] 模式下把"伪 tool call"按训练痕迹直接输出成 XML 字符串
      (而非结构化 tool_use block)。Anthropic SDK 只看 tool_use block, 不会
      parse 这段, 所以会作为普通 text 漏给用户。

      业界做法: Qwen-Agent / Step-Agent-SDK 在 SDK 层做 XML → 结构化 call 的
      反向解析。mmag 走的是 Anthropic SDK, 没这能力, 所以在 return 前兜底过滤。

    设计:
      - 非贪婪 + re.DOTALL: 一次只剥一个块, 不会误伤中间的有效文本
      - 快路径: 不含 "<tool_call>" 子串时直接返回, 避免热路径 regex 开销
      - 整段都是 XML (剥后空): 由调用方决定走兜底, 本函数不抛、不改语义
    """
    if "<tool_call>" not in text:
        return text
    return _RE_TOOL_CALL_XML.sub("", text)
