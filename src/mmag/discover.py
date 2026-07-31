"""
Mattermost ID 探测工具
======================
自动从 .env 读取连接信息，探测当前 Bot 可见的所有 Team / Channel / User，
输出完整 ID 映射表，方便首次配置时选择目标。

用法:
    make discover
    uv run python -m mmag.discover
    uv run python -m mmag.discover --env .env2   # 指定环境文件
    uv run python -m mmag.discover --update-env  # 探测后自动更新 .env
    uv run python -m mmag.discover --team test   # 只看某个 team 下的频道
"""

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .client import MMClient, channel_type_label

# ── 路径 ──
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"


def load_env(env_file: Path | None = None) -> dict[str, str]:
    """加载 .env 文件 (优先级最高) > 系统环境变量

    行为与 config.py 一致：使用 python-dotenv 解析，override=True 让 .env 覆盖系统 env
    """
    target = env_file or ENV_FILE
    if target.exists():
        load_dotenv(target, override=True)
    keys = (
        "MM_URL",
        "MM_TOKEN",
        "MM_TEAM_ID",
        "MM_CHANNEL_ID",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
    )
    return {k: os.getenv(k, "") for k in keys}


class MMDiscoverer:
    """Mattermost 环境探测器 — 包装 MMClient，加一层表格打印与配置建议"""

    def __init__(self, base_url: str, token: str):
        self.client = MMClient(base_url=base_url, token=token)
        self.base_url = base_url.rstrip("/")

    # ── 便捷方法（直接转发到 MMClient._get）──

    def _get(self, path: str, **params) -> dict:
        return self.client._get(path, **params)

    @property
    def me(self) -> dict:
        return self.client.get_me()

    def get_teams(self) -> list[dict]:
        return self.client._get("/users/me/teams")

    def get_channels(self) -> list[dict]:
        return self.client._get("/users/me/channels")

    def get_team_channels(self, team_id: str) -> list[dict]:
        return self.client._get(f"/teams/{team_id}/channels")

    def get_user(self, user_id: str) -> dict:
        return self.client._get(f"/users/{user_id}")

    # ── 格式化输出 ──

    def print_banner(self):
        me = self.me
        print()
        print("╔══════════════════════════════════════════════════════╗")
        print("║       Mattermost 环境探测器                           ║")
        print("╚══════════════════════════════════════════════════════╝")
        print(f"  服务器: {self.base_url}")
        print(
            f"  Bot:   @{me['username']} ({me['first_name'] or ''} {me['last_name'] or ''})".strip()
        )
        print(f"  用户ID: {me['id']}")
        print()

    def print_teams(self, teams: list[dict], highlight_team_id: str = ""):
        if not teams:
            print("  ⚠️  未找到任何 Team")
            return
        print("┌──────────────────────────────────────────────────────┐")
        print("│  Teams (团队)                                        │")
        print("├──────────┬──────────┬───────────────────────────────┤")
        print(f"  {'显示名':<12s} │ {'Name':<10s} │ {'Team ID':<30s}")
        print("├──────────┼──────────┼───────────────────────────────┤")
        for t in teams:
            tid = t["id"]
            marker = " ⭐" if tid == highlight_team_id else ""
            name = t["display_name"] or t["name"]
            print(f"  {name:<12s} │ {t['name']:<10s} │ {tid}{marker}")
        print("└──────────┴──────────┴───────────────────────────────┘")
        print()

    def print_channels(self, channels: list[dict], highlight_channel_id: str = ""):
        if not channels:
            print("  ⚠️  未找到任何 Channel")
            return
        print("┌────────────────────────────────────────────────────────────────────┐")
        print("│  Channels (频道)                                                   │")
        print("├──────────────┬──────────┬────────────────┬────┬──────────────────┤")
        print(
            f"  {'显示名':<16s} │ {'Name':<10s} │ {'Channel ID':<16s} │ {'T':>2s} │ {'消息数':>6s}          │"
        )
        print("├──────────────┼──────────┼────────────────┼────┼──────────────────┤")
        for ch in channels:
            cid = ch["id"]
            marker = " ⭐" if cid == highlight_channel_id else ""
            name = ch.get("display_name") or ch.get("name", "?")
            type_label = channel_type_label(ch.get("type", "?"))
            # 获取消息数
            try:
                posts_data = self._get(f"/channels/{cid}/posts", per_page=1)
                msg_count: int | str = len(posts_data.get("order", []))
            except Exception:
                msg_count = "?"
            print(
                f"  {name:<16s} │ {ch.get('name', '?'):<10s} │ {cid}{marker:<{16 + len(marker)}s} │ {type_label:>2s} │ {str(msg_count):>6s}          │"
            )
        print("└──────────────┴──────────┴────────────────┴────┴──────────────────┘")
        print("  T = 类型 (O=Open公开, P=Private私有, D=Direct私聊)")
        print()

    def print_users_summary(self, teams: list[dict]):
        """打印关键用户摘要"""
        me = self.me
        print("┌──────────────────────────────────────────────────┐")
        print("│  关键用户                                         │")
        print("├──────────┬───────────────────────────────────────┤")
        print(f"  {'用户名':<12s} │ {'User ID':<32s} │ 说明")
        print("├──────────┼───────────────────────────────────────┤")
        print(f"  @{me['username']:<11s} │ {me['id']:<32s} │ Bot (你)")
        # 尝试找管理员/其他活跃用户
        seen = {me["id"]}
        for team in teams[:2]:
            try:
                members = self._get(f"/teams/{team['id']}/members", per_page=5)
                for m in members:
                    uid = m["user_id"]
                    if uid in seen:
                        continue
                    seen.add(uid)
                    u = self.get_user(uid)
                    role = "管理员" if "system_admin" in m.get("roles", "") else ""
                    print(f"  @{u['username']:<11s} │ {uid:<32s} │ {role}".rstrip())
            except Exception:
                pass
        print("└──────────┴───────────────────────────────────────┘")
        print()

    def print_env_suggestion(
        self,
        teams: list[dict],
        channels: list[dict],
        current_team_id: str = "",
        current_channel_id: str = "",
    ):
        """打印 .env 建议配置"""
        print("┌─────────────────────────────────────────────────────────────┐")
        print("│  .env 配置建议                                              │")
        print("├─────────────────────────────────────────────────────────────┤")

        # 推荐：选一个有消息的公开频道
        public_channels = [c for c in channels if c.get("type") == "O"]
        recommended = None
        if public_channels:
            # 选消息最多的
            best = public_channels[0]
            for ch in public_channels:
                try:
                    pd = self._get(f"/channels/{ch['id']}/posts", per_page=1)
                    count = len(pd.get("order", []))
                    if count > 0:
                        best = ch
                        break
                except Exception:
                    pass
            recommended = best

        rec_team = ""
        rec_channel = ""
        if recommended:
            rec_channel = recommended["id"]
            # 找所属 team
            for t in teams:
                try:
                    tc = self._get(f"/teams/{t['id']}/channels")
                    if any(c["id"] == rec_channel for c in tc):
                        rec_team = t["id"]
                        break
                except Exception:
                    pass

        print()
        print("  # 复制以下内容到 .env:")
        print()
        print(f"  MM_TEAM_ID={rec_team or '<Team ID>'}")
        print(f"  MM_CHANNEL_ID={rec_channel or '<Channel ID>'}")
        print()

        if current_team_id or current_channel_id:
            print("  当前 .env 配置:")
            print(f"    MM_TEAM_ID    = {current_team_id or '(未设置)'}")
            print(f"    MM_CHANNEL_ID = {current_channel_id or '(未设置)'}")
            if rec_team and rec_team != current_team_id:
                print("    ⚠️  Team ID 不匹配!")
            if rec_channel and rec_channel != current_channel_id:
                print("    ⚠️  Channel ID 不匹配!")
            print()

        print("└─────────────────────────────────────────────────────────────┘")
        return rec_team, rec_channel


