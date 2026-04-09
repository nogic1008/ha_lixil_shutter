# Architectural and Design Decisions

This document records significant architectural and design decisions made during the development of this integration.

## Format

Each decision is documented with:

- **Date:** When the decision was made
- **Context:** Why this decision was necessary
- **Decision:** What was decided
- **Rationale:** Why this approach was chosen
- **Consequences:** Expected impacts and trade-offs

---

## Decision Log

### Direct BLE Client Instead of DataUpdateCoordinator

**Date:** 2025-11-29

**Context:** Home Assistant's `DataUpdateCoordinator` is the standard pattern for integrations that poll an external API. However, this integration communicates via Bluetooth LE with GATT notifications (local push), not a network API.

**Decision:** Use a direct `LixilShutterBleClient` instance per config entry instead of a coordinator. The cover entity manages the BLE connection lifecycle directly.

**Rationale:**

- The coordinator pattern assumes periodic HTTP polling; BLE GATT notifications are push-based
- Only one entity per device — no need to share fetched data across multiple entities
- BLE connection must remain on-demand to avoid holding the link permanently (physical remote still needs access)
- The cover entity needs fine-grained control over connection and disconnection timing after commands
- `iot_class: local_push` aligns with GATT notification-driven updates

**Consequences:**

- Cover entity is responsible for its own BLE connection lifecycle
- No `CoordinatorEntity` inheritance needed
- State derived directly from GATT notification callbacks and periodic status polls
- Each config entry stores the client in `entry.runtime_data.client` (no coordinator)

---

### On-Demand BLE Connection with Idle Disconnect

**Date:** 2025-11-29

**Context:** A permanently held BLE connection would prevent the physical remote and other BLE clients from connecting to the device.

**Decision:** Connect on-demand when a command or status poll is issued, then automatically disconnect after an idle timeout (configurable via `CONF_COMMAND_MONITOR`, default 30 s).

**Rationale:**

- LIXIL MyWindow shutters accept only one BLE connection at a time
- Users still use physical remotes — a permanent HA connection would block them
- On-demand connection is transparent to HA: commands are slightly slower (~1–2 s connect time) but the device remains accessible to its hardware remote
- `bleak-retry-connector` handles connection retries automatically

**Consequences:**

- BLE commands have a ~1–2 s connection overhead when the link is not already open
- `CONF_COMMAND_MONITOR` must be long enough for the device to send the completion notification
- `CONF_POLL_INTERVAL` determines how stale the state can be between physical remote uses

---

### BLE Pairing Mode Requirement for Discovery

**Date:** 2025-11-29

**Context:** LIXIL MyWindow shutters advertise the integration's `SERVICE_UUID` continuously when powered on, not only in pairing mode. Without filtering, the "Discovered" panel would show every configured shutter on every HA restart.

**Decision:** Filter discovered devices by the `PAIRING_MODE_BIT` flag in manufacturer data. Only devices actively in pairing mode trigger the config flow.

**Rationale:**

- Avoids cluttering the Discovered panel with already-configured devices
- The pairing mode bit (`MANUFACTURER_ID` manufacturer data, `bytes[1] & 0x80`) is already available in the BLE advertisement
- Users intentionally activate pairing mode → clear intent to configure

**Consequences:**

- Users must put the shutter in pairing mode before setup (by design)
- Devices not in pairing mode are silently ignored by the discovery flow
- Automatic re-discovery after HA restart does not occur (each device is added once with a stable unique ID from the BLE address)

---

### BLE Address as Unique ID

**Date:** 2025-11-29

**Context:** Config entries require a stable unique ID to prevent duplicate entries and support entity registry continuity.

**Decision:** Use the Bluetooth address (e.g., `AA:BB:CC:DD:EE:FF`) as the config entry unique ID.

**Rationale:**

- Bluetooth addresses are hardware-assigned and stable for a given device
- Available from the BLE advertisement before pairing
- Standard approach for Bluetooth integrations in Home Assistant

**Consequences:**

- If a device's Bluetooth address changes (rare, but possible with random address rotation), it would be treated as a new device
- LIXIL MyWindow devices use static addresses, so this is not a practical concern

---

### Platform-Specific Directories

**Date:** 2025-11-29

**Context:** Integration currently supports only the `cover` platform, but the directory structure should support future platform additions.

**Decision:** Each platform gets its own directory (`cover/`) with an `__init__.py` for platform setup and individual entity files.

**Rationale:**

- Clear organization that scales as new platforms are added
- Follows Home Assistant Core pattern
- `__init__.py` imports are explicit and platform-specific

**Consequences:**

- More files than a flat structure for a single-platform integration
- Adding a new platform is straightforward: create directory, implement `async_setup_entry()`, add to `PLATFORMS`

---

## Future Considerations

### Memory Position Command

The `KEY_CODE_POSITION` command with `SUB_CODE_VENTILATION` (type 0/1) or `SUB_CODE_MEMORY` (type 2–7) sends the shutter to a memorised intermediate position. This is not yet exposed as a HA entity action but could be added as a `cover.set_cover_tilt_position` action with discrete position values.

### Multiple Shutters per Entry

**Status:** Not planned

Current architecture creates one config entry per shutter. HA's Bluetooth integration model is one device per config entry (`integration_type: device`), which aligns with this design.

---

## Decision Review

These decisions should be reviewed when major features are added or when Home Assistant's Bluetooth integration patterns evolve significantly.


**Consequences:**

- More files than a flat structure for a single-platform integration
- Adding a new platform is straightforward: create directory, implement `async_setup_entry()`, add to `PLATFORMS`
