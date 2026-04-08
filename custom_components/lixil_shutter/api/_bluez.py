"""BlueZ D-Bus helpers for LIXIL MyWindow BLE client.

Contains BlueZ-specific functionality used when the shutter is managed by the
local Linux Bluetooth adapter (e.g., Raspberry Pi built-in Bluetooth).  These
helpers are NOT used for Bluetooth Proxy devices (ESPHome/ESP32).

All D-Bus operations are best-effort: unexpected failures are logged at DEBUG
level and suppressed rather than surfaced to the caller.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from dbus_fast import BusType
from dbus_fast.aio import MessageBus as DBusMessageBus
from dbus_fast.constants import MessageType as DBusMessageType
from dbus_fast.message import Message as DBusMessage
from dbus_fast.service import ServiceInterface, method as dbus_method

from custom_components.lixil_shutter.const import CHAR_UCG_IN_UUID, LOGGER

from .exceptions import LixilShutterBleClientCommunicationError

_PAIRING_AGENT_PATH = "/com/lixil_shutter/pairing_agent"


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


def is_local_bluez_device(ble_device: BLEDevice) -> bool:
    """Return True if *ble_device* is managed by the local BlueZ adapter.

    On BlueZ (Linux), ``BLEDevice.details`` contains a D-Bus object path of
    the form ``{"path": "/org/bluez/hciX/dev_..."}``.  Bluetooth Proxy
    devices (ESPHome/ESP32) have a different ``details`` structure without
    this path, so they are identified as non-local.
    """
    details = getattr(ble_device, "details", None)
    if not isinstance(details, dict):
        return False
    path = details.get("path", "")
    return isinstance(path, str) and "/org/bluez/" in path


async def dbus_stop_notify(address: str) -> None:
    """Clear BlueZ 'Notify acquired' state by calling StopNotify via D-Bus.

    Called when start_notify raises "[org.bluez.Error.NotPermitted] Notify
    acquired" — which means BlueZ has a stale acquired flag from a previous
    HA session (BlueZ persists it across process restarts).

    bleak's stop_notify() cannot be used here: it checks whether the current
    bleak session called start_notify first and raises BleakError if not, so
    it never reaches D-Bus.  This function uses dbus_fast directly (a
    transitive HA dependency via bleak) to call StopNotify without that guard.

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
        addr_upper = address.upper().replace(":", "_")
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
                address,
            )
            return

        LOGGER.debug(
            "[dbus_stop_notify] calling StopNotify for %s path=%s",
            address,
            char_path,
        )
        stop_reply = await bus.call(
            DBusMessage(
                destination="org.bluez",
                path=char_path,
                interface="org.bluez.GattCharacteristic1",
                member="StopNotify",
            )
        )
        LOGGER.debug(
            "[dbus_stop_notify] StopNotify reply for %s: %s",
            address,
            stop_reply,
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("[dbus_stop_notify] error for %s: %s", address, exc)
    finally:
        if bus is not None:
            with suppress(Exception):
                bus.disconnect()


async def dbus_pair(address: str, ble_device: BLEDevice) -> None:
    """Perform BLE-level pairing via BlueZ D-Bus Pair().

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

    Args:
        address: BLE MAC address of the device (e.g. "AA:BB:CC:DD:EE:FF").
        ble_device: Current BLEDevice instance from the HA scanner.

    Raises:
        LixilShutterBleClientCommunicationError: On connection or pairing failure.
    """
    addr_upper = address.upper().replace(":", "_")
    device_path = f"/org/bluez/hci0/dev_{addr_upper}"
    adapter_path = "/org/bluez/hci0"

    LOGGER.debug("[do_pairing] start for %s (dbus path=%s)", address, device_path)
    client: BleakClient | None = None
    bus: DBusMessageBus | None = None
    agent_registered = False
    try:
        bus = await DBusMessageBus(bus_type=BusType.SYSTEM).connect()

        # Step 1: Register Just-Works agent BEFORE connecting.
        LOGGER.debug("[do_pairing] registering Just-Works agent for %s", address)
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
        LOGGER.debug("[do_pairing] removing stale device from BlueZ for %s", address)
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
            LOGGER.debug("[do_pairing] RemoveDevice succeeded for %s", address)

        # Step 3: Pre-connect via habluetooth to create a fresh BLE device
        # object. Keep the connection open — Pair() over an active LE link
        # avoids BR/EDR fallback (Page Timeout) that happens when calling
        # Pair() after disconnect.
        LOGGER.debug("[do_pairing] connecting to %s (keeping alive for Pair)", address)
        client = await establish_connection(
            BleakClientWithServiceCache,
            ble_device,
            ble_device.address,
        )
        LOGGER.debug("[do_pairing] connected to %s — calling D-Bus Pair()", address)

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
            LOGGER.debug("[do_pairing] Pair() succeeded for %s", address)
        elif reply.message_type == DBusMessageType.ERROR:
            error_name = reply.error_name or ""
            # AlreadyExists = already bonded; InProgress = other attempt running.
            if "AlreadyExists" in error_name or "InProgress" in error_name:
                LOGGER.debug(
                    "[do_pairing] already paired / in-progress for %s (%s)",
                    address,
                    error_name,
                )
            else:
                msg = f"Pairing failed for {address}: [{error_name}] {reply.body}"
                raise LixilShutterBleClientCommunicationError(msg)  # noqa: TRY301
        else:
            msg = f"Pairing unexpected reply for {address}: {reply}"
            raise LixilShutterBleClientCommunicationError(msg)  # noqa: TRY301
        LOGGER.debug("[do_pairing] done for %s", address)
    except LixilShutterBleClientCommunicationError:
        raise
    except Exception as exc:
        msg = f"Pairing failed for {address}: {exc}"
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
