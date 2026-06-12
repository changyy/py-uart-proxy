"""
Single source of truth for the package version.

Version scheme: ``1.YYYYmmdd.1HHmmss`` (see CHANGELOG.md / ROADMAP.md).

This is the ONE place the version is defined. It is consumed by:
  * the runtime / UI            -> ``uart_proxy.__version__``
  * the build backend (pyproject.toml ``[tool.hatch.version]``)
so the toml and the code can never drift apart.
"""

__version__ = "1.20260612.1215230"
