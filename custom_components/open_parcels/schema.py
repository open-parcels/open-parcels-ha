"""The schema library Home Assistant expects.

Newer Home Assistant releases validate with probatio, an API-compatible
successor of voluptuous, and check markers by its types. Older releases ship
only voluptuous. Import ``vol`` from here so both work.
"""

from __future__ import annotations

try:
    import probatio as vol
except ImportError:  # Home Assistant releases before probatio
    import voluptuous as vol  # type: ignore[no-redef,unused-ignore]  # same API, older releases

__all__ = ["vol"]
