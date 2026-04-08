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

from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_register_callback,
)
from homeassistant.const import Platform
import homeassistant.helpers.config_validation as cv

from .api import LixilShutterBleClient
from .const import CONF_ADDRESS, CONF_PRODUCTION_INFO, DOMAIN, LOGGER
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
    """Set up LIXIL Shutter from a config entry."""
    address: str = entry.data[CONF_ADDRESS]

    # Resolve BLE device from HA bluetooth scanner cache
    ble_device = async_ble_device_from_address(hass, address, connectable=True)
    if ble_device is None:
        from homeassistant.exceptions import ConfigEntryNotReady  # noqa: PLC0415

        msg = f"BLE device {address} not found — is it powered on and in range?"
        raise ConfigEntryNotReady(msg)

    # Create BLE client and store as runtime data
    client = LixilShutterBleClient(ble_device, production_info_id=entry.data.get(CONF_PRODUCTION_INFO, 0))
    entry.runtime_data = LixilShutterData(client=client)

    # Keep BLEDevice updated whenever the HA scanner sees a new advertisement.
    # This is essential for Bluetooth Proxy support: if the device is first seen
    # by one proxy and later by another (or by the local adapter), the client
    # must use the freshest BLEDevice so establish_connection routes correctly.
    def _on_bluetooth_advertisement(
        service_info: BluetoothServiceInfoBleak,
        _change: BluetoothChange,
    ) -> None:
        updated = async_ble_device_from_address(hass, address, connectable=True)
        if updated is not None:
            client.update_ble_device(updated)
            LOGGER.debug("BLEDevice for %s updated (source: %s)", address, service_info.source)

    entry.async_on_unload(
        async_register_callback(
            hass,
            _on_bluetooth_advertisement,
            BluetoothCallbackMatcher(address=address, connectable=True),
            BluetoothScanningMode.PASSIVE,
        )
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    LOGGER.debug("LIXIL Shutter %s set up successfully", address)
    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: LixilShutterConfigEntry,
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
