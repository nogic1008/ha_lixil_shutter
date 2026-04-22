# Configuration Reference

This document describes all configuration options and settings available in the Lixil Bluetooth Shutter custom integration.

## Integration Configuration

### Initial Setup

No credentials are required. The config flow only asks you to confirm the detected device.

Information shown during setup:

| Field            | Description                                               |
| ---------------- | --------------------------------------------------------- |
| **Name**         | Device name from the BLE advertisement                    |
| **Address**      | Bluetooth address of the shutter                          |
| **Product Type** | Detected product type (e.g., `ShutterItalia`, `Sunshade`) |

### Options Flow (Post-Setup Configuration)

After initial setup, you can adjust connection behaviour:

1. Go to **Settings** → **Devices & Services**
2. Find "Lixil Bluetooth Shutter"
3. Click **Configure**
4. Modify settings
5. Click **Submit**

**Available options:**

| Option                     | Default | Description                                                                                                                                                  |
| -------------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Poll Interval**          | 300 s   | How often Home Assistant requests the shutter state over BLE. Increase to reduce BLE traffic; decrease for more responsive state updates.                    |
| **Command Monitor Window** | 30 s    | How long the BLE connection stays open after sending a command. This allows the device's completion notification to arrive before the connection is dropped. |

Changes take effect immediately without restarting Home Assistant — the cover entity reschedules its poll timer automatically.

## Entity Configuration

### Cover Entity

One `cover` entity is created per configured shutter.

**Device class:** `shutter`

**Supported actions:**

| Action           | All models | Ventilation models only |
| ---------------- | ---------- | ----------------------- |
| Open             | ✅         | —                       |
| Close            | ✅         | —                       |
| Stop             | ✅         | —                       |
| Open Tilt (採風) | —          | ✅                      |
| Close Tilt       | —          | ✅                      |

Ventilation models: ShutterItalia, Sunshade, Skylight, Screen, ACAdapter, InHouseGarage.
Non-ventilation models: DecorativeWindow, ShutterEaris.

**Extra state attributes:**

| Attribute      | Description                     |
| -------------- | ------------------------------- |
| `ble_address`  | Bluetooth address of the device |
| `product_type` | Detected product type string    |

**Tilt position values (ventilation models only):**

| Value | Meaning                                       |
| ----- | --------------------------------------------- |
| `0`   | Flap slats closed                             |
| `100` | Flap slats open (ventilation / 採風 position) |

### Entity Customization

Customize entities via the Home Assistant UI:

1. Go to **Settings** → **Devices & Services** → **Entities**
2. Find and click the entity
3. Click the settings icon
4. Modify name, icon, or area assignment

### Disabling Entities

If you don't need an entity:

1. Go to **Settings** → **Devices & Services** → **Entities**
2. Find the entity
3. Click it, then click the **Settings** icon
4. Toggle **Enable entity** off

## Polling Behaviour

The integration uses a hybrid approach:

- **BLE GATT notifications** (push): The device pushes state updates after each command completes — no polling required for command responses.
- **Periodic polling** (pull): Home Assistant periodically requests the shutter status via BLE to detect any state changes made without HA (e.g., using the physical remote).

The BLE connection is on-demand: it connects when a poll or command is issued and automatically disconnects after the command monitor window expires. This avoids holding a permanent BLE link.

### Tuning Polling

| Use Case                       | Recommended Poll Interval  |
| ------------------------------ | -------------------------- |
| Rarely use physical remote     | 10–30 minutes (600–1800 s) |
| Sometimes use physical remote  | 5 minutes (300 s, default) |
| Frequently use physical remote | 1–2 minutes (60–120 s)     |

## Diagnostic Data

The integration provides diagnostic data for troubleshooting:

1. Go to **Settings** → **Devices & Services**
2. Find "Lixil Bluetooth Shutter"
3. Click on the device
4. Click **Download Diagnostics**

Diagnostic data includes:

- Config entry ID and title
- BLE address and connection status
- Registered device and entity details

**Privacy note:** The diagnostic data for this integration does not contain sensitive personal information. The BLE address identifies your device hardware.

          domain: sensor
          integration: lixil_shutter
    threshold:
      name: Threshold
      selector:
        number:
          min: 0
          max: 100

trigger:

- trigger: numeric_state
  entity_id: !input sensor_entity
  above: !input threshold

action:

- action: notify.notify
  data:
  message: "Sensor exceeded threshold!"

```

## Configuration Examples

See [EXAMPLES.md](./EXAMPLES.md) for complete automation and dashboard examples.

## Troubleshooting Configuration

### Config Entry Fails to Load

If the integration fails to load after configuration:

1. Check Home Assistant logs for errors
2. Verify connection details are correct
3. Test connectivity from Home Assistant to the device
4. Try removing and re-adding the integration

### Options Don't Save

If configuration changes aren't persisted:

1. Check for validation errors in the UI
2. Ensure values are within allowed ranges
3. Review logs for detailed error messages
4. Try restarting Home Assistant

## Related Documentation

- [Getting Started](./GETTING_STARTED.md) - Installation and initial setup
- [Examples](./EXAMPLES.md) - Automation and dashboard examples
- [GitHub Issues](https://github.com/nogic1008/ha_lixil_shutter/issues) - Report problems
```
