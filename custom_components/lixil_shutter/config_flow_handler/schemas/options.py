"""Options flow schemas for the BLE shutter integration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from custom_components.lixil_shutter.const import (
    CONF_COMMAND_MONITOR,
    CONF_POLL_INTERVAL,
    DEFAULT_COMMAND_MONITOR,
    DEFAULT_POLL_INTERVAL,
)
from homeassistant.helpers import selector


def get_options_schema(defaults: Mapping[str, Any] | None = None) -> vol.Schema:
    """Return options schema with poll_interval and command_monitor number inputs.

    Both options are stored as int (NumberSelector produces int/float) and
    cast to int when used in the cover entity.
    """
    d = defaults or {}
    poll_default = int(d.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL))
    monitor_default = int(d.get(CONF_COMMAND_MONITOR, DEFAULT_COMMAND_MONITOR))
    return vol.Schema(
        {
            vol.Optional(CONF_POLL_INTERVAL, default=poll_default): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=10,
                    max=3600,
                    step=1,
                    unit_of_measurement="seconds",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(CONF_COMMAND_MONITOR, default=monitor_default): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=5,
                    max=300,
                    step=1,
                    unit_of_measurement="seconds",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
        }
    )


__all__ = ["get_options_schema"]
