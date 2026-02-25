"""Constants for lixil_shutter."""

from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

# Integration metadata
DOMAIN = "lixil_shutter"

# Config entry data keys
CONF_ADDRESS = "address"
CONF_PRODUCTION_INFO = "production_info"

# Config entry options keys
CONF_POLL_INTERVAL = "poll_interval"  # seconds between background status polls
CONF_COMMAND_MONITOR = "command_monitor"  # seconds to keep connection after open/close

# Default option values
DEFAULT_POLL_INTERVAL = 300  # 5 minutes
DEFAULT_COMMAND_MONITOR = 30  # 30 seconds

# BLE UUIDs
CHAR_UCG_IN_UUID = "2141e111-213a-11e6-b67b-9e71128cae77"  # Notify
CHAR_UCG_OUT_UUID = "2141e112-213a-11e6-b67b-9e71128cae77"  # Write

# BLE manufacturer ID for MyWindow series
MANUFACTURER_ID = 0xFFFF
# Manufacturer data bytes[1] bit7 = 1 means pairing mode (PairingMode flag)
# bytes[0] = ProductionInfo (lower 4 bits), bytes[1] = status flags
PAIRING_MODE_BIT = 0x80

# Timeouts
CONNECT_TIMEOUT_SEC = 10.0
COMMAND_TIMEOUT_SEC = 3.0

# Key release delay (100ms) — matches ActionHandler.execute()
RELEASE_DELAY_SEC = 0.1

# Command definitions
# Command format: [keyState, keyCode, subCode, tag] (4 bytes)

# keyState values
KEY_STATE_PRESS = 0x01  # Key pressed
KEY_STATE_RELEASE = 0x03  # Key released (also used for request commands)

# keyCode values
KEY_CODE_OPEN = 0x03  # Open (up)
KEY_CODE_CLOSE = 0x04  # Close (down)
KEY_CODE_STOP = 0x05  # Stop
KEY_CODE_POSITION = 0x06  # Move to position (see sub-codes below)
KEY_CODE_STATUS = 0x0B  # Status request
KEY_CODE_WRITE_NAME = 0x0C  # Write device name (pairing)

# subCode values for KEY_CODE_POSITION
SUB_CODE_DEFAULT = 0x00  # Default / unused
SUB_CODE_VENTILATION = 0x01  # Ventilation (saifu) position
SUB_CODE_MEMORY = 0x02  # Memory (favourite) position

# Status strings (bytes[2] bit analysis)
STATUS_OPEN = "open"  # bytes[2] bit4 = 0
STATUS_CLOSED = "closed"  # bytes[2] bit4 = 1
STATUS_MIN = "min"  # bytes[2] bit5 = 1 (fully closed / minimum position)
STATUS_UNKNOWN = "unknown"

# Product type (advertising bytes[0] & 0x07)
PRODUCTION_INFO: dict[int, str] = {
    0: "DecorativeWindow",
    1: "ShutterEaris",
    2: "ShutterItalia",
    3: "Sunshade",
    4: "Skylight",
    5: "Screen",
    6: "ACAdapter",
    7: "InHouseGarage",
}
