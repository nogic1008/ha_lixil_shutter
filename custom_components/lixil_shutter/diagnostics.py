"""Diagnostics support for lixil_shutter.

Learn more about diagnostics:
https://developers.home-assistant.io/docs/core/integration_diagnostics
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.redact import async_redact_data

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data import LixilShutterConfigEntry

# Fields to redact from diagnostics
TO_REDACT: set[str] = set()


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: LixilShutterConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    client = entry.runtime_data.client

    device_reg = dr.async_get(hass)
    entity_reg = er.async_get(hass)

    devices = dr.async_entries_for_config_entry(device_reg, entry.entry_id)
    device_info = []
    for device in devices:
        entities = er.async_entries_for_device(entity_reg, device.id)
        device_info.append(
            {
                "id": device.id,
                "name": device.name,
                "manufacturer": device.manufacturer,
                "model": device.model,
                "entity_count": len(entities),
                "entities": [
                    {
                        "entity_id": entity.entity_id,
                        "platform": entity.platform,
                        "original_name": entity.original_name,
                        "disabled": entity.disabled,
                    }
                    for entity in entities
                ],
            }
        )

    ble_info = {
        "address": client.address,
        "is_connected": client.is_connected,
    }

    return async_redact_data(
        {
            "entry_id": entry.entry_id,
            "entry_title": entry.title,
            "entry_data": dict(entry.data),
            "ble_client": ble_info,
            "devices": device_info,
        },
        TO_REDACT,
    )
