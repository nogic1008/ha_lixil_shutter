# Lixil Bluetooth Shutter

[![GitHub Release][releases-shield]][releases]
[![GitHub Activity][commits-shield]][commits]
[![License][license-shield]](LICENSE)

[![hacs][hacsbadge]][hacs]
![Project Maintenance][maintenance-shield]

<!--
Uncomment and customize these badges if you want to use them:

[![BuyMeCoffee][buymecoffeebadge]][buymecoffee]
[![Discord][discord-shield]][discord]
-->

**✨ Develop in the cloud:** Want to contribute or customize this integration? Open it directly in GitHub Codespaces - no local setup required!

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/nogic1008/ha_lixil_shutter?quickstart=1)

## ✨ Features

- **Local Control**: Direct Bluetooth LE communication — no cloud, no hub required
- **Automatic Discovery**: Home Assistant detects shutters automatically when in pairing mode
- **Open / Close / Stop**: Full shutter control from Home Assistant
- **Ventilation Mode** (selected models): Open/close the flap slats (採風) for partial ventilation
- **Real-time State**: Status updates via BLE GATT notifications pushed by the device
- **Multiple Product Types**: Supports DecorativeWindow, ShutterItalia, Sunshade, Skylight, Screen, and more
- **Options Flow**: Adjust polling interval and connection settings after setup

**This integration will set up the following platform.**

| Platform | Description                                                            |
| -------- | ---------------------------------------------------------------------- |
| `cover`  | Shutter device — open, close, stop, and tilt (ventilation models only) |

## 🚀 Quick Start

### Step 1: Install the Integration

**Prerequisites:**

- Home Assistant with Bluetooth capability (built-in adapter or [ESPHome Bluetooth Proxy](https://esphome.io/components/bluetooth_proxy))
- [HACS](https://hacs.xyz/) (Home Assistant Community Store) installed

Click the button below to open the integration directly in HACS:

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=nogic1008&repository=ha_lixil_shutter&category=integration)

Then:

1. Click "Download" to install the integration
2. **Restart Home Assistant** (required after installation)

<details>
<summary>**Manual Installation (Advanced)**</summary>

If you prefer not to use HACS:

1. Download the `custom_components/lixil_shutter/` folder from this repository
2. Copy it to your Home Assistant's `custom_components/` directory
3. Restart Home Assistant

</details>

### Step 2: Put the Shutter in Pairing Mode

Before adding the integration, activate pairing mode on your LIXIL MyWindow shutter.
Refer to your device's manual for the exact button sequence (typically a long press on the remote).

The device must be in pairing mode for Home Assistant to detect it.

### Step 3: Add and Configure the Integration

**Option A — Automatic Discovery**

When Home Assistant detects a shutter in pairing mode, a notification appears in
**Settings** → **Devices & Services** under "Discovered". Click **Configure** and:

1. Review the detected device name, address, and product type
2. Click **Submit** to confirm and pair

**Option B — Manual Setup**

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=lixil_shutter)

Or go to **Settings** → **Devices & Services** → **+ Add Integration** → search "Lixil Bluetooth Shutter".

Home Assistant scans for nearby shutters in pairing mode and displays a list to choose from.
If no devices appear, ensure the shutter is in pairing mode and try again.

### Step 4: Adjust Settings (Optional)

After setup, you can fine-tune connection behaviour:

1. Go to **Settings** → **Devices & Services**
2. Find **Lixil Bluetooth Shutter**
3. Click **Configure** to adjust:
   - **Poll interval**: How often HA requests the shutter state (default: 5 minutes)
   - **Command monitor window**: How long the BLE connection stays open after a command (default: 30 seconds)

## Available Entities

### Cover

Each configured shutter creates one `cover` entity (device class: `shutter`):

- **Open**: Fully retracts the shutter
- **Close**: Fully lowers the shutter
- **Stop**: Stops the shutter mid-travel

**Tilt (ventilation models only — ShutterItalia, Sunshade, Skylight, Screen, etc.):**

- **Open Tilt**: Opens the flap slats to allow ventilation (採風 position)
- **Close Tilt**: Closes the flap slats

The tilt feature is automatically enabled or disabled based on the detected product type.

**Extra state attributes:**

| Attribute      | Description                                               |
| -------------- | --------------------------------------------------------- |
| `ble_address`  | Bluetooth address of the device                           |
| `product_type` | Detected product type (e.g., `ShutterItalia`, `Sunshade`) |

## Configuration Options

### During Setup

No credentials are required. The setup wizard shows the detected device and asks for confirmation only.

### After Setup (Options)

You can change these anytime by clicking **Configure**:

| Name                   | Default       | Description                                                                                     |
| ---------------------- | ------------- | ----------------------------------------------------------------------------------------------- |
| Poll Interval          | 300 s (5 min) | How often HA polls the shutter for its current state                                            |
| Command Monitor Window | 30 s          | How long the BLE connection stays open after a command (to receive the completion notification) |

## Troubleshooting

### Device Not Found During Setup

If no devices appear in the list:

1. Make sure the shutter is in pairing mode (check your device manual)
2. Confirm your Home Assistant host has Bluetooth capability or an ESPHome Bluetooth Proxy is configured
3. Move the shutter closer to the Bluetooth adapter and try again
4. Check that the device is not already configured (each shutter can only be added once)

### Shutter Shows "Unavailable"

If the cover entity shows unavailable:

1. Check that the shutter is powered on and within Bluetooth range
2. Check Home Assistant logs for BLE connection errors
3. Download integration diagnostics: **Settings** → **Devices & Services** → **Lixil Bluetooth Shutter** → 3 dots → **Download Diagnostics**

### Enable Debug Logging

To enable debug logging for this integration, add the following to your `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.lixil_shutter: debug
```

## 🤝 Contributing

Contributions are welcome! Please open an issue or pull request if you have suggestions or improvements.

### 🛠️ Development Setup

Want to contribute or customize this integration? You have two options:

#### Cloud Development (Recommended)

The easiest way to get started - develop directly in your browser with GitHub Codespaces:

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/nogic1008/ha_lixil_shutter?quickstart=1)

