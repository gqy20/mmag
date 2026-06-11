"""
提示词管理
"""

import logging
from pathlib import Path

import yaml

from .config import config

log = logging.getLogger("agent")


class PromptManager:
    """从 prompts.yml 加载和渲染提示词"""

    def __init__(self, path: Path | None = None):
        self.path = path or (Path(__file__).resolve().parents[2] / "prompts.yml")
        self._templates: dict[str, str] = {}
        self._load()

    def _load(self):
        if not self.path.exists():
            log.warning(f"提示词文件不存在: {self.path}，使用内置默认值")
            return
        with open(self.path, "r", encoding="utf-8") as f:
            self._templates = yaml.safe_load(f) or {}
        log.info(f"已加载 {len(self._templates)} 个提示词模板")

    def get(self, name: str, **kwargs) -> str:
        template = self._templates.get(name, "")
        if not template:
            return ""
        try:
            return template.format(**kwargs)
        except KeyError as e:
            log.warning(f"提示词 '{name}' 缺少变量: {e}")
            return template


prompts = PromptManager()
