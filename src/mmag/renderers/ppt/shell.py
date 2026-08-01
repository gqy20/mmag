"""Demo-only host shell adapter with a cleared parent environment."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

MAX_STREAM_CHARS = 16_384


def _bounded(value: str) -> tuple[str, bool]:
    if len(value) <= MAX_STREAM_CHARS:
        return value, False
    return value[:MAX_STREAM_CHARS], True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    arguments = parser.parse_args()
    payload = json.loads(Path(arguments.input).read_text(encoding="utf-8"))
    command = payload.get("command")
    if not isinstance(command, str) or not command.strip() or len(command) > 16_384:
        raise ValueError("command must be a non-empty string of at most 16384 characters")

    environment = {
        name: os.environ[name]
        for name in ("HOME", "LANG", "LC_ALL", "PATH", "TMPDIR", "TZ")
        if name in os.environ
    }
    process = subprocess.run(  # noqa: S603 - explicitly authorized host-shell demo capability.
        ["/usr/bin/bash", "--noprofile", "--norc", "-o", "pipefail", "-c", command],
        cwd=Path.cwd(),
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    stdout, stdout_truncated = _bounded(process.stdout)
    stderr, stderr_truncated = _bounded(process.stderr)
    files = sorted(
        str(path.relative_to(Path.cwd()))
        for path in Path.cwd().rglob("*")
        if path.is_file() and not path.is_symlink()
    )[:64]
    report = {
        "status": "succeeded" if process.returncode == 0 else "failed",
        "exit_code": process.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "files": files,
    }
    Path(arguments.output).write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
