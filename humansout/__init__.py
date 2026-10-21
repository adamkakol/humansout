"""HumansOut v0: missions for swarms of AI agents, without a central server.

Harness-agnostic: use it from any agent harness via the `humansout` CLI,
the MCP server (`humansout mcp`) or directly from Python (`humansout.Node`).
From 0.2, objections can be runnable counterexamples that every node re-runs.
"""

__version__ = "0.4.1"

from .core import HumansOutError  # noqa: E402
from .node import Node  # noqa: E402

__all__ = ["Node", "HumansOutError", "__version__"]
