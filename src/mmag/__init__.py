"""
mmag — Mattermost AI Agent
"""

__version__ = "0.1.0"

from .application import Agent
from .config import Config, config

__all__ = ["Agent", "Config", "config", "__version__"]
