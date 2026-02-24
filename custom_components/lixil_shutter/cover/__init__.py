"""Cover platform for LIXIL Bluetooth Shutter."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .shutter import LixilShutterCover

if TYPE_CHECKING:
    from custom_components.lixil_shutter.data import LixilShutterConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LixilShutterConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up LIXIL Shutter cover entity from a config entry."""
    async_add_entities([LixilShutterCover(entry)])
