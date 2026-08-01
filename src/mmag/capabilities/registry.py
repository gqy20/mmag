"""Runtime-visible capability bindings and their canonical registry."""

from __future__ import annotations

import hashlib
import inspect
import json
import time
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from .base import CapabilityAuthorization, CapabilityExecutor, CapabilitySpec

from ..logger import get_logger, trace
from .sources import enrich_with_sources

log = get_logger(__name__)


@dataclass
class CapabilityBinding:
    """One capability projected onto a model runtime surface."""

    name: str
    description: str
    input_schema: Mapping[str, Any]
    handler: Callable[..., Any]
    capability: CapabilitySpec | None = None
    executor: CapabilityExecutor | None = None


class CapabilityRegistry:
    """Canonical runtime registry for built-in and MCP capabilities."""

    def __init__(self):
        self._bindings: dict[str, CapabilityBinding] = {}

    def register(self, binding: CapabilityBinding) -> None:
        if binding.name in self._bindings:
            raise ValueError(f"capability binding {binding.name!r} is already registered")
        self._bindings[binding.name] = binding
        log.info(
            "Capability 已注册: %s (%d 参数)",
            binding.name,
            len(binding.input_schema.get("properties", {})),
        )

    def unregister(self, name: str) -> bool:
        """注销工具，返回是否存在并被删除"""
        if name in self._bindings:
            del self._bindings[name]
            return True
        return False

    def unregister_prefix(self, prefix: str) -> int:
        """注销所有 name 以 prefix 开头的工具，返回注销数量"""
        names = [name for name in self._bindings if name.startswith(prefix)]
        for n in names:
            del self._bindings[n]
        return len(names)

    def get_all(self) -> list[CapabilityBinding]:
        return list(self._bindings.values())

    def get(self, name: str) -> CapabilityBinding:
        try:
            return self._bindings[name]
        except KeyError as error:
            raise LookupError(f"unknown capability binding {name!r}") from error

    def names(self) -> tuple[str, ...]:
        return tuple(self._bindings)

    def resolve_names(
        self,
        allow: tuple[str, ...],
        deny: tuple[str, ...] = (),
        *,
        additional_names: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        """Resolve manifest patterns against the deployed capability catalog."""
        candidates = tuple(dict.fromkeys((*self._bindings, *additional_names)))
        missing = tuple(
            pattern
            for pattern in allow
            if not any(token in pattern for token in "*?[") and pattern not in candidates
        )
        if missing:
            raise LookupError(f"unknown allowed capabilities: {', '.join(sorted(missing))}")
        return tuple(
            name
            for name in candidates
            if any(fnmatch(name, pattern) for pattern in allow)
            and not any(fnmatch(name, pattern) for pattern in deny)
        )

    def authorization(
        self, name: str, input_data: dict[str, Any]
    ) -> CapabilityAuthorization | None:
        """Return a capability policy decision without running the tool."""
        binding = self._bindings.get(name)
        if binding is None or binding.capability is None or binding.executor is None:
            return None
        return binding.executor.authorize(binding.capability, input_data)

    def get_schema_list(self, names: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        """Project the selected capability allowlist onto the model tool schema."""
        selected = set(names) if names is not None else None
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in self._bindings.values()
            if selected is None or t.name in selected
        ]

    async def execute(
        self, name: str, input_data: dict[str, Any], *, approval_granted: bool = False
    ) -> str:
        """
        执行指定工具并返回结果字符串。

        Returns:
            工具执行的 JSON 字符串结果，或错误信息。
        """
        binding = self._bindings.get(name)
        if not binding:
            log.warning("%s 未知工具: %s", trace.prefix(), name)
            return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)

        t0 = time.monotonic()
        log.info(
            "%s 调用工具: %s keys=%s input_sha256=%s",
            trace.prefix(),
            name,
            sorted(input_data),
            hashlib.sha256(
                json.dumps(
                    input_data,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode()
            ).hexdigest()[:16],
        )

        try:
            if approval_granted and binding.capability is not None and binding.executor is not None:
                capability_result = await binding.executor.execute_approved(
                    binding.capability, input_data
                )
                result = capability_result.to_payload()
            else:
                result = binding.handler(**input_data)
            # async generator 不是 awaitable，需要先逐项收集。
            if inspect.isasyncgen(result):
                result = [item async for item in result]
            elif inspect.isawaitable(result):
                result = await result

            # 为外部数据工具注入结构化来源元数据（_sources 字段）
            # 本地工具（get_posts 等）会静默跳过，零额外开销
            result = enrich_with_sources(result, name, input_data)

            # 统一序列化为 JSON 字符串
            if isinstance(result, (dict, list)):
                result_str = json.dumps(result, ensure_ascii=False, default=str)
            elif isinstance(result, str):
                result_str = result
            else:
                result_str = json.dumps({"result": result}, ensure_ascii=False, default=str)

            elapsed = time.monotonic() - t0
            log.info(
                "%s 工具完成: %s (%.3fs, 结果 %d 字符)",
                trace.prefix(),
                name,
                elapsed,
                len(result_str),
            )
            return result_str

        except Exception as e:
            elapsed = time.monotonic() - t0
            log.error(
                "%s 工具 '%s' 执行失败 (%.3fs): %s", trace.prefix(), name, elapsed, e, exc_info=True
            )
            return json.dumps(
                {"error": f"工具执行错误: {type(e).__name__}: {e}"},
                ensure_ascii=False,
            )
