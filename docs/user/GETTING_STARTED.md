# Getting Started with Lixil Bluetooth Shutter

This guide will help you install and set up the Lixil Bluetooth Shutter custom integration for Home Assistant.

## Prerequisites

- Home Assistant 2025.7.0 or newer
- Bluetooth capability on your Home Assistant host (built-in adapter, USB dongle, or [ESPHome Bluetooth Proxy](https://esphome.io/components/bluetooth_proxy))
- [HACS](https://hacs.xyz/) (Home Assistant Community Store) installed

## Installation

### Via HACS (Recommended)

1. Open HACS in your Home Assistant instance
2. Go to "Integrations"
3. Click the three dots in the top right corner
4. Select "Custom repositories"
5. Add this repository URL: `https://github.com/nogic1008/ha_lixil_shutter`
6. Set category to "Integration"
7. Click "Add"
8. Find "Lixil Bluetooth Shutter" in the integration list
9. Click "Download"
10. Restart Home Assistant

### Manual Installation

1. Download the latest release from the [releases page](https://github.com/nogic1008/ha_lixil_shutter/releases)
2. Extract the `lixil_shutter` folder from the archive
3. Copy it to `custom_components/lixil_shutter/` in your Home Assistant configuration directory
4. Restart Home Assistant

## Initial Setup

### Step 1: Activate Pairing Mode on the Shutter

Before adding the integration, the shutter must be in pairing mode.
The exact button sequence depends on your model — refer to your device's manual.

While in pairing mode, the shutter advertises itself via Bluetooth and Home Assistant can discover it.

### Step 2: Add the Integration

#### Option A — Automatic Discovery

When Home Assistant detects a shutter in pairing mode, a notification badge appears on
**Settings** → **Devices & Services**. The device will be listed under "Discovered".

1. Click **Configure** next to the discovered device
2. Review the device details (name, Bluetooth address, product type)
3. Click **Submit** to confirm and pair

#### Option B — Manual Setup

1. Go to **Settings** → **Devices & Services**
2. Click **+ Add Integration**
3. Search for "Lixil Bluetooth Shutter"
4. Home Assistant scans for shutters in pairing mode and shows a list
5. Select your shutter and click **Submit**
6. Review the detected device details and click **Submit** to pair

If no devices appear in the list, make sure your shutter is in pairing mode and retry.

## What Gets Created

After successful pairing, the integration creates one device with one entity per shutter.

### Device

A device entry is created in Home Assistant with:

- Manufacturer and model information from the BLE advertisement
- Bluetooth address as the unique identifier

### Entities

#### Cover

One `cover` entity is created for each shutter:

- `cover.<device_name>` — Controls open, close, and stop

**Tilt support (ventilation models only):**

On ShutterItalia, Sunshade, Skylight, Screen, ACAdapter, and InHouseGarage product types,
two additional tilt actions are available:

- **Open Tilt** — Opens the flap slats to the ventilation position (採風)
- **Close Tilt** — Closes the flap slats

DecorativeWindow and ShutterEaris models do not support tilt.

## First Steps

### Dashboard Cards

Add the shutter to your dashboard:

1. Go to your dashboard
2. Click **Edit Dashboard** → **Add Card**
3. Choose "Tile" or "Button" card type
4. Select the shutter entity

Example cover card:

```yaml
type: tile
entity: cover.my_shutter
```

### Automations

Control the shutter in automations:

**Example — Close shutter at sunset:**

```yaml
automation:
  - alias: "Close shutter at sunset"
    trigger:
      - trigger: sun
        event: sunset
    action:
      - action: cover.close_cover
        target:
          entity_id: cover.my_shutter
```

**Example — Open ventilation in the morning:**

```yaml
automation:
  - alias: "Open ventilation in the morning"
    trigger:
      - trigger: time
        at: "07:00:00"
    action:
      - action: cover.open_cover_tilt
        target:
          entity_id: cover.my_shutter
```

## Troubleshooting

### No Devices Found

If the device list is empty during setup:

1. Confirm the shutter is in pairing mode (check your device manual)
2. Verify your Home Assistant host has Bluetooth access
3. Move the shutter closer to the Bluetooth adapter and try again

### Shutter Shows "Unavailable"

1. Check the shutter is powered on and within Bluetooth range
2. Enable debug logging and reproduce the issue (see below)
3. Download integration diagnostics: **Settings** → **Devices & Services** → **Lixil Bluetooth Shutter** → 3 dots → **Download Diagnostics**

### Debug Logging

Enable debug logging to troubleshoot issues:

```yaml
logger:
  default: warning
  logs:
    custom_components.lixil_shutter: debug
```

Add this to `configuration.yaml`, restart Home Assistant, and reproduce the issue.

## Next Steps

- See [CONFIGURATION.md](./CONFIGURATION.md) for post-setup configuration options
- Report issues at [GitHub Issues](https://github.com/nogic1008/ha_lixil_shutter/issues)

## Support

- [GitHub Discussions](https://github.com/nogic1008/ha_lixil_shutter/discussions)
- [Home Assistant Community Forum](https://community.home-assistant.io/)

