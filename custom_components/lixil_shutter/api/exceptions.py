"""Exceptions for the LIXIL shutter BLE API client."""


class LixilShutterBleClientError(Exception):
    """Base BLE client error."""


class LixilShutterBleClientCommunicationError(LixilShutterBleClientError):
    """BLE communication or connection error."""
