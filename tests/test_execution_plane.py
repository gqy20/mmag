import asyncio
import hashlib
import json
import shutil
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from mmag.agent_packages import AgentPackageRegistry, ManifestValidationError
from mmag.capabilities import CapabilityContext, bind_capability_context
from mmag.control_plane import SQLiteControlPlane
from mmag.execution import (
    ArtifactRepository,
    ExecutionProfileError,
    ExecutionProfileLoader,
    ExecutionProfileRegistry,
    ExecutionWorkspace,
    ProcessRequest,
    ProcessResult,
    ProcessRunner,
    SandboxUnavailableError,
    ScriptExecutionError,
    ScriptExecutor,
    WorkspaceManager,
)
from mmag.skill_packages import (
    SkillPackageLoader,
    SkillPackageRegistry,
    SkillResourceLoader,
    bind_skill_resource_session,
)

ROOT = Path(__file__).resolve().parents[1]


class WritingRunner:
    def __init__(self, *, symlink_output: bool = False, oversized: bool = False) -> None:
        self.requests = []
        self.symlink_output = symlink_output
        self.oversized = oversized

    async def run(self, request):
        self.requests.append(request)
        if self.symlink_output:
            request.output_path.symlink_to("/etc/passwd")
        elif self.oversized:
            with request.output_path.open("wb") as handle:
                handle.truncate(request.profile.limits.max_artifact_bytes + 1)
        else:
            request.output_path.write_bytes(b"PK\x03\x04governed-pptx")
        return ProcessResult(0, 12, 0, 0, "a" * 64, "b" * 64)


def _profiles() -> ExecutionProfileRegistry:
    registry = ExecutionProfileRegistry()
    registry.load_directory(ROOT / "execution-profiles")
    return registry


def _slides():
    return SkillPackageLoader().load(ROOT / "skills" / "slides")


def _executor(tmp_path, runner):
    store = SQLiteControlPlane(tmp_path / "control.db")
    workspaces = WorkspaceManager(tmp_path / "workspaces")
    artifacts = ArtifactRepository(tmp_path / "artifacts", store)
    executor = ScriptExecutor(_profiles(), runner, workspaces, artifacts, store)
    return executor, store, workspaces, artifacts


def _context() -> CapabilityContext:
    return CapabilityContext(
        "trace-1",
        "user-1",
        "channel-1",
        "post-1",
        "create the deck",
        "mattermost:team/channel-1",
        frozenset({"ppt.render"}),
        "run-1",
        frozenset({"ppt@1.0.0"}),
    )


def _deck(marker: str = "safe") -> dict:
    return {
        "title": marker,
        "audience": "leadership",
        "objective": "decide",
        "narrative": "context to action",
        "slides": [
            {
                "number": 1,
                "title": "Decision",
                "purpose": "make a decision",
                "body": ["Evidence"],
                "visual": None,
                "source_refs": [],
                "notes": "",
            }
        ],
    }


def test_profile_registry_loads_fixed_offline_commands():
    profile = _profiles().get("ppt@1.0.0")

    assert profile.runner == "bubblewrap"
    assert profile.network == "none"
    assert set(profile.commands) == {"ppt.render", "ppt.export_pdf"}
    assert profile.commands["ppt.render"].argv[0:2] == ("-I", "-B")
    assert len(profile.sha256) == 64
    assert (
        profile.image_digest
        == f"sha256:{hashlib.sha256((ROOT / 'uv.lock').read_bytes()).hexdigest()}"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (("spec", "runner", "kind"), "shell"),
        (("spec", "network", "mode"), "host"),
        (("spec", "environment", "inherit"), ["PATH"]),
        (("spec", "environment", "set"), {"PATH": "/tmp/host"}),
    ],
)
def test_profile_rejects_uncontrolled_runner_network_and_environment(tmp_path, field, value):
    raw = yaml.safe_load((ROOT / "execution-profiles" / "ppt.yml").read_text())
    target = raw
    for key in field[:-1]:
        target = target[key]
    target[field[-1]] = value
    path = tmp_path / "ppt.yml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ExecutionProfileError):
        ExecutionProfileLoader().load(path)


