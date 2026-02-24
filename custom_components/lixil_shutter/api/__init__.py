"""
API package for lixil_shutter.

Architecture:
    BLE client for direct communication with LIXIL shutter.
    Entities access the client via entry.runtime_data.client.

Exception hierarchy:
    LixilShutterBleClientError (base)
    └── LixilShutterBleClientCommunicationError (GATT / connection errors)
"""

from .client import LixilShutterBleClient, LixilShutterBleClientCommunicationError, LixilShutterBleClientError

__all__ = [
    "LixilShutterBleClient",
    "LixilShutterBleClientCommunicationError",
    "LixilShutterBleClientError",
]
