"""
BLE GATT client for LIXIL MyWindow shutter.

Handles BLE connection, command sending, and status notification
via bleak library (bundled with Home Assistant).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice

from custom_components.lixil_shutter.const import (
    CHAR_UCG_IN_UUID,
    CHAR_UCG_OUT_UUID,
    COMMAND_TIMEOUT_SEC,
    CONNECT_TIMEOUT_SEC,
    KEY_CODE_CLOSE,
    KEY_CODE_OPEN,
    KEY_CODE_POSITION,
    KEY_CODE_STATUS,
    KEY_CODE_STOP,
    KEY_CODE_WRITE_NAME,
    KEY_STATE_PRESS,
    KEY_STATE_RELEASE,
    LOGGER,
    RELEASE_DELAY_SEC,
    STATUS_CLOSED,
    STATUS_MIN,
    STATUS_OPEN,
    STATUS_UNKNOWN,
    SUB_CODE_DEFAULT,
    SUB_CODE_MEMORY,
    SUB_CODE_VENTILATION,
)


class LixilShutterBleClientError(Exception):
    """Base BLE client error."""


class LixilShutterBleClientCommunicationError(LixilShutterBleClientError):
    """BLE communication or connection error."""


class LixilShutterBleClient:
    """
    BLE GATT client for LIXIL MyWindow shutter.

    Manages the full BLE lifecycle:
    - Connect / disconnect
    - Enable GATT notifications on UCG_IN characteristic
    - Send commands to UCG_OUT characteristic (press + release pattern)
    - Parse status notifications

    Usage:
        client = LixilShutterBleClient(ble_device)
        await client.connect(status_callback=my_callback)
        await client.open()
        await client.disconnect()
    """

    def __init__(self, ble_device: BLEDevice) -> None:
        """
        Initialize the BLE client.

        Args:
            ble_device: The discovered BLE device from HA bluetooth scanner.
        """
        self._ble_device = ble_device
        self._client: BleakClient | None = None
        self._tag: int = 0
        self._status_callback: Callable[[str], None] | None = None
        self._connected = False

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

    async def connect(self, status_callback: Callable[[str], None] | None = None) -> None:
        """
        Connect to the shutter and start notifications.

        Args:
            status_callback: Called with status string on each notification.

        Raises:
            LixilShutterBleClientCommunicationError: On connection failure.
        """
        self._status_callback = status_callback
        try:
            self._client = BleakClient(
                self._ble_device,
                disconnected_callback=self._on_disconnected,
            )
            async with asyncio.timeout(CONNECT_TIMEOUT_SEC):
                await self._client.connect()
            await self._client.start_notify(CHAR_UCG_IN_UUID, self._on_notification)
            self._connected = True
            LOGGER.debug("Connected to shutter %s", self.address)
        except Exception as exc:
            self._connected = False
            msg = f"Failed to connect to {self.address}: {exc}"
            raise LixilShutterBleClientCommunicationError(msg) from exc

    async def disconnect(self) -> None:
        """Disconnect from the shutter and clean up resources."""
        self._connected = False
        if self._client and self._client.is_connected:
            try:
                await self._client.stop_notify(CHAR_UCG_IN_UUID)
                await self._client.disconnect()
            except Exception as exc:  # noqa: BLE001
                LOGGER.debug("Error during disconnect from %s: %s", self.address, exc)
        self._client = None
        LOGGER.debug("Disconnected from shutter %s", self.address)

    def _on_disconnected(self, _client: BleakClient) -> None:
        """Handle unexpected BLE disconnection."""
        LOGGER.warning("Shutter %s disconnected unexpectedly", self.address)
        self._connected = False

    # ------------------------------------------------------------------
    # Command sending
    # ------------------------------------------------------------------

    async def open(self) -> None:
        """Send open (up) command (keyCode=0x03)."""
        await self._execute(KEY_CODE_OPEN)

    async def close(self) -> None:
        """Send close (down) command (keyCode=0x04)."""
        await self._execute(KEY_CODE_CLOSE)

    async def stop(self) -> None:
        """Send stop command (keyCode=0x05)."""
        await self._execute(KEY_CODE_STOP)

    async def memory_position(self) -> None:
        """Send memory (favourite) position command (keyCode=0x06, subCode=0x02)."""
        await self._execute(KEY_CODE_POSITION, SUB_CODE_MEMORY)

    async def ventilation_position(self) -> None:
        """Send ventilation (saifu) position command (keyCode=0x06, subCode=0x01)."""
        await self._execute(KEY_CODE_POSITION, SUB_CODE_VENTILATION)

    async def request_status(self) -> None:
        """Send status request command (keyState=0x03, keyCode=0x0B)."""
        cmd = self._build_command(KEY_STATE_RELEASE, KEY_CODE_STATUS, SUB_CODE_DEFAULT, self._next_tag())
        await self._write(cmd)

    # ------------------------------------------------------------------
    # Pairing
    # ------------------------------------------------------------------

    async def do_pairing(self, device_name: str = "HA_LIXIL") -> None:
        """
        Perform application-level pairing sequence.

        This executes after OS-level BLE bonding (client.pair()) is complete:
        1. OS BLE bonding (client.pair())
        2. writeDeviceName command
        3. PAIR_ACTIONS sequence (5 commands)

        Call from config flow after connecting in pairing mode.

        Args:
            device_name: Name to register on the device (max 99 chars).

        Raises:
            LixilShutterBleClientCommunicationError: On GATT write failure.
        """
        if not self._client or not self._client.is_connected:
            msg = "Not connected — cannot pair"
            raise LixilShutterBleClientCommunicationError(msg)
        try:
            # Step 1: OS-level BLE bonding
            await self._client.pair()

            # Step 2: writeDeviceName (keyState=0x03, keyCode=0x0C, len, 0x00) + name bytes
            name_bytes = device_name.encode("utf-8")[:99]
            header = bytes([KEY_STATE_RELEASE, KEY_CODE_WRITE_NAME, len(name_bytes), SUB_CODE_DEFAULT])
            await self._client.write_gatt_char(CHAR_UCG_OUT_UUID, header + name_bytes, response=True)

            # Step 3: PAIR_ACTIONS — 5 press+release commands
            pair_actions: list[tuple[int, int]] = [
                (KEY_CODE_OPEN, SUB_CODE_DEFAULT),
                (KEY_CODE_CLOSE, SUB_CODE_DEFAULT),
                (KEY_CODE_STOP, SUB_CODE_DEFAULT),
                (KEY_CODE_POSITION, SUB_CODE_VENTILATION),
                (KEY_CODE_POSITION, SUB_CODE_VENTILATION),
            ]
            for key_code, sub_code in pair_actions:
                press = self._build_command(KEY_STATE_PRESS, key_code, sub_code, 0)
                release = self._build_command(KEY_STATE_RELEASE, key_code, sub_code, 0)
                await self._client.write_gatt_char(CHAR_UCG_OUT_UUID, press, response=True)
                await asyncio.sleep(RELEASE_DELAY_SEC)
                await self._client.write_gatt_char(CHAR_UCG_OUT_UUID, release, response=True)
                await asyncio.sleep(0.5)

            LOGGER.debug("Pairing complete for %s", self.address)
        except LixilShutterBleClientCommunicationError:
            raise
        except Exception as exc:
            msg = f"Pairing failed for {self.address}: {exc}"
            raise LixilShutterBleClientCommunicationError(msg) from exc

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _execute(self, key_code: int, sub_code: int = SUB_CODE_DEFAULT) -> None:
        """
        Send a press+release command pair to the shutter.

        Mirrors ActionHandler.execute():
        1. Send press bytes  (keyState=KEY_STATE_PRESS)
        2. Wait RELEASE_DELAY_SEC (100 ms)
        3. Send release bytes (keyState=KEY_STATE_RELEASE)

        Args:
            key_code: Command key code byte (e.g. KEY_CODE_OPEN).
            sub_code: Sub-code byte (default SUB_CODE_DEFAULT=0x00).

        Raises:
            LixilShutterBleClientCommunicationError: On GATT write failure.
        """
        tag = self._next_tag()
        press = self._build_command(KEY_STATE_PRESS, key_code, sub_code, tag)
        release = self._build_command(KEY_STATE_RELEASE, key_code, sub_code, tag)
        try:
            async with asyncio.timeout(COMMAND_TIMEOUT_SEC):
                await self._write(press)
                await asyncio.sleep(RELEASE_DELAY_SEC)
                await self._write(release)
        except LixilShutterBleClientCommunicationError:
            raise
        except Exception as exc:
            msg = f"Command execution failed on {self.address}: {exc}"
            raise LixilShutterBleClientCommunicationError(msg) from exc

    async def _write(self, data: bytes) -> None:
        """Write bytes to UCG_OUT characteristic."""
        if not self._client or not self._client.is_connected:
            msg = f"Not connected to {self.address}"
            raise LixilShutterBleClientCommunicationError(msg)
        await self._client.write_gatt_char(CHAR_UCG_OUT_UUID, data, response=True)

    def _next_tag(self) -> int:
        """Return next tag byte (0–99 cycling), increments internal counter."""
        tag = self._tag % 100
        self._tag += 1
        return tag

    @staticmethod
    def _build_command(key_state: int, key_code: int, sub_code: int, tag: int) -> bytes:
        """
        Build 4-byte GATT command.

        Mirrors ShutterClient.makeCommand():
          bytes = [keyState, keyCode, subCode, tag]

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

        Routing (mirrors ShutterClient$createGatt$1.onCharacteristicChanged):
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

        Bit analysis (mirrors ShutterClient.fetchStatus):
          bits[5] == '1' → STATUS_MIN  (fully closed / minimum)
          bits[4] == '0' → STATUS_OPEN
          bits[4] == '1' → STATUS_CLOSED

        Args:
            data: Raw notification bytes (must have at least 3 bytes for status).

        Returns:
            One of STATUS_OPEN, STATUS_CLOSED, STATUS_MIN, STATUS_UNKNOWN.
        """
        if len(data) < 3:
            return STATUS_UNKNOWN
        byte = data[2] & 0xFF
        bits = format(byte, "08b")  # MSB on the left, e.g. "01001000"
        if bits[5] == "1":
            return STATUS_MIN
        if bits[4] == "0":
            return STATUS_OPEN
        if bits[4] == "1":
            return STATUS_CLOSED
        return STATUS_UNKNOWN


__all__ = [
    "LixilShutterBleClient",
    "LixilShutterBleClientCommunicationError",
    "LixilShutterBleClientError",
]
