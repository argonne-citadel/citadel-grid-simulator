"""
Grid Simulator Package.

This package provides a power system grid simulator with support for
multiple modeling engines, SCADA protocols, and cybersecurity integration.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("citadel-simulator")
except PackageNotFoundError:
    # Not pip-installed - running directly from a source checkout (e.g. dev/test
    # environments, which don't install this package into site-packages).
    __version__ = "0.0.0.dev0"
