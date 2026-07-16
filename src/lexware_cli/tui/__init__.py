"""Interactive Textual TUI for lexware-cli.

A second, read-only frontend that talks exclusively to ``lexware_cli.core`` —
it never imports CLI/Typer code. Built on Textual (which builds on Rich, already
a dependency) for the event loop, widgets, keybinding display and robust
terminal handling (alternate screen + restoration on exit and on crash).
"""
