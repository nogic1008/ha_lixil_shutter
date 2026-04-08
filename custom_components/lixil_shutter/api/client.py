"""
BLE GATT client for LIXIL MyWindow shutter.

Manages the full BLE lifecycle for the shutter:
- On-demand connection: ``_ensure_connected`` connects only when a command or
  status poll is issued, avoiding a permanently held BLE link.
- Idle disconnect timer: after each command the client schedules an automatic
  disconnect (``_IDLE_DISCONNECT_SEC`` or the caller-supplied ``idle_after``
  value) so other BLE clients can connect in the gap.
- GATT notifications: status updates pushed by the device are delivered to the
  registered callback without polling.
- Bluetooth Proxy support (ESPHome/ESP32): detected via BLEDevice.details;
  D-Bus operations are skipped and the proxy handles BLE-level SMP bonding.

Underlying BLE library: bleak / bleak-retry-connector (bundled with HA).
For local BlueZ adapters, pairing is handled by ``api._bluez`` using BlueZ
D-Bus Pair() via dbus-fast (transitive HA dependency).  For Bluetooth Proxy
devices, pairing is handled by the proxy's ESP32 chip through bleak's
establish_connection.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from custom_components.lixil_shutter.const import (
    CHAR_UCG_IN_UUID,
    CHAR_UCG_OUT_UUID,
    COMMAND_TIMEOUT_SEC,
    KEY_CODE_CLOSE,
    KEY_CODE_OPEN,
    KEY_CODE_POSITION,
    KEY_CODE_STATUS,
    KEY_CODE_STOP,
    KEY_STATE_PRESS,
    KEY_STATE_RELEASE,
    LOGGER,
    RELEASE_DELAY_SEC,
    STATUS_CLOSED,
    STATUS_OPEN,
    STATUS_UNKNOWN,
    STATUS_VENTILATION,
    SUB_CODE_DEFAULT,
    SUB_CODE_MEMORY,
    SUB_CODE_VENTILATION,
)

from ._bluez import dbus_pair, dbus_stop_notify, is_local_bluez_device
from .exceptions import LixilShutterBleClientCommunicationError, LixilShutterBleClientError

_IDLE_DISCONNECT_SEC: float = 30.0

# ProductionInfo IDs that use SUB_CODE_VENTILATION (0x01) for the memory position command.
# type=0 (DecorativeWindow) and type=1 (ShutterEaris) send 01 06 01 00.
# All other types send 01 06 02 00 (SUB_CODE_MEMORY).
# The same boundary also determines whether the ventilation button is present.
_NORMAL_TYPE_IDS: frozenset[int] = frozenset({0, 1})


class LixilShutterBleClient:
    """
    BLE GATT client for LIXIL MyWindow shutter.

    Manages the full BLE lifecycle:
    - On-demand connection via ``_ensure_connected`` (no manual ``connect()`` needed
      for normal operation)
    - Idle-disconnect timer that releases the BLE link after inactivity
    - GATT notifications on UCG_IN characteristic for device-pushed status updates
    - Commands written to UCG_OUT characteristic using press + release pattern
    - BLE-level pairing via BlueZ D-Bus Pair()

    Command routing is handled entirely within this client based on
    ``production_info_id``.  Callers use the semantic methods (``open``,
    ``close``, ``tilt_position``, etc.) without knowing BLE byte details.

    Typical usage (cover entity):
        client = LixilShutterBleClient(ble_device, production_info_id=1)
        client.set_status_callback(my_callback)  # register once
        await client.open(idle_after=30)          # connects on demand, auto-disconnects
        await client.request_status()             # connect → request → idle disconnect
    """

    def __init__(self, ble_device: BLEDevice, production_info_id: int = 0) -> None:
        """
        Initialize the BLE client.

        Args:
            ble_device: The discovered BLE device from HA bluetooth scanner.
            production_info_id: ProductionInfo type ID from BLE advertisement
                (``bytes[0] & 0x07``).  Determines which commands are available
                and how they are encoded.  Defaults to ``0`` (unknown type).
        """
        self._ble_device = ble_device
        self._production_info_id = production_info_id
        self._client: BleakClient | None = None
        self._tag: int = 0
        self._status_callback: Callable[[str], None] | None = None
        self._connected = False
        self._notify_active = False  # True only when start_notify() was called
        self._idle_task: asyncio.Task[None] | None = None
        self._disconnecting: bool = False  # True only during intentional disconnect

    def update_ble_device(self, ble_device: BLEDevice) -> None:
        """Update the stored BLEDevice with a fresher instance from the HA scanner.

        Called whenever HA's bluetooth component reports a new advertisement for
        this device.  Keeps the client pointing at the best available scanner
        (local adapter or Bluetooth Proxy) so ``establish_connection`` uses
        up-to-date routing information.

        Args:
            ble_device: Latest BLEDevice from ``async_ble_device_from_address``.
        """
        self._ble_device = ble_device

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    @property
    def address(self) -> str:
        """Return BLE MAC address."""
        return self._ble_device.address

    @property
    def is_connected(self) -> bool:
        """Return True if BLE connection is active."""
        return self._connected and self._client is not None and self._client.is_connected

    @property
    def has_ventilation(self) -> bool:
        """Return True if this device has a ventilation (saifu) position button.

        True for all types except type=0 (DecorativeWindow) and type=1 (ShutterEaris).
        Exposed via ``CoverEntityFeature.OPEN_TILT`` in the cover entity.
        """
        return self._production_info_id not in _NORMAL_TYPE_IDS

    @property
    def has_memory_position(self) -> bool:
        """Return True if this device has a memory position button.

        All product types have this button.  The subCode byte sent depends on
        the type — see ``move_to_memory_position()``.
        """
        return True

    async def connect(
        self,
        status_callback: Callable[[str], None] | None = None,
    ) -> None:
        """
        Connect to the shutter and start GATT notifications.

        Establishes BLE connection and enables CCCD notifications (start_notify),
        which completes the GATT handshake so the device enters CONNECTED state.

        Args:
            status_callback: Called with status string on each notification.

        Raises:
            LixilShutterBleClientCommunicationError: On connection failure.
        """
        self._status_callback = status_callback
        LOGGER.debug("[connect] start: _notify_active=%s", self._notify_active)

        # Clean up any existing client.
        if self._client is not None:
            LOGGER.debug(
                "[connect] cleaning up existing client (notify_active=%s)",
                self._notify_active,
            )
            if self._notify_active:
                with suppress(Exception):
                    await self._client.stop_notify(CHAR_UCG_IN_UUID)
                self._notify_active = False
            with suppress(Exception):
                await self._client.disconnect()
            self._client = None
        try:
            LOGGER.debug("[connect] establishing connection to %s", self.address)
            self._client = await establish_connection(
                BleakClientWithServiceCache,
                self._ble_device,
                self._ble_device.address,
                disconnected_callback=self._on_disconnected,
            )
            LOGGER.debug("[connect] connection OK for %s, calling start_notify", self.address)
            try:
                await self._client.start_notify(CHAR_UCG_IN_UUID, self._on_notification)
            except Exception as start_exc:
                exc_str = str(start_exc)
                if "Insufficient authentication" in exc_str:
                    # BLE peripheral is requesting encryption/bonding (error=5).
                    # The correct BLE flow is to initiate SMP pairing on this
                    # active connection (not a separate one) while the device is
                    # already waiting for the security request.  After pair()
                    # the link is encrypted and start_notify will succeed.
                    LOGGER.debug(
                        "[connect] Insufficient authentication for %s — initiating pair() on active connection",
                        self.address,
                    )
                    await self._client.pair()
                    LOGGER.debug(
                        "[connect] pair() completed for %s, retrying start_notify",
                        self.address,
                    )
                    await self._client.start_notify(CHAR_UCG_IN_UUID, self._on_notification)
                elif "NotPermitted" not in exc_str or "Notify acquired" not in exc_str:
                    raise
                else:
                    # Stale "Notify acquired" flag from a previous HA session.
                    if is_local_bluez_device(self._ble_device):
                        # BlueZ persists the Notify-acquired flag across process restarts;
                        # clear it via D-Bus StopNotify before reconnecting.
                        LOGGER.debug(
                            "[connect] NotPermitted Notify acquired for %s — clearing via D-Bus StopNotify",
                            self.address,
                        )
                        await dbus_stop_notify(self.address)
                    else:
                        # Bluetooth Proxy devices do not have BlueZ stale state;
                        # just reconnect and retry start_notify directly.
                        LOGGER.debug(
                            "[connect] NotPermitted Notify acquired for %s (proxy device) — reconnecting",
                            self.address,
                        )
                    with suppress(Exception):
                        await self._client.disconnect()
                    self._client = None
                    LOGGER.debug("[connect] reconnecting after StopNotify for %s", self.address)
                    self._client = await establish_connection(
                        BleakClientWithServiceCache,
                        self._ble_device,
                        self._ble_device.address,
                        disconnected_callback=self._on_disconnected,
                    )
                    LOGGER.debug(
                        "[connect] reconnect OK for %s, retrying start_notify",
                        self.address,
                    )
                    await self._client.start_notify(CHAR_UCG_IN_UUID, self._on_notification)
            self._notify_active = True
            self._connected = True
            LOGGER.debug("[connect] done: connected to %s", self.address)
        except Exception as exc:
            LOGGER.debug(
                "[connect] exception for %s: %s (notify_active=%s)",
                self.address,
                exc,
                self._notify_active,
            )
            self._connected = False
            if self._client is not None:
                if self._notify_active:
                    with suppress(Exception):
                        await self._client.stop_notify(CHAR_UCG_IN_UUID)
                    LOGGER.debug(
                        "[connect] stop_notify called in except block for %s",
                        self.address,
                    )
                self._notify_active = False
                with suppress(Exception):
                    await self._client.disconnect()
                self._client = None
            msg = f"Failed to connect to {self.address}: {exc}"
            raise LixilShutterBleClientCommunicationError(msg) from exc

    def set_status_callback(self, callback: Callable[[str], None] | None) -> None:
        """Set (or clear) the notification callback without reconnecting."""
        self._status_callback = callback

    async def _ensure_connected(self) -> None:
        """Reuse existing BLE connection or establish a new one.

        If already connected, cancels the idle-disconnect timer so the
        connection is kept alive for another round.  If not connected,
        performs a full connect() using the currently stored callback.
        """
        self._cancel_idle_disconnect()
        if self.is_connected:
            return
        await self.connect(status_callback=self._status_callback)

    def _schedule_idle_disconnect(self, delay: float | None = None) -> None:
        """Schedule a disconnect after ``delay`` seconds of inactivity.

        If *delay* is None the module-level ``_IDLE_DISCONNECT_SEC`` constant
        is used.  Commands pass their ``idle_after`` argument here so the
        post-command monitoring window can be longer than the default idle.
        """
        self._cancel_idle_disconnect()
        self._idle_task = asyncio.create_task(self._idle_disconnect_task(delay or _IDLE_DISCONNECT_SEC))

    def _cancel_idle_disconnect(self) -> None:
        """Cancel any pending idle-disconnect timer."""
        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = None

    async def _idle_disconnect_task(self, delay: float) -> None:
        """Disconnect after *delay* seconds."""
        try:
            await asyncio.sleep(delay)
            LOGGER.debug("[idle] disconnecting %s after %.0fs inactivity", self.address, delay)
            await self.disconnect()
        except asyncio.CancelledError:
            pass

    async def disconnect(self) -> None:
        """Disconnect from the shutter and release all BLE/GATT resources.

        Cancels the idle-disconnect timer, stops GATT notifications, and closes
        the BLE connection.  Safe to call even if not currently connected.
        Sets ``_disconnecting`` so ``_on_disconnected`` suppresses the
        unexpected-disconnect warning.
        """
        self._cancel_idle_disconnect()
        self._disconnecting = True
        self._connected = False
        try:
            if self._client and self._client.is_connected:
                if self._notify_active:
                    try:
                        await self._client.stop_notify(CHAR_UCG_IN_UUID)
                    except Exception as exc:  # noqa: BLE001
                        LOGGER.debug("Error stopping notify on %s: %s", self.address, exc)
                try:
                    await self._client.disconnect()
                except Exception as exc:  # noqa: BLE001
                    LOGGER.debug("Error during disconnect from %s: %s", self.address, exc)
        finally:
            self._notify_active = False
            self._client = None
            self._disconnecting = False
        LOGGER.debug("Disconnected from shutter %s", self.address)

    def _on_disconnected(self, _client: BleakClient) -> None:
        """BleakClient disconnection callback (called on the event loop).

        Logs a warning for unexpected disconnects (device out of range, BLE
        error).  Expected disconnects triggered by ``disconnect()`` set
        ``_disconnecting=True`` beforehand to suppress the warning.
        Clears ``_notify_active`` so the next ``connect()`` can call
        ``start_notify`` without hitting a stale BlueZ "Notify acquired" error.
        """
        if not self._disconnecting:
            LOGGER.warning("Shutter %s disconnected unexpectedly", self.address)
        LOGGER.debug(
            "[_on_disconnected] disconnecting=%s notify_active=%s",
            self._disconnecting,
            self._notify_active,
        )
        self._connected = False
        # bleak's stop_notify() raises BleakError("Not connected") when
        # is_connected=False, so we cannot call StopNotify here.
        # The proactive stop_notify() at the start of the next connect() will
        # clear any stale BlueZ "Notify acquired" state.
        self._notify_active = False

    # ------------------------------------------------------------------
    # Command sending
    # ------------------------------------------------------------------

    async def open(self, idle_after: float | None = None) -> None:
        """Open the shutter fully (keyCode=0x03).

        Connects on demand, sends press+release, then schedules an idle
        disconnect after ``idle_after`` seconds (default: ``_IDLE_DISCONNECT_SEC``).
        """
        await self._execute(KEY_CODE_OPEN, idle_after=idle_after)

    async def close(self, idle_after: float | None = None) -> None:
        """Close the shutter fully (keyCode=0x04).

        Connects on demand, sends press+release, then schedules an idle
        disconnect after ``idle_after`` seconds (default: ``_IDLE_DISCONNECT_SEC``).
        """
        await self._execute(KEY_CODE_CLOSE, idle_after=idle_after)

    async def stop(self, idle_after: float | None = None) -> None:
        """Stop the shutter mid-travel (keyCode=0x05).

        Connects on demand, sends press+release, then schedules an idle
        disconnect after ``idle_after`` seconds (default: ``_IDLE_DISCONNECT_SEC``).
        """
        await self._execute(KEY_CODE_STOP, idle_after=idle_after)

    async def open_flap_slats(self, idle_after: float | None = None) -> None:
        """Open the flap slats to allow ventilation (採風).

        Tilts the flap slats open so wind and light can pass through while the
        shutter remains closed.  Available on all types except type=0
        (DecorativeWindow) and type=1 (ShutterEaris) — see ``has_ventilation``.
        Exposed via ``CoverEntityFeature.OPEN_TILT`` in the cover entity.
        Sends press only — no release frame (keyCode=0x06, subCode=0x01).
        Connects on demand, then schedules an idle disconnect after ``idle_after``
        seconds (default: ``_IDLE_DISCONNECT_SEC``).

        Note: the flap slats are also closed as part of the shutter's normal
        close motion — sending the close command closes both the shutter
        and the flap slats.
        """
        await self._execute(
            KEY_CODE_POSITION,
            SUB_CODE_VENTILATION,
            press_only=True,
            idle_after=idle_after,
        )

    async def move_to_memory_position(self, idle_after: float | None = None) -> None:
        """Move to the stored memory position.

        All device types have this button.  The subCode byte differs by type:
        - type=0,1 (DecorativeWindow, ShutterEaris) : ``01 06 01 00`` (SUB_CODE_VENTILATION, no RELEASE)
        - all other types : ``01 06 02 00`` (SUB_CODE_MEMORY, no RELEASE)

        No HA cover feature currently exposes this.
        Connects on demand, then schedules an idle disconnect after ``idle_after``
        seconds (default: ``_IDLE_DISCONNECT_SEC``).
        """
        sub_code = SUB_CODE_VENTILATION if self._production_info_id in _NORMAL_TYPE_IDS else SUB_CODE_MEMORY
        await self._execute(KEY_CODE_POSITION, sub_code, press_only=True, idle_after=idle_after)

    async def request_status(self, idle_after: float | None = None) -> None:
        """Send status request command (keyState=0x03, keyCode=0x0B).

        Connects on demand if not already connected, then schedules an idle
        disconnect so the connection is released after inactivity.
        """
        await self._ensure_connected()
        cmd = self._build_command(KEY_STATE_RELEASE, KEY_CODE_STATUS, SUB_CODE_DEFAULT, self._next_tag())
        await self._write(cmd)
        self._schedule_idle_disconnect(idle_after)

    # ------------------------------------------------------------------
    # Pairing
    # ------------------------------------------------------------------

    async def do_pairing(self) -> None:
        """
        Perform BLE-level pairing.

        Routing:
        - **Local BlueZ adapter** (Raspberry Pi built-in Bluetooth): uses the
          BlueZ D-Bus ``Pair()`` API for explicit Just-Works bonding.  Strategy:
          1. Register a Just-Works (NoInputNoOutput) pairing agent.
          2. RemoveDevice — wipe stale BlueZ cache.
          3. Connect via habluetooth (keeps the link alive for Pair()).
          4. Call Pair() while connected — BlueZ runs SMP over the LE link.
          5. Disconnect and unregister the agent.

        - **Bluetooth Proxy** (ESPHome/ESP32): D-Bus is not available for the
          remote device.  The proxy's ESP32 chip handles BLE-level SMP bonding
          automatically when a connection is established.  We verify connectivity
          via ``establish_connection`` and then disconnect — the proxy takes care
          of the security handshake.

        Raises:
            LixilShutterBleClientCommunicationError: On connection or pairing failure.
        """
        if not is_local_bluez_device(self._ble_device):
            LOGGER.debug(
                "[do_pairing] %s is a Bluetooth Proxy device — verifying BLE connectivity",
                self.address,
            )
            await self._verify_proxy_connection()
            return

        await dbus_pair(self.address, self._ble_device)

    async def _verify_proxy_connection(self) -> None:
        """Pair and verify BLE connectivity for a Bluetooth Proxy device.

        For Bluetooth Proxy devices (ESPHome/ESP32), D-Bus is not available.
        Instead, ``BleakClient.pair()`` is used which routes through the
        ESPHome API: the proxy's ESP32 chip initiates SMP pairing and stores
        the bond keys in its NVS flash.  Subsequent connections will be
        established over an authenticated (encrypted) link automatically.

        The device must be in pairing mode (PAIRING_MODE_BIT set) before
        calling this method; the shutter will reject new bond requests from
        unknown peers unless it is actively in pairing mode.

        Raises:
            LixilShutterBleClientCommunicationError: If connection or pairing fails.
        """
        client: BleakClient | None = None
        try:
            LOGGER.debug("[do_pairing] proxy: connecting to %s for bonding", self.address)
            client = await establish_connection(
                BleakClientWithServiceCache,
                self._ble_device,
                self._ble_device.address,
            )
            LOGGER.debug(
                "[do_pairing] proxy: calling pair() on %s to initiate SMP bonding",
                self.address,
            )
            await client.pair()
            LOGGER.debug("[do_pairing] proxy: bonding completed for %s", self.address)
        except LixilShutterBleClientCommunicationError:
            raise
        except Exception as exc:
            msg = f"Proxy pairing failed for {self.address}: {exc}"
            raise LixilShutterBleClientCommunicationError(msg) from exc
        finally:
            if client is not None:
                with suppress(Exception):
                    await client.disconnect()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _execute(
        self,
        key_code: int,
        sub_code: int = SUB_CODE_DEFAULT,
        *,
        press_only: bool = False,
        idle_after: float | None = None,
    ) -> None:
        """
        Send a command to the shutter.

        When ``press_only=False`` (default):
          1. Send press bytes  (keyState=KEY_STATE_PRESS)
          2. Wait RELEASE_DELAY_SEC (100 ms)
          3. Send release bytes (keyState=KEY_STATE_RELEASE)

        When ``press_only=True``:
          1. Send press bytes only — the device acts on the press alone.

        Args:
            key_code:   Command key code byte (e.g. KEY_CODE_OPEN).
            sub_code:   Sub-code byte (default SUB_CODE_DEFAULT=0x00).
            press_only: If True, skip the release frame.

        Raises:
            LixilShutterBleClientCommunicationError: On GATT write failure.
        """
        tag = self._next_tag()
        press = self._build_command(KEY_STATE_PRESS, key_code, sub_code, tag)
        try:
            async with asyncio.timeout(COMMAND_TIMEOUT_SEC):
                await self._ensure_connected()
                await self._write(press)
                if not press_only:
                    release = self._build_command(KEY_STATE_RELEASE, key_code, sub_code, tag)
                    await asyncio.sleep(RELEASE_DELAY_SEC)
                    await self._write(release)
        except LixilShutterBleClientCommunicationError:
            raise
        except Exception as exc:
            msg = f"Command execution failed on {self.address}: {exc}"
            raise LixilShutterBleClientCommunicationError(msg) from exc
        self._schedule_idle_disconnect(idle_after)

    async def _write(self, data: bytes) -> None:
        """Write *data* to the UCG_OUT GATT characteristic (write-with-response).

        Raises:
            LixilShutterBleClientCommunicationError: If not connected.
        """
        if not self._client or not self._client.is_connected:
            msg = f"Not connected to {self.address}"
            raise LixilShutterBleClientCommunicationError(msg)
        await self._client.write_gatt_char(CHAR_UCG_OUT_UUID, data, response=True)

    def _next_tag(self) -> int:
        """Return the next tag byte (cycles 0–99) and increment the counter.

        The tag byte is the 4th byte of each command frame; it lets the device
        match press and release frames from the same command invocation.
        """
        tag = self._tag % 100
        self._tag += 1
        return tag

    @staticmethod
    def _build_command(key_state: int, key_code: int, sub_code: int, tag: int) -> bytes:
        """
        Build 4-byte GATT command.

        Args:
            key_state: KEY_STATE_PRESS or KEY_STATE_RELEASE.
            key_code:  Key code byte (e.g. KEY_CODE_OPEN).
            sub_code:  Sub-code byte (e.g. SUB_CODE_MEMORY).
            tag:       Tag byte (0–99).

        Returns:
            4-byte command.
        """
        return bytes([key_state, key_code, sub_code, tag])

    # ------------------------------------------------------------------
    # Notification handler
    # ------------------------------------------------------------------

    def _on_notification(self, _characteristic: BleakGATTCharacteristic, data: bytearray) -> None:
        """
        Handle incoming GATT notification from UCG_IN.

        Routing:
        - data length < 6  → status notification, parse and call status_callback
        - any length       → command executed notification (ignored here)

        Args:
            _characteristic: GATT characteristic (unused).
            data: Raw notification bytearray.
        """
        if len(data) < 6 and self._status_callback:
            status = self._parse_status(bytes(data))
            self._status_callback(status)

    @staticmethod
    def _parse_status(data: bytes) -> str:
        """
        Parse status from notification bytes[2].

        Bit analysis:
          bit2 set (byte & 0x04) → STATUS_VENTILATION  (flap slats open / saifu)
          bit3 set (byte & 0x08) → STATUS_CLOSED
          bit3 clear             → STATUS_OPEN

        Args:
            data: Raw notification bytes (must have at least 3 bytes for status).

        Returns:
            One of STATUS_OPEN, STATUS_CLOSED, STATUS_VENTILATION.
            STATUS_UNKNOWN is returned only if *data* is too short (< 3 bytes).
        """
        if len(data) < 3:
            return STATUS_UNKNOWN
        byte = data[2] & 0xFF
        if byte & 0x04:  # bit2 set → ventilation (saifu)
            return STATUS_VENTILATION
        if byte & 0x08:  # bit3 set → closed
            return STATUS_CLOSED
        return STATUS_OPEN  # bit3 clear → open


__all__ = [
    "LixilShutterBleClient",
    "LixilShutterBleClientCommunicationError",
    "LixilShutterBleClientError",
]
