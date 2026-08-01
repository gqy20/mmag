"""Deterministic Mattermost rendering for platform-neutral response views."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ..client import PROP_FROM_BOT, PROP_TRUE
from .views import ResponseAction, ResponseView, RunStatus

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_FENCE = re.compile(r"^\s*(```|~~~)")
_MAX_FIELD = 4_000


@dataclass(frozen=True, slots=True)
class RenderedResponse:
    chunks: tuple[str, ...]
    props: dict[str, Any]
    artifact_refs: tuple[str, ...]
    actions: tuple[dict[str, Any], ...]


class MattermostRenderer:
    def __init__(
        self,
        *,
        max_chars: int = 12_000,
        action_callback_url: str = "",
    ) -> None:
        if max_chars < 1_000:
            raise ValueError("Mattermost chunk size must be at least 1000 characters")
        self.max_chars = max_chars
        self.action_callback_url = self.safe_url(action_callback_url)

    def render(self, view: ResponseView) -> RenderedResponse:
        markdown = self._markdown(view)
        actions = self._actions(view.actions)
        props: dict[str, Any] = {
            PROP_FROM_BOT: PROP_TRUE,
            "mmag_kind": view.kind.value,
            "mmag_status": view.status.value,
        }
        if view.run_id:
            props["mmag_run_id"] = self.clean(view.run_id, 256)
        if actions:
            props["attachments"] = [
                {
                    "text": "请选择操作；如果按钮不可用，可使用上方文本命令。",
                    "actions": list(actions),
                }
            ]
        return RenderedResponse(
            chunks=split_markdown(markdown, self.max_chars),
            props=props,
            artifact_refs=tuple(item.ref for item in view.artifacts if item.deliver),
            actions=actions,
        )

    def _markdown(self, view: ResponseView) -> str:
        status_icon = {
            RunStatus.RUNNING: "⏳",
            RunStatus.WAITING_APPROVAL: "⏸️",
            RunStatus.SUCCEEDED: "✅",
            RunStatus.EXHAUSTED: "⚠️",
            RunStatus.FAILED: "⚠️",
        }[view.status]
        lines = [f"### {status_icon} {self.clean(view.title)}", "", self.clean(view.summary)]
        for section in view.sections:
            if not section.body and not section.items:
                continue
            lines.extend(("", f"#### {self.clean(section.title)}"))
            if section.body:
                lines.extend(("", self.clean(section.body)))
            for item in section.items:
                lines.append(f"- {self.clean(item)}")
        if view.sources:
            lines.extend(("", "#### 来源"))
            for source in view.sources:
                label = self.clean(source.title, 300)
                ref = self.safe_url(source.ref)
                suffix = f" — {self.clean(source.detail, 300)}" if source.detail else ""
                lines.append(f"- [{label}]({ref}){suffix}" if ref else f"- {label}{suffix}")
        if view.artifacts:
            lines.extend(("", "#### 产物"))
            for artifact in view.artifacts:
                size = f" · {artifact.size_bytes} bytes" if artifact.size_bytes else ""
                lines.append(
                    f"- 📎 {self.clean(artifact.filename, 500)}"
                    f" · `{self.clean(artifact.ref, 200)}`{size}"
                )
        if view.warnings:
            lines.extend(("", "#### 注意"))
            lines.extend(f"- ⚠️ {self.clean(item)}" for item in view.warnings)
        if view.actions:
            fallbacks = [self.clean(item.fallback) for item in view.actions if item.fallback]
            if fallbacks:
                lines.extend(("", "#### 可用操作", *dict.fromkeys(fallbacks)))
        if view.run_id:
            lines.extend(("", f"Run：`{self.clean(view.run_id, 256)}`"))
        return "\n".join(lines).strip()

    def _actions(self, actions: tuple[ResponseAction, ...]) -> tuple[dict[str, Any], ...]:
        if not self.action_callback_url:
            return ()
        rendered: list[dict[str, Any]] = []
        for action in actions[:5]:
            if not action.token:
                continue
            item: dict[str, Any] = {
                "id": self.clean(action.id, 64),
                "name": self.clean(action.label, 80),
                "integration": {
                    "url": self.action_callback_url,
                    "context": {"token": action.token},
                },
            }
            if action.style in {"primary", "success", "danger", "warning"}:
                item["style"] = action.style
            rendered.append(item)
        return tuple(rendered)

    @staticmethod
    def clean(value: str, limit: int = _MAX_FIELD) -> str:
        text = _CONTROL.sub("", str(value or "")).replace("@", "@\u200b")
        text = text.replace("<", "&lt;").replace(">", "&gt;")
        text = re.sub(r"([\\`*_\[\]()#!|~])", r"\\\1", text)
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)].rstrip() + "…"

    @staticmethod
    def safe_url(value: str) -> str:
        try:
            parsed = urlsplit(str(value or "").strip())
        except ValueError:
            return ""
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        if parsed.username or parsed.password:
            return ""
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def split_markdown(markdown: str, max_chars: int) -> tuple[str, ...]:
    """Split on line boundaries while closing and reopening fenced code blocks."""
    if len(markdown) <= max_chars:
        return (markdown,)
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    fence = ""

    def flush() -> None:
        nonlocal current, current_len
        if not current:
            return
        if fence:
            current.append(fence)
        chunks.append("\n".join(current).strip())
        current = [fence] if fence else []
        current_len = len(fence) + 1 if fence else 0

    for line in markdown.splitlines():
        pieces = _split_line(line, max_chars - 16)
        for piece in pieces:
            needed = len(piece) + (1 if current else 0)
            reserve = len(fence) + 1 if fence else 0
            if current and current_len + needed + reserve > max_chars:
                flush()
            current.append(piece)
            current_len += needed
            match = _FENCE.match(piece)
            if match:
                marker = match.group(1)
                fence = "" if fence else marker
    flush()
    return tuple(chunk for chunk in chunks if chunk)


def _split_line(line: str, limit: int) -> tuple[str, ...]:
    if len(line) <= limit:
        return (line,)
    pieces: list[str] = []
    remaining = line
    while len(remaining) > limit:
        cut = remaining.rfind(" ", 0, limit + 1)
        if cut < limit // 2:
            cut = limit
        pieces.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        pieces.append(remaining)
    return tuple(pieces)
