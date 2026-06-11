"""
mmag — Mattermost AI Agent
"""

__version__ = "0.1.0"

from .agent import Agent
from .config import Config, config

__all__ = ["Agent", "Config", "config", "__version__"]
