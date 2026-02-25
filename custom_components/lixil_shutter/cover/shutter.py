"""
LIXIL Shutter cover entity.

Implements Cover platform for LIXIL MyWindow Bluetooth shutter.
Controls: open / close / stop / memory position / ventilation position.

State management:
- Connected via BLE (managed by async_added_to_hass / async_will_remove_from_hass)
- Status received through GATT notifications pushed by the device
- Polling fallback via HA's scan interval using async_update()
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from custom_components.lixil_shutter.api import LixilShutterBleClient, LixilShutterBleClientCommunicationError
from custom_components.lixil_shutter.const import (
    CONF_ADDRESS,
    CONF_PRODUCTION_INFO,
    DOMAIN,
    LOGGER,
    PRODUCTION_INFO,
    STATUS_CLOSED,
    STATUS_MIN,
    STATUS_OPEN,
)
from homeassistant.components.cover import CoverDeviceClass, CoverEntity, CoverEntityFeature
from homeassistant.core import callback

if TYPE_CHECKING:
    from custom_components.lixil_shutter.data import LixilShutterConfigEntry
    from homeassistant.helpers.device_registry import DeviceInfo

SCAN_INTERVAL = timedelta(seconds=30)

# ProductionInfo IDs that support the ventilation (saifu) position command.
# type=0 (DecorativeWindow) or type=1 (ShutterEaris).
_VENTILATION_TYPES: frozenset[int] = frozenset({0, 1})

# Cover state constants (opaque internal state strings)
_STATE_OPEN = "open"
_STATE_CLOSED = "closed"
_STATE_OPENING = "opening"
_STATE_CLOSING = "closing"


class LixilShutterCover(CoverEntity):
    """
    Cover entity for LIXIL MyWindow Bluetooth shutter.

    Manages its own BLE connection using the BleClient.
    State is updated immediately during commands (optimistic) and
    confirmed via device notification callbacks.

    Supported features:
    - OPEN / CLOSE / STOP (all types)
    - OPEN_TILT: ventilation (saifu) position — ventilation-type only (ProductionInfo type=0 or 1)
    """

    _attr_device_class = CoverDeviceClass.SHUTTER
    _attr_has_entity_name = True
    _attr_name = None  # use device name as entity name

    _BASE_FEATURES = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP

    def __init__(self, entry: LixilShutterConfigEntry) -> None:
        """
        Initialize the cover entity.

        Args:
            entry: The config entry containing BLE address and device info.
        """
        self._entry = entry
        self._address: str = entry.data[CONF_ADDRESS]
        self._production_info_id: int = entry.data.get(CONF_PRODUCTION_INFO, 0)
        self._client: LixilShutterBleClient = entry.runtime_data.client
        self._state: str | None = None  # None = unavailable

        # HA entity attributes
        self._attr_unique_id = f"{entry.entry_id}_cover"
        self._attr_device_info = self._build_device_info()
        # Enable OPEN_TILT (ventilation position) only for ventilation-type shutters (type=0,1).
        if self._production_info_id in _VENTILATION_TYPES:
            self._attr_supported_features = self._BASE_FEATURES | CoverEntityFeature.OPEN_TILT
        else:
            self._attr_supported_features = self._BASE_FEATURES

    # ------------------------------------------------------------------
    # HA lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Connect to the shutter when entity is added to HA."""
        await self._async_connect()

    async def async_will_remove_from_hass(self) -> None:
        """Disconnect from the shutter when entity is removed."""
        await self._client.disconnect()

    async def _async_connect(self) -> None:
        """Establish BLE connection and register notification callback."""
        try:
            await self._client.connect(status_callback=self._on_status_notification)
            LOGGER.debug("Shutter %s connected", self._address)
        except LixilShutterBleClientCommunicationError as exc:
            LOGGER.warning("Could not connect to shutter %s: %s", self._address, exc)
            self._state = None
            self.async_write_ha_state()

    @callback
    def _on_status_notification(self, status: str) -> None:
        """
        Handle status notification pushed by the device.

        Called from BLE notification thread — must schedule HA state update
        using async_write_ha_state() via call_soon_threadsafe.

        Args:
            status: STATUS_OPEN / STATUS_CLOSED / STATUS_MIN / STATUS_UNKNOWN.
        """
        new_state = self._map_device_status(status)
        if new_state != self._state:
            self._state = new_state
            self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Cover entity properties
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """Return True when BLE connection is active."""
        return self._client.is_connected

    @property
    def is_closed(self) -> bool | None:
        """Return True if the shutter is fully closed."""
        if self._state == _STATE_CLOSED:
            return True
        if self._state == _STATE_OPEN:
            return False
        return None

    @property
    def is_opening(self) -> bool:
        """Return True if the shutter is currently opening."""
        return self._state == _STATE_OPENING

    @property
    def is_closing(self) -> bool:
        """Return True if the shutter is currently closing."""
        return self._state == _STATE_CLOSING

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose additional diagnostic attributes."""
        return {
            "ble_address": self._address,
            "product_type": PRODUCTION_INFO.get(self._production_info_id, "Unknown"),
        }

    # ------------------------------------------------------------------
    # Cover commands
    # ------------------------------------------------------------------

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the shutter (CMD_OPEN_PRESS 01030000)."""
        self._state = _STATE_OPENING
        self.async_write_ha_state()
        try:
            await self._client.open()
        except LixilShutterBleClientCommunicationError as exc:
            LOGGER.error("Failed to open shutter %s: %s", self._address, exc)
            await self._handle_ble_error()

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the shutter (CMD_CLOSE_PRESS 01040000)."""
        self._state = _STATE_CLOSING
        self.async_write_ha_state()
        try:
            await self._client.close()
        except LixilShutterBleClientCommunicationError as exc:
            LOGGER.error("Failed to close shutter %s: %s", self._address, exc)
            await self._handle_ble_error()

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the shutter (CMD_STOP_PRESS 01050000)."""
        try:
            await self._client.stop()
        except LixilShutterBleClientCommunicationError as exc:
            LOGGER.error("Failed to stop shutter %s: %s", self._address, exc)
            await self._handle_ble_error()

    async def async_open_cover_tilt(self, **kwargs: Any) -> None:
        """
        Move to ventilation (saifu) position (CMD_SAIFU_PRESS 01060100).

        Only supported on ventilation-type shutters (ProductionInfo type=0 or 1).
        """
        try:
            await self._client.ventilation_position()
        except LixilShutterBleClientCommunicationError as exc:
            LOGGER.error("Failed to set ventilation position on %s: %s", self._address, exc)
            await self._handle_ble_error()

    async def async_update(self) -> None:
        """
        Poll device status (fallback for when notifications are missed).

        Called by HA at SCAN_INTERVAL. Sends CMD_STATUS_REQUEST;
        response arrives asynchronously via _on_status_notification().
        """
        if not self._client.is_connected:
            await self._async_connect()
            return
        try:
            await self._client.request_status()
        except LixilShutterBleClientCommunicationError as exc:
            LOGGER.warning("Status poll failed for %s: %s", self._address, exc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _handle_ble_error(self) -> None:
        """Mark entity unavailable and attempt reconnection on BLE error."""
        self._state = None
        self.async_write_ha_state()
        await self._async_connect()

    @staticmethod
    def _map_device_status(status: str) -> str | None:
        """
        Map device status string to internal state.

        Args:
            status: One of STATUS_OPEN, STATUS_CLOSED, STATUS_MIN, STATUS_UNKNOWN.

        Returns:
            Internal state string or None if unknown.
        """
        if status == STATUS_OPEN:
            return _STATE_OPEN
        if status in (STATUS_CLOSED, STATUS_MIN):
            return _STATE_CLOSED
        return None

    def _build_device_info(self) -> DeviceInfo:
        """Build HA DeviceInfo from config entry data."""
        from homeassistant.helpers.device_registry import DeviceInfo  # noqa: PLC0415

        model_name = PRODUCTION_INFO.get(self._production_info_id, "MyWindow")
        return DeviceInfo(
            identifiers={(DOMAIN, self._address)},
            name=self._entry.title,
            manufacturer="LIXIL",
            model=model_name,
        )
