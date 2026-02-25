"""
Config flow for LIXIL Bluetooth Shutter.

Supports two registration paths:
1. **Automatic** — HA discovers the shutter via the SERVICE_UUID declared in
   manifest.json and calls async_step_bluetooth().
2. **Manual** — user opens "Add Integration" in Settings and searches for
   "Lixil Bluetooth Shutter". async_step_user() shows discovered devices.

Only devices actively in pairing mode (PAIRING_MODE_BIT set in manufacturer data)
are shown.  If no devices in pairing mode are detected the flow aborts with
a message instructing the user to activate pairing mode first.

Pairing sequence:
  Step 1  confirm  — Show device info, user confirms.
  Step 2  pair     — Execute BLE pairing immediately (no extra user prompt needed).
                      Shown again only if pairing fails (retry form).
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from custom_components.lixil_shutter.api import LixilShutterBleClient, LixilShutterBleClientCommunicationError
from custom_components.lixil_shutter.config_flow_handler.options_flow import LixilShutterOptionsFlow
from custom_components.lixil_shutter.const import (
    CONF_ADDRESS,
    CONF_PRODUCTION_INFO,
    DOMAIN,
    LOGGER,
    MANUFACTURER_ID,
    PAIRING_MODE_BIT,
    PRODUCTION_INFO,
)
from homeassistant import config_entries
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak, async_discovered_service_info


class LixilShutterConfigFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """
    Config flow handler for LIXIL Bluetooth Shutter.

    Supported flows:
    - bluetooth: Automatic discovery via manifest.json SERVICE_UUID
    - user: Manual setup

    For details:
    https://developers.home-assistant.io/docs/config_entries_config_flow_handler
    """

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the flow state."""
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}

    # ------------------------------------------------------------------
    # Automatic BLE discovery (manifest.json bluetooth service_uuid)
    # ------------------------------------------------------------------

    async def async_step_bluetooth(
        self,
        discovery_info: BluetoothServiceInfoBleak,
    ) -> config_entries.ConfigFlowResult:
        """
        Handle automatic Bluetooth discovery.

        Called by HA when a device advertising our SERVICE_UUID is detected.
        Sets unique ID (BLE address) and forwards to confirm step.

        Args:
            discovery_info: Discovered BLE device info from HA scanner.

        Returns:
            Config flow result — confirm form or abort if already configured.
        """
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        # Only trigger the flow when the device is actively in pairing mode.
        # Devices advertising the service UUID while *not* in pairing mode
        # should be silently ignored to avoid cluttering the "Discovered" panel.
        payload = discovery_info.manufacturer_data.get(MANUFACTURER_ID, b"")
        if len(payload) < 2 or not (payload[1] & PAIRING_MODE_BIT):
            return self.async_abort(reason="not_in_pairing_mode")

        self._discovery_info = discovery_info
        self.context["title_placeholders"] = {
            "name": discovery_info.name or discovery_info.address,
            "address": discovery_info.address,
        }
        return await self.async_step_confirm()

    # ------------------------------------------------------------------
    # Manual setup
    # ------------------------------------------------------------------

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """
        Handle manual setup initiated by the user.

        Scans for unconfigured LIXIL shutter devices in pairing mode and shows
        a selector.  If none are found, aborts with a "no_devices_found" reason
        so the user knows to activate pairing mode first.

        Args:
            user_input: Selected address, or None for initial display.

        Returns:
            Config flow result — device selector form or confirm step.
        """
        # Collect already-configured addresses so we can filter them out
        configured_addresses = {
            e.data.get(CONF_ADDRESS) for e in self.hass.config_entries.async_entries(DOMAIN) if CONF_ADDRESS in e.data
        }

        # Gather discovered LIXIL devices from HA BLE scanner cache.
        # Only show devices that are currently in pairing mode (PAIRING_MODE_BIT set)
        # so the user can clearly identify which shutter to select.
        for service_info in async_discovered_service_info(self.hass, connectable=True):
            if service_info.address in configured_addresses:
                continue
            payload = service_info.manufacturer_data.get(MANUFACTURER_ID, b"")
            if len(payload) < 2 or not (payload[1] & PAIRING_MODE_BIT):
                # Skip devices not in pairing mode
                continue
            self._discovered_devices[service_info.address] = service_info

        if not self._discovered_devices:
            # No pairing-mode devices detected — instruct the user to activate
            # pairing mode first, then re-add the integration.
            return self.async_abort(reason="no_devices_found")

        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            self._discovery_info = self._discovered_devices[address]
            await self.async_set_unique_id(address)
            self._abort_if_unique_id_configured()
            return await self.async_step_confirm()

        # Show discovered device selector
        device_labels: dict[str, str] = {
            addr: f"{info.name or addr}  [{addr}]" for addr, info in self._discovered_devices.items()
        }
        schema = vol.Schema(
            {
                vol.Required(CONF_ADDRESS): vol.In(device_labels),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    # ------------------------------------------------------------------
    # Confirm step
    # ------------------------------------------------------------------

    async def async_step_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """
        Ask the user to confirm the device before pairing.

        Args:
            user_input: Confirmation (any truthy value), or None to show form.

        Returns:
            Config flow result — confirm form or next step (pair).
        """
        if user_input is not None:
            # Device is already confirmed to be in pairing mode (filter applied in
            # async_step_bluetooth / async_step_user), so execute pairing immediately
            # without an extra "please put in pairing mode" prompt.
            return await self.async_step_pair(user_input={})

        info = self._discovery_info
        address = info.address if info else ""
        name = (info.name or address) if info else address
        prod_id = self._get_production_info_id(info)
        product_type = PRODUCTION_INFO.get(prod_id, f"Unknown (type {prod_id})")

        return self.async_show_form(
            step_id="confirm",
            description_placeholders={
                "name": name,
                "address": address,
                "product_type": product_type,
            },
        )

    # ------------------------------------------------------------------
    # Pairing step
    # ------------------------------------------------------------------

    async def async_step_pair(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """
        Prompt user to activate pairing mode, then execute BLE bonding.

        Execute BLE pairing immediately.

        Called with a non-None user_input from async_step_confirm (device is already
        confirmed to be in pairing mode).  No form is shown for this step.

        Pairing sequence (spec section 8.1):
          1. writeDeviceName command
          2. PAIR_ACTIONS (5 release-form commands)

        Args:
            user_input: Pairing data (non-None triggers execution; called with {} from confirm).

        Returns:
            Config flow result — success entry or error form.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            info = self._discovery_info
            address = info.address if info else ""

            try:
                from homeassistant.components.bluetooth import async_ble_device_from_address  # noqa: PLC0415

                ble_device = async_ble_device_from_address(self.hass, address, connectable=True)
                if ble_device is None:
                    errors["base"] = "device_not_found"
                else:
                    # Refresh service info to check pairing mode flag (PAIRING_MODE_BIT).
                    # We use the latest scan result if available; warn but proceed if unknown.
                    latest_infos = {s.address: s for s in async_discovered_service_info(self.hass, connectable=True)}
                    latest = latest_infos.get(address)
                    if latest is not None:
                        payload = latest.manufacturer_data.get(MANUFACTURER_ID, b"")
                        if len(payload) >= 2 and not (payload[1] & PAIRING_MODE_BIT):
                            LOGGER.warning(
                                "Device %s does not appear to be in pairing mode (PAIRING_MODE_BIT not set). "
                                "Make sure to hold the remote pairing button until the LED flashes.",
                                address,
                            )

                    client = LixilShutterBleClient(ble_device)
                    await client.do_pairing()

                if not errors:
                    production_info_id = self._get_production_info_id(info)
                    name = (info.name or address) if info else address
                    return self.async_create_entry(
                        title=name,
                        data={
                            CONF_ADDRESS: address,
                            CONF_PRODUCTION_INFO: production_info_id,
                        },
                    )
            except LixilShutterBleClientCommunicationError as exc:
                LOGGER.warning("Pairing failed for %s: %s", address, exc)
                errors["base"] = "pairing_failed"
            except Exception:  # noqa: BLE001
                LOGGER.exception("Unexpected error during pairing for %s", address)
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="pair",
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_production_info_id(info: BluetoothServiceInfoBleak | None) -> int:
        """Extract ProductionInfo ID from BLE advertising data.

        ``bytes[0] & 0x07`` of manufacturer data gives the product type (0–7).
        Defined types are in ``PRODUCTION_INFO``.

        Returns 0 if *info* is None or manufacturer data is absent.
        """
        if info is None:
            return 0
        payload = info.manufacturer_data.get(MANUFACTURER_ID, b"")
        if not payload:
            return 0
        return payload[0] & 0x07

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> LixilShutterOptionsFlow:
        """Return the options flow handler."""
        return LixilShutterOptionsFlow()


__all__ = ["LixilShutterConfigFlowHandler"]
