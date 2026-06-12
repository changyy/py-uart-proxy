"""
uart-proxy: a cross-platform UART log reader / controller.

Features:
    * Enumerate serial ports and open them for read & write.
    * Dual time axis: absolute wall-clock window and relative elapsed time.
    * Multi-stream file recording (raw / relative-timestamp / full-timestamp).
    * Socket proxy so another machine (or a future mobile client) can attach
      with an auth code and a role (full / readonly).
    * Plugin system for line-by-line pattern matching (grep-style and beyond).
    * Command-line entry point with an optional Textual TUI.

The serial engine is provided by the ``uart_helper`` package
(PyPI: `uart-helper <https://pypi.org/project/uart-helper/>`_).
"""

from __future__ import annotations

# Version is defined once in _version.py and shared with the build backend
# (pyproject.toml [tool.hatch.version]). See _version.py.
from ._version import __version__

__all__ = ["__version__"]
