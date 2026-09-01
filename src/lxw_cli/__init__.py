"""lxw-cli — die Version kommt aus den Paket-Metadaten (pyproject.toml)."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("lxw-cli")
except PackageNotFoundError:  # uninstalliert, z.B. direkt aus dem Quellbaum
    __version__ = "0.0.0.dev0"

# Zeitpunkt dieses Builds. Wird beim Versionsbump mitgepflegt und macht
# unterscheidbar, welcher Stand tatsächlich läuft — die Versionsnummer allein
# sagt das nicht, wenn zwischen zwei Releases neu deployt wurde. Ein per
# Deploy gesetztes LXW_MCP_BUILD überschreibt den Wert zur Laufzeit.
__build__ = "2026-09-01 13:11 CEST"
