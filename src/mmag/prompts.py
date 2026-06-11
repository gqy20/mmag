"""
提示词与行为配置管理

prompts.yml 是 LLM 提示词和 Bot 行为配置的单一入口:
  - 字符串值节点: LLM 模板,PromptManager.get() 渲染后注入 system prompt
  - 其他结构节点: Bot 行为配置,PromptManager.get_section() 读取
"""

from pathlib import Path

import yaml

from .logger import get_logger

log = get_logger(__name__)


class PromptManager:
    """从 prompts.yml 加载和渲染配置"""

    def __init__(self, path: Path | None = None):
        self.path = path or (Path(__file__).resolve().parents[2] / "prompts.yml")
        self._raw: dict = {}
        self._load()

    def _load(self):
        if not self.path.exists():
            log.warning("提示词文件不存在: %s，使用内置默认值", self.path)
            return
        with open(self.path, encoding="utf-8") as f:
            self._raw = yaml.safe_load(f) or {}
        log.info("已加载 %d 个 prompts.yml 节点", len(self._raw))

    def get(self, name: str, **kwargs) -> str:
        """读取字符串模板节点,做 .format(**kwargs) 渲染后返回

        用于 system_prompt 等 LLM 模板。
        """
        template = self._raw.get(name, "")
        if not isinstance(template, str):
            log.warning("prompts.yml['%s'] 不是字符串,返回空", name)
            return ""
        if not template:
            return ""
        try:
            return template.format(**kwargs)
        except KeyError as e:
            log.warning("提示词 '%s' 缺少变量: %s", name, e)
            return template

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
    """递归对 dict / list / str 中的 str 节点做 .format(**kwargs)"""
    if isinstance(obj, str):
        try:
            return obj.format(**kwargs)
        except KeyError:
            return obj
    if isinstance(obj, dict):
        return {k: _format_dict(v, kwargs) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_format_dict(v, kwargs) for v in obj]
    return obj


prompts = PromptManager()
