"""Reject changed Execution Profiles whose version did not increase."""

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
        parts = tuple(int(part) for part in raw["metadata"]["version"].split("."))
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise ValueError(f"{source} has an invalid metadata.version") from error
    if len(parts) != 3:
        raise ValueError(f"{source} has an invalid metadata.version")
    return parts


def changed_profiles(base_ref: str) -> tuple[str, ...]:
    paths = _git("diff", "--name-only", base_ref, "--", "execution-profiles").splitlines()
    return tuple(sorted(Path(path).stem for path in paths if Path(path).suffix == ".yml"))


def verify(base_ref: str) -> tuple[str, ...]:
    errors: list[str] = []
    for name in changed_profiles(base_ref):
        current_path = Path("execution-profiles") / f"{name}.yml"
        if not current_path.is_file():
            continue
        try:
            previous = _git("show", f"{base_ref}:execution-profiles/{name}.yml")
        except subprocess.CalledProcessError:
            continue
        current_version = _version(current_path.read_text(encoding="utf-8"), str(current_path))
        previous_version = _version(previous, f"{base_ref}:execution-profiles/{name}.yml")
        if current_version <= previous_version:
            errors.append(
                f"{name}: Execution Profile changed but version did not increase "
                f"({'.'.join(map(str, previous_version))} -> "
                f"{'.'.join(map(str, current_version))})"
            )
    return tuple(errors)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: check_execution_profile_versions.py <base-git-ref>")
    errors = verify(sys.argv[1])
    if errors:
        raise SystemExit("\n".join(errors))
    print("Execution Profile version gate: ok")


if __name__ == "__main__":
    main()
