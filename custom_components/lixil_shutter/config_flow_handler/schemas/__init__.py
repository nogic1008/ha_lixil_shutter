"""
Data schemas for config flow forms.

This package contains all voluptuous schemas used in config flows and options flows.

Package structure:
-----------------
- options.py: Options flow schemas

All schemas are re-exported from this __init__.py for convenient imports.
"""

from __future__ import annotations

from custom_components.lixil_shutter.config_flow_handler.schemas.options import get_options_schema

__all__ = ["get_options_schema"]
