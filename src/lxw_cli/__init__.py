"""lxw-cli — die Version kommt aus den Paket-Metadaten (pyproject.toml)."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("lxw-cli")
except PackageNotFoundError:  # uninstalliert, z.B. direkt aus dem Quellbaum
    __version__ = "0.0.0.dev0"
