"""Repeatable, transport-safe Mattermost capability discovery."""

from __future__ import annotations

import contextlib
import time
from dataclasses import asdict, dataclass
from ipaddress import ip_address
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class MattermostCapabilities:
    checked_at: float
    trusted_transport: bool
    server_version: str = "unknown"
    edition: str = "unknown"
    files_enabled: bool | None = None
    plugins_enabled: bool | None = None
    interactive_messages_enabled: bool | None = None
    clients: tuple[str, ...] = ("web", "desktop", "mobile")
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)


class MattermostCapabilityProbe:
    def __init__(self, client) -> None:
        self.client = client

    async def probe(self) -> MattermostCapabilities:
        trusted = self._trusted_transport(self.client.base_url)
        if not trusted:
            return MattermostCapabilities(
                checked_at=time.time(),
                trusted_transport=False,
                warnings=(
                    "认证级能力探测已跳过：非本机 Mattermost 必须使用 HTTPS。",
                ),
            )
        warnings: list[str] = []
        version = "unknown"
        edition = "unknown"
        settings: dict = {}
        try:
            response = await self.client._request_async("GET", "/system/ping")
            payload = response.json()
            version = str(
                response.headers.get("X-Version-ID")
                or (payload.get("version") if isinstance(payload, dict) else "")
                or "unknown"
            )
        except Exception as error:
            warnings.append(f"Server 版本探测失败：{type(error).__name__}")
        try:
            response = await self.client._request_async("GET", "/config/client?format=old")
            payload = response.json()
            settings = payload if isinstance(payload, dict) else {}
            if "IsLicensed" in settings:
                edition = "enterprise" if self._flag(settings, "IsLicensed") else "team"
        except Exception as error:
            warnings.append(f"Client 配置探测失败：{type(error).__name__}")
        return MattermostCapabilities(
            checked_at=time.time(),
            trusted_transport=True,
            server_version=version,
            edition=edition,
            files_enabled=self._optional_flag(settings, "EnableFileAttachments"),
            plugins_enabled=self._optional_flag(settings, "EnablePlugins"),
            interactive_messages_enabled=self._optional_flag(
                settings, "EnablePostActionIntegration"
            ),
            warnings=tuple(warnings),
        )

    @staticmethod
    def _trusted_transport(base_url: str) -> bool:
        parsed = urlsplit(base_url)
        if parsed.scheme == "https":
            return True
        if parsed.scheme != "http" or not parsed.hostname:
            return False
        if parsed.hostname == "localhost":
            return True
        loopback = False
        with contextlib.suppress(ValueError):
            loopback = ip_address(parsed.hostname).is_loopback
        return loopback

    @classmethod
    def _optional_flag(cls, settings: dict, name: str) -> bool | None:
        if name not in settings:
            return None
        return cls._flag(settings, name)

    @staticmethod
    def _flag(settings: dict, name: str) -> bool:
        value = settings.get(name)
        if isinstance(value, bool):
            return value
        return str(value or "").lower() in {"true", "1", "yes"}
