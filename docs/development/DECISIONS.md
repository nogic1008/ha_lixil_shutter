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

### Motion Timer: Optimistic Opening/Closing State After Commands

**Date:** 2026-04-10

**Context:** The LIXIL MyWindow shutter reports `STATUS_OPEN` (i.e., not fully closed) at any intermediate position — whether fully open, halfway, or stopped mid-travel. After issuing an open or close command, the device takes several seconds to complete the motion. During that time it continues to report `STATUS_OPEN` via GATT notifications, so the HA entity would immediately revert from the command's optimistic state (`opening` / `closing`) back to `open`, re-enabling the "open" button instantly even while the shutter is actively closing.

Additionally, when the user stops the shutter mid-travel the device reports `STATUS_OPEN`, causing HA to show state `open` — which disables the "open" button and prevents the user from reopening a partially closed shutter.

> **Device limitation:** Because `STATUS_OPEN` is returned for every non-fully-closed position (fully open, halfway, stopped mid-travel), this integration cannot distinguish "fully open" from "partially open" from BLE notifications alone.  Therefore, `STATUS_OPEN` is mapped to `None` (unknown state in HA) in all cases **except** after the open motion timer expires naturally.  Only when the open command has been running uninterrupted for at least `command_monitor` seconds (without a stop command) is the incoming `STATUS_OPEN` treated as `CoverState.OPEN` (fully open).

**Decision:** Introduce a *motion timer* driven by `CONF_COMMAND_MONITOR` (default 30 s):

- **open** command: set state to `opening` and start the window before sending the BLE command. All GATT notifications are suppressed inside the window; the window expires naturally after `command_monitor` seconds. If the BLE command fails the window is cancelled at once.
- **close** command: set state to `closing` and start the window before sending. `STATUS_OPEN` notifications are suppressed; `STATUS_CLOSED` / `STATUS_VENTILATION` cancel the window early and apply the confirmed state.
- **stop** command: cancel any active window and immediately set state to `None` (unknown/partial position) before sending. Both open and close buttons remain available via `assumed_state = True`.
- `_attr_assumed_state = True` is set so HA always renders both the open and close buttons regardless of the current state.

**Rationale:**

- The device cannot report a percentage position — only `STATUS_OPEN` / `STATUS_CLOSED` / `STATUS_VENTILATION`. Because `STATUS_OPEN` covers every non-fully-closed state, a motion timer is the only way to give users useful in-progress feedback.
- During an OPENING window all notifications are suppressed (not just `STATUS_OPEN`) because the device reports `STATUS_CLOSED` — its current physical position — before the shutter actually starts moving.  Accepting that notification would cancel the window and revert the state to `closed` immediately.  No such ambiguity exists for the CLOSING window: `STATUS_CLOSED` there genuinely means the shutter has arrived at the closed position.
- `command_monitor` already controls the BLE idle-disconnect timeout, so it is a natural proxy for "time the shutter takes to complete a full motion". Reusing it avoids introducing a separate config key.
- Setting state to `None` (unknown) on stop and on `STATUS_OPEN` outside the open window accurately reflects that the shutter is at an indeterminate position.
- `assumed_state = True` matches the HA semantic: the integration cannot confirm the exact position, so both actions must always be available.

**Consequences:**

- Users must configure `command_monitor` to roughly match their shutter's full-travel time. If set too short, HA will revert to unknown state before the motion is complete; if set too long, the state stays `opening` / `closing` after the shutter has already finished.
- If the device completes motion and sends `STATUS_CLOSED` before the window expires, the window is cancelled and the real state is applied immediately — no lag.
- After the timer expires, `async_update()` is automatically called to confirm the final device state over BLE.
- `STATUS_OPEN` maps to `CoverState.OPEN` (fully open) **only** after the OPENING timer expires naturally. In all other cases — initial poll, post-stop poll, mid-travel poll — `STATUS_OPEN` maps to `None` (unknown state).

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
