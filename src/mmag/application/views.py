"""Platform-neutral response contracts and Agent output presentation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..agent_system import AgentOutput


class ResponseKind(StrEnum):
    RESULT = "result"
    STATUS = "status"
    APPROVAL = "approval"
    ERROR = "error"


class RunStatus(StrEnum):
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    EXHAUSTED = "exhausted"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ResponseSection:
    title: str
    body: str = ""
    items: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResponseSource:
    title: str
    ref: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ResponseArtifact:
    ref: str
    filename: str
    kind: str = ""
    media_type: str = "application/octet-stream"
    size_bytes: int = 0
    deliver: bool = False


@dataclass(frozen=True, slots=True)
class ResponseAction:
    id: str
    label: str
    action: str
    target: str
    style: str = "default"
    fallback: str = ""
    token: str = ""


@dataclass(frozen=True, slots=True)
class ResponseView:
    kind: ResponseKind
    title: str
    summary: str
    status: RunStatus
    run_id: str = ""
    sections: tuple[ResponseSection, ...] = ()
    sources: tuple[ResponseSource, ...] = ()
    warnings: tuple[str, ...] = ()
    artifacts: tuple[ResponseArtifact, ...] = ()
    actions: tuple[ResponseAction, ...] = ()


class ResponsePresenter:
    """Turn governed Agent contracts into a stable user-facing view."""

    def present(self, output: AgentOutput, *, run_id: str = "") -> ResponseView:
        result = output.result or {}
        envelope = output.envelope or {}
        status = self._status(output)
        artifacts = self._artifacts(output.artifacts, output.runtime_result)
        warnings = self._strings(envelope.get("warnings"))
        if output.agent_name == "link":
            return self._link(result, run_id, status, artifacts, warnings)
        if output.agent_name == "report" and "message_range" in result:
            return self._meeting(result, run_id, status, artifacts, warnings)
        if output.agent_name == "report":
            return self._report(result, run_id, status, artifacts, warnings)
        if output.agent_name == "ppt":
            return self._ppt(result, run_id, status, artifacts, warnings)
        if output.agent_name == "project":
            return self._project(
                result,
                run_id,
                status,
                artifacts,
                warnings,
                fallback_text=output.text,
            )
        return self._generic(output, result, run_id, status, artifacts, warnings)

    @staticmethod
    def approval(
        *,
        capability: str,
        approval_id: str,
        run_id: str,
        actions: tuple[ResponseAction, ...] = (),
    ) -> ResponseView:
        fallback = f"回复 `批准 {approval_id}` 或 `拒绝 {approval_id}`。"
        return ResponseView(
            kind=ResponseKind.APPROVAL,
            title="需要审批",
            summary=f"操作 `{capability}` 正在等待人工确认。",
            status=RunStatus.WAITING_APPROVAL,
            run_id=run_id,
            sections=(ResponseSection("文本操作", fallback),),
            actions=actions,
        )

    @staticmethod
    def error(
        *,
        title: str,
        summary: str,
        run_id: str,
        warnings: tuple[str, ...] = (),
    ) -> ResponseView:
        return ResponseView(
            kind=ResponseKind.ERROR,
            title=title,
            summary=summary,
            status=RunStatus.FAILED,
            run_id=run_id,
            warnings=warnings,
        )

    @staticmethod
    def status(*, summary: str, run_id: str) -> ResponseView:
        return ResponseView(
            kind=ResponseKind.STATUS,
            title="正在处理",
            summary=summary,
            status=RunStatus.RUNNING,
            run_id=run_id,
        )

    def _generic(
        self,
        output: AgentOutput,
        result: Mapping[str, Any],
        run_id: str,
        status: RunStatus,
        artifacts: tuple[ResponseArtifact, ...],
        warnings: tuple[str, ...],
    ) -> ResponseView:
        summary = self._text(result.get("summary")) or self._text(result.get("text"))
        if not summary:
            summary = output.text if output.result is None else "任务已完成。"
        return ResponseView(
            kind=ResponseKind.RESULT,
            title=self._text(result.get("title")) or "处理结果",
            summary=summary,
            status=status,
            run_id=run_id,
            sections=self._remaining_sections(result, {"title", "summary", "text"}),
            warnings=warnings,
            artifacts=artifacts,
        )

    def _link(
        self,
        result: Mapping[str, Any],
        run_id: str,
        status: RunStatus,
        artifacts: tuple[ResponseArtifact, ...],
        warnings: tuple[str, ...],
    ) -> ResponseView:
        source = self._text(result.get("url"))
        stats = result.get("stats")
        sections: list[ResponseSection] = []
        if isinstance(stats, dict):
            sections.append(ResponseSection("关键数据", items=self._mapping_items(stats)))
        details = result.get("repo_info") or result.get("issue_info") or result.get("webpage")
        if isinstance(details, dict):
            sections.append(ResponseSection("详情", items=self._mapping_items(details)))
        link_warnings = list(warnings)
        if result.get("status") not in {None, "ok"}:
            link_warnings.append(self._text(result.get("error")) or "链接读取未成功。")
        return ResponseView(
            kind=ResponseKind.RESULT,
            title=self._text(result.get("title")) or "链接分析",
            summary=self._text(result.get("summary")) or "未提取到摘要。",
            status=status,
            run_id=run_id,
            sections=tuple(sections),
            sources=(ResponseSource("原始链接", source),) if source else (),
            warnings=tuple(link_warnings),
            artifacts=artifacts,
        )

    def _report(
        self,
        result: Mapping[str, Any],
        run_id: str,
        status: RunStatus,
        artifacts: tuple[ResponseArtifact, ...],
        warnings: tuple[str, ...],
    ) -> ResponseView:
        findings: list[str] = []
        for item in self._mappings(result.get("findings")):
            claim = self._text(item.get("claim"))
            confidence = self._text(item.get("confidence"))
            if claim:
                findings.append(f"{claim}（置信度：{confidence or 'unknown'}）")
        sections = [ResponseSection("关键发现", items=tuple(findings))]
        for key, title in (("recommendations", "建议"), ("limitations", "局限")):
            items = self._strings(result.get(key))
            if items:
                sections.append(ResponseSection(title, items=items))
        sources = tuple(
            ResponseSource(
                self._text(item.get("title")) or self._text(item.get("id")) or "来源",
                self._text(item.get("ref")),
                self._text(item.get("source_type")),
            )
            for item in self._mappings(result.get("sources"))
            if self._text(item.get("ref"))
        )
        return ResponseView(
            kind=ResponseKind.RESULT,
            title=self._text(result.get("title")) or "研究报告",
            summary=self._text(result.get("executive_summary")) or "研究已完成。",
            status=status,
            run_id=run_id,
            sections=tuple(section for section in sections if section.items or section.body),
            sources=sources,
            warnings=warnings,
            artifacts=artifacts,
        )

    def _meeting(
        self,
        result: Mapping[str, Any],
        run_id: str,
        status: RunStatus,
        artifacts: tuple[ResponseArtifact, ...],
        warnings: tuple[str, ...],
    ) -> ResponseView:
        def sourced(items: object) -> tuple[str, ...]:
            values: list[str] = []
            for item in self._mappings(items):
                content = self._text(item.get("content"))
                refs = self._strings(item.get("source_post_ids"))
                if content:
                    values.append(f"{content}（来源：{', '.join(refs)}）" if refs else content)
            return tuple(values)

        actions: list[str] = []
        for item in self._mappings(result.get("action_items")):
            content = self._text(item.get("content"))
            owner = self._text(item.get("owner_username")) or "未提及"
            due = self._text(item.get("due_text")) or "未指定"
            refs = self._strings(item.get("source_post_ids"))
            if content:
                source = f"；来源：{', '.join(refs)}" if refs else ""
                actions.append(f"{content}（相关人物：{owner}；截止：{due}{source}）")
        sections = (
            ResponseSection("已确认决定", items=sourced(result.get("decisions"))),
            ResponseSection("行动项", items=tuple(actions)),
            ResponseSection("开放问题", items=sourced(result.get("open_questions"))),
            ResponseSection("覆盖说明", items=self._strings(result.get("coverage_notes"))),
        )
        return ResponseView(
            kind=ResponseKind.RESULT,
            title=self._text(result.get("title")) or "会议纪要",
            summary=self._text(result.get("summary")) or "讨论总结已完成。",
            status=status,
            run_id=run_id,
            sections=tuple(section for section in sections if section.items),
            warnings=warnings,
            artifacts=artifacts,
        )

    def _ppt(
        self,
        result: Mapping[str, Any],
        run_id: str,
        status: RunStatus,
        artifacts: tuple[ResponseArtifact, ...],
        warnings: tuple[str, ...],
    ) -> ResponseView:
        slides = self._mappings(result.get("slides"))
        slide_items = tuple(
            f"{self._text(item.get('number'))}. {self._text(item.get('title'))}"
            for item in slides
            if self._text(item.get("title"))
        )
        sections = (
            ResponseSection("目标受众", self._text(result.get("audience"))),
            ResponseSection("目标", self._text(result.get("objective"))),
            ResponseSection("页面结构", items=slide_items),
        )
        return ResponseView(
            kind=ResponseKind.RESULT,
            title=self._text(result.get("title")) or "演示文稿",
            summary=self._text(result.get("narrative")) or "演示文稿已生成。",
            status=status,
            run_id=run_id,
            sections=tuple(section for section in sections if section.body or section.items),
            warnings=warnings,
            artifacts=artifacts,
        )

    def _project(
        self,
        result: Mapping[str, Any],
        run_id: str,
        status: RunStatus,
        artifacts: tuple[ResponseArtifact, ...],
        warnings: tuple[str, ...],
        *,
        fallback_text: str = "",
    ) -> ResponseView:
        sections: list[ResponseSection] = []
        for key, title in (
            ("goals", "目标"),
            ("milestones", "里程碑"),
            ("tasks", "任务"),
            ("risks", "风险"),
            ("decisions", "决策"),
            ("open_questions", "待确认"),
            ("next_actions", "下一步"),
        ):
            items = self._value_items(result.get(key))
            if items:
                sections.append(ResponseSection(title, items=items))
        project_status = self._text(result.get("status"))
        return ResponseView(
            kind=ResponseKind.RESULT,
            title=self._text(result.get("name")) or "项目计划",
            summary=self._text(result.get("summary")) or self._text(fallback_text) or "计划已生成。",
            status=status,
            run_id=run_id,
            sections=tuple(sections),
            warnings=warnings + ((f"项目状态：{project_status}",) if project_status else ()),
            artifacts=artifacts,
        )

    def _remaining_sections(
        self, result: Mapping[str, Any], excluded: set[str]
    ) -> tuple[ResponseSection, ...]:
        sections: list[ResponseSection] = []
        for key, value in result.items():
            if key in excluded or value in (None, "", [], {}):
                continue
            items = self._value_items(value)
            if items:
                sections.append(ResponseSection(key.replace("_", " ").title(), items=items))
        return tuple(sections[:8])

    @classmethod
    def _artifacts(cls, values, runtime_result) -> tuple[ResponseArtifact, ...]:
        deliveries: set[str] = set()
        if runtime_result is not None:
            for item in getattr(runtime_result, "deliveries", ()):
                if isinstance(item, dict):
                    ref = cls._text(item.get("artifact_ref"))
                    if ref:
                        deliveries.add(ref)
        result: list[ResponseArtifact] = []
        for item in values:
            if not isinstance(item, dict):
                continue
            ref = cls._text(item.get("ref"))
            if not ref:
                continue
            result.append(
                ResponseArtifact(
                    ref=ref,
                    filename=cls._text(item.get("filename")) or "artifact",
                    kind=cls._text(item.get("kind")),
                    media_type=cls._text(item.get("media_type"))
                    or "application/octet-stream",
                    size_bytes=int(item.get("size_bytes") or 0),
                    deliver=ref in deliveries,
                )
            )
        return tuple(result)

    @staticmethod
    def _status(output: AgentOutput) -> RunStatus:
        status = getattr(getattr(output, "runtime_result", None), "status", None)
        value = getattr(status, "value", status)
        if value == "exhausted":
            return RunStatus.EXHAUSTED
        if value == "waiting_approval":
            return RunStatus.WAITING_APPROVAL
        return RunStatus.SUCCEEDED

    @classmethod
    def _value_items(cls, value: Any) -> tuple[str, ...]:
        if isinstance(value, list):
            items: list[str] = []
            for item in value:
                if isinstance(item, dict):
                    items.append(" · ".join(cls._mapping_items(item)))
                elif cls._text(item):
                    items.append(cls._text(item))
            return tuple(item for item in items if item)
        if isinstance(value, dict):
            return cls._mapping_items(value)
        text = cls._text(value)
        return (text,) if text else ()

    @classmethod
    def _mapping_items(cls, value: Mapping[str, Any]) -> tuple[str, ...]:
        items: list[str] = []
        for key, raw in value.items():
            if raw in (None, "", [], {}):
                continue
            if isinstance(raw, list):
                rendered = ", ".join(cls._text(item) for item in raw[:8])
            elif isinstance(raw, dict):
                rendered = ", ".join(cls._mapping_items(raw))
            else:
                rendered = cls._text(raw)
            if rendered:
                items.append(f"{key.replace('_', ' ')}: {rendered}")
        return tuple(items[:12])

    @staticmethod
    def _mappings(value: Any) -> tuple[Mapping[str, Any], ...]:
        if not isinstance(value, list):
            return ()
        return tuple(item for item in value if isinstance(item, dict))

    @staticmethod
    def _strings(value: Any) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            return ()
        return tuple(str(item) for item in value if isinstance(item, (str, int, float)))

    @staticmethod
    def _text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "是" if value else "否"
        if isinstance(value, (str, int, float)):
            return str(value).strip()
        return ""
