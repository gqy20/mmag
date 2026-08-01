"""发布产物必须携带运行所需资源。"""

import subprocess
import zipfile


def test_wheel_contains_flat_agent_and_skill_resources(tmp_path):
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(tmp_path.glob("mmag-*.whl"))

    with zipfile.ZipFile(wheel) as archive:
        prompt = archive.read("mmag/agents/mmchat/prompts/system.md").decode()
        skill = archive.read("mmag/skills/web-research/SKILL.md").decode()
        profile = archive.read("mmag/execution-profiles/ppt.yml").decode()
        renderer = archive.read("mmag/skills/slides/scripts/ppt.py").decode()

    assert "## 身份" in prompt
    assert "# Web Research" in skill
    assert "kind: ExecutionProfile" in profile
    assert "def render(" in renderer