def test_profile_rejects_shell_and_dynamic_python_commands(tmp_path):
    raw = yaml.safe_load((ROOT / "execution-profiles" / "ppt.yml").read_text())
    command = raw["spec"]["commands"][0]
    command["id"] = "shell.exec"
    command["argv"] = ["-c", "{input}", "{output}"]
    path = tmp_path / "ppt.yml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ExecutionProfileError, match="forbidden general"):
        ExecutionProfileLoader().load(path)

    raw = yaml.safe_load((ROOT / "execution-profiles" / "ppt.yml").read_text())
    raw["spec"]["commands"][0]["argv"] = ["-c", "print('dynamic')"]
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ExecutionProfileError, match="registered script"):
        ExecutionProfileLoader().load(path)

    raw = yaml.safe_load((ROOT / "execution-profiles" / "ppt.yml").read_text())
    raw["spec"]["commands"][0]["script_ref"] = "../escape.py"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ExecutionProfileError, match="failed at"):
        ExecutionProfileLoader().load(path)


def test_process_argv_is_fixed_offline_and_contains_no_payload(tmp_path):
    profile = _profiles().get("ppt@1.0.0")
    manager = WorkspaceManager(tmp_path / "workspaces")
    marker = "x; touch /tmp/owned"
    with manager.create("run-1") as workspace:
        input_path = manager.write_input(
            workspace,
            {"deck": _deck(marker)},
            max_bytes=profile.limits.max_input_bytes,
        )
        skill = _slides()
        asset = skill.resources["scripts/ppt.py"]
        script = manager.copy_asset(
            workspace,
            skill.root / "scripts/ppt.py",
            expected_sha256=asset.sha256,
        )
        output = manager.output_path(workspace, "deck.pptx")
        runner = ProcessRunner(Path(sys.prefix), bubblewrap_path="/usr/bin/bwrap")
        argv, _ = runner.build_argv(
            ProcessRequest(
                profile,
                profile.commands["ppt.render"],
                workspace,
                script,
                input_path,
                output,
            )
        )

    assert "--unshare-all" in argv
    assert "--clearenv" in argv
    assert marker not in " ".join(argv)
    assert not any(token in argv for token in ("sh", "bash", "-c", "eval", "exec"))


def test_process_runner_fails_closed_without_sandbox(tmp_path):
    profile = _profiles().get("ppt@1.0.0")
    manager = WorkspaceManager(tmp_path / "workspaces")
    with manager.create("run-1") as workspace:
        input_path = manager.write_input(
            workspace,
            {"deck": _deck()},
            max_bytes=profile.limits.max_input_bytes,
        )
        output = manager.output_path(workspace, "deck.pptx")
        request = ProcessRequest(
            profile,
            profile.commands["ppt.render"],
            workspace,
            workspace.assets / "missing.py",
            input_path,
            output,
        )
        with pytest.raises(SandboxUnavailableError, match="bubblewrap"):
            ProcessRunner(Path(sys.prefix), bubblewrap_path="/missing/bwrap").build_argv(request)


@pytest.mark.asyncio
async def test_process_communication_timeout_cancels_stream_tasks(tmp_path):
    class HangingProcess:
        def __init__(self) -> None:
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()

        async def wait(self):
            await asyncio.Future()

    profile = _profiles().get("ppt@1.0.0")
    profile = replace(
        profile,
        limits=replace(profile.limits, timeout_seconds=0.01),
    )
    workspace = ExecutionWorkspace(tmp_path, tmp_path, tmp_path, tmp_path, tmp_path)
    request = ProcessRequest(
        profile,
        profile.commands["ppt.render"],
        workspace,
        None,
        tmp_path / "input.json",
        tmp_path / "deck.pptx",
    )

    with pytest.raises(TimeoutError):
        await ProcessRunner(Path(sys.prefix))._communicate(HangingProcess(), request)


