"""Update and collect helpers for the Bugs channel."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx

from mmag.diagnostics import DiagnosticReader, resolve_project_path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Load .env then .env.debug (debug overrides for debug-specific keys)
for _env_file in (PROJECT_ROOT / ".env", PROJECT_ROOT / ".env.debug"):
    if _env_file.is_file():
        for _line in _env_file.read_text().splitlines():
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _, _val = _line.partition("=")
                os.environ.setdefault(_key.strip(), _val.strip())

BUGS_CHANNEL_ID = os.getenv("DEBUG_BUGS_CHANNEL_ID", "")
TEST_CHANNEL_ID = os.getenv("DEBUG_TEST_CHANNEL_ID", "")
MM_URL = os.getenv("MM_URL", "")
MM_USERNAME = os.getenv("MM_USERNAME", "")
MM_PASSWORD = os.getenv("MM_PASSWORD", "")
MM_TOKEN = os.getenv("MM_TOKEN", "")
LOGS_DIR = resolve_project_path(
    os.getenv("DEBUG_LOG_DIR", os.getenv("LOG_DIR", "logs")) or "logs",
    project_root=PROJECT_ROOT,
)
DB_PATH = resolve_project_path(
    os.getenv("DEBUG_MEMORY_DB_PATH", os.getenv("MEMORY_DB_PATH", "agent_memory.db")),
    project_root=PROJECT_ROOT,
)


def _diagnostics() -> DiagnosticReader:
    return DiagnosticReader(DB_PATH, LOGS_DIR)


def _tls_verify() -> bool:
    return os.getenv("DEBUG_TLS_VERIFY", "true").strip().lower() not in {"0", "false", "no"}


def _validate_debug_url() -> None:
    from urllib.parse import urlsplit

    parsed = urlsplit(MM_URL)
    if parsed.scheme == "https" and parsed.hostname:
        return
    if parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        return
    print(
        "❌ 调试用户登录要求 HTTPS；仅 localhost/127.0.0.1/::1 允许 HTTP",
        file=sys.stderr,
    )
    raise SystemExit(1)


def login() -> str:
    if not MM_URL or not MM_USERNAME or not MM_PASSWORD:
        print("❌ 需要 MM_URL、MM_USERNAME、MM_PASSWORD 环境变量", file=sys.stderr)
        sys.exit(1)
    _validate_debug_url()
    resp = httpx.post(
        f"{MM_URL}/api/v4/users/login",
        json={"login_id": MM_USERNAME, "password": MM_PASSWORD},
        verify=_tls_verify(),
    )
    resp.raise_for_status()
    token = resp.headers.get("Token", "")
    if not token:
        print("❌ 登录失败：未返回 Token", file=sys.stderr)
        sys.exit(1)
    return token


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _git(args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=True,
        cwd=PROJECT_ROOT,
    )
    return result.stdout.strip()


def collect_update() -> str:
    lines: list[str] = []

    commits = _git(["log", "--oneline", "-10"])
    lines.append("## 近期 Git Commits")
    lines.append("```")
    lines.append(commits)
    lines.append("```")
    lines.append("")

    diff_stat = _git(["diff", "--stat", "HEAD~5"])
    if diff_stat:
        lines.append("## 变更统计 (HEAD~5)")
        lines.append("```")
        lines.append(diff_stat)
        lines.append("```")
        lines.append("")

    changelog = PROJECT_ROOT / "CHANGELOG.md"
    if changelog.is_file():
        content = changelog.read_text(encoding="utf-8")
        sections = content.split("\n## ")
        if len(sections) > 1:
            latest = sections[1].strip()
            lines.append("## CHANGELOG 最近条目")
            lines.append(latest)
            lines.append("")

    return "\n".join(lines)


def post_update() -> None:
    if not BUGS_CHANNEL_ID:
        print("❌ 需要在 .env.debug 中设置 DEBUG_BUGS_CHANNEL_ID", file=sys.stderr)
        sys.exit(1)
    token = login()
    body = collect_update()
    message = f"### 🔄 开发更新摘要\n\n{body}"
    resp = httpx.post(
        f"{MM_URL}/api/v4/posts",
        headers=_headers(token),
        json={"channel_id": BUGS_CHANNEL_ID, "message": message},
        verify=_tls_verify(),
    )
    resp.raise_for_status()
    post_id = resp.json().get("id", "")
    print(f"✅ 更新已发布到 Bugs 频道 (post_id={post_id[:12]}...)")


def _channel_name(channel_id: str, token: str) -> str:
    try:
        resp = httpx.get(
            f"{MM_URL}/api/v4/channels/{channel_id}",
            headers=_headers(token),
            verify=_tls_verify(),
        )
        resp.raise_for_status()
        return resp.json().get("display_name", channel_id[:8])
    except Exception:
        return channel_id[:8]


def _post_summary(post: dict, bot_user_id: str) -> str:
    uid = post.get("user_id", "")[:12]
    msg = post.get("message", "").replace("\n", " ")[:120]
    if len(post.get("message", "")) > 120:
        msg += "..."
    pid = post.get("id", "")[:12]
    is_bot = "🤖" if uid == bot_user_id[:12] else "👤"
    return f"{is_bot} [{pid}] {msg}"


def collect_issues() -> None:
    if not BUGS_CHANNEL_ID:
        print("❌ 需要在 .env.debug 中设置 DEBUG_BUGS_CHANNEL_ID", file=sys.stderr)
        sys.exit(1)
    token = login()
    bot_uid = _bot_user_id(token)
    chan_name = _channel_name(BUGS_CHANNEL_ID, token)
    print(f"📋 Bugs 频道 ({chan_name}) 最近 20 条消息:\n")
    resp = httpx.get(
        f"{MM_URL}/api/v4/channels/{BUGS_CHANNEL_ID}/posts",
        headers=_headers(token),
        params={"per_page": 20},
        verify=_tls_verify(),
    )
    resp.raise_for_status()
    data = resp.json()
    posts = data.get("posts", {})
    order = data.get("order", [])
    sorted_posts = sorted(order, key=lambda pid: posts[pid].get("create_at", 0))
    for pid in sorted_posts:
        print(_post_summary(posts[pid], bot_uid))


def _bot_user_id(token: str) -> str:
    resp = httpx.get(
        f"{MM_URL}/api/v4/users/me",
        headers=_headers(token),
        verify=_tls_verify(),
    )
    resp.raise_for_status()
    return resp.json().get("id", "")


def _resolve_post_id(short_id: str, token: str) -> str:
    if len(short_id) >= 26:
        return short_id
    resp = httpx.get(
        f"{MM_URL}/api/v4/channels/{BUGS_CHANNEL_ID}/posts",
        headers=_headers(token),
        params={"per_page": 50},
        verify=_tls_verify(),
    )
    resp.raise_for_status()
    posts = resp.json().get("posts", {})
    for pid in posts:
        if pid.startswith(short_id):
            return pid
    print(f"❌ 找不到以 {short_id} 开头的帖子", file=sys.stderr)
    sys.exit(1)


def reply_to_post(post_id: str, message: str) -> None:
    if not BUGS_CHANNEL_ID:
        print("❌ 需要在 .env.debug 中设置 DEBUG_BUGS_CHANNEL_ID", file=sys.stderr)
        sys.exit(1)
    token = login()
    full_id = _resolve_post_id(post_id, token)
    resp = httpx.get(
        f"{MM_URL}/api/v4/posts/{full_id}",
        headers=_headers(token),
        verify=_tls_verify(),
    )
    resp.raise_for_status()
    post = resp.json()
    channel_id = post.get("channel_id", BUGS_CHANNEL_ID)
    root_id = post.get("root_id") or full_id
    reply_resp = httpx.post(
        f"{MM_URL}/api/v4/posts",
        headers=_headers(token),
        json={
            "channel_id": channel_id,
            "message": message,
            "root_id": root_id,
        },
        verify=_tls_verify(),
    )
    reply_resp.raise_for_status()
    reply_id = reply_resp.json().get("id", "")
    print(f"✅ 回复已发送 (reply_id={reply_id[:12]}...)")


# ---- debug-test: 发消息 → 等 reply → 输出日志 + 审计 ----

def _bot_info() -> tuple[str, str]:
    if not MM_TOKEN:
        print("❌ 需要 MM_TOKEN 环境变量", file=sys.stderr)
        sys.exit(1)
    resp = httpx.get(
        f"{MM_URL}/api/v4/users/me",
        headers=_headers(MM_TOKEN),
        verify=_tls_verify(),
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("id", ""), data.get("username", "")


def _latest_log_file() -> Path | None:
    logs = _log_files()
    return logs[0] if logs else None


def _log_files() -> list[Path]:
    return sorted(LOGS_DIR.glob("mmag-*.log*"), key=lambda p: p.stat().st_mtime, reverse=True)


def _query_audit(trace_id: str) -> list[dict]:
    return list(_diagnostics().report(trace_id).audits)


def _terminal_reply(post: dict, *, bot_uid: str, root_post_id: str, created_at: int) -> bool:
    if post.get("user_id") != bot_uid or post.get("create_at", 0) <= created_at:
        return False
    if str(post.get("root_id") or "") != root_post_id:
        return False
    props = post.get("props") if isinstance(post.get("props"), dict) else {}
    kind = str(props.get("mmag_kind") or "")
    status = str(props.get("mmag_status") or "")
    return kind not in {"status", "stream"} or status in {
        "completed",
        "failed",
        "waiting_approval",
    }


def _reply_exit_code(post: dict | None) -> int:
    if not isinstance(post, dict):
        return 1
    props = post.get("props") if isinstance(post.get("props"), dict) else {}
    return (
        1
        if props.get("mmag_kind") == "error"
        or props.get("mmag_status") in {"failed", "exhausted"}
        else 0
    )


def _print_timeline(trace_id: str, log_file: Path | None = None) -> None:
    report = _diagnostics().report(trace_id)
    audit = list(report.audits)
    print(f"\n🧭 执行时间线 (trace={trace_id}):")
    if not audit:
        print("   未找到审计事件")
    for item in audit:
        details = item.get("details", {}) if isinstance(item.get("details"), dict) else {}
        agent = str(details.get("agent_ref") or "")
        skill = str(details.get("skill_ref") or "")
        reason = str(details.get("reason") or details.get("rule_id") or "")
        suffix = " ".join(value for value in (agent, skill, reason) if value)
        print(
            f"   {item['event_type']:25s} {item['decision']:16s} "
            f"{str(item.get('target') or '')[:24]:24s} {suffix}"
        )
    events = list(report.logs)
    if events:
        files = sorted({str(item.get("log_file") or "") for item in events})
        print(f"\n📋 结构化运行事件 ({', '.join(value for value in files if value)}):")
    for payload in events:
        print(
            "   "
            f"{str(payload.get('event') or ''):30s} "
            f"{str(payload.get('status') or ''):16s} "
            f"agent={str(payload.get('agent_ref') or '-'):18s} "
            f"skill={str(payload.get('skill_ref') or '-'):18s} "
            f"cap={str(payload.get('capability') or '-'):20s} "
            f"duration={payload.get('duration_ms', '-')}"
        )


def show_trace(identifier: str, *, json_output: bool = False) -> int:
    report = _diagnostics().report(identifier)
    if json_output:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if report.found else 1
    if not report.found:
        print(f"❌ 未找到 trace/run: {identifier}", file=sys.stderr)
        for warning in report.warnings:
            print(f"   {warning}", file=sys.stderr)
        return 1
    _print_timeline(report.trace_id)
    return 0


def test_message(message: str, wait_seconds: int = 120, *, json_output: bool = False) -> int:
    if not TEST_CHANNEL_ID:
        print("❌ 需要在 .env.debug 中设置 DEBUG_TEST_CHANNEL_ID", file=sys.stderr)
        sys.exit(1)
    token = login()
    bot_uid, bot_name = _bot_info()
    if not json_output:
        print(f"🤖 Bot: {bot_name} ({bot_uid[:12]}...)")
        print(f"📤 发送消息到 mmag-test: {message[:80]}")
    resp = httpx.post(
        f"{MM_URL}/api/v4/posts",
        headers=_headers(token),
        json={"channel_id": TEST_CHANNEL_ID, "message": message},
        verify=_tls_verify(),
    )
    resp.raise_for_status()
    post = resp.json()
    post_id = post["id"]
    run_id = f"mattermost:{post_id}"
    if not json_output:
        print(f"✅ 消息已发送 (post_id={post_id[:12]}..., run_id={run_id[:24]}...)")
        print(f"⏳ 等待 bot 回复 (最多 {wait_seconds}s)...")
    bot_reply = None
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        time.sleep(3)
        posts_resp = httpx.get(
            f"{MM_URL}/api/v4/channels/{TEST_CHANNEL_ID}/posts",
            headers=_headers(token),
            params={"per_page": 5},
            verify=_tls_verify(),
        )
        posts_resp.raise_for_status()
        data = posts_resp.json()
        all_posts = data.get("posts", {})
        recent = sorted(
            all_posts.values(),
            key=lambda p: p.get("create_at", 0),
        )
        for p in recent:
            if _terminal_reply(
                p,
                bot_uid=bot_uid,
                root_post_id=post_id,
                created_at=post["create_at"],
            ):
                bot_reply = p
                break
        if bot_reply:
            break
    if bot_reply and not json_output:
        print("\n💬 Bot 回复:")
        print(f"   {bot_reply['message'][:500]}")
    elif not bot_reply and not json_output:
        print(f"\n⚠️  {wait_seconds}s 内未收到 bot 回复")
    report = _diagnostics().report(run_id)
    trace_id = report.trace_id
    if json_output:
        props = bot_reply.get("props", {}) if isinstance(bot_reply, dict) else {}
        kind = str(props.get("mmag_kind") or "") if isinstance(props, dict) else ""
        status = str(props.get("mmag_status") or "") if isinstance(props, dict) else ""
        success = _reply_exit_code(bot_reply) == 0
        payload = {
            "success": success,
            "message_id": post_id,
            "run_id": run_id,
            "response_kind": kind,
            "terminal_status": status,
            "diagnostics": report.to_dict(),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if success else 1
    if trace_id:
        print(f"\n📋 日志 ({len(report.logs)} 条, trace={trace_id[:12]}...):")
        for item in report.logs:
            print(f"   {json.dumps(item, ensure_ascii=False)[:200]}")
        audit = list(report.audits)
        if audit:
            print(f"\n🔍 审计 ({len(audit)} 条):")
            for a in audit:
                details = a.get("details", {}) if isinstance(a.get("details"), dict) else {}
                skill = details.get("skill_ref", "")
                print(f"   {a['event_type']:25s} {a['decision']:10s} {a.get('target','')[:20]:20s} {skill}")
        else:
            print("\n🔍 审计: 未找到相关条目")
        _print_timeline(trace_id)
    else:
        print("\n📋 日志: 未找到相关条目")
        print("\n🔍 审计: 无法关联 trace_id")
    return _reply_exit_code(bot_reply)


# ---- debug-status: bot 状态 + 加载的 agent/skill + 最近运行 ----

def show_status() -> None:
    print("📊 MMAG 调试状态\n")
    result = subprocess.run(
        ["ps", "-eo", "pid=,args="],
        capture_output=True,
        text=True,
    )
    pids = []
    for line in result.stdout.splitlines():
        pid, _, args = line.strip().partition(" ")
        if args == "uv run python -m mmag.cli" or args.endswith("/python3 -m mmag.cli"):
            pids.append(pid)
    if pids:
        print(f"🟢 Bot 进程运行中 (PID: {', '.join(pids)})")
    else:
        print("🔴 Bot 进程未运行")
    log_file = _latest_log_file()
    if log_file and log_file.is_file():
        print(f"\n📄 最新日志: {log_file.name}")
    else:
        print("\n📄 未找到日志文件")
    ready = _diagnostics().latest_event("application.ready")
    if ready:
        details = ready.get("details", {}) if isinstance(ready.get("details"), dict) else {}
        print(
            "\n🧩 结构化启动状态: "
            f"agents={details.get('agent_count', '-')} "
            f"skills={details.get('skill_count', '-')} "
            f"capabilities={details.get('capability_count', '-')}"
        )
        for label, key in (
            ("Agent", "agent_names"),
            ("Skill", "skill_refs"),
            ("Capability", "capability_names"),
        ):
            values = details.get(key, ())
            if isinstance(values, list):
                print(f"   {label}: {', '.join(str(value) for value in values)}")
    if DB_PATH.is_file():
        print(f"\n🗡️  最近运行 ({DB_PATH.name}):")
        db = sqlite3.connect(str(DB_PATH))
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT trace_id, event_type, decision, created_at "
            "FROM audit_events "
            "WHERE event_type IN ('agent.run', 'model.route') "
            "ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
        for r in rows:
            print(f"   {r['trace_id'][:16]}  {r['event_type']:15s} {r['decision']}")
        db.close()
    else:
        print(f"\n🗡️  未找到 {DB_PATH}")


def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python -m scripts.debug <update|collect|reply|test|status|trace> [args]")
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "update":
        post_update()
    elif cmd == "collect":
        collect_issues()
    elif cmd == "reply":
        if len(sys.argv) < 4:
            print("用法: python -m scripts.debug reply <post_id> <message>")
            sys.exit(1)
        reply_to_post(sys.argv[2], sys.argv[3])
    elif cmd == "test":
        if len(sys.argv) < 3:
            print("用法: python -m scripts.debug test <message> [wait_seconds]")
            sys.exit(1)
        arguments = [value for value in sys.argv[3:] if value != "--json"]
        wait = int(arguments[0]) if arguments else 120
        raise SystemExit(test_message(sys.argv[2], wait_seconds=wait, json_output="--json" in sys.argv))
    elif cmd == "status":
        show_status()
    elif cmd == "trace":
        if len(sys.argv) < 3:
            print("用法: python -m scripts.debug trace <trace-id|run-id>")
            sys.exit(1)
        raise SystemExit(show_trace(sys.argv[2], json_output="--json" in sys.argv))
    else:
        print(f"未知命令: {cmd}")
        sys.exit(1)
