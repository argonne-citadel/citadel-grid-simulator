"""Configuration settings for the Grid Simulator.

This module provides centralized configuration management with support for
local overrides via settingslocal.py.
"""

# Simulation Settings
TIMESTEP_SECONDS = 0.1  # 100ms timestep for real-time simulation

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
