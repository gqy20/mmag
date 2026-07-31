"""
提示词与行为配置管理

prompts.yml 是 LLM 提示词和 Bot 行为配置的单一入口:
  - 字符串值节点: LLM 模板,PromptManager.get() 渲染后注入 system prompt
  - 其他结构节点: Bot 行为配置,PromptManager.get_section() 读取
"""

import os
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

import yaml

from .logger import get_logger

log = get_logger(__name__)

_REPOSITORY_PROMPTS = Path(__file__).resolve().parents[2] / "prompts.yml"


def _default_prompt_path() -> Path | Traversable:
    """Resolve an explicit override, installed package resource, or source file."""
    override = os.getenv("PROMPTS_PATH", "").strip()
    if override:
        return Path(override).expanduser()

    packaged = resources.files("mmag").joinpath("prompts.yml")
    if packaged.is_file():
        return packaged
    return _REPOSITORY_PROMPTS


class PromptManager:
    """从 prompts.yml 加载和渲染配置"""

    def __init__(self, path: Path | Traversable | None = None):
        self.path = path or _default_prompt_path()
        self._raw: dict = {}
        self._load()

    def _load(self):
        if not self.path.is_file():
            log.warning("提示词文件不存在: %s，使用内置默认值", self.path)
            return
        with self.path.open(encoding="utf-8") as f:
            self._raw = yaml.safe_load(f) or {}
        log.info("已加载 %d 个 prompts.yml 节点", len(self._raw))

    def get(self, name: str, **kwargs) -> str:
        """读取字符串模板节点,做 .format(**kwargs) 渲染后返回

        用于 system_prompt 等 LLM 模板。

        占位符策略（与 str.format 一致）:
          - 传入的 kwargs 会被替换
          - 模板里有但 kwargs 没传的占位符，保留为 `{key}` 原样（不回退整个模板）
          - 仅当 kwargs 出现多余 key 时记录 warning（不抛）
        """
        template = self._raw.get(name, "")
        if not isinstance(template, str):
            log.warning("prompts.yml['%s'] 不是字符串,返回空", name)
            return ""
        if not template:
            return ""
        return _safe_format(template, kwargs)

    def get_section(self, name: str, **kwargs) -> dict:
        """读取非模板结构节点(dict),递归对其中所有字符串做 .format(**kwargs) 替换

        用于 triggers 等 Bot 行为配置。kwargs 传空时不替换。
        返回深拷贝(防止调用方修改污染全局)。
        """
        section = self._raw.get(name)
        if not isinstance(section, dict):
            return {}
        if not kwargs:
            return dict(section)
        return _format_dict(section, kwargs)


def _format_dict(obj, kwargs: dict):
    """递归对 dict / list / str 中的 str 节点做 .format(**kwargs)

    缺失的占位符保留为 `{key}` 原样，行为与 PromptManager.get() 一致。
    """
    if isinstance(obj, str):
        return _safe_format(obj, kwargs)
    if isinstance(obj, dict):
        return {k: _format_dict(v, kwargs) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_format_dict(v, kwargs) for v in obj]
    return obj


class _SafeDict(dict):
    """format_map 用的兜底 mapping: 缺失的 key 保留为 `{key}` 原样而非抛 KeyError"""

    def __missing__(self, key: str) -> str:  # type: ignore[override]
        return "{" + key + "}"


def _safe_format(template: str, kwargs: dict) -> str:
    """用 _SafeDict 兜底的 .format_map。缺失占位符保留原字面量。"""
    return template.format_map(_SafeDict(kwargs))


prompts = PromptManager()
