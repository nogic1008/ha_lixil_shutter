"""Repairs platform for lixil_shutter."""

from __future__ import annotations

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create a repair flow based on the issue_id.

    No repair flows are currently defined for this integration.
    """
    raise NotImplementedError(f"No repair flow defined for issue: {issue_id}")
