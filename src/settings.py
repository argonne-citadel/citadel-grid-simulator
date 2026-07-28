"""Configuration settings for the Grid Simulator.

This module provides centralized configuration management with support for
local overrides via settingslocal.py.
"""

import os

# Simulation Settings
# Default 1.0s is safe for all models (IEEE 8500 step takes ~0.5s).
# Override via TIMESTEP_SECONDS env var for faster simulation on small models.
TIMESTEP_SECONDS = float(os.environ.get("TIMESTEP_SECONDS", "1.0"))

# Network Settings
DEFAULT_NETWORK_CLASS = 1
DEFAULT_TRANSFORMERS = 1

# Protocol Settings
DEFAULT_DNP3_PORT = 20000
DEFAULT_MODBUS_PORT = 502

# Logging Settings
LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

# Try to import local settings overrides
try:
    from .settingslocal import *  # noqa: F401, F403
except ImportError:
    # No local settings file, use defaults
    pass
