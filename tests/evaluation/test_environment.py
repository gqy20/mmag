import pytest

from mmag.evaluation import (
    EvaluationAssetLoader,
    EvaluationConfigurationError,
    EvaluationEnvironment,
)


def _profile():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "evals"
    return EvaluationAssetLoader(root).load_profile("profiles/staging-mattermost.yml")


def test_external_environment_requires_double_gate():
    environment = EvaluationEnvironment(
        {
            "MM_URL": "https://mattermost.example",
            "DEBUG_TEST_CHANNEL_ID": "channel-1",
        }
    )

    with pytest.raises(EvaluationConfigurationError, match="MMAG_E2E_ENABLED"):
        environment.resolve_profile(_profile())


def test_credentialed_remote_http_is_rejected():
    environment = EvaluationEnvironment(
        {
            "MMAG_E2E_ENABLED": "1",
            "MM_URL": "http://mattermost.example",
            "DEBUG_TEST_CHANNEL_ID": "channel-1",
        }
    )

    with pytest.raises(EvaluationConfigurationError, match="HTTPS"):
        environment.resolve_profile(_profile())


def test_profile_requires_the_configured_readiness_url():
    environment = EvaluationEnvironment(
        {
            "MMAG_E2E_ENABLED": "1",
            "MM_URL": "https://mattermost.example",
            "DEBUG_TEST_CHANNEL_ID": "channel-1",
        }
    )

    with pytest.raises(EvaluationConfigurationError, match="MMAG_E2E_READY_URL"):
        environment.resolve_profile(_profile())


def test_actor_secret_is_not_printable():
    environment = EvaluationEnvironment(
        {"MM_USERNAME": "eval-user", "MM_PASSWORD": "top-secret"}
    )

    actor = environment.resolve_actor(_profile(), "requester")

    assert actor.username == "eval-user"
    assert "top-secret" not in repr(actor)
