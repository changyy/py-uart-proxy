"""Plugin system: line-by-line pattern watchers and actions."""

from __future__ import annotations

from .base import Plugin, PluginContext
from .manager import PluginManager

__all__ = ["Plugin", "PluginContext", "PluginManager"]
