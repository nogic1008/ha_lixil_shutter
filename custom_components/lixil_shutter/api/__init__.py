"""
API package for lixil_shutter.

Architecture:
    Three-layer data flow: Entities → Coordinator → API Client.
    Only the coordinator should call the API client. Entities must never
    import or call the API client directly.

Exception hierarchy:
    LixilShutterApiClientError (base)
    ├── LixilShutterApiClientCommunicationError (network/timeout)
    └── LixilShutterApiClientAuthenticationError (401/403)

Coordinator exception mapping:
    ApiClientAuthenticationError → ConfigEntryAuthFailed (triggers reauth)
    ApiClientCommunicationError → UpdateFailed (auto-retry)
    ApiClientError             → UpdateFailed (auto-retry)
"""

from .client import (
    LixilShutterApiClient,
    LixilShutterApiClientAuthenticationError,
    LixilShutterApiClientCommunicationError,
    LixilShutterApiClientError,
)

__all__ = [
    "LixilShutterApiClient",
    "LixilShutterApiClientAuthenticationError",
    "LixilShutterApiClientCommunicationError",
    "LixilShutterApiClientError",
]
