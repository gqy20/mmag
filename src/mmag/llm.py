"""
LLM 适配器 (Anthropic)
"""

import asyncio
import logging
from typing import Any

from anthropic import Anthropic

from .config import config

log = logging.getLogger("agent")


class LLM:
    """Anthropic Claude 封装"""

    def __init__(self):
        kwargs: dict[str, Any] = {"api_key": config.anthropic_api_key}
        if config.anthropic_base_url:
            kwargs["base_url"] = config.anthropic_base_url
        self.client = Anthropic(**kwargs)
        self.model = config.anthropic_model
        self.call_count = 0
        log.info(f"LLM 初始化完成 | 模型: {self.model}")

    async def chat(self, messages: list[dict], system: str = "",
                   max_tokens: int = 1024) -> str:
        """普通对话

        注意: StepFun 等兼容 API 可能返回 ThinkingBlock (思考过程)，
        需要过滤掉，只取 TextBlock 的文本内容。
        """
        self.call_count += 1
        try:
            kwargs: dict[str, Any] = {
                "model": self.model,
                "max_tokens": max_tokens,
                "messages": messages,
            }
            if system:
                kwargs["system"] = system
            response = await asyncio.to_thread(self.client.messages.create, **kwargs)
            # 提取所有文本块的内容 (跳过 ThinkingBlock 等)
            texts = []
            for block in response.content:
                if hasattr(block, "text"):
                    texts.append(block.text)
            result = "\n".join(texts).strip()
            return result if result else "(模型返回为空)"
        except Exception as e:
            log.error(f"LLM 调用失败: {e}")
            return f"⚠️ LLM 服务暂时不可用: {e}"

    async def chat_with_system(self, system_prompt: str, user_message: str,
                                max_tokens: int = 1024) -> str:
        """带系统提示词的对话快捷方法"""
        return await self.chat(
            messages=[{"role": "user", "content": user_message}],
            system=system_prompt,
            max_tokens=max_tokens,
        )
