"""Tencent Meeting capability backed by the ``tmeet`` CLI."""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..logger import get_logger
from .base import CapabilityEffect, CapabilitySpec, SourcePolicy

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

log = get_logger(__name__)

TMEET_BIN = "tmeet"
TMEET_TIMEOUT = 30
TMEET_AUTH_TIMEOUT = 300


@dataclass(frozen=True)
class _TmeetCommand:
    subcommand: tuple[str, ...]
    needs_auth: bool = True


_TMEET_COMMANDS: dict[str, _TmeetCommand] = {
    "tencent_meeting_auth_login": _TmeetCommand(
        subcommand=("auth", "login", "--no-browser"), needs_auth=False
    ),
    "tencent_meeting_auth_status": _TmeetCommand(
        subcommand=("auth", "status"), needs_auth=False
    ),
    "tencent_meeting_list_meetings": _TmeetCommand(
        subcommand=("meeting", "list"),
    ),
    "tencent_meeting_list_ended_meetings": _TmeetCommand(
        subcommand=("meeting", "list-ended"),
    ),
    "tencent_meeting_get_meeting": _TmeetCommand(
        subcommand=("meeting", "get"),
    ),
    "tencent_meeting_create_meeting": _TmeetCommand(
        subcommand=("meeting", "create"),
    ),
    "tencent_meeting_cancel_meeting": _TmeetCommand(
        subcommand=("meeting", "cancel"),
    ),
    "tencent_meeting_list_records": _TmeetCommand(
        subcommand=("record", "list"),
    ),
    "tencent_meeting_get_smart_minutes": _TmeetCommand(
        subcommand=("record", "smart-minutes"),
    ),
    "tmeet_transcript_get": _TmeetCommand(
        subcommand=("record", "transcript-get"),
    ),
    "tmeet_transcript_search": _TmeetCommand(
        subcommand=("record", "transcript-search"),
    ),
    "tmeet_record_address": _TmeetCommand(
        subcommand=("record", "address"),
    ),
    "tmeet_participants": _TmeetCommand(
        subcommand=("report", "participants"),
    ),
}


def _find_tmeet() -> str:
    path = shutil.which(TMEET_BIN)
    if not path:
        raise FileNotFoundError(f"{TMEET_BIN} CLI is not installed (npm install -g @tencentcloud/tmeet)")
    return path


async def _run_tmeet(args: Sequence[str], *, timeout: int = TMEET_TIMEOUT) -> dict:
    """Execute ``tmeet`` with fixed argv and return parsed JSON output."""
    bin_path = _find_tmeet()
    argv = [bin_path, *args, "--format", "json"]
    log.info("tmeet exec: %s", " ".join(argv[1:]))

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return {"status": "error", "error": f"tmeet timeout after {timeout}s"}

    out = stdout.decode("utf-8", errors="replace").strip()
    err = stderr.decode("utf-8", errors="replace").strip()

    if proc.returncode != 0:
        return {
            "status": "error",
            "error": err or out or f"tmeet exited with code {proc.returncode}",
            "exit_code": proc.returncode,
        }

    if not out:
        return {"status": "ok", "message": err or "completed"}

    try:
        parsed = json.loads(out)
    except json.JSONDecodeError:
        return {"status": "ok", "raw_output": out[:2000]}

    if isinstance(parsed, dict) and "error_info" in parsed:
        return {"status": "error", "error": parsed["error_info"].get("message", "unknown API error")}

    return {"status": "ok", "data": parsed}


def _format_args(action: str, params: Mapping[str, object]) -> list[str]:
    """Translate capability parameters into ``tmeet`` CLI flags."""
    args: list[str] = []
    cmd = _TMEET_COMMANDS[action]
    args.extend(cmd.subcommand)

    flag_map: dict[str, str] = {
        "meeting_id": "--meeting-id",
        "meeting_code": "--meeting-code",
        "subject": "--subject",
        "start": "--start",
        "end": "--end",
        "record_file_id": "--record-file-id",
        "lang": "--lang",
        "page_token": "--page-token",
        "page_size": "--page-size",
        "query": "--query",
        "pwd": "--pwd",
    }

    for key, value in params.items():
        if key in ("action", "type"):
            continue
        flag = flag_map.get(key)
        if flag and value is not None:
            args.extend([flag, str(value)])
        elif flag is None and value is not None:
            log.debug("tmeet: ignoring unrecognised parameter '%s'", key)

    return args


