"""
Custom types for lixil_shutter.

This module defines the runtime data structure attached to each config entry.
Access pattern: entry.runtime_data.client / entry.runtime_data.coordinator

The LixilShutterConfigEntry type alias is used throughout the integration
for type-safe access to the config entry's runtime data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .api import LixilShutterBleClient


type LixilShutterConfigEntry = ConfigEntry[LixilShutterData]


@dataclass
class LixilShutterData:
    """Runtime data for lixil_shutter config entries.

    Stored as entry.runtime_data after successful setup.
    Provides typed access to the BLE client.
    """

    client: LixilShutterBleClient
