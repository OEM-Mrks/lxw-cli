"""UI-agnostic core layer for lxw-cli.

Holds everything that knows about the Lexware Office API but nothing about how
results are presented — no Typer, no Rich, no print. Both frontends (the CLI
and the MCP server, and the upcoming TUI) build on this package.

Import the submodules directly (e.g. ``from lxw_cli.core.client import
LexwareClient``); this package intentionally does no eager re-exports to avoid
an import cycle with ``lxw_cli.config``.
"""
