"""
Config flow handler package for lixil_shutter.

Package structure:
------------------
- config_flow.py: Main configuration flow (bluetooth discovery, user, confirm, pair)
- options_flow.py: Options flow for post-setup configuration changes
- schemas/: Voluptuous schemas (options)
- validators/: Validation logic for user inputs
"""

from __future__ import annotations

from .config_flow import LixilShutterConfigFlowHandler
from .options_flow import LixilShutterOptionsFlow

__all__ = [
    "LixilShutterConfigFlowHandler",
    "LixilShutterOptionsFlow",
]
