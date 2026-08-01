"""Capability boundary for progressively disclosed Skill resources."""

from __future__ import annotations

from ..skill_packages import load_active_skill_resource
from .base import CapabilitySpec


def create_load_skill_resource_capability() -> CapabilitySpec:
    return CapabilitySpec(
        name="load_skill_resource",
        description=(
            "Load one template or reference declared by the active Skill. "
            "Use only refs listed in the active Skill resource catalog."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "ref": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Exact relative ref from the active Skill resource catalog.",
                }
            },
            "required": ["ref"],
        },
        handler=load_active_skill_resource,
        permission="skill:resource:read",
        timeout_seconds=2,
    )
