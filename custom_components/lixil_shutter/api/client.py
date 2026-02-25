"""
BLE GATT client for LIXIL MyWindow shutter.

Manages the full BLE lifecycle for the shutter:
- On-demand connection: ``_ensure_connected`` connects only when a command or
  status poll is issued, avoiding a permanently held BLE link.
- Idle disconnect timer: after each command the client schedules an automatic
  disconnect (``_IDLE_DISCONNECT_SEC`` or the caller-supplied ``idle_after``
  value) so other BLE clients (e.g. the Android app) can connect in the gap.
- GATT notifications: status updates pushed by the device are delivered to the
  registered callback without polling.

Underlying BLE library: bleak / bleak-retry-connector (bundled with HA).
Pairing uses BlueZ D-Bus Pair() via dbus-fast (transitive HA dependency).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from dbus_fast import BusType
from dbus_fast.aio import MessageBus as DBusMessageBus
from dbus_fast.constants import MessageType as DBusMessageType
from dbus_fast.message import Message as DBusMessage
from dbus_fast.service import ServiceInterface, method as dbus_method

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
    STATUS_MIN,
    STATUS_OPEN,
    STATUS_UNKNOWN,
    SUB_CODE_DEFAULT,
    SUB_CODE_MEMORY,
    SUB_CODE_VENTILATION,
)

_PAIRING_AGENT_PATH = "/com/lixil_shutter/pairing_agent"
_IDLE_DISCONNECT_SEC: float = 30.0


class _JustWorksAgent(ServiceInterface):
    """Minimal BlueZ NoInputNoOutput pairing agent for Just-Works bonding.

    Registered as the default BlueZ agent before calling Pair() so that
    BlueZ uses Just-Works / NoInputNoOutput pairing (auto-confirm) instead
    of rejecting with AuthenticationFailed when no suitable agent is found.
    All methods auto-confirm. The agent is unregistered after pairing.

    The dbus_fast @dbus_method() decorator requires parameter annotations to be
    D-Bus type strings ('o', 's', 'u', 'q').  Return type annotations must be
    omitted for void methods because 'None' is not a valid D-Bus type string in
    this library version.
    """

    def __init__(self) -> None:
        super().__init__("org.bluez.Agent1")

    @dbus_method()
    def Release(self):
        pass

    @dbus_method()
    def Cancel(self):
        pass

    @dbus_method()  # type: ignore[misc]
    def RequestAuthorization(self, device: o):  # type: ignore[reportUndefinedVariable]  # noqa: F821
        """Auto-authorize for Just-Works; never called with NoInputNoOutput."""

    @dbus_method()  # type: ignore[misc]
    def RequestPinCode(self, device: o) -> s:  # type: ignore[reportUndefinedVariable]  # noqa: F821
        """Fallback pin; not used in Just-Works."""
        return "0000"  # type: ignore[return-value]

    @dbus_method()  # type: ignore[misc]
    def RequestPasskey(self, device: o) -> u:  # type: ignore[reportUndefinedVariable]  # noqa: F821
        """Fallback passkey; not used in Just-Works."""
        return 0  # type: ignore[return-value]

    @dbus_method()  # type: ignore[misc]
    def DisplayPasskey(self, device: o, passkey: u, entered: q):  # type: ignore[reportUndefinedVariable]  # noqa: F821
        """No display available (NoInputNoOutput)."""

    @dbus_method()  # type: ignore[misc]
    def DisplayPinCode(self, device: o, pincode: s):  # type: ignore[reportUndefinedVariable]  # noqa: F821
        """No display available (NoInputNoOutput)."""

    @dbus_method()  # type: ignore[misc]
    def RequestConfirmation(self, device: o, passkey: u):  # type: ignore[reportUndefinedVariable]  # noqa: F821
        """Auto-confirm for Just-Works numeric comparison."""

    @dbus_method()  # type: ignore[misc]
    def AuthorizeService(self, device: o, uuid: s):  # type: ignore[reportUndefinedVariable]  # noqa: F821
        """Auto-authorize all services."""


class LixilShutterBleClientError(Exception):
    """Base BLE client error."""


class LixilShutterBleClientCommunicationError(LixilShutterBleClientError):
    """BLE communication or connection error."""


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

    Typical usage (cover entity):
        client = LixilShutterBleClient(ble_device)
        client.set_status_callback(my_callback)  # register once
        await client.open(idle_after=30)          # connects on demand, auto-disconnects
        await client.request_status()             # connect → request → idle disconnect
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
        self._notify_active = False  # True only when start_notify() was called
        self._idle_task: asyncio.Task[None] | None = None
        self._disconnecting: bool = False  # True only during intentional disconnect

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
            LOGGER.debug("[connect] cleaning up existing client (notify_active=%s)", self._notify_active)
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
                if "NotPermitted" not in str(start_exc) or "Notify acquired" not in str(start_exc):
                    raise
                # Stale BlueZ "Notify acquired" flag from a previous HA session.
                # Fix: clear via D-Bus StopNotify, reconnect, retry.
                LOGGER.debug(
                    "[connect] NotPermitted Notify acquired for %s — clearing via D-Bus StopNotify",
                    self.address,
                )
                await self._dbus_stop_notify_for_device()
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
                LOGGER.debug("[connect] reconnect OK for %s, retrying start_notify", self.address)
                await self._client.start_notify(CHAR_UCG_IN_UUID, self._on_notification)
            self._notify_active = True
            self._connected = True
            LOGGER.debug("[connect] done: connected to %s", self.address)
        except Exception as exc:
            LOGGER.debug("[connect] exception for %s: %s (notify_active=%s)", self.address, exc, self._notify_active)
            self._connected = False
            if self._client is not None:
                if self._notify_active:
                    with suppress(Exception):
                        await self._client.stop_notify(CHAR_UCG_IN_UUID)
                    LOGGER.debug("[connect] stop_notify called in except block for %s", self.address)
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
        LOGGER.debug("[_on_disconnected] disconnecting=%s notify_active=%s", self._disconnecting, self._notify_active)
        self._connected = False
        # bleak's stop_notify() raises BleakError("Not connected") when
        # is_connected=False, so we cannot call StopNotify here.
        # The proactive stop_notify() at the start of the next connect() will
        # clear any stale BlueZ "Notify acquired" state.
        self._notify_active = False

    async def _dbus_stop_notify_for_device(self) -> None:
        """Clear BlueZ 'Notify acquired' state by calling StopNotify via D-Bus.

        Called when start_notify raises "[org.bluez.Error.NotPermitted] Notify
        acquired" — which means BlueZ has a stale acquired flag from a previous
        HA session (BlueZ persists it across process restarts).

        bleak's stop_notify() cannot be used here: it checks whether the current
        bleak session called start_notify first and raises BleakError if not, so
        it never reaches D-Bus.  This method uses dbus_fast directly (a transitive
        HA dependency via bleak) to call StopNotify without that guard.

        Note: StopNotify causes the BLE device to disconnect.  The caller must
        disconnect the existing client and reconnect after this call.

        Best-effort: any failure is logged at DEBUG level and suppressed.
        """
        bus: DBusMessageBus | None = None
        try:
            bus = await DBusMessageBus(bus_type=BusType.SYSTEM).connect()

            # Enumerate all BlueZ objects to find the D-Bus path for our characteristic.
            reply = await bus.call(
                DBusMessage(
                    destination="org.bluez",
                    path="/",
                    interface="org.freedesktop.DBus.ObjectManager",
                    member="GetManagedObjects",
                )
            )
            if reply.message_type != DBusMessageType.METHOD_RETURN:
                LOGGER.debug("[dbus_stop_notify] GetManagedObjects failed: %s", reply)
                return

            objects: dict = reply.body[0]
            addr_upper = self.address.upper().replace(":", "_")
            dev_prefix = f"/org/bluez/hci0/dev_{addr_upper}"

            char_path: str | None = None
            for path, interfaces in objects.items():
                if not path.startswith(dev_prefix):
                    continue
                if "org.bluez.GattCharacteristic1" not in interfaces:
                    continue
                props = interfaces["org.bluez.GattCharacteristic1"]
                uuid_variant = props.get("UUID")
                if uuid_variant is None:
                    continue
                uuid_val = uuid_variant.value if hasattr(uuid_variant, "value") else uuid_variant
                if str(uuid_val).lower() == CHAR_UCG_IN_UUID.lower():
                    char_path = path
                    break

            if char_path is None:
                LOGGER.debug(
                    "[dbus_stop_notify] char %s not found in BlueZ for %s",
                    CHAR_UCG_IN_UUID,
                    self.address,
                )
                return

            LOGGER.debug("[dbus_stop_notify] calling StopNotify for %s path=%s", self.address, char_path)
            stop_reply = await bus.call(
                DBusMessage(
                    destination="org.bluez",
                    path=char_path,
                    interface="org.bluez.GattCharacteristic1",
                    member="StopNotify",
                )
            )
            LOGGER.debug("[dbus_stop_notify] StopNotify reply for %s: %s", self.address, stop_reply)
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("[dbus_stop_notify] error for %s: %s", self.address, exc)
        finally:
            if bus is not None:
                with suppress(Exception):
                    bus.disconnect()

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

    async def memory_position(self, idle_after: float | None = None) -> None:
        """Move to the stored memory (favourite) position (keyCode=0x06, subCode=0x02).

        Connects on demand, sends press+release, then schedules an idle
        disconnect after ``idle_after`` seconds (default: ``_IDLE_DISCONNECT_SEC``).
        """
        await self._execute(KEY_CODE_POSITION, SUB_CODE_MEMORY, idle_after=idle_after)

    async def ventilation_position(self, idle_after: float | None = None) -> None:
        """Move to the ventilation (saifu) position (keyCode=0x06, subCode=0x01).

        Connects on demand, sends press+release, then schedules an idle
        disconnect after ``idle_after`` seconds (default: ``_IDLE_DISCONNECT_SEC``).
        """
        await self._execute(KEY_CODE_POSITION, SUB_CODE_VENTILATION, idle_after=idle_after)

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
        Perform BLE-level pairing via BlueZ D-Bus Pair().

        Strategy:
        1. Register a Just-Works (NoInputNoOutput) pairing agent first.
        2. RemoveDevice — wipe stale BlueZ cache.
        3. Pre-connect via habluetooth (BLE).
        4. Call Pair() WHILE CONNECTED — BlueZ runs SMP over the existing LE
           link.  The Just-Works agent auto-confirms the bond.
        5. Disconnect.
        6. UnregisterAgent.

        Calling Pair() after disconnect caused BLE→BR/EDR fallback (Page
        Timeout) even when AddressType=public was correctly set.  Keeping the
        connection alive avoids that path entirely.

        Raises:
            LixilShutterBleClientCommunicationError: On connection or pairing failure.
        """
        addr_upper = self.address.upper().replace(":", "_")
        device_path = f"/org/bluez/hci0/dev_{addr_upper}"
        adapter_path = "/org/bluez/hci0"

        LOGGER.debug("[do_pairing] start for %s (dbus path=%s)", self.address, device_path)
        client: BleakClient | None = None
        bus: DBusMessageBus | None = None
        agent_registered = False
        try:
            bus = await DBusMessageBus(bus_type=BusType.SYSTEM).connect()

            # Step 1: Register Just-Works agent BEFORE connecting.
            LOGGER.debug("[do_pairing] registering Just-Works agent for %s", self.address)
            agent = _JustWorksAgent()
            bus.export(_PAIRING_AGENT_PATH, agent)
            reg_reply = await bus.call(
                DBusMessage(
                    destination="org.bluez",
                    path="/org/bluez",
                    interface="org.bluez.AgentManager1",
                    member="RegisterAgent",
                    signature="os",
                    body=[_PAIRING_AGENT_PATH, "NoInputNoOutput"],
                )
            )
            if reg_reply.message_type == DBusMessageType.ERROR:
                LOGGER.debug(
                    "[do_pairing] RegisterAgent failed (continuing): %s %s",
                    reg_reply.error_name,
                    reg_reply.body,
                )
            else:
                agent_registered = True
                LOGGER.debug("[do_pairing] agent registered; requesting default-agent")
                await bus.call(
                    DBusMessage(
                        destination="org.bluez",
                        path="/org/bluez",
                        interface="org.bluez.AgentManager1",
                        member="RequestDefaultAgent",
                        signature="o",
                        body=[_PAIRING_AGENT_PATH],
                    )
                )

            # Step 2: RemoveDevice — wipe stale cached device object.
            LOGGER.debug("[do_pairing] removing stale device from BlueZ for %s", self.address)
            remove_reply = await bus.call(
                DBusMessage(
                    destination="org.bluez",
                    path=adapter_path,
                    interface="org.bluez.Adapter1",
                    member="RemoveDevice",
                    signature="o",
                    body=[device_path],
                )
            )
            if remove_reply.message_type == DBusMessageType.ERROR:
                LOGGER.debug(
                    "[do_pairing] RemoveDevice reply (DoesNotExist is expected): %s %s",
                    remove_reply.error_name,
                    remove_reply.body,
                )
            else:
                LOGGER.debug("[do_pairing] RemoveDevice succeeded for %s", self.address)

            # Step 3: Pre-connect via habluetooth to create a fresh BLE device
            # object. Keep the connection open — Pair() over an active LE link
            # avoids BR/EDR fallback (Page Timeout) that happens when calling
            # Pair() after disconnect.
            LOGGER.debug("[do_pairing] connecting to %s (keeping alive for Pair)", self.address)
            client = await establish_connection(
                BleakClientWithServiceCache,
                self._ble_device,
                self._ble_device.address,
            )
            LOGGER.debug("[do_pairing] connected to %s — calling D-Bus Pair()", self.address)

            # Step 4: Pair() while connected. The Just-Works agent handles SMP.
            reply = await asyncio.wait_for(
                bus.call(
                    DBusMessage(
                        destination="org.bluez",
                        path=device_path,
                        interface="org.bluez.Device1",
                        member="Pair",
                    )
                ),
                timeout=60.0,
            )
            if reply.message_type == DBusMessageType.METHOD_RETURN:
                LOGGER.debug("[do_pairing] Pair() succeeded for %s", self.address)
            elif reply.message_type == DBusMessageType.ERROR:
                error_name = reply.error_name or ""
                # AlreadyExists = already bonded; InProgress = other attempt running.
                if "AlreadyExists" in error_name or "InProgress" in error_name:
                    LOGGER.debug(
                        "[do_pairing] already paired / in-progress for %s (%s)",
                        self.address,
                        error_name,
                    )
                else:
                    msg = f"Pairing failed for {self.address}: [{error_name}] {reply.body}"
                    raise LixilShutterBleClientCommunicationError(msg)  # noqa: TRY301
            else:
                msg = f"Pairing unexpected reply for {self.address}: {reply}"
                raise LixilShutterBleClientCommunicationError(msg)  # noqa: TRY301
            LOGGER.debug("[do_pairing] done for %s", self.address)
        except LixilShutterBleClientCommunicationError:
            raise
        except Exception as exc:
            msg = f"Pairing failed for {self.address}: {exc}"
            raise LixilShutterBleClientCommunicationError(msg) from exc
        finally:
            if client is not None:
                with suppress(Exception):
                    await client.disconnect()
            if bus is not None:
                if agent_registered:
                    with suppress(Exception):
                        await bus.call(
                            DBusMessage(
                                destination="org.bluez",
                                path="/org/bluez",
                                interface="org.bluez.AgentManager1",
                                member="UnregisterAgent",
                                signature="o",
                                body=[_PAIRING_AGENT_PATH],
                            )
                        )
                with suppress(Exception):
                    bus.disconnect()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _execute(
        self, key_code: int, sub_code: int = SUB_CODE_DEFAULT, *, idle_after: float | None = None
    ) -> None:
        """
        Send a press+release command pair to the shutter.

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
                await self._ensure_connected()
                await self._write(press)
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
