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
LOGS_DIR = PROJECT_ROOT / "logs"
DB_PATH = PROJECT_ROOT / "agent_memory.db"


def login() -> str:
    if not MM_URL or not MM_USERNAME or not MM_PASSWORD:
        print("❌ 需要 MM_URL、MM_USERNAME、MM_PASSWORD 环境变量", file=sys.stderr)
        sys.exit(1)
    resp = httpx.post(
        f"{MM_URL}/api/v4/users/login",
        json={"login_id": MM_USERNAME, "password": MM_PASSWORD},
        verify=False,
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
        verify=False,
    )
    resp.raise_for_status()
    post_id = resp.json().get("id", "")
    print(f"✅ 更新已发布到 Bugs 频道 (post_id={post_id[:12]}...)")


def _channel_name(channel_id: str, token: str) -> str:
    try:
        resp = httpx.get(
            f"{MM_URL}/api/v4/channels/{channel_id}",
            headers=_headers(token),
            verify=False,
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
        verify=False,
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
        verify=False,
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
        verify=False,
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
        verify=False,
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
        verify=False,
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
        verify=False,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("id", ""), data.get("username", "")


def _latest_log_file() -> Path | None:
    logs = _log_files()
    return logs[0] if logs else None


def _latest_bot_log_file() -> Path | None:
    for path in _log_files()[:20]:
        try:
            if "Agent 就绪" in path.read_text(encoding="utf-8", errors="replace"):
                return path
        except OSError:
            continue
    return _latest_log_file()


def _log_files() -> list[Path]:
    return sorted(LOGS_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)


def _extract_trace_id(post_id: str) -> str:
    return post_id[:16] if len(post_id) >= 16 else post_id


def _find_trace_id_from_log(log_file: Path, run_id: str) -> str:
    """Scan log for run_id, extract the trace_id from the matching line."""
    if not log_file or not log_file.is_file():
        return ""
    for line in log_file.read_text(encoding="utf-8", errors="replace").splitlines():
        if run_id in line and "trace_id" in line:
            import re
            m = re.search(r'"trace_id":"([0-9a-f]+)"', line)
            if m:
                return m.group(1)
            m = re.search(r'trace=([0-9a-f]+)', line)
            if m:
                return m.group(1)
    return ""


def _find_trace_log(run_id: str) -> tuple[str, Path | None]:
    for log_file in _log_files()[:20]:
        trace_id = _find_trace_id_from_log(log_file, run_id)
        if trace_id:
            return trace_id, log_file
    return "", _latest_log_file()


def _grep_log(log_file: Path, trace_id: str) -> list[str]:
    if not log_file or not log_file.is_file():
        return []
    lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    matches = [line for line in lines if trace_id in line]
    return matches[-30:] if len(matches) > 30 else matches


def _query_audit(trace_id: str) -> list[dict]:
    if not DB_PATH.is_file():
        return []
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT event_type, target, decision, details, created_at "
        "FROM audit_events WHERE trace_id = ? ORDER BY created_at ASC",
        (trace_id,),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


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


def _print_timeline(trace_id: str, log_file: Path | None = None) -> None:
    audit = _query_audit(trace_id)
    print(f"\n🧭 执行时间线 (trace={trace_id}):")
    if not audit:
        print("   未找到审计事件")
    for item in audit:
        details = json.loads(item.get("details", "{}")) if item.get("details") else {}
        agent = str(details.get("agent_ref") or "")
        skill = str(details.get("skill_ref") or "")
        reason = str(details.get("reason") or details.get("rule_id") or "")
        suffix = " ".join(value for value in (agent, skill, reason) if value)
        print(
            f"   {item['event_type']:25s} {item['decision']:16s} "
            f"{str(item.get('target') or '')[:24]:24s} {suffix}"
        )
    if log_file is None:
        _, log_file = _find_trace_log(trace_id)
    if log_file is None:
        return
    events: list[dict] = []
    for line in _grep_log(log_file, trace_id):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.append(payload)
    if events:
        print(f"\n📋 结构化运行事件 ({log_file.name}):")
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


def show_trace(identifier: str) -> None:
    if not DB_PATH.is_file():
        print("❌ 未找到 agent_memory.db", file=sys.stderr)
        sys.exit(1)
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    row = db.execute(
        "SELECT trace_id FROM audit_events "
        "WHERE trace_id=? OR details LIKE ? ORDER BY created_at DESC LIMIT 1",
        (identifier, f"%{identifier}%"),
    ).fetchone()
    db.close()
    trace_id = str(row["trace_id"] or "") if row else ""
    if not trace_id:
        trace_id, _ = _find_trace_log(identifier)
    if not trace_id:
        print(f"❌ 未找到 trace/run: {identifier}", file=sys.stderr)
        sys.exit(1)
    _, log_file = _find_trace_log(trace_id)
    _print_timeline(trace_id, log_file)


def test_message(message: str, wait_seconds: int = 120) -> None:
    if not TEST_CHANNEL_ID:
        print("❌ 需要在 .env.debug 中设置 DEBUG_TEST_CHANNEL_ID", file=sys.stderr)
        sys.exit(1)
    token = login()
    bot_uid, bot_name = _bot_info()
    print(f"🤖 Bot: {bot_name} ({bot_uid[:12]}...)")
    print(f"📤 发送消息到 mmag-test: {message[:80]}")
    resp = httpx.post(
        f"{MM_URL}/api/v4/posts",
        headers=_headers(token),
        json={"channel_id": TEST_CHANNEL_ID, "message": message},
        verify=False,
    )
    resp.raise_for_status()
    post = resp.json()
    post_id = post["id"]
    run_id = f"mattermost:{post_id}"
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
            verify=False,
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
    if bot_reply:
        print("\n💬 Bot 回复:")
        print(f"   {bot_reply['message'][:500]}")
    else:
        print(f"\n⚠️  {wait_seconds}s 内未收到 bot 回复")
    trace_id, log_file = _find_trace_log(run_id)
    if trace_id:
        log_lines = _grep_log(log_file, trace_id)
        print(f"\n📋 日志 ({len(log_lines)} 条, trace={trace_id[:12]}..., {log_file.name}):")
        for line in log_lines:
            print(f"   {line[:200]}")
        audit = _query_audit(trace_id)
        if audit:
            print(f"\n🔍 审计 ({len(audit)} 条):")
            for a in audit:
                details = json.loads(a.get("details", "{}")) if a.get("details") else {}
                skill = details.get("skill_ref", "")
                print(f"   {a['event_type']:25s} {a['decision']:10s} {a.get('target','')[:20]:20s} {skill}")
        else:
            print("\n🔍 审计: 未找到相关条目")
        _print_timeline(trace_id, log_file)
    else:
        log_lines = _grep_log(log_file, run_id)
        if log_lines:
            print(f"\n📋 日志 ({len(log_lines)} 条, run_id 关联, {log_file.name}):")
            for line in log_lines:
                print(f"   {line[:200]}")
        else:
            print("\n📋 日志: 未找到相关条目")
        print("\n🔍 审计: 无法关联 trace_id")


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
    log_file = _latest_bot_log_file()
    if log_file and log_file.is_file():
        print(f"\n📄 最新日志: {log_file.name}")
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines:
            if "Capability 已注册" in line:
                cap = line.split("Capability 已注册:")[1].strip().rstrip("}")
                print(f"   🔧 {cap}")
        for line in lines:
            if "MCP Server" in line and "已就绪" in line:
                print(f"   🔌 {line.split('event=')[0].strip()}")
        for line in lines:
            if "Agent 就绪" in line:
                print(f"   ✅ {line.split('event=')[0].strip()}")
                break
    else:
        print("\n📄 未找到日志文件")
    if DB_PATH.is_file():
        print("\n🗡️  最近运行 (agent_memory.db):")
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
        print("\n🗡️  未找到 agent_memory.db")


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
        wait = int(sys.argv[3]) if len(sys.argv) > 3 else 120
        test_message(sys.argv[2], wait_seconds=wait)
    elif cmd == "status":
        show_status()
    elif cmd == "trace":
        if len(sys.argv) < 3:
            print("用法: python -m scripts.debug trace <trace-id|run-id>")
            sys.exit(1)
        show_trace(sys.argv[2])
    else:
        print(f"未知命令: {cmd}")
        sys.exit(1)
