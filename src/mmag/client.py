"""
Mattermost REST API 客户端
"""

from typing import Any

import requests

from .config import config
from .logger import get_logger

log = get_logger(__name__)


# Channel 类型标签（O=Open 公开, P=Private 私有, D=Direct 私聊）
CHANNEL_TYPE_LABELS: dict[str, str] = {
    "O": "公开",
    "P": "私有",
    "D": "私聊",
}


def channel_type_label(channel_type: str) -> str:
    """把 Mattermost channel type 字母转中文标签，未知值原样返回"""
    return CHANNEL_TYPE_LABELS.get(channel_type, channel_type)


class MMClient:
    """Mattermost REST API + 元数据缓存

    Args:
        base_url: API 根 URL（默认从 config.mm_url 读）
        token: Bearer token（默认从 config.mm_token 读）
    """

    def __init__(self, base_url: str | None = None, token: str | None = None):
        self.base_url = (base_url or config.mm_url).rstrip("/")
        self.session = requests.Session()
        bearer = token or config.mm_token
        self.session.headers.update(
            {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"}
        )
        self._users: dict[str, dict] = {}  # user_id → user info
        self._channels: dict[str, dict] = {}  # channel_id → channel info
        self._me: dict | None = None

    def _get(self, path: str, **params) -> Any:
        resp = self.session.get(f"{self.base_url}/api/v4{path}", params=params)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, **kwargs) -> Any:
        resp = self.session.post(f"{self.base_url}/api/v4{path}", **kwargs)
        resp.raise_for_status()
        return resp.json()

    def get_me(self) -> dict:
        if not self._me:
            self._me = self._get("/users/me")
            log.info(f"Bot 身份: @{self._me['username']} ({self._me['id']})")
        return self._me

    def get_user(self, user_id: str) -> dict:
        if user_id not in self._users:
            try:
                self._users[user_id] = self._get(f"/users/{user_id}")
            except Exception:
                self._users[user_id] = {"username": user_id[:8], "id": user_id}
        return self._users[user_id]

    def get_username(self, user_id: str) -> str:
        return self.get_user(user_id).get("username", user_id[:8])

    def get_channel(self, channel_id: str) -> dict:
        if channel_id not in self._channels:
            try:
                self._channels[channel_id] = self._get(f"/channels/{channel_id}")
            except Exception:
                self._channels[channel_id] = {
                    "id": channel_id,
                    "name": channel_id[:8],
                    "display_name": channel_id[:8],
                }
        return self._channels[channel_id]

    def send_post(
        self, channel_id: str, message: str, root_id: str = "", props: dict | None = None
    ) -> str | None:
        """发送消息到频道"""
        payload: dict[str, Any] = {
            "channel_id": channel_id,
            "message": message,
        }
        if root_id:
            payload["root_id"] = root_id
        if props:
            payload["props"] = props
        try:
            result = self._post("/posts", json=payload)
            post_id = result.get("id")
            log.debug(f"消息已发送: {post_id[:12]}... → {channel_id[:8]}")
            return post_id
        except Exception as e:
            log.error(f"发送消息失败: {e}")
            return None

    def send_ephemeral(self, user_id: str, channel_id: str, message: str):
        """发送仅对某用户可见的消息 (ephemeral)"""
        payload = {
            "user_id": user_id,
            "post": {"channel_id": channel_id, "message": message},
        }
        try:
            self._post("/posts/ephemeral", json=payload)
        except Exception as e:
            log.error(f"发送 ephemeral 失败: {e}")

    def get_posts(self, channel_id: str, limit: int = 30) -> list[dict]:
        """获取频道最近消息"""
        try:
            data = self._get(f"/channels/{channel_id}/posts", per_page=limit)
            order = data.get("order", [])
            posts = data.get("posts", {})
            return [posts[pid] for pid in order if pid in posts]
        except Exception as e:
            log.error(f"获取消息失败: {e}")
            return []
