"""Narrow presentation-generation capabilities backed by Execution Profiles."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import CapabilityEffect, CapabilitySpec, SourcePolicy

if TYPE_CHECKING:
    from ..execution import ScriptExecutor

PPT_PROFILE_REF = "ppt@1.0.0"
ARTIFACT_PERMISSION = "artifact:generate"


def create_ppt_capabilities(executor: ScriptExecutor) -> tuple[CapabilitySpec, ...]:
    async def render(deck: dict) -> dict:
        return await executor.execute(
            profile_ref=PPT_PROFILE_REF,
            capability="ppt.render",
            permission=ARTIFACT_PERMISSION,
            payload={"deck": deck},
        )

    async def export_pdf(artifact_ref: str) -> dict:
        return await executor.execute(
            profile_ref=PPT_PROFILE_REF,
            capability="ppt.export_pdf",
            permission=ARTIFACT_PERMISSION,
            payload={"artifact_ref": artifact_ref},
            source_ref=artifact_ref,
        )

    return (
        CapabilitySpec(
            name="ppt.render",
            description=(
                "Render a validated slide-deck structure into a governed PPTX Artifact. "
                "The renderer runs offline inside the registered Execution Profile."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "deck": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "title",
                            "audience",
                            "objective",
                            "narrative",
                            "slides",
                        ],
                        "properties": {
                            "title": {"type": "string"},
                            "audience": {"type": "string"},
                            "objective": {"type": "string"},
                            "narrative": {"type": "string"},
                            "slides": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 40,
                                "items": {"type": "object"},
                            },
                        },
                    }
                },
                "required": ["deck"],
            },
            handler=render,
            effect=CapabilityEffect.WRITE,
            permission=ARTIFACT_PERMISSION,
            timeout_seconds=180,
            source_policy=SourcePolicy.NONE,
        ),
        CapabilitySpec(
            name="ppt.export_pdf",
            description=(
                "Export one same-scope slide_deck Artifact to a governed PDF Artifact "
                "through fixed LibreOffice argv inside the offline Execution Profile."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "artifact_ref": {
                        "type": "string",
                        "pattern": "^artifact://[a-f0-9]{32}$",
                    }
                },
                "required": ["artifact_ref"],
            },
            handler=export_pdf,
            effect=CapabilityEffect.WRITE,
            permission=ARTIFACT_PERMISSION,
            timeout_seconds=180,
            source_policy=SourcePolicy.NONE,
        ),
    )
