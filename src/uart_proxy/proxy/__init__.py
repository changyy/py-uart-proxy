"""Socket proxy: expose a session to remote clients with auth + roles."""

from __future__ import annotations

from .protocol import Role, parse_auth_spec
from .server import ProxyServer

__all__ = ["ProxyServer", "Role", "parse_auth_spec"]
