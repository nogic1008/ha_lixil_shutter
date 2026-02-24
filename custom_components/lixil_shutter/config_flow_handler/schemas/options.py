"""Options flow schemas (placeholder — no options defined for BLE shutter)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol


def get_options_schema(defaults: Mapping[str, Any] | None = None) -> vol.Schema:
    """Return empty options schema (no configurable options for BLE shutter)."""
    return vol.Schema({})


__all__ = ["get_options_schema"]
