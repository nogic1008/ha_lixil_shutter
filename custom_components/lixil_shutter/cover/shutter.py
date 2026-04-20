"""
LIXIL Shutter cover entity.

Implements Cover platform for LIXIL MyWindow Bluetooth shutter.
Controls: open / close / stop / tilt.

State management:
- Connected via BLE (managed by async_added_to_hass / async_will_remove_from_hass)
- Status received through GATT notifications pushed by the device
- Polling fallback via HA's scan interval using async_update()
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
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
    STATUS_OPEN,
    STATUS_VENTILATION,
)
from homeassistant.components.cover import CoverDeviceClass, CoverEntity, CoverEntityFeature, CoverState
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_call_later, async_track_time_interval

if TYPE_CHECKING:
    from custom_components.lixil_shutter.data import LixilShutterConfigEntry

# Mapping from device STATUS_* constants to HA CoverState.
# STATUS_OPEN is intentionally excluded: it is returned for every non-fully-closed
# position (fully open, halfway, stopped mid-travel) so it cannot be reliably mapped
# to CoverState.OPEN.  STATUS_OPEN is handled explicitly in _on_status_notification.
# STATUS_UNKNOWN and any unrecognised value map to None (unavailable).
_STATUS_TO_COVER_STATE: dict[str, CoverState] = {
    STATUS_CLOSED: CoverState.CLOSED,
    STATUS_VENTILATION: CoverState.CLOSED,
}

# Tilt position (0–100) for each device status.
# STATUS_OPEN has no defined tilt (shutter fully retracted).
# STATUS_CLOSED: slats fully closed. STATUS_VENTILATION: slats fully open.
_STATUS_TO_TILT_POSITION: dict[str, int] = {
    STATUS_CLOSED: 0,
    STATUS_VENTILATION: 100,
}


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
    _attr_assumed_state: bool = True  # position is not precisely known; allow both open/close from any state

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
        self._unsub_poll: Callable[[], None] | None = None

        # HA entity attributes
        self._attr_is_closed: bool | None = None  # None = state unknown / unavailable
        self._attr_current_cover_tilt_position: int | None = None  # 0 = closed, 100 = ventilation
        self._attr_available: bool = False  # True after first successful BLE operation
        self._attr_unique_id = f"{entry.entry_id}_cover"
        # Motion window: while active, STATUS_OPEN notifications are suppressed so
        # the UI keeps showing the optimistic opening/closing state until the timer
        # expires or a definitive final status (closed/ventilation) arrives.
        self._motion_state: CoverState | None = None
        self._motion_unsub: Callable[[], None] | None = None
        # Set to True when the OPENING window expires naturally (no stop/new command).
        # The next STATUS_OPEN notification will then be treated as fully open.
        self._after_opening_window: bool = False
        self._attr_device_info = self._build_device_info()
        self._attr_extra_state_attributes: dict[str, Any] = {
            "ble_address": self._address,
            "product_type": PRODUCTION_INFO.get(self._production_info_id, "Unknown"),
        }
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
        is immediately available to other BLE clients.
        """
        self._cancel_motion_state()
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
        Updates ``_attr_is_*`` fields and writes the new state to HA.

        During the motion window (``_motion_state`` is set), ``STATUS_OPEN``
        notifications are suppressed so the UI keeps showing the optimistic
        ``opening`` or ``closing`` state until the window expires.  Any
        definitive final status (``STATUS_CLOSED`` / ``STATUS_VENTILATION``)
        cancels the window immediately and applies the real device state.

        ``STATUS_OPEN`` outside the motion window maps to ``None`` (unknown)
        **except** when the OPENING window has just expired naturally, in which
        case the shutter is considered fully open and ``CoverState.OPEN`` is used.

        Args:
            status: One of STATUS_OPEN, STATUS_CLOSED, STATUS_VENTILATION, STATUS_UNKNOWN.
        """
        if self._motion_state is not None:
            if status == STATUS_OPEN:
                # Device not yet at final state; keep displaying the motion state.
                return
            # Definitive final state arrived — cancel the window and apply it.
            self._cancel_motion_state()
        if status == STATUS_OPEN:
            if self._after_opening_window:
                # Open window expired naturally → shutter has been opening for
                # command_monitor seconds without interruption: treat as fully open.
                self._after_opening_window = False
                cover_state: CoverState | None = CoverState.OPEN
            else:
                # Partial or indeterminate position — state is unknown.
                cover_state = None
        else:
            cover_state = _STATUS_TO_COVER_STATE.get(status)
        self._apply_state(cover_state)
        if self._client.has_ventilation:
            self._attr_current_cover_tilt_position = _STATUS_TO_TILT_POSITION.get(status)
        self._attr_available = True
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Cover commands
    # ------------------------------------------------------------------

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the shutter fully (keyCode=0x03).

        The motion window is started *before* the BLE command is sent so that
        any ``STATUS_OPEN`` GATT notifications arriving during the BLE round-trip
        (~1–2 s) are already suppressed.  If the command fails the window is
        cancelled immediately.
        """
        self._cancel_motion_state()
        self._apply_state(CoverState.OPENING)
        self._start_motion(CoverState.OPENING)
        self.async_write_ha_state()
        if not await self._run_command(
            self._client.open(idle_after=self._monitor_sec),
            "Failed to open shutter %s: %s",
        ):
            self._cancel_motion_state()

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the shutter fully (keyCode=0x04).

        On ventilation-type shutters, also closes the flap slats as part of
        the full-close motion.  ``async_close_cover_tilt`` uses this same command.

        The motion window is started *before* the BLE command is sent (same
        rationale as ``async_open_cover``).
        """
        self._cancel_motion_state()
        self._apply_state(CoverState.CLOSING)
        self._start_motion(CoverState.CLOSING)
        self.async_write_ha_state()
        if not await self._run_command(
            self._client.close(idle_after=self._monitor_sec),
            "Failed to close shutter %s: %s",
        ):
            self._cancel_motion_state()

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the shutter mid-travel (keyCode=0x05).

        Cancels any active motion window and immediately sets the state to
        ``None`` (unknown / partial position) *before* sending the BLE command
        (optimistic).  This ensures the open and close buttons are re-enabled
        the moment the user taps stop.  Subsequent device notifications will
        keep the state unknown until the next confirmed open/close cycle.
        """
        self._cancel_motion_state()
        # Set state to unknown (partial position) immediately so both buttons are enabled.
        self._apply_state(None)
        self.async_write_ha_state()
        await self._run_command(
            self._client.stop(idle_after=self._monitor_sec),
            "Failed to stop shutter %s: %s",
        )

    async def async_open_cover_tilt(self, **kwargs: Any) -> None:
        """Open the flap slats to allow ventilation (採風).

        Available on all types except type=0 and type=1.
        ``CoverEntityFeature.OPEN_TILT`` is enabled for these devices only.
        """
        self._cancel_motion_state()
        await self._run_command(
            self._client.open_flap_slats(idle_after=self._monitor_sec),
            "Failed to open flap slats on %s: %s",
        )

    async def async_close_cover_tilt(self, **kwargs: Any) -> None:
        """Close the flap slats by issuing a full-close command.

        The close command causes the flap slats to close as part of the
        shutter's full-close motion.  Uses the same BLE command as
        ``async_close_cover``.
        """
        self._cancel_motion_state()
        await self._run_command(
            self._client.close(idle_after=self._monitor_sec),
            "Failed to close flap slats on %s: %s",
            CoverState.CLOSING,
        )

    async def async_update(self) -> None:
        """Request the current shutter status from the device.

        The client connects on demand if not already connected, sends a
        STATUS_REQUEST command (keyCode=0x0B), and the device responds with
        a GATT notification handled by ``_on_status_notification``.  The
        client's idle-disconnect timer releases the BLE link shortly after
        the notification arrives, allowing other BLE clients to connect
        in between polls.
        """
        try:
            await self._client.request_status()
        except LixilShutterBleClientCommunicationError as exc:
            LOGGER.warning("Status poll failed for %s: %s", self._address, exc)
            self._attr_available = False
            self._apply_state(None)
        else:
            self._attr_available = True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _apply_state(self, state: CoverState | None) -> None:
        """Set ``_attr_is_closed``, ``_attr_is_opening``, ``_attr_is_closing`` from *state*.

        Passing ``None`` clears all state fields (treated as unavailable).
        Tilt position is also cleared when *state* is ``None`` (error / optimistic motion).
        """
        self._attr_is_closed = None if state is None else state == CoverState.CLOSED
        self._attr_is_opening = state == CoverState.OPENING
        self._attr_is_closing = state == CoverState.CLOSING
        if state is None:
            self._attr_current_cover_tilt_position = None

    @callback
    def _cancel_motion_state(self) -> None:
        """Cancel the active motion window if any."""
        if self._motion_unsub is not None:
            self._motion_unsub()
            self._motion_unsub = None
        self._motion_state = None
        self._after_opening_window = False

    @callback
    def _start_motion(self, state: CoverState) -> None:
        """Start the motion window for *state* (OPENING or CLOSING).

        Records the expected motion direction so that STATUS_OPEN notifications
        are suppressed during the window.  Schedules a poll when the window
        expires so the real device state is confirmed.
        """
        self._motion_state = state
        self._motion_unsub = async_call_later(self.hass, self._monitor_sec, self._on_motion_expired)

    @callback
    def _on_motion_expired(self, _now: Any) -> None:
        """Motion window expired; poll the device for its current state.

        If the window was for OPENING, sets ``_after_opening_window`` so that
        the next ``STATUS_OPEN`` notification is interpreted as fully open rather
        than unknown.
        """
        self._motion_unsub = None
        was_opening = self._motion_state == CoverState.OPENING
        self._motion_state = None
        if was_opening:
            self._after_opening_window = True
        self.hass.async_create_task(self.async_update())

    async def _run_command(
        self,
        cmd: Awaitable[None],
        error_msg: str,
        optimistic_state: CoverState | None = None,
    ) -> bool:
        """Run a BLE command with standard error handling.

        Optionally sets an optimistic state before sending the command.
        On failure, logs the error, marks the device unreachable, and clears
        the state.  On success, marks the device as reachable.

        Args:
            cmd: Coroutine to await (BLE client command).
            error_msg: ``LOGGER.error`` template; receives address and exception.
            optimistic_state: State to set before sending (e.g. ``CoverState.OPENING``).

        Returns:
            ``True`` if the command succeeded, ``False`` otherwise.
        """
        if optimistic_state is not None:
            self._apply_state(optimistic_state)
            self.async_write_ha_state()
        try:
            await cmd
        except LixilShutterBleClientCommunicationError as exc:
            LOGGER.error(error_msg, self._address, exc)
            self._attr_available = False
            self._apply_state(None)
            self.async_write_ha_state()
            return False
        else:
            self._attr_available = True
            return True

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

    def _build_device_info(self) -> DeviceInfo:
        """Build the HA ``DeviceInfo`` dict from config entry data.

        Uses the BLE address as the unique device identifier so the entity
        remains linked to the same physical device even if the entry is
        re-created.
        """

        model_name = PRODUCTION_INFO.get(self._production_info_id, "Unknown")
        return DeviceInfo(
            identifiers={(DOMAIN, self._address)},
            name=self._entry.title,
            manufacturer="LIXIL",
            model=model_name,
        )