def _build_capability_spec(action: str, cmd: _TmeetCommand) -> CapabilitySpec:
    """Build a ``CapabilitySpec`` for one tmeet sub-command."""

    descriptions: dict[str, tuple[str, dict, str]] = {
        "tencent_meeting_auth_login": (
            "触发腾讯会议 OAuth2 设备授权。返回授权 URL，用户需在浏览器中打开并扫码完成授权。"
            "授权成功后凭证自动加密存储，后续调用无需再次授权。",
            {"type": "object", "properties": {}},
            "tencent:auth:write",
        ),
        "tencent_meeting_auth_status": (
            "检查腾讯会议登录状态。返回已登录/未登录。",
            {"type": "object", "properties": {}},
            "tencent:auth:read",
        ),
        "tencent_meeting_list_meetings": (
            "查询即将开始或进行中的腾讯会议列表。可选按时间范围过滤。",
            {
                "type": "object",
                "properties": {
                    "start": {"type": "string", "description": "ISO 8601 开始时间, 如 2026-08-01T00:00+08:00"},
                    "end": {"type": "string", "description": "ISO 8601 结束时间"},
                    "page_size": {"type": "integer", "description": "每页数量, 最大 20", "default": 20},
                    "page_token": {"type": "string", "description": "分页游标"},
                },
            },
            "tencent:meeting:read",
        ),
        "tencent_meeting_list_ended_meetings": (
            "查询已结束的腾讯会议列表。必须提供 start 和 end 参数（ISO 8601）。",
            {
                "type": "object",
                "properties": {
                    "start": {"type": "string", "description": "ISO 8601 开始时间 (必填)"},
                    "end": {"type": "string", "description": "ISO 8601 结束时间 (必填)"},
                    "page_size": {"type": "integer", "description": "每页数量, 最大 20", "default": 20},
                    "page_token": {"type": "string", "description": "分页游标"},
                },
                "required": ["start", "end"],
            },
            "tencent:meeting:read",
        ),
        "tencent_meeting_get_meeting": (
            "查询单个腾讯会议详情。需提供 meeting_id 或 meeting_code 之一。",
            {
                "type": "object",
                "properties": {
                    "meeting_id": {"type": "string", "description": "会议 ID"},
                    "meeting_code": {"type": "string", "description": "9 位会议号"},
                },
            },
            "tencent:meeting:read",
        ),
        "tencent_meeting_create_meeting": (
            "创建腾讯会议。需要主题、开始时间和结束时间。",
            {
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "会议主题 (必填)"},
                    "start": {"type": "string", "description": "ISO 8601 开始时间 (必填)"},
                    "end": {"type": "string", "description": "ISO 8601 结束时间 (必填)"},
                },
                "required": ["subject", "start", "end"],
            },
            "tencent:meeting:write",
        ),
        "tencent_meeting_cancel_meeting": (
            "取消已预约的腾讯会议。需要 meeting_id。",
            {
                "type": "object",
                "properties": {
                    "meeting_id": {"type": "string", "description": "会议 ID (必填)"},
                },
                "required": ["meeting_id"],
            },
            "tencent:meeting:write",
        ),
        "tencent_meeting_list_records": (
            "查询会议录制列表。可按 meeting_id、meeting_code 或时间范围查询。",
            {
                "type": "object",
                "properties": {
                    "meeting_id": {"type": "string"},
                    "meeting_code": {"type": "string"},
                    "start": {"type": "string", "description": "ISO 8601 (按时间查时必填)"},
                    "end": {"type": "string", "description": "ISO 8601 (按时间查时必填)"},
                    "page_size": {"type": "integer", "default": 30},
                    "page_token": {"type": "string"},
                },
            },
            "tencent:record:read",
        ),
        "tencent_meeting_get_smart_minutes": (
            "获取会议的 AI 智能纪要。需要 record_file_id（从 list_records 获取）。",
            {
                "type": "object",
                "properties": {
                    "record_file_id": {"type": "string", "description": "录制文件 ID (必填)"},
                    "lang": {"type": "string", "description": "语言: default/zh/en/ja", "default": "default"},
                    "pwd": {"type": "string", "description": "录制文件访问密码 (如有)"},
                },
                "required": ["record_file_id"],
            },
            "tencent:record:read",
        ),
        "tmeet_transcript_get": (
            "获取会议转写全文。需要 record_file_id。",
            {
                "type": "object",
                "properties": {
                    "record_file_id": {"type": "string", "description": "录制文件 ID (必填)"},
                    "page_token": {"type": "string"},
                    "page_size": {"type": "integer"},
                },
                "required": ["record_file_id"],
            },
            "tencent:record:read",
        ),
        "tmeet_transcript_search": (
            "在会议转写中搜索关键词。需要 record_file_id 和 query。",
            {
                "type": "object",
                "properties": {
                    "record_file_id": {"type": "string", "description": "录制文件 ID (必填)"},
                    "query": {"type": "string", "description": "搜索关键词 (必填)"},
                },
                "required": ["record_file_id", "query"],
            },
            "tencent:record:read",
        ),
        "tmeet_record_address": (
            "获取录制文件下载地址。需要 record_file_id。",
            {
                "type": "object",
                "properties": {
                    "record_file_id": {"type": "string", "description": "录制文件 ID (必填)"},
                },
                "required": ["record_file_id"],
            },
            "tencent:record:read",
        ),
        "tmeet_participants": (
            "查询会议参会人员列表。需要 meeting_id。",
            {
                "type": "object",
                "properties": {
                    "meeting_id": {"type": "string", "description": "会议 ID (必填)"},
                    "page_size": {"type": "integer"},
                    "page_token": {"type": "string"},
                },
                "required": ["meeting_id"],
            },
            "tencent:meeting:read",
        ),
    }

    desc, schema, permission = descriptions[action]
    effect = CapabilityEffect.WRITE if ":write" in permission else CapabilityEffect.READ

    async def handler(**kwargs: object) -> dict:
        args = _format_args(action, kwargs)
        timeout = TMEET_AUTH_TIMEOUT if action == "tencent_meeting_auth_login" else TMEET_TIMEOUT

        if cmd.needs_auth:
            status = await _run_tmeet(["auth", "status", "--format", "json"], timeout=10)
            if status.get("status") != "ok" or "Not logged in" in status.get("data", ""):
                return {
                    "status": "auth_required",
                    "message": "腾讯会议未登录，请先调用 tencent_meeting_auth_login 获取授权 URL",
                }

        return await _run_tmeet(args, timeout=timeout)

    return CapabilitySpec(
        name=action,
        description=desc,
        input_schema=schema,
        handler=handler,
        effect=effect,
        permission=permission,
        timeout_seconds=TMEET_AUTH_TIMEOUT if action == "tencent_meeting_auth_login" else TMEET_TIMEOUT,
        source_policy=SourcePolicy.NONE,
    )


def create_tencent_meeting_capabilities() -> list[CapabilitySpec]:
    """Create all Tencent Meeting capability specs."""
    return [_build_capability_spec(action, cmd) for action, cmd in _TMEET_COMMANDS.items()]
