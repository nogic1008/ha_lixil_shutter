"""Service actions package for lixil_shutter (no custom services defined)."""

from __future__ import annotations

from homeassistant.core import HomeAssistant


async def async_setup_services(hass: HomeAssistant) -> None:
    """Register services for the integration (no custom services defined)."""


__all__ = ["async_setup_services"]
