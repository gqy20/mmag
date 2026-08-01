"""Verify imports and all packaged configuration resources from a built wheel."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


def _latest_wheel(directory: Path) -> Path:
    wheels = list(directory.glob("mmag-*.whl"))
    if not wheels:
        raise FileNotFoundError(f"no mmag wheel found in {directory}")
    return max(wheels, key=lambda wheel: wheel.stat().st_mtime)


def verify_wheel(wheel: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="mmag-wheel-") as temporary:
        extracted = Path(temporary)
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(extracted)

        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(extracted)
        environment.pop("AGENT_PACKAGES_PATH", None)
        environment.pop("SKILL_PACKAGES_PATH", None)
        environment.pop("POLICIES_PATH", None)
        environment.pop("MODEL_POLICIES_PATH", None)
        subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from mmag import __version__; "
                    "from mmag.cli import main; "
                    "from pathlib import Path; "
                    "from mmag.agent_packages import AgentPackageRegistry; "
                    "from mmag.skill_packages import SkillPackageRegistry; "
                    "from mmag.config import config; "
                    "from mmag.governance import ModelPolicyRegistry, PolicyRegistry; "
                    "policies = PolicyRegistry(); "
                    "policies.load_directory(Path(config.policies_path)); "
                    "models = ModelPolicyRegistry(); "
                    "models.load_directory(Path(config.model_policies_path)); "
                    "skills = SkillPackageRegistry(); "
                    "skills.load_directory(Path(config.skill_packages_path)); "
                    "agents = AgentPackageRegistry(policy_registry=policies, model_policy_registry=models, skill_registry=skills); "
                    "agents.load_directory(Path(config.agent_packages_path)); "
                    "mmchat = agents.get('mmchat'); "
                    "link = agents.get('link'); "
                    "assert __version__ and main and mmchat.skills and link.snapshot.policy_hash; "
                    "print('wheel import and package resource load: ok')"
                ),
            ],
            cwd=extracted,
            env=environment,
            check=True,
        )


def main() -> None:
    directory = Path(sys.argv[1] if len(sys.argv) > 1 else "dist")
    verify_wheel(_latest_wheel(directory))


if __name__ == "__main__":
    main()