def update_env_file(team_id: str, channel_id: str, target_file: Path | None = None):
    """更新环境文件中的 MM_TEAM_ID 和 MM_CHANNEL_ID"""
    target = target_file or ENV_FILE
    if not target.exists():
        print(f"  ❌ 环境文件不存在: {target}")
        return False

    lines = target.read_text(encoding="utf-8").splitlines()
    new_lines = []
    updated_team = False
    updated_channel = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("MM_TEAM_ID="):
            new_lines.append(f"MM_TEAM_ID={team_id}")
            updated_team = True
        elif stripped.startswith("MM_CHANNEL_ID="):
            new_lines.append(f"MM_CHANNEL_ID={channel_id}")
            updated_channel = True
        else:
            new_lines.append(line)

    # 如果原来没有这两个字段，追加到合适位置
    if not updated_team:
        insert_idx = 0
        for i, line in enumerate(new_lines):
            if line.strip().startswith("MM_TOKEN="):
                insert_idx = i + 1
                break
        new_lines.insert(insert_idx, f"MM_TEAM_ID={team_id}")

    if not updated_channel:
        insert_idx = 0
        for i, line in enumerate(new_lines):
            if line.strip().startswith("MM_TEAM_ID="):
                insert_idx = i + 1
                break
        new_lines.insert(insert_idx, f"MM_CHANNEL_ID={channel_id}")

    target.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    print(f"  ✅ {target.name} 已更新: MM_TEAM_ID={team_id}, MM_CHANNEL_ID={channel_id}")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Mattermost ID 探测工具 — 发现 Team/Channel/User ID",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  make discover               # 探测全部
  uv run python -m mmag.discover --update-env   # 探测后自动写入 .env
  uv run python -m mmag.discover --team test    # 只看指定 team 的频道
        """,
    )
    parser.add_argument(
        "--update-env",
        action="store_true",
        help="探测完成后自动更新 .env 中的 MM_TEAM_ID 和 MM_CHANNEL_ID",
    )
    parser.add_argument("--env", type=str, default="", help="指定环境文件路径 (默认: .env)")
    parser.add_argument("--team", type=str, default="", help="只显示指定 team name 下的频道")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")
    args = parser.parse_args()

    # ── 加载环境 ──
    env_path = Path(args.env) if args.env else None
    env = load_env(env_path)

    mm_url = env.get("MM_URL", "")
    mm_token = env.get("MM_TOKEN", "")

    if not mm_url or not mm_token:
        print("❌ 缺少必要配置！请确保 .env 中包含:")
        print("   MM_URL=http://your-mattermost-server")
        print("   MM_TOKEN=your-bot-token")
        sys.exit(1)

    # ── 开始探测 ──
    disc = MMDiscoverer(mm_url, mm_token)

    if args.json:
        # JSON 模式
        result = {
            "server": mm_url,
            "bot": disc.me,
            "teams": disc.get_teams(),
            "channels": disc.get_channels(),
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    disc.print_banner()

    # Teams
    teams = disc.get_teams()
    current_team_id = env.get("MM_TEAM_ID", "")
    disc.print_teams(teams, highlight_team_id=current_team_id)

    # 过滤 team
    if args.team:
        target_team = None
        for t in teams:
            if t["name"] == args.team or t["display_name"] == args.team:
                target_team = t
                break
        if not target_team:
            print(f"  ❌ 未找到 Team: '{args.team}'")
            print(f"  可用 Team: {[t['name'] for t in teams]}")
            sys.exit(1)
        channels = disc.get_team_channels(target_team["id"])
        print(f"  📍 仅显示 Team [{target_team['display_name']}] 下的频道\n")
    else:
        channels = disc.get_channels()

    current_channel_id = env.get("MM_CHANNEL_ID", "")
    disc.print_channels(channels, highlight_channel_id=current_channel_id)

    # 关键用户
    disc.print_users_summary(teams)

    # 配置建议
    rec_team, rec_channel = disc.print_env_suggestion(
        teams, channels, current_team_id, current_channel_id
    )

    # 自动更新 .env
    if args.update_env:
        if rec_team and rec_channel:
            update_env_file(rec_team, rec_channel, env_path)
        else:
            print("  ⚠️  无法确定推荐配置，跳过更新")


if __name__ == "__main__":
    main()