- ✅ Zero local setup required
- ✅ Pre-configured development environment
- ✅ Home Assistant included for testing
- ✅ 60 hours/month free for personal accounts

#### Local Development

Prefer working on your machine? You'll need:

- Docker Desktop
- VS Code with the [Dev Containers extension](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers)

Then:

1. Clone this repository
2. Open in VS Code
3. Click "Reopen in Container" when prompted

Both options give you the same fully-configured development environment with Home Assistant, Python 3.14, and all necessary tools.

---

## 🤖 AI-Assisted Development

> **ℹ️ Transparency Notice**
>
> This integration was developed with assistance from AI coding agents (GitHub Copilot, Claude, and others). While the codebase follows Home Assistant Core standards, AI-generated code may not be reviewed or tested to the same extent as manually written code.
>
> If you encounter any issues, please [open an issue](../../issues) on GitHub.

---

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

**Made with ❤️ by [@nogic1008][user_profile]**

---

[commits-shield]: https://img.shields.io/github/commit-activity/y/nogic1008/ha_lixil_shutter.svg?style=for-the-badge
[commits]: https://github.com/nogic1008/ha_lixil_shutter/commits/main
[hacs]: https://github.com/hacs/integration
[hacsbadge]: https://img.shields.io/badge/HACS-Default-orange.svg?style=for-the-badge
[license-shield]: https://img.shields.io/github/license/nogic1008/ha_lixil_shutter.svg?style=for-the-badge
[maintenance-shield]: https://img.shields.io/badge/maintainer-%40nogic1008-blue.svg?style=for-the-badge
[releases-shield]: https://img.shields.io/github/release/nogic1008/ha_lixil_shutter.svg?style=for-the-badge
[releases]: https://github.com/nogic1008/ha_lixil_shutter/releases
[user_profile]: https://github.com/nogic1008

<!-- Optional badge definitions - uncomment if needed:
[buymecoffee]: https://www.buymeacoffee.com/nogic1008
[buymecoffeebadge]: https://img.shields.io/badge/buy%20me%20a%20coffee-donate-yellow.svg?style=for-the-badge
[discord]: https://discord.gg/Qa5fW2R
[discord-shield]: https://img.shields.io/discord/330944238910963714.svg?style=for-the-badge
-->
