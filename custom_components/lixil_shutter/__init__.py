"""
LIXIL Bluetooth Shutter Home Assistant integration.

Controls LIXIL MyWindow series shutters/blinds via Bluetooth LE.
Device discovery uses HA's built-in bluetooth scanner with the
integration's SERVICE_UUID declared in manifest.json.

For more details:
https://github.com/nogic1008/ha_lixil_shutter
https://developers.home-assistant.io/docs/creating_integration_manifest
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import Platform
import homeassistant.helpers.config_validation as cv

from .api import LixilShutterBleClient
from .const import CONF_ADDRESS, DOMAIN, LOGGER
from .data import LixilShutterData

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data import LixilShutterConfigEntry

PLATFORMS: list[Platform] = [Platform.COVER]

# This integration is configured via config entries only
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the integration (no services to register for this BLE integration)."""
    return True


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LixilShutterConfigEntry,
) -> bool:
    """
    Set up LIXIL Shutter integration from a config entry.

    1. Resolves the BLE device via HA bluetooth scanner
    2. Creates the BLE client
    3. Forwards setup to Cover platform

    Args:
        hass: The Home Assistant instance.
        entry: The config entry with BLE address in entry.data[CONF_ADDRESS].

    Returns:
        True if setup was successful.
    """
    address: str = entry.data[CONF_ADDRESS]

    # Resolve BLE device from HA bluetooth scanner cache
    from homeassistant.components.bluetooth import async_ble_device_from_address  # noqa: PLC0415

    ble_device = async_ble_device_from_address(hass, address, connectable=True)
    if ble_device is None:
        from homeassistant.exceptions import ConfigEntryNotReady  # noqa: PLC0415

        msg = f"BLE device {address} not found — is it powered on and in range?"
        raise ConfigEntryNotReady(msg)

    # Create BLE client and store as runtime data
    entry.runtime_data = LixilShutterData(
        client=LixilShutterBleClient(ble_device),
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    LOGGER.debug("LIXIL Shutter %s set up successfully", address)
    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: LixilShutterConfigEntry,
) -> bool:
    """
    Unload a config entry.

    Platforms will call async_will_remove_from_hass on their entities,
    which triggers BLE disconnect.

    Args:
        hass: The Home Assistant instance.
        entry: The config entry being unloaded.

    Returns:
        True if unload was successful.
    """
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
