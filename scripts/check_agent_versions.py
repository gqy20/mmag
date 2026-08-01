"""Reject changed Agent Packages whose Manifest version did not increase."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml


def _git(*arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments),
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _version(content: str, source: str) -> tuple[int, int, int]:
    raw = yaml.safe_load(content)
    try:
        value = raw["metadata"]["version"]
        parts = tuple(int(part) for part in value.split("."))
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise ValueError(f"{source} has an invalid metadata.version") from error
    if len(parts) != 3:
        raise ValueError(f"{source} has an invalid metadata.version")
    return parts


def changed_agents(base_ref: str) -> tuple[str, ...]:
    paths = _git("diff", "--name-only", base_ref, "--", "agents").splitlines()
    names = {
        parts[1] for path in paths if len(parts := Path(path).parts) >= 2 and parts[0] == "agents"
    }
    return tuple(sorted(names))


def verify(base_ref: str) -> tuple[str, ...]:
    errors: list[str] = []
    for name in changed_agents(base_ref):
        current_path = Path("agents") / name / "agent.yml"
        if not current_path.is_file():
            continue
        try:
            previous = _git("show", f"{base_ref}:agents/{name}/agent.yml")
        except subprocess.CalledProcessError:
            continue
        current_version = _version(current_path.read_text(encoding="utf-8"), str(current_path))
        previous_version = _version(previous, f"{base_ref}:agents/{name}/agent.yml")
        if current_version <= previous_version:
            errors.append(
                f"{name}: Package changed but version did not increase "
                f"({'.'.join(map(str, previous_version))} -> "
                f"{'.'.join(map(str, current_version))})"
            )
    return tuple(errors)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: check_agent_versions.py <base-git-ref>")
    errors = verify(sys.argv[1])
    if errors:
        raise SystemExit("\n".join(errors))
    print("Agent Package version gate: ok")


if __name__ == "__main__":
    main()
