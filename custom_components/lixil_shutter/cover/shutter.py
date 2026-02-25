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

from collections.abc import Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from custom_components.lixil_shutter.api import LixilShutterBleClient, LixilShutterBleClientCommunicationError
from custom_components.lixil_shutter.const import (
    CONF_ADDRESS,
    CONF_COMMAND_MONITOR,
    CONF_POLL_INTERVAL,
    CONF_PRODUCTION_INFO,
    DEFAULT_COMMAND_MONITOR,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    LOGGER,
    PRODUCTION_INFO,
    STATUS_CLOSED,
    STATUS_MIN,
    STATUS_OPEN,
)
from homeassistant.components.cover import CoverDeviceClass, CoverEntity, CoverEntityFeature
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval

if TYPE_CHECKING:
    from custom_components.lixil_shutter.data import LixilShutterConfigEntry

# Cover state constants (opaque internal state strings)
_STATE_OPEN = "open"
_STATE_CLOSED = "closed"
_STATE_OPENING = "opening"
_STATE_CLOSING = "closing"


class LixilShutterCover(CoverEntity):
    """
    Cover entity for LIXIL MyWindow Bluetooth shutter.

    BLE connection is managed on-demand: the client connects when a command
    or status poll is issued and automatically disconnects after an idle
    period (configurable via ``CONF_COMMAND_MONITOR`` / ``CONF_POLL_INTERVAL``).
    State is updated optimistically on commands and confirmed via GATT
    notification callbacks pushed by the device.

    Supported features:
    - OPEN / CLOSE / STOP (all product types)
    - OPEN_TILT: open the flap slats (採風) (all types except type=0 / type=1)
    - CLOSE_TILT: close the flap slats by sending the CLOSE command (same types)
    """

    _attr_device_class = CoverDeviceClass.SHUTTER
    _attr_has_entity_name = True
    _attr_name = None  # use device name as entity name
    _attr_should_poll: bool = False  # disable HA built-in polling; we use async_track_time_interval

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
        self._reachable: bool = False  # True after first successful connection
        self._unsub_poll: Callable[[], None] | None = None

        # HA entity attributes
        self._attr_unique_id = f"{entry.entry_id}_cover"
        self._attr_device_info = self._build_device_info()
        # Enable OPEN_TILT / CLOSE_TILT for ventilation types (all except type=0 / type=1).
        if self._client.has_ventilation:
            self._attr_supported_features = (
                self._BASE_FEATURES | CoverEntityFeature.OPEN_TILT | CoverEntityFeature.CLOSE_TILT
            )
        else:
            self._attr_supported_features = self._BASE_FEATURES

    # ------------------------------------------------------------------
    # HA lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Set up entity after HA registration.

        - Registers the GATT notification callback on the shared BLE client.
        - Starts the configurable periodic status-poll timer.
        - Registers an options-update listener to react to interval changes.
        - Performs an initial status poll so the entity state is populated
          immediately without waiting for the first timer tick.
        """
        self._client.set_status_callback(self._on_status_notification)
        self._schedule_poll()
        # Re-schedule the poll timer whenever the user saves new options.
        self._entry.async_on_unload(self._entry.add_update_listener(self._on_options_updated))
        await self.async_update()

    async def async_will_remove_from_hass(self) -> None:
        """Clean up resources when the entity is removed from HA.

        Cancels the periodic poll timer, clears the GATT notification
        callback, and explicitly disconnects the BLE link so the device
        is immediately available to other clients (e.g. the Android app).
        """
        if self._unsub_poll is not None:
            self._unsub_poll()
            self._unsub_poll = None
        self._client.set_status_callback(None)
        await self._client.disconnect()

    @callback
    def _on_status_notification(self, status: str) -> None:
        """
        Handle a status notification pushed by the device over GATT.

        Decorated with ``@callback`` — runs on the HA event loop.
        Updates internal state and writes the new state to HA only when
        the status actually changes, to avoid redundant state writes.

        Args:
            status: One of STATUS_OPEN, STATUS_CLOSED, STATUS_MIN, STATUS_UNKNOWN.
        """
        new_state = self._map_device_status(status)
        if new_state != self._state:
            self._state = new_state
            self._reachable = True
            self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Cover entity properties
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """Return True when at least one successful BLE operation has completed."""
        return self._reachable

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
        """Open the shutter fully (CMD_OPEN_PRESS keyCode=0x03).

        Sets state optimistically to *opening*, sends the BLE command, and
        keeps the BLE connection alive for ``_monitor_sec`` seconds so the
        device's completion notification can be received.
        """
        self._state = _STATE_OPENING
        self.async_write_ha_state()
        try:
            await self._client.open(idle_after=self._monitor_sec)
        except LixilShutterBleClientCommunicationError as exc:
            LOGGER.error("Failed to open shutter %s: %s", self._address, exc)
            self._reachable = False
            self._state = None
            self.async_write_ha_state()
        else:
            self._reachable = True

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the shutter fully (CMD_CLOSE_PRESS keyCode=0x04).

        Sets state optimistically to *closing*, sends the BLE command, and
        keeps the BLE connection alive for ``_monitor_sec`` seconds so the
        device's completion notification can be received.

        On ventilation-type shutters (``has_ventilation=True``), this command
        also closes the flap slats as part of the full-close
        motion.  ``async_close_cover_tilt`` uses this same command.
        """
        self._state = _STATE_CLOSING
        self.async_write_ha_state()
        try:
            await self._client.close(idle_after=self._monitor_sec)
        except LixilShutterBleClientCommunicationError as exc:
            LOGGER.error("Failed to close shutter %s: %s", self._address, exc)
            self._reachable = False
            self._state = None
            self.async_write_ha_state()
        else:
            self._reachable = True

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the shutter mid-travel (CMD_STOP_PRESS keyCode=0x05)."""
        try:
            await self._client.stop(idle_after=self._monitor_sec)
        except LixilShutterBleClientCommunicationError as exc:
            LOGGER.error("Failed to stop shutter %s: %s", self._address, exc)
            self._reachable = False
            self._state = None
            self.async_write_ha_state()
        else:
            self._reachable = True

    async def async_open_cover_tilt(self, **kwargs: Any) -> None:
        """Open the flap slats to allow ventilation (採風).

        Available on all types except type=0 (DecorativeWindow) and type=1
        (ShutterEaris).  ``CoverEntityFeature.OPEN_TILT`` is enabled for these
        devices only.
        """
        try:
            await self._client.open_flap_slats(idle_after=self._monitor_sec)
        except LixilShutterBleClientCommunicationError as exc:
            LOGGER.error("Failed to open flap slats on %s: %s", self._address, exc)
            self._reachable = False
            self._state = None
            self.async_write_ha_state()
        else:
            self._reachable = True

    async def async_close_cover_tilt(self, **kwargs: Any) -> None:
        """Close the flap slats by issuing a full-close command.

        On ventilation-type shutters the close command causes the flap slats
        to close as part of the shutter's full-close motion.  Uses the same
        BLE command as ``async_close_cover``.
        """
        self._state = _STATE_CLOSING
        self.async_write_ha_state()
        try:
            await self._client.close(idle_after=self._monitor_sec)
        except LixilShutterBleClientCommunicationError as exc:
            LOGGER.error("Failed to close flap slats on %s: %s", self._address, exc)
            self._reachable = False
            self._state = None
            self.async_write_ha_state()
        else:
            self._reachable = True

    async def async_update(self) -> None:
        """Request the current shutter status from the device.

        The client connects on demand if not already connected, sends a
        STATUS_REQUEST command (keyCode=0x0B), and the device responds with
        a GATT notification handled by ``_on_status_notification``.  The
        client's idle-disconnect timer releases the BLE link shortly after
        the notification arrives, allowing other BLE clients (e.g. the
        Android app) to connect in between polls.
        """
        try:
            await self._client.request_status()
        except LixilShutterBleClientCommunicationError as exc:
            LOGGER.warning("Status poll failed for %s: %s", self._address, exc)
            self._reachable = False
            self._state = None
        else:
            self._reachable = True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def _monitor_sec(self) -> float:
        """BLE connection hold time (seconds) after an open/close/stop command.

        Read from ``entry.options`` at call time so option changes take
        effect on the next command without restarting HA.
        Defaults to ``DEFAULT_COMMAND_MONITOR`` if not yet configured.
        """
        return float(self._entry.options.get(CONF_COMMAND_MONITOR, DEFAULT_COMMAND_MONITOR))

    def _schedule_poll(self) -> None:
        """(Re)start the periodic status-poll timer using the current option value.

        Cancels the previous ``async_track_time_interval`` subscription before
        registering a new one, so calling this multiple times is safe.  The
        interval is read from ``entry.options`` each time, so it reflects the
        latest value saved by the user via the options flow.
        """
        if self._unsub_poll is not None:
            self._unsub_poll()
        poll_sec = int(self._entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL))
        self._unsub_poll = async_track_time_interval(self.hass, self._async_poll, timedelta(seconds=poll_sec))
        LOGGER.debug("Scheduled status poll every %ds for %s", poll_sec, self._address)

    async def _async_poll(self, _now: Any) -> None:
        """Periodic poll callback registered by ``_schedule_poll``.

        Invoked by ``async_track_time_interval``; delegates to ``async_update``.
        """
        await self.async_update()

    async def _on_options_updated(self, _hass: Any, _entry: Any) -> None:
        """Options-update listener — re-schedules the poll timer on option save.

        Registered via ``entry.add_update_listener`` in ``async_added_to_hass``.
        Called by HA after the user confirms new values in the options flow.
        """
        self._schedule_poll()

    @staticmethod
    def _map_device_status(status: str) -> str | None:
        """Map a device status string from the GATT notification to an internal state.

        Args:
            status: Parsed status constant from the notification
                (STATUS_OPEN, STATUS_CLOSED, STATUS_MIN, or STATUS_UNKNOWN).

        Returns:
            Internal state string (_STATE_OPEN / _STATE_CLOSED) or
            None when the status cannot be mapped (treated as unavailable).
        """
        if status == STATUS_OPEN:
            return _STATE_OPEN
        if status in (STATUS_CLOSED, STATUS_MIN):
            return _STATE_CLOSED
        return None

    def _build_device_info(self) -> DeviceInfo:
        """Build the HA ``DeviceInfo`` dict from config entry data.

        Uses the BLE address as the unique device identifier so the entity
        remains linked to the same physical device even if the entry is
        re-created.
        """

        model_name = PRODUCTION_INFO.get(self._production_info_id, "MyWindow")
        return DeviceInfo(
            identifiers={(DOMAIN, self._address)},
            name=self._entry.title,
            manufacturer="LIXIL",
            model=model_name,
        )
