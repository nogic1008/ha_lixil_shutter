"""
Options flow for lixil_shutter.

Allows users to adjust the following settings after initial setup:

- **Poll interval** (``CONF_POLL_INTERVAL``): How often HA requests the current
  shutter state over BLE (default 300 s / 5 minutes).
- **Command monitor window** (``CONF_COMMAND_MONITOR``): How long the BLE
  connection is kept open after an open/close/stop command so that the
  device's completion notification can arrive (default 30 s).

For more information:
https://developers.home-assistant.io/docs/config_entries_options_flow_handler
"""

from __future__ import annotations

from typing import Any

from custom_components.lixil_shutter.config_flow_handler.schemas import get_options_schema
from homeassistant import config_entries


class LixilShutterOptionsFlow(config_entries.OptionsFlow):
    """
    Options flow for the LIXIL shutter integration.

    Presents a single-step form (``async_step_init``) with two numeric inputs:
    - Poll interval: BLE status-poll frequency in seconds.
    - Command monitor window: post-command BLE connection hold time in seconds.

    Changes take effect immediately — the cover entity re-schedules its poll
    timer and updates its ``_monitor_sec`` value without restarting HA.
    """

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Show the options form or save submitted values.

        Entry point for the options flow.  Displays a form pre-filled with
        the current option values.  On submission, persists the new values
        to the config entry; the cover entity's update listener applies them
        without requiring an HA restart.

        Args:
            user_input: Submitted form data, or None to display the form.

        Returns:
            A form result on first call; a completed entry result on submit.
        """
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=get_options_schema(self.config_entry.options),
        )


__all__ = ["LixilShutterOptionsFlow"]
