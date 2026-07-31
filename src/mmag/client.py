"""
Mattermost REST API 客户端
"""

import asyncio
import mimetypes
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


# Mattermost post props 业务常量
# 用途: send_post 时通过 props 标记消息类型,前端可据此区分 Bot 消息/摘要
PROP_FROM_BOT = "from_bot"
PROP_SUMMARY = "summary"
PROP_TRUE = "true"


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
        self, channel_id: str, message: str, root_id: str = "", props: dict | None = None,
        file_ids: list[str] | None = None,
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
        if file_ids:
            payload["file_ids"] = file_ids
        try:
            result = self._post("/posts", json=payload)
            post_id = result.get("id")
            log.debug(f"消息已发送: {post_id[:12]}... → {channel_id[:8]}")
            return post_id
        except Exception as e:
            log.error(f"发送消息失败: {e}")
            return None

    def upload_file(
        self, channel_id: str, filename: str, data: bytes, content_type: str = "",
    ) -> str | None:
        """上传文件到频道，返回 file_id

        Mattermost API: POST /api/v4/files (multipart/form-data)
        上传后文件不可见，需创建带 file_ids 的 post 才在频道显示。
        """
        if not content_type:
            guessed_type, _ = mimetypes.guess_type(filename)
            content_type = guessed_type or "application/octet-stream"
        try:
            resp = self.session.post(
                f"{self.base_url}/api/v4/files",
                data={"channel_id": channel_id, "filename": filename},
                files={"files": (filename, data, content_type)},
                timeout=60,
            )
            resp.raise_for_status()
            result = resp.json()
            if isinstance(result, list) and result:
                file_id = result[0].get("id")
                log.debug(f"文件已上传: {filename} ({len(data)} bytes) → file_id={file_id[:12] if file_id else '?'}...")
                return file_id
            log.error(f"上传文件响应格式异常: {type(result)}")
            return None
        except Exception as e:
            log.error(f"上传文件失败: {e}")
            return None

    def send_typing(self, channel_id: str) -> bool:
        """发送 typing indicator (频道内显示"正在输入...")

        Mattermost typing indicator ~3s 后过期,调用方需定期重发。
        """
        user_id = self.get_me()["id"]
        try:
            self._post(f"/users/{user_id}/typing", json={"channel_id": channel_id})
            return True
        except Exception as e:
            log.debug(f"typing indicator 失败: {e}")
            return False

    def get_posts(self, channel_id: str, limit: int = 30) -> list[dict]:
        """获取频道最近消息(limit 不分页,一次性)"""
        return self.get_posts_page(channel_id, page=0, per_page=limit)

    def get_posts_page(self, channel_id: str, page: int = 0, per_page: int = 200) -> list[dict]:
        """分页获取频道消息 — Mattermost 原生 page/per_page, 用于 backfill 历史

        Returns: 该页的消息列表(按 order 数组顺序,最新在前)
        """
        try:
            data = self._get(
                f"/channels/{channel_id}/posts", page=page, per_page=per_page
            )
            order = data.get("order", [])
            posts = data.get("posts", {})
            return [posts[pid] for pid in order if pid in posts]
        except Exception as e:
            log.error(f"分页获取消息失败 (page={page}): {e}")
            return []

    def get_file_bytes(self, file_id: str) -> tuple[bytes, str] | None:
        """同步下载附件二进制 — 保持向后兼容(被非 async 代码/单测用)

        异步场景请用 `get_file_bytes_async` (避免阻塞 event loop)
        """
        return _download_file_sync(self.session, self.base_url, file_id)

    async def get_file_bytes_async(self, file_id: str) -> tuple[bytes, str] | None:
        """异步下载附件二进制 — 把阻塞的 requests 调用扔到默认 thread pool 跑

        多图场景下, 调用方应该用 `asyncio.gather` 并发拉多张,
        比串行下载快 N 倍 (N = 图片数, 受 MM 服务器并发限制)。

        Args:
            file_id: Mattermost file id

        Returns:
            (bytes, mime_type) — 成功;或 None — 失败
        """
        if not file_id:
            return None
        try:
            return await asyncio.to_thread(
                _download_file_sync, self.session, self.base_url, file_id
            )
        except Exception as e:
            log.error("下载附件异常 file_id=%s: %s", file_id[:12], e)
            return None


# ============================================================
# 内部: 同步下载函数 (可被 asyncio.to_thread 复用)
# ============================================================


def _download_file_sync(session: requests.Session, base_url: str, file_id: str) -> tuple[bytes, str] | None:
    """同步下载附件 — 提取出来便于 `asyncio.to_thread` 包装

    Mattermost 设计: `GET /api/v4/files/{file_id}` 直接返回文件二进制
    (Content-Type 头带真实 MIME, image/jpeg / image/png / application/pdf 等)。
    元信息 (name/mime/size/dimensions) 不需要走此 API, 而是已经嵌在
    `post["metadata"]["files"]` 里, 由调用方在发消息时一起拿到。

    Returns:
        (bytes, mime_type) — 成功;或 None — 下载失败/文件不存在/无权限
    """
    if not file_id:
        return None
    try:
        # 绕过 _get (那用 raise_for_status + .json(), 对二进制不合适)
        resp = session.get(
            f"{base_url}/api/v4/files/{file_id}",
            timeout=30,
        )
        if resp.status_code != 200:
            log.warning(
                "下载附件失败 file_id=%s status=%d", file_id[:12], resp.status_code
            )
            return None
        # 优先用响应头, 因为有些服务器会对 jpg 返回 application/octet-stream
        mime = resp.headers.get("Content-Type") or "application/octet-stream"
        # 去掉可能附带的 charset (e.g. "image/jpeg; charset=utf-8")
        mime = mime.split(";", 1)[0].strip().lower()
        return resp.content, mime
    except Exception as e:
        log.error("下载附件异常 file_id=%s: %s", file_id[:12], e)
        return None
