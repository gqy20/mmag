"""Atomic presentation build capability backed by governed execution steps."""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from typing import TYPE_CHECKING, Any

from ..skill_packages import get_skill_context
from .base import CapabilityEffect, CapabilitySpec, SourcePolicy

if TYPE_CHECKING:
    from ..execution import ScriptExecutor

PPT_PROFILE_REF = "ppt@2.2.0"
ARTIFACT_PERMISSION = "artifact:generate"
THEMES = {"corp@1.0.0": "ppt/themes/corp.json"}


def _load_theme(theme_ref: str) -> tuple[dict[str, Any], str]:
    if get_skill_context() is None:
        raise RuntimeError("presentation build requires an active Skill")
    try:
        resource_ref = THEMES[theme_ref]
    except KeyError as error:
        raise ValueError(f"unknown presentation theme {theme_ref!r}") from error
    asset = files("mmag.renderers").joinpath(*resource_ref.split("/"))
    if not asset.is_file():
        raise RuntimeError("presentation theme is not installed")
    content = asset.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    theme = json.loads(content)
    metadata = theme.get("metadata", {})
    actual_ref = f"{metadata.get('name')}@{metadata.get('version')}"
    if actual_ref != theme_ref:
        raise RuntimeError("presentation theme metadata does not match its registered ref")
    return theme, digest


def create_ppt_capabilities(executor: ScriptExecutor) -> tuple[CapabilitySpec, ...]:
    async def build(source: str, theme_ref: str) -> dict[str, Any]:
        theme, theme_hash = _load_theme(theme_ref)
        payload = {"source": source, "theme_ref": theme_ref, "theme": theme}
        provenance = {
            "presentation_source_format": "mmag-markdown@1.0.0",
            "presentation_renderer": "pptxgenjs@4.0.1",
            "presentation_theme": theme_ref,
            "presentation_theme_hash": theme_hash,
        }
        source_result = await executor.execute(
            profile_ref=PPT_PROFILE_REF,
            capability="ppt.build",
            command_id="ppt.source",
            permission=ARTIFACT_PERMISSION,
            payload=payload,
            provenance=provenance,
        )
        pptx_result = await executor.execute(
            profile_ref=PPT_PROFILE_REF,
            capability="ppt.build",
            command_id="ppt.render",
            permission=ARTIFACT_PERMISSION,
            payload=payload,
            provenance=provenance,
        )
        preview_svg_result = await executor.execute(
            profile_ref=PPT_PROFILE_REF,
            capability="ppt.build",
            command_id="ppt.preview_svg",
            permission=ARTIFACT_PERMISSION,
            payload=payload,
            provenance=provenance,
        )
        preview_result = await executor.execute(
            profile_ref=PPT_PROFILE_REF,
            capability="ppt.build",
            command_id="ppt.preview",
            permission=ARTIFACT_PERMISSION,
            payload={"artifact_ref": preview_svg_result["artifact_ref"]},
            source_ref=preview_svg_result["artifact_ref"],
            provenance=provenance,
        )
        artifacts = [
            *source_result["artifacts"],
            *pptx_result["artifacts"],
            *preview_result["artifacts"],
        ]
        return {
            "status": "succeeded",
            "source_ref": source_result["artifact_ref"],
            "pptx_ref": pptx_result["artifact_ref"],
            "preview_refs": [preview_result["artifact_ref"]],
            "theme_ref": theme_ref,
            "editable_ratio": 1.0,
            "artifacts": artifacts,
            "execution": pptx_result["execution"],
        }

    return (
        CapabilitySpec(
            name="ppt.build",
            description=(
                "Build a complete presentation bundle from governed MMAG Markdown. "
                "Returns editable PPTX, normalized source, and direct PNG preview Artifact refs."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "source": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 200000,
                        "description": "MMAG presentation Markdown using registered layouts.",
                    },
                    "theme_ref": {
                        "type": "string",
                        "enum": sorted(THEMES),
                    },
                },
                "required": ["source", "theme_ref"],
            },
            handler=build,
            effect=CapabilityEffect.WRITE,
            permission=ARTIFACT_PERMISSION,
            timeout_seconds=420,
            source_policy=SourcePolicy.NONE,
        ),
    )
