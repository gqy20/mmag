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
        environment.pop("PROMPTS_PATH", None)
        environment.pop("AGENT_PACKAGES_PATH", None)
        environment.pop("POLICIES_PATH", None)
        subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from mmag import __version__; "
                    "from mmag.cli import main; "
                    "from pathlib import Path; "
                    "from mmag.agent_packages import AgentPackageRegistry; "
                    "from mmag.config import config; "
                    "from mmag.governance import PolicyRegistry; "
                    "from mmag.prompts import PromptManager; "
                    "prompt = PromptManager().get('system_prompt'); "
                    "agents = AgentPackageRegistry(); "
                    "agents.load_directory(Path(config.agent_packages_path)); "
                    "policies = PolicyRegistry(); "
                    "policies.load_directory(Path(config.policies_path)); "
                    "package = agents.get('link'); "
                    "policies.get(package.manifest.policy_ref); "
                    "assert __version__ and main and prompt and package.snapshot.package_hash; "
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
