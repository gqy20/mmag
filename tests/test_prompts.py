"""Agent Package prompts are immutable and rendered strictly."""

from pathlib import Path

import pytest

from mmag.agent_packages import AgentPackageLoader, PromptContractError
from mmag.agent_packages.assets import PromptRegistry, render_prompt

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "agents" / "mmchat"


def test_conversation_system_prompt_renders_all_identity_variables():
    package = AgentPackageLoader().load(PACKAGE_ROOT)
    asset = package.prompts[package.manifest.prompt.system_ref]
    rendered = render_prompt(
        asset,
        {
            "current_time": "2026-08-01 10:00:00",
            "bot_username": "agent2",
            "bot_user_id": "bot-1",
            "current_user_id": "user-1",
            "current_user_username": "alice",
            "current_user_profile": "关注:architecture",
            "recent_speakers": "- @alice",
            "channel_members": "| alice | member |",
        },
    )

    assert "@agent2" in rendered
    assert "user-1" in rendered
    assert "{current_time}" not in rendered


def test_missing_required_runtime_variable_is_rejected():
    package = AgentPackageLoader().load(PACKAGE_ROOT)
    asset = package.prompts[package.manifest.prompt.system_ref]

    with pytest.raises(PromptContractError, match="missing variables"):
        render_prompt(asset, {"bot_username": "agent2"})


def test_prompt_registry_rejects_undeclared_variables(tmp_path):
    prompt = tmp_path / "system.md"
    prompt.write_text("Hello {undeclared}", encoding="utf-8")
    registry = PromptRegistry(tmp_path, ("declared",))

    with pytest.raises(PromptContractError, match="undeclared variables"):
        registry.load("system.md")
