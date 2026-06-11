"""
提示词管理
"""

from pathlib import Path

import yaml

from .config import config
from .logger import get_logger

log = get_logger(__name__)


class PromptManager:
    """从 prompts.yml 加载和渲染提示词"""

    def __init__(self, path: Path | None = None):
        self.path = path or (Path(__file__).resolve().parents[2] / "prompts.yml")
        self._templates: dict[str, str] = {}
        self._load()

    def _load(self):
        if not self.path.exists():
            log.warning("提示词文件不存在: %s，使用内置默认值", self.path)
            return
        with open(self.path, "r", encoding="utf-8") as f:
            self._templates = yaml.safe_load(f) or {}
        log.info("已加载 %d 个提示词模板", len(self._templates))

    def get(self, name: str, **kwargs) -> str:
        template = self._templates.get(name, "")
        if not template:
            return ""
        try:
            return template.format(**kwargs)
        except KeyError as e:
            log.warning("提示词 '%s' 缺少变量: %s", name, e)
            return template


prompts = PromptManager()
