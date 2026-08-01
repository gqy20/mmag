from pathlib import Path

import pytest

from mmag.evaluation import EvaluationAssetError, EvaluationAssetLoader

ROOT = Path(__file__).resolve().parents[2] / "evals"


def test_evaluation_tree_loads_strict_versioned_assets():
    loader = EvaluationAssetLoader(ROOT)

    profiles, suites, cases = loader.validate_tree()

    assert profiles == 1
    assert suites == 3
    assert cases == 3
    assert loader.load_suite("suites/smoke.yml").sha256
    assert loader.load_profile("profiles/staging-mattermost.yml").actors["requester"]


def test_evaluation_loader_rejects_path_escape():
    loader = EvaluationAssetLoader(ROOT)

    with pytest.raises(EvaluationAssetError, match="escapes root"):
        loader.load_suite("../pyproject.toml")
