"""Narrow MCP facade for approved lark-cli product capabilities."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

_MAX_OUTPUT_BYTES = 2 * 1024 * 1024
_TIMEOUT_SECONDS = 60
_mcp = FastMCP(
    "mmag-lark",
    instructions=(
        "Fixed Lark document and task operations. User identity and authorization are "
        "resolved by lark-cli; never accept credentials as tool arguments."
    ),
    log_level="WARNING",
)


class LarkCLIError(RuntimeError):
    """A bounded, credential-free lark-cli failure."""


def _required_text(value: str, field: str, *, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or "\x00" in normalized:
        raise ValueError(f"{field} is invalid")
    return normalized


def _environment() -> dict[str, str]:
    allowed = ("HOME", "LANG", "LC_ALL", "LC_CTYPE", "PATH", "TMPDIR")
    environment = {name: os.environ[name] for name in allowed if os.environ.get(name)}
    environment["LARK_CLI_NO_PROXY"] = "1"
    return environment


async def _run(*arguments: str) -> dict[str, Any]:
    executable = shutil.which("lark-cli", path=_environment().get("PATH"))
    if not executable:
        raise LarkCLIError("lark-cli is unavailable")
    process = await asyncio.create_subprocess_exec(
        executable,
        *arguments,
        "--as",
        "user",
        "--json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_environment(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), _TIMEOUT_SECONDS)
    except TimeoutError as error:
        process.kill()
        await process.wait()
        raise LarkCLIError("lark-cli timed out") from error
    if len(stdout) + len(stderr) > _MAX_OUTPUT_BYTES:
        raise LarkCLIError("lark-cli output exceeded its limit")
    try:
        payload = json.loads(stdout or stderr)
    except json.JSONDecodeError as error:
        raise LarkCLIError("lark-cli returned an invalid response") from error
    if process.returncode != 0 or (isinstance(payload, dict) and payload.get("ok") is False):
        detail = payload.get("error", {}) if isinstance(payload, dict) else {}
        code = str(detail.get("subtype") or detail.get("type") or "command_failed")
        raise LarkCLIError(f"lark-cli failed: {code}")
    if not isinstance(payload, dict):
        raise LarkCLIError("lark-cli returned a non-object response")
    payload.pop("_notice", None)
    return payload


@_mcp.tool(
    description="Read one Lark Docx/Wiki document by URL or token using user identity.",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    structured_output=True,
)
async def fetch_document(document: str) -> dict[str, Any]:
    target = _required_text(document, "document", maximum=2048)
    payload = await _run("docs", "+fetch", "--doc", target, "--detail", "simple")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    token = str(payload.get("document_id") or payload.get("document_token") or target)
    revision = str(payload.get("revision_id") or payload.get("revision") or "latest")
    return {
        "document": payload,
        "sources": [
            {
                "source_system": "lark_doc",
                "resource_id": token,
                "version": revision,
                "content_sha256": hashlib.sha256(encoded).hexdigest(),
            }
        ],
    }


@_mcp.tool(
    description="List tasks assigned to the authenticated Lark user.",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    structured_output=True,
)
async def list_my_tasks(include_completed: bool = False) -> dict[str, Any]:
    arguments = ["task", "+get-my-tasks"]
    if not include_completed:
        arguments.append("--complete=false")
    return await _run(*arguments)


@_mcp.tool(
    description=(
        "Create a Lark task. assignee_open_id must be a trusted ou_* identity; omit it "
        "when a person's identity has not been confirmed."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
    structured_output=True,
)
async def create_task(
    summary: str,
    description: str = "",
    due: str = "",
    assignee_open_id: str = "",
    idempotency_key: str = "",
) -> dict[str, Any]:
    arguments = ["task", "+create", "--summary", _required_text(summary, "summary", maximum=500)]
    if description:
        arguments.extend(("--description", _required_text(description, "description", maximum=10000)))
    if due:
        arguments.extend(("--due", _required_text(due, "due", maximum=64)))
    if assignee_open_id:
        assignee = _required_text(assignee_open_id, "assignee_open_id", maximum=128)
        if not assignee.startswith("ou_"):
            raise ValueError("assignee_open_id must be an open_id")
        arguments.extend(("--assignee", assignee))
    if idempotency_key:
        arguments.extend(
            ("--idempotency-key", _required_text(idempotency_key, "idempotency_key", maximum=128))
        )
    return await _run(*arguments)


@_mcp.tool(
    description="Set one relative reminder on an existing Lark task with a due time.",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
    structured_output=True,
)
async def set_task_reminder(task_guid: str, reminder: str) -> dict[str, Any]:
    return await _run(
        "task",
        "+reminder",
        "--task-id",
        _required_text(task_guid, "task_guid", maximum=128),
        "--set",
        _required_text(reminder, "reminder", maximum=16),
    )


def main() -> None:
    _mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
