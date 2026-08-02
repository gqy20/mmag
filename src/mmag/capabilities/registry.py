"""Runtime-visible capability bindings and their canonical registry."""

from __future__ import annotations

import time
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .base import CapabilityAuthorization, CapabilityExecutor, CapabilitySpec

from ..logger import get_logger, log_event, safe_hash
from .base import CapabilityResult, CapabilityStatus
from .sources import enrich_with_sources

log = get_logger(__name__)


@dataclass(frozen=True)
class CapabilityBinding:
    """One capability projected onto a model runtime surface."""

    capability: CapabilitySpec
    executor: CapabilityExecutor

    @property
    def name(self) -> str:
        return self.capability.name

    @property
    def description(self) -> str:
        return self.capability.description

    @property
    def input_schema(self) -> Mapping[str, Any]:
        return self.capability.input_schema


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

    def authorization(self, name: str, input_data: dict[str, Any]) -> CapabilityAuthorization:
        """Return a capability policy decision without running the tool."""
        binding = self.get(name)
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
        self, name: str, input_data: dict[str, Any], *, preauthorized: bool = False
    ) -> CapabilityResult:
        """Execute one capability and preserve its typed result until a transport boundary."""
        binding = self._bindings.get(name)
        if not binding:
            log_event(
                log,
                "capability.unknown",
                level=30,
                status="rejected",
                capability=name,
                error_code="UNKNOWN_CAPABILITY",
            )
            return CapabilityResult(
                CapabilityStatus.ERROR,
                message=f"Unknown capability: {name}",
            )

        t0 = time.monotonic()
        input_sha256 = safe_hash(input_data)
        log_event(
            log,
            "capability.started",
            status="running",
            capability=name,
            input_keys=sorted(input_data),
            input_sha256=input_sha256,
        )

        try:
            if preauthorized:
                result = await binding.executor.execute_approved(
                    binding.capability, input_data
                )
            else:
                result = await binding.executor.execute(binding.capability, input_data)

            if result.status is CapabilityStatus.SUCCESS:
                result = CapabilityResult(
                    result.status,
                    data=enrich_with_sources(result.data, name, input_data),
                    message=result.message,
                    duration_ms=result.duration_ms,
                )

            elapsed = time.monotonic() - t0
            if result.duration_ms == 0:
                result = CapabilityResult(
                    result.status,
                    data=result.data,
                    message=result.message,
                    duration_ms=round(elapsed * 1000),
                )
            log_event(
                log,
                "capability.completed",
                status=result.status.value,
                capability=name,
                duration_ms=round(elapsed * 1000),
                input_sha256=input_sha256,
                output_type=type(result.data).__name__,
            )
            return result

        except Exception as e:
            elapsed = time.monotonic() - t0
            log_event(
                log,
                "capability.failed",
                level=40,
                status="failed",
                capability=name,
                duration_ms=round(elapsed * 1000),
                input_sha256=input_sha256,
                error_code=type(e).__name__,
            )
            return CapabilityResult(
                CapabilityStatus.ERROR,
                message=f"Capability failed: {type(e).__name__}",
            )