@pytest.mark.asyncio
async def test_script_executor_commits_artifact_and_redacted_audit(tmp_path):
    runner = WritingRunner()
    executor, store, workspaces, artifacts = _executor(tmp_path, runner)
    skill = _slides()
    session = SkillResourceLoader().create_session(skill)
    marker = "SECRET_MARKER_NOT_FOR_AUDIT"

    with bind_capability_context(_context()), bind_skill_resource_session(session):
        result = await executor.execute(
            profile_ref="ppt@1.0.0",
            capability="ppt.render",
            permission="artifact:generate",
            payload={"deck": _deck(marker)},
        )

    artifact, path = artifacts.resolve(
        result["artifact_ref"],
        scope_id="mattermost:team/channel-1",
        expected_kind="slide_deck",
    )
    audits = store.list_audits(event_type="execution.process", target="ppt.render")
    assert path.read_bytes().startswith(b"PK")
    assert artifact.sha256 == result["artifacts"][0]["sha256"]
    assert audits[0].decision == "succeeded"
    assert marker not in json.dumps(audits[0].details)
    assert audits[0].details["input_sha256"]
    assert not runner.requests[0].workspace.root.exists()
    with pytest.raises(PermissionError):
        artifacts.resolve(result["artifact_ref"], scope_id="mattermost:other/channel")

    shard = artifacts.root / "00"
    incomplete = shard / ".commit-interrupted"
    orphan = shard / ("0" * 32)
    incomplete.mkdir(parents=True)
    orphan.mkdir()
    assert artifacts.reconcile() == (1, 1)
    assert not incomplete.exists() and not orphan.exists()


@pytest.mark.asyncio
async def test_script_executor_denies_missing_agent_profile_before_process(tmp_path):
    runner = WritingRunner()
    executor, store, _, _ = _executor(tmp_path, runner)
    session = SkillResourceLoader().create_session(_slides())
    context = replace(_context(), allowed_execution_profiles=frozenset())

    with (
        bind_capability_context(context),
        bind_skill_resource_session(session),
        pytest.raises(ScriptExecutionError),
    ):
        await executor.execute(
            profile_ref="ppt@1.0.0",
            capability="ppt.render",
            permission="artifact:generate",
            payload={"deck": _deck()},
        )

    assert runner.requests == []
    audit = store.list_audits(event_type="execution.process", target="ppt.render")[0]
    assert audit.decision == "denied"
    assert audit.details["error_code"] == "agent_profile_forbidden"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runner", [WritingRunner(symlink_output=True), WritingRunner(oversized=True)]
)
async def test_script_executor_rejects_symlink_and_oversized_artifacts(tmp_path, runner):
    executor, store, _, _ = _executor(tmp_path, runner)
    session = SkillResourceLoader().create_session(_slides())

    with (
        bind_capability_context(_context()),
        bind_skill_resource_session(session),
        pytest.raises(ScriptExecutionError),
    ):
        await executor.execute(
            profile_ref="ppt@1.0.0",
            capability="ppt.render",
            permission="artifact:generate",
            payload={"deck": _deck()},
        )

    audit = store.list_audits(event_type="execution.process", target="ppt.render")[0]
    assert audit.decision == "failed"


@pytest.mark.asyncio
async def test_script_executor_rejects_tampered_skill_script(tmp_path):
    skill_root = tmp_path / "slides"
    shutil.copytree(ROOT / "skills" / "slides", skill_root)
    skill = SkillPackageLoader().load(skill_root)
    (skill_root / "scripts" / "ppt.py").write_text("raise SystemExit(0)", encoding="utf-8")
    executor, _, _, _ = _executor(tmp_path, WritingRunner())
    session = SkillResourceLoader().create_session(skill)

    with (
        bind_capability_context(_context()),
        bind_skill_resource_session(session),
        pytest.raises(ScriptExecutionError),
    ):
        await executor.execute(
            profile_ref="ppt@1.0.0",
            capability="ppt.render",
            permission="artifact:generate",
            payload={"deck": _deck()},
        )


def test_agent_manifest_cannot_self_authorize_skill_execution_profile(tmp_path):
    agents_root = tmp_path / "agents"
    shutil.copytree(ROOT / "agents" / "ppt", agents_root / "ppt")
    manifest = agents_root / "ppt" / "agent.yml"
    raw = yaml.safe_load(manifest.read_text())
    raw["spec"].pop("execution_profiles")
    manifest.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    skills = SkillPackageRegistry()
    skills.load_directory(ROOT / "skills")

    with pytest.raises(ManifestValidationError, match="cannot grant Skill"):
        AgentPackageRegistry(
            skill_registry=skills,
            execution_profile_registry=_profiles(),
        ).load_directory(agents_root)
