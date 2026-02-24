"""
Entity package for lixil_shutter.

Architecture:
    All platform entities inherit from (PlatformEntity, LixilShutterEntity).
    MRO order matters — platform-specific class first, then the integration base.
    Entities read data from coordinator.data and NEVER call the API client directly.
    Unique IDs follow the pattern: {entry_id}_{description.key}

See entity/base.py for the LixilShutterEntity base class.
"""

from .base import LixilShutterEntity

__all__ = ["LixilShutterEntity"]
